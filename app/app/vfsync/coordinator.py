"""SyncCoordinator: flight events -> session state -> (AP-5) matcher/writer.

Event payloads come from the APRS worker (``event:{slug}`` PubSub):
``{"type", "flarm_id", "message", "data"}`` with ``data`` =
``FlightState.to_redis_dict()`` (all values strings, times ISO ``...Z``).

Dispatch (docs/dev-guides/vfsync-internals.md):

* ``takeoff``              -> session upsert (state TRACKING)
* ``launch_type_detected`` -> start type, tow plane, release data, tow time
* ``touch_and_go``         -> landing_count, conf_touchgo
* ``landing_final``        -> landing_ts / method / confidence / count
* ``landing_retracted``    -> landing fields cleared
* everything else          -> ignored (``landing`` may still turn into a T&G)

Sessions are anchored on (airfield_id, flarm_id, takeoff_ts). Events
that arrive without a prior takeoff (consumer restart) create the
session from their payload so nothing is lost.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

import structlog

from app.vfsync.budget import BudgetGuard, Stage
from app.vfsync.confidence import WRITABLE_FIELDS
from app.vfsync.matcher import MatchDecision, match_session
from app.vfsync.models import AuditEntry, Session, SessionState, TenantConfig, utcnow
from app.vfsync.stores import AuditStore, BudgetStore, ConfigStore, SessionStore
from app.vfsync.vf_client.client import VfClient, VfError, VfForbidden
from app.vfsync.vf_client.models import VfFlight

log = structlog.get_logger()

DEFAULT_LIST_CACHE_S = 300
# states the worker never touches again
FINAL_STATES = frozenset({SessionState.COMPLETED, SessionState.EXPIRED, SessionState.REVIEW})

# Match reasons no retry can ever change: the session goes to REVIEW
# instead of being re-evaluated against VF every retry cycle.
TERMINAL_MATCH_REASONS = frozenset({"no_registration"})

FlightRowFetcher = Callable[[UUID, str, datetime], Awaitable[list[dict[str, Any]]]]

HANDLED_EVENTS = frozenset({
    "takeoff", "launch_type_detected", "touch_and_go", "landing_final", "landing_retracted",
})


# ---------------------------------------------------------------------------
# Payload parsing helpers (event data values are strings)
# ---------------------------------------------------------------------------

def parse_iso(value: Any) -> datetime | None:
    """ISO 8601 (``2025-05-01T10:00:00Z``) -> aware UTC datetime, else None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value: Any) -> int | None:
    f = parse_float(value)
    return None if f is None else round(f)


def _text(value: Any) -> str | None:
    s = "" if value is None else str(value).strip()
    return s or None


def _tow_time_minutes(seconds: int) -> int:
    """Tow time in minutes via vf_client.mapping (AP-1); inline half-up fallback."""
    try:
        from app.vfsync.vf_client.mapping import tow_time_minutes
    except ImportError:
        return (seconds + 30) // 60 if seconds >= 0 else 0
    return tow_time_minutes(seconds)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class SyncCoordinator:
    """Glue between flight events, the session store and (AP-5) the writer."""

    def __init__(
        self,
        sessions: SessionStore,
        audit: AuditStore,
        budget: BudgetStore,
        config: ConfigStore,
        writer: Any | None = None,
        matcher: Any | None = None,
        clock: Callable[[], datetime] = utcnow,
        fetch_flight_rows: FlightRowFetcher | None = None,
        guard: BudgetGuard | None = None,
        client_for: Callable[[TenantConfig], VfClient] | None = None,
        list_cache_s: int = DEFAULT_LIST_CACHE_S,
    ) -> None:
        self.sessions = sessions
        self.audit = audit
        self.budget = budget
        self.config = config
        self.writer = writer
        self.matcher = matcher or match_session
        self.clock = clock
        self._fetch_flight_rows = fetch_flight_rows
        self.guard = guard or BudgetGuard(budget, clock=clock)
        self._client_for = client_for or self._default_client
        self._list_cache_s = list_cache_s
        # per tenant: cached flight/list/today, VF clients, monitoring counters
        self._list_cache: dict[UUID, tuple[datetime, list[VfFlight]]] = {}
        self._clients: dict[UUID, tuple[tuple, VfClient]] = {}
        # Match + write of one tenant are serialised: consumer and scheduler
        # run concurrently and must not match the same VF flight twice.
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._last_match: dict[UUID, str] = {}   # session_id -> last audited decision
        self.write_error_streak: dict[str, int] = {}
        self.paused: set[str] = set()          # slugs with a 403 login (until config reload)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def on_event(self, tenant: TenantConfig, event: dict[str, Any]) -> Session | None:
        """Dispatch one flight event of ``tenant``; returns the touched session."""
        etype = event.get("type")
        if etype not in HANDLED_EVENTS:
            return None
        data = event.get("data") or {}
        flarm_id = _text(event.get("flarm_id")) or _text(data.get("flarm_id"))
        if not flarm_id:
            log.warning("vfsync_event_without_flarm_id", slug=tenant.slug, type=etype)
            return None
        if _text(data.get("is_visitor")) == "1":
            # Visitors (aircraft that did not start here) are shown in the
            # tower monitor only - they are never booked into this club's
            # Vereinsflieger.
            log.debug("vfsync_visitor_ignored", slug=tenant.slug, flarm_id=flarm_id, type=etype)
            return None

        session = await self._session_for(tenant, flarm_id, data, etype)
        if session is None:
            return None

        handler = {
            "takeoff": self._apply_takeoff,
            "launch_type_detected": self._apply_launch,
            "touch_and_go": self._apply_touch_and_go,
            "landing_final": self._apply_landing_final,
            "landing_retracted": self._apply_landing_retracted,
        }[etype]
        handler(session, data)
        await self.sessions.save(session)
        log.info(
            "vfsync_session_updated",
            slug=tenant.slug, type=etype, flarm_id=flarm_id,
            session_id=str(session.session_id), state=session.state.value,
            landing_count=session.landing_count,
        )
        await self.process(session, tenant, trigger=etype)
        return session

    async def _session_for(self, tenant: TenantConfig, flarm_id: str,
                           data: dict[str, Any], etype: str) -> Session | None:
        """Find or create the session an event belongs to.

        The session is upserted on its natural key (airfield, flarm_id,
        takeoff_time): this creates it after a missed takeoff and never
        attaches a landing to an older open session of the same aircraft.
        An event without a takeoff time is ignored - there is no session
        it can safely belong to (no clock fallback, no "newest open
        session" guess; both would book the wrong flight).
        """
        takeoff_ts = parse_iso(data.get("takeoff_time"))
        if takeoff_ts is None:
            if etype == "takeoff":
                log.warning("vfsync_takeoff_without_time", slug=tenant.slug, flarm_id=flarm_id)
            else:
                log.warning("vfsync_event_without_takeoff_time", slug=tenant.slug,
                            flarm_id=flarm_id, type=etype)
            return None
        session = await self.sessions.upsert(Session(
            airfield_id=tenant.airfield_id,
            flarm_id=flarm_id,
            registration=_text(data.get("registration")),
            takeoff_ts=takeoff_ts,
            state=SessionState.TRACKING,
            created_at=self.clock(),
            updated_at=self.clock(),
        ))
        if session.registration is None:
            session.registration = _text(data.get("registration"))
        return session

    @staticmethod
    def _apply_takeoff(session: Session, data: dict[str, Any]) -> None:
        if session.registration is None:
            session.registration = _text(data.get("registration"))

    @staticmethod
    def _apply_launch(session: Session, data: dict[str, Any]) -> None:
        session.start_type_detected = _text(data.get("launch_type"))
        session.tow_registration = _text(data.get("tow_plane_reg"))
        session.release_ts = parse_iso(data.get("release_time"))
        alt = parse_int(data.get("release_alt_agl_m"))
        session.release_alt_agl_m = alt if alt is not None and alt > 0 else None
        session.release_method = _text(data.get("release_method"))
        tow_s = parse_int(data.get("tow_duration_s"))
        session.tow_time_min = _tow_time_minutes(tow_s) if tow_s is not None and tow_s > 0 else None
        session.conf_pairing = parse_float(data.get("pairing_confidence"))

    @staticmethod
    def _apply_touch_and_go(session: Session, data: dict[str, Any]) -> None:
        count = parse_int(data.get("landing_count"))
        if count is not None:
            session.landing_count = max(session.landing_count, count)
        session.conf_touchgo = parse_float(data.get("touch_go_confidence"))

    @staticmethod
    def _apply_landing_final(session: Session, data: dict[str, Any]) -> None:
        session.landing_ts = parse_iso(data.get("landing_time"))
        session.landing_method = _text(data.get("landing_method"))
        session.conf_landing = parse_float(data.get("landing_confidence"))
        count = parse_int(data.get("landing_count"))
        if count is not None:
            session.landing_count = max(session.landing_count, count)

    @staticmethod
    def _apply_landing_retracted(session: Session, data: dict[str, Any]) -> None:
        session.landing_ts = None
        session.landing_method = None
        session.conf_landing = None
        if session.state == SessionState.COMPLETED:
            # The landing bundle is already in VF - a human has to look.
            session.state = SessionState.REVIEW
            session.add_review_reason("landing_retracted_after_completion")

    # ------------------------------------------------------------------
    # Match + write (AP-5)
    # ------------------------------------------------------------------

    async def process(self, session: Session, tenant: TenantConfig, trigger: str) -> Any | None:
        """Match the session against VF and write what is due.

        Live departure (session airborne) only in budget stage NORMAL;
        after landing_final the whole bundle. Returns the writer's
        WriteResult, or None when nothing was attempted.
        """
        if self.writer is None:
            log.debug("vfsync_process_noop", slug=tenant.slug,
                      session_id=str(session.session_id), trigger=trigger)
            return None
        lock = self._locks.setdefault(tenant.airfield_id, asyncio.Lock())
        async with lock:
            return await self._process_locked(session, tenant, trigger)

    async def _process_locked(self, session: Session, tenant: TenantConfig,
                              trigger: str) -> Any | None:
        if session.state in FINAL_STATES:
            return None
        if tenant.slug in self.paused or not tenant.has_credentials:
            return None

        movements = await self.sessions.count_today(
            tenant.airfield_id, self.guard.day_for(tenant), tenant.timezone,
        )
        stage = await self.guard.stage(tenant, movements)
        if stage is Stage.HARD_STOP:
            log.warning("vfsync_hard_stop", slug=tenant.slug, trigger=trigger)
            return None

        if session.is_airborne:
            if session.state == SessionState.DEPARTURE_WRITTEN:
                return None          # nothing more to write while in the air
            if stage is not Stage.NORMAL:
                return None          # live departure is the first thing to give up
            fields: set[str] | None = {"departuretime"}
        else:
            if stage is Stage.AEROTOW_ONLY and not session.is_aerotow:
                return None          # billing priority: aerotows first
            fields = None            # bundle: everything still empty in VF

        if session.matched_flid is None:
            # Only fields the session can actually offer count as targets:
            # a winch flight must not grab a VF flight just because its
            # towheight is still empty.
            from app.vfsync.writer import session_fields
            target = (fields or set(WRITABLE_FIELDS)) & set(session_fields(session))
            if not target:
                return None
            matched = await self._match(session, tenant, target)
            if not matched:
                return None

        result = await self.writer.write(session, tenant, fields)
        self._track_result(tenant, result)
        if result.status in ("written", "dryrun") and result.sent:
            self._patch_cache(tenant, session.matched_flid, result.sent)
        if "flight_gone" in result.reasons:
            self.invalidate_list_cache(tenant)
        return result

    async def _match(self, session: Session, tenant: TenantConfig,
                     target_fields: set[str]) -> bool:
        """Find the VF flight of a session; persists state/reasons.

        A flid already held by another open session of the tenant is never
        offered again (one VF flight <-> one session, N-01).
        """
        try:
            flights = await self._flights_today(tenant)
        except VfError as exc:
            self._track_exception(tenant, exc)
            await self.audit.append(AuditEntry(
                airfield_id=tenant.airfield_id, session_id=session.session_id,
                action="error", http_status=exc.status,
                detail=f"list_today:{type(exc).__name__}:{exc}", ts=self.clock(),
            ))
            return False

        # A VF flight belongs to exactly one session - including completed
        # ones (a second flight of the same registration must not land on
        # the first flight's VF entry).
        all_sessions = await self.sessions.list_open(
            airfield_id=tenant.airfield_id, states=set(SessionState),
        )
        taken = {
            s.matched_flid for s in all_sessions
            if s.session_id != session.session_id and s.matched_flid is not None
        }
        candidates = [f for f in flights if f.flid not in taken]
        decision: MatchDecision = self.matcher(session, candidates, target_fields)

        for flid in decision.conflict_flids:
            session.add_review_reason(f"starttype_conflict:{flid}")
        detail = f"{decision.kind}:{decision.reason}"
        if self._last_match.get(session.session_id) != detail:
            # audit only decision changes, not every retry tick
            self._last_match[session.session_id] = detail
            await self.audit.append(AuditEntry(
                airfield_id=tenant.airfield_id, session_id=session.session_id,
                action="match", flid=decision.flid, detail=detail, ts=self.clock(),
            ))

        if decision.kind == "matched":
            session.matched_flid = decision.flid
            if session.state in (SessionState.TRACKING, SessionState.AWAITING_MATCH):
                session.state = SessionState.MATCHED
        elif decision.reason in TERMINAL_MATCH_REASONS:
            # No registration (DDB identified = N, or a device unknown to
            # DDB and APRS): nothing to match on, ever - hand the session
            # to a human instead of polling VF until it expires.
            session.add_review_reason(decision.reason)
            session.state = SessionState.REVIEW
        else:
            # awaiting_match, ambiguous, starttype_conflict: nothing was
            # written, so keep retrying - the pilot may complete the second
            # flight or fix the start type; the reason stays visible.
            if decision.kind != "awaiting_match":
                session.add_review_reason(decision.reason)
            session.state = SessionState.AWAITING_MATCH
        await self.sessions.save(session)
        log.info("vfsync_match", slug=tenant.slug, session_id=str(session.session_id),
                 kind=decision.kind, flid=decision.flid, reason=decision.reason)
        return decision.kind == "matched"

    async def _flights_today(self, tenant: TenantConfig) -> list[VfFlight]:
        """`flight/list/today` with a per-tenant cache (Kap. 4.2)."""
        now = self.clock()
        cached = self._list_cache.get(tenant.airfield_id)
        if cached and (now - cached[0]).total_seconds() < self._list_cache_s:
            return cached[1]
        if not await self.guard.reserve(tenant, 1):
            raise VfError("budget exhausted for flight/list/today")
        flights = await self.client_for(tenant).list_today()
        self._list_cache[tenant.airfield_id] = (now, flights)
        return flights

    def invalidate_list_cache(self, tenant: TenantConfig | None = None) -> None:
        if tenant is None:
            self._list_cache.clear()
        else:
            self._list_cache.pop(tenant.airfield_id, None)

    def _patch_cache(self, tenant: TenantConfig, flid: int | None, sent: dict[str, Any]) -> None:
        """Reflect a write in the cached list so the next match sees it."""
        cached = self._list_cache.get(tenant.airfield_id)
        if not cached or flid is None:
            return
        for f in cached[1]:
            if f.flid == flid:
                for k, v in sent.items():
                    setattr(f, k, v)

    def client_for(self, tenant: TenantConfig) -> VfClient:
        """VfClient per tenant, rebuilt when credentials change."""
        key = (tenant.vf_base_url, tenant.vf_username, tenant.vf_password_md5,
               tenant.vf_appkey, tenant.vf_cid)
        cached = self._clients.get(tenant.airfield_id)
        if cached and cached[0] == key:
            return cached[1]
        client = self._client_for(tenant)
        self._clients[tenant.airfield_id] = (key, client)
        return client

    def _default_client(self, tenant: TenantConfig) -> VfClient:
        return VfClient(
            tenant.vf_base_url, tenant.vf_username or "", tenant.vf_password_md5 or "",
            tenant.vf_appkey or "", tenant.vf_cid, on_request=self.guard.hook_for(tenant),
        )

    def _track_result(self, tenant: TenantConfig, result: Any) -> None:
        if result.status == "error":
            self.write_error_streak[tenant.slug] = self.write_error_streak.get(tenant.slug, 0) + 1
            if "login_forbidden" in result.reasons:
                self._pause(tenant)
        elif result.status in ("written", "dryrun"):
            self.write_error_streak[tenant.slug] = 0

    def _track_exception(self, tenant: TenantConfig, exc: VfError) -> None:
        self.write_error_streak[tenant.slug] = self.write_error_streak.get(tenant.slug, 0) + 1
        if isinstance(exc, VfForbidden):
            self._pause(tenant)

    def _pause(self, tenant: TenantConfig) -> None:
        if tenant.slug not in self.paused:
            log.error("vfsync_tenant_paused", slug=tenant.slug, reason="login_forbidden")
        self.paused.add(tenant.slug)

    def resume(self, tenant: TenantConfig) -> None:
        """Config reload (new credentials) lifts a login pause."""
        self.paused.discard(tenant.slug)
        self._clients.pop(tenant.airfield_id, None)

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def recover(self, tenant: TenantConfig,
                      fetch_flight_rows: FlightRowFetcher | None = None) -> int:
        """Reconcile open sessions with flight_log / flight_status.

        For each open session the archived (flight_log) and live
        (flight_status) rows of the same flight are read; landing and
        launch fields the session lacks are taken over and an audit entry
        ``recovery`` names the source table.

        Args:
            tenant: Tenant whose open sessions are reconciled.
            fetch_flight_rows: Overrides the fetcher given at construction.

        Returns:
            Number of sessions that received new data.
        """
        fetch = fetch_flight_rows or self._fetch_flight_rows
        if fetch is None:
            raise RuntimeError("recover() needs a fetch_flight_rows callable")

        updated = 0
        sessions = await self.sessions.list_open(airfield_id=tenant.airfield_id)
        for session in sessions:
            if session.takeoff_ts is None:
                continue
            rows = await fetch(tenant.airfield_id, session.flarm_id, session.takeoff_ts)
            changed = self._merge_flight_rows(session, rows)
            if not changed:
                continue
            await self.sessions.save(session)
            sources = sorted({src for _, src in changed})
            fields = [f for f, _ in changed]
            await self.audit.append(AuditEntry(
                airfield_id=tenant.airfield_id,
                session_id=session.session_id,
                action="recovery",
                detail=f"source={','.join(sources)}; fields={','.join(fields)}",
                fields_sent={f: _jsonable(getattr(session, f)) for f in fields},
                ts=self.clock(),
            ))
            log.info("vfsync_session_recovered", slug=tenant.slug,
                     session_id=str(session.session_id), sources=sources, fields=fields)
            updated += 1
            await self.process(session, tenant, trigger="recovery")
        log.info("vfsync_recovery_done", slug=tenant.slug,
                 open_sessions=len(sessions), updated=updated)
        return updated

    @staticmethod
    def _merge_flight_rows(session: Session, rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
        """Fill missing session fields from flight rows; returns (field, source) pairs."""
        changed: list[tuple[str, str]] = []

        def take(field: str, value: Any, source: str) -> None:
            if value is None or getattr(session, field) is not None:
                return
            setattr(session, field, value)
            changed.append((field, source))

        for row in rows:
            source = str(row.get("source", "?"))
            landing_ts = parse_iso(row.get("landing_time"))
            # A live flight_status row may still be inside the touch & go
            # window - only a final landing is a landing (Kap. 6.1).
            landing_is_final = row.get("landing_final", True) is not False
            if landing_ts is not None and session.landing_ts is None and landing_is_final:
                take("landing_ts", landing_ts, source)
                take("landing_method", _text(row.get("landing_method")), source)
                take("conf_landing", parse_float(row.get("landing_confidence")), source)
            count = parse_int(row.get("landing_count"))
            if count is not None and count > session.landing_count:
                session.landing_count = count
                changed.append(("landing_count", source))
            take("start_type_detected", _text(row.get("launch_type")), source)
            take("tow_registration", _text(row.get("tow_plane_registration")), source)
            take("release_ts", parse_iso(row.get("release_time")), source)
            alt = parse_int(row.get("release_altitude_agl"))
            take("release_alt_agl_m", alt if alt is not None and alt > 0 else None, source)
            take("release_method", _text(row.get("release_method")), source)
            tow_s = parse_int(row.get("tow_duration_s"))
            take("tow_time_min",
                 _tow_time_minutes(tow_s) if tow_s is not None and tow_s > 0 else None, source)
            take("conf_pairing", parse_float(row.get("pairing_confidence")), source)
        return changed


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    return value
