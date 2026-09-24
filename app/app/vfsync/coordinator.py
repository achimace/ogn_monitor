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

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID

import structlog

from app.vfsync.models import AuditEntry, Session, SessionState, TenantConfig, utcnow
from app.vfsync.stores import AuditStore, BudgetStore, ConfigStore, SessionStore

log = structlog.get_logger()

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
    ) -> None:
        self.sessions = sessions
        self.audit = audit
        self.budget = budget
        self.config = config
        self.writer = writer
        self.matcher = matcher
        self.clock = clock
        self._fetch_flight_rows = fetch_flight_rows

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

        With a takeoff_time in the payload the session is upserted on its
        natural key (creates it after a missed takeoff and never attaches a
        landing to an older open session of the same aircraft). Without one,
        the newest open session of the aircraft is used.
        """
        takeoff_ts = parse_iso(data.get("takeoff_time"))
        if takeoff_ts is None and etype == "takeoff":
            takeoff_ts = self.clock()
            log.warning("vfsync_takeoff_without_time", slug=tenant.slug, flarm_id=flarm_id)
        if takeoff_ts is not None:
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
        session = await self.sessions.find_open_for_aircraft(tenant.airfield_id, flarm_id)
        if session is None:
            log.warning("vfsync_event_without_session", slug=tenant.slug,
                        flarm_id=flarm_id, type=etype)
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
    # Match + write (filled in by AP-5)
    # ------------------------------------------------------------------

    async def process(self, session: Session, tenant: TenantConfig, trigger: str) -> Any | None:
        """Match the session against VF and write - hook for AP-5.

        Returns the writer's WriteResult (None while no writer is wired).
        """
        if self.writer is None:
            log.debug("vfsync_process_noop", slug=tenant.slug,
                      session_id=str(session.session_id), trigger=trigger)
            return None
        raise NotImplementedError("SyncCoordinator.process: matcher/writer wiring is AP-5")

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
            if landing_ts is not None and session.landing_ts is None:
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
