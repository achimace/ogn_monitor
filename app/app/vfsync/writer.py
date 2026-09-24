"""VF write path with the non-negotiable invariants (Konzept Kap. 5.4).

Every write of a session runs, in this order:

1. Budget guard          - remaining daily budget >= get + edit, else deferred
2. Confidence gate       - per-field gates (confidence.py), dropped fields
                           become review reasons, the rest is still written
3. Read-before-write     - flight/get, VF state goes to the audit as pre_state
4. Start-type conflict   - VF starttype set and incompatible -> abort, review
5. Field filter          - only fields that are empty in VF *now*;
                           landingcount only ever raised, never lowered
6. Dry-run               - steps 1-5, then an audit entry `dryrun_edit`
                           with the exact payload instead of the PUT
7. Edit + audit          - PUT, response into the audit, session state
8. Idempotency           - re-running a session cannot double-write (step 5)

Tests that pin these invariants must never be relaxed to go green.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Literal

import structlog

from app.vfsync.audit import AuditLog
from app.vfsync.budget import CALLS_PER_WRITE, BudgetGuard
from app.vfsync.confidence import WRITABLE_FIELDS, gate_fields
from app.vfsync.models import Session, SessionState, TenantConfig
from app.vfsync.stores import SessionStore
from app.vfsync.vf_client.client import (
    VfBadRequest,
    VfClient,
    VfError,
    VfForbidden,
)
from app.vfsync.vf_client.mapping import is_starttype_compatible, to_vf_time
from app.vfsync.vf_client.models import VfFlight

log = structlog.get_logger()

WriteStatus = Literal["written", "dryrun", "skipped", "review", "deferred", "error"]

ClientFactory = Callable[[TenantConfig], VfClient | Awaitable[VfClient]]


@dataclass
class WriteResult:
    status: WriteStatus
    sent: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    http_status: int | None = None


def session_fields(session: Session) -> dict[str, str | int]:
    """VF field values a session can offer (formatted for the API).

    Tow height / time only for a clean aerotow (R-03); no value -> no key.
    """
    out: dict[str, str | int] = {}
    if session.takeoff_ts is not None:
        out["departuretime"] = to_vf_time(session.takeoff_ts)
    if session.landing_ts is not None:
        out["arrivaltime"] = to_vf_time(session.landing_ts)
    if session.is_aerotow:
        if session.release_alt_agl_m is not None:
            out["towheight"] = int(session.release_alt_agl_m)
        if session.tow_time_min is not None and session.tow_time_min > 0:
            out["towtime"] = int(session.tow_time_min)
    if session.landing_count > 1:
        out["landingcount"] = int(session.landing_count)
    return out


class VfWriter:
    """Executes writes for sessions under the Kap. 5.4 invariants."""

    def __init__(
        self,
        client_for: ClientFactory,
        sessions: SessionStore,
        audit: AuditLog,
        budget: BudgetGuard,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._client_for = client_for
        self._sessions = sessions
        self._audit = audit
        self._budget = budget
        self._clock = clock

    async def _client(self, tenant: TenantConfig) -> VfClient:
        client = self._client_for(tenant)
        if hasattr(client, "__await__"):
            client = await client
        return client

    async def write(
        self,
        session: Session,
        tenant: TenantConfig,
        fields: set[str] | None = None,
    ) -> WriteResult:
        """Write all known-and-empty fields of a matched session to VF.

        Args:
            session: Matched session (matched_flid set).
            tenant: Tenant configuration (dry_run decides PUT vs. audit).
            fields: Restrict to these VF fields (default: all writable).

        Returns:
            WriteResult; the session is persisted with the new state,
            attempts and review reasons in every case.
        """
        requested = set(fields) if fields else set(WRITABLE_FIELDS)
        aid = session.airfield_id
        sid = session.session_id
        flid = session.matched_flid

        if flid is None:
            return WriteResult("skipped", reasons=["not_matched"])

        # 1. Budget guard (get + edit, plus accesstoken + signin if needed)
        client = await self._client(tenant)
        needed = CALLS_PER_WRITE + (0 if getattr(client, "signed_in", False) else 2)
        if not await self._budget.reserve(tenant, needed):
            log.warning("vfsync_budget_exhausted", slug=tenant.slug, session_id=str(sid))
            return WriteResult("deferred", reasons=["budget_exhausted"])

        # 2. Confidence gate
        allowed, gate_reasons = gate_fields(session, requested)
        for r in gate_reasons:
            session.add_review_reason(r)
        values = session_fields(session)
        candidates = {f: values[f] for f in allowed if f in values}
        if not candidates:
            await self._audit.record(aid, "abstain", session_id=sid, flid=flid,
                                     detail="nothing_to_write;" + ";".join(gate_reasons))
            await self._touch(session)
            return WriteResult("skipped", reasons=["nothing_to_write", *gate_reasons])

        session.attempts += 1
        session.last_attempt = self._clock()

        # 3. Read-before-write
        try:
            flight = await client.get_flight(flid)
        except VfError as exc:
            return await self._fail(session, tenant, "get", exc, gate_reasons)
        pre_state = flight.raw()
        await self._audit.record(aid, "get", session_id=sid, flid=flid,
                                 pre_state=pre_state, http_status=200)

        # 4. Start-type conflict: never correct VF, never write next to it
        if not is_starttype_compatible(session.start_type_detected, flight.starttype):
            reason = f"starttype_conflict:vf={flight.starttype},detected={session.start_type_detected}"
            session.add_review_reason(reason)
            session.state = SessionState.REVIEW
            await self._audit.record(aid, "abstain", session_id=sid, flid=flid,
                                     pre_state=pre_state, detail=reason)
            await self._sessions.save(session)
            return WriteResult("review", reasons=[reason, *gate_reasons])

        # 5. Field filter: only what is empty in VF *now*
        payload, filter_reasons = _filter_payload(session, flight, candidates)
        for r in filter_reasons:
            session.add_review_reason(r)
        reasons = [*gate_reasons, *filter_reasons]

        if not payload:
            await self._audit.record(aid, "abstain", session_id=sid, flid=flid,
                                     pre_state=pre_state,
                                     detail="all_fields_already_set;" + ";".join(reasons))
            self._advance_state(session, flight, requested, wrote=set())
            await self._sessions.save(session)
            return WriteResult("skipped", reasons=["all_fields_already_set", *reasons])

        # 6. Dry-run: everything except the PUT
        if tenant.dry_run:
            await self._audit.record(aid, "dryrun_edit", session_id=sid, flid=flid,
                                     fields_sent=payload, pre_state=pre_state,
                                     detail="dry_run")
            self._advance_state(session, flight, requested, wrote=set(payload))
            await self._sessions.save(session)
            return WriteResult("dryrun", sent=payload, reasons=reasons)

        # 7. Edit + audit
        try:
            await client.edit_flight(flid, payload)
        except VfError as exc:
            return await self._fail(session, tenant, "edit", exc, reasons,
                                    payload=payload, pre_state=pre_state)
        await self._audit.record(aid, "edit", session_id=sid, flid=flid,
                                 fields_sent=payload, pre_state=pre_state,
                                 http_status=200, detail="ok")
        log.info("vfsync_written", slug=tenant.slug, session_id=str(sid), flid=flid,
                 fields=sorted(payload))
        self._advance_state(session, flight, requested, wrote=set(payload))
        await self._sessions.save(session)
        return WriteResult("written", sent=payload, reasons=reasons, http_status=200)

    # ------------------------------------------------------------------

    async def _touch(self, session: Session) -> None:
        await self._sessions.save(session)

    async def _fail(self, session: Session, tenant: TenantConfig, phase: str,
                    exc: VfError, reasons: list[str], payload: dict | None = None,
                    pre_state: dict | None = None) -> WriteResult:
        """Audit an API failure and decide retry vs. review."""
        detail = f"{phase}:{type(exc).__name__}:{exc}"
        await self._audit.record(
            session.airfield_id, "error", session_id=session.session_id,
            flid=session.matched_flid, fields_sent=payload, pre_state=pre_state,
            http_status=exc.status, detail=detail,
        )
        if isinstance(exc, VfBadRequest):
            # Payload rejected: retrying blindly cannot help (Kap. 5.2)
            session.add_review_reason(f"vf_400:{phase}")
            session.state = SessionState.REVIEW
            reasons = [*reasons, "vf_400"]
        elif phase == "get" and exc.status == 404:
            # The matched VF flight was deleted: forget it and match again
            session.matched_flid = None
            session.state = SessionState.AWAITING_MATCH
            session.add_review_reason("vf_flight_deleted")
            reasons = [*reasons, "flight_gone"]
        elif isinstance(exc, VfForbidden):
            reasons = [*reasons, "login_forbidden"]
        else:
            reasons = [*reasons, "retry"]
        await self._sessions.save(session)
        log.warning("vfsync_write_failed", slug=tenant.slug, session_id=str(session.session_id),
                    phase=phase, error=type(exc).__name__, status=exc.status)
        return WriteResult("error", sent=payload or {}, reasons=reasons, http_status=exc.status)

    @staticmethod
    def _advance_state(session: Session, flight: VfFlight, requested: set[str],
                       wrote: set[str]) -> None:
        """Move the session forward based on what VF now holds."""
        if session.state == SessionState.REVIEW and not wrote:
            return
        departure_done = "departuretime" in wrote or not flight.is_empty("departuretime")
        arrival_done = "arrivaltime" in wrote or not flight.is_empty("arrivaltime")
        if session.landing_ts is not None and arrival_done:
            # Landing bundle handled: nothing more to write for this flight
            session.state = (SessionState.REVIEW if session.review_reasons()
                             else SessionState.COMPLETED)
        elif departure_done:
            session.state = (SessionState.REVIEW if session.review_reasons()
                             and session.state == SessionState.REVIEW
                             else SessionState.DEPARTURE_WRITTEN)
        else:
            session.state = SessionState.MATCHED


def _filter_payload(session: Session, flight: VfFlight,
                    candidates: dict[str, str | int]) -> tuple[dict[str, str | int], list[str]]:
    """Step 5: keep only fields that are empty in VF; landingcount only up."""
    payload: dict[str, str | int] = {}
    reasons: list[str] = []
    for name, value in candidates.items():
        if name == "landingcount":
            vf_count = flight.landingcount or 1
            if session.landing_count > vf_count:
                payload[name] = int(session.landing_count)
            elif vf_count > session.landing_count:
                reasons.append(f"landingcount_vf_higher:vf={vf_count},session={session.landing_count}")
            continue
        if flight.is_empty(name):
            payload[name] = value
    return payload, reasons
