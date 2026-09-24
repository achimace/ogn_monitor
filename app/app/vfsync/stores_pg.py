"""asyncpg implementations of the VF-Sync store protocols (vf_sync_* tables).

Every query is parametrized and, where the table carries an airfield_id,
scoped by it. vf_sync_audit is append-only: this module never issues an
UPDATE or DELETE against it.

The stores take an ``Executor`` - an asyncpg Pool or a single Connection
(tests run the stores inside one transaction that is rolled back).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Protocol
from uuid import UUID

import structlog
from cryptography.fernet import InvalidToken

from app.config import settings
from app.vfsync import crypto
from app.vfsync.models import (
    OPEN_STATES,
    AuditEntry,
    Session,
    SessionState,
    TenantConfig,
    utcnow,
)

log = structlog.get_logger()


class Executor(Protocol):
    """Subset of asyncpg Pool / Connection used by the stores."""

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...
    async def fetchrow(self, query: str, *args: Any) -> Any: ...
    async def fetchval(self, query: str, *args: Any) -> Any: ...
    async def execute(self, query: str, *args: Any) -> str: ...


FlightRowFetcher = Callable[[UUID, str, datetime], Awaitable[list[dict[str, Any]]]]

_OPEN_STATE_VALUES = tuple(s.value for s in OPEN_STATES)


def _uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    return value if type(value) is UUID else UUID(str(value))


def _json_load(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _json_dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_SELECT = """
    SELECT c.airfield_id, a.slug, c.enabled, c.dry_run, c.vf_base_url, c.vf_cid,
           c.vf_username, c.vf_password_enc, c.vf_appkey_enc, c.flags, c.daily_budget
      FROM vf_sync_config c
      JOIN airfields a ON a.id = c.airfield_id
"""


def row_to_tenant(row: Any) -> TenantConfig:
    """Map a vf_sync_config+airfields row to TenantConfig (decrypts credentials).

    Raises:
        cryptography.fernet.InvalidToken: key mismatch.
        CredentialKeyMissing: VFSYNC_CRED_KEY not set.
    """
    flags_raw = _json_load(row["flags"]) or {}
    return TenantConfig(
        airfield_id=_uuid(row["airfield_id"]),
        slug=row["slug"],
        enabled=bool(row["enabled"]),
        dry_run=bool(row["dry_run"]),
        vf_base_url=row["vf_base_url"],
        vf_cid=row["vf_cid"],
        vf_username=row["vf_username"],
        vf_password_md5=crypto.decrypt(row["vf_password_enc"]),
        vf_appkey=crypto.decrypt(row["vf_appkey_enc"]),
        flags={str(k): bool(v) for k, v in flags_raw.items()},
        daily_budget=int(row["daily_budget"]),
        timezone=settings.vfsync_timezone,
    )


class PgConfigStore:
    """ConfigStore over vf_sync_config joined with airfields (for the slug)."""

    def __init__(self, db: Executor) -> None:
        self._db = db
        # Enabled tenants whose credentials could not be decrypted on the
        # last load (surfaced in health / alerts instead of silently skipped)
        self.decrypt_failed: list[str] = []

    async def load_enabled(self) -> list[TenantConfig]:
        rows = await self._db.fetch(_CONFIG_SELECT + " WHERE c.enabled = TRUE ORDER BY a.slug")
        tenants: list[TenantConfig] = []
        failed: list[str] = []
        for row in rows:
            try:
                tenants.append(row_to_tenant(row))
            except (InvalidToken, crypto.CredentialKeyMissing) as exc:
                # Tenant is skipped rather than run without credentials.
                failed.append(row["slug"])
                log.error("vfsync_config_decrypt_failed", slug=row["slug"],
                          error=type(exc).__name__)
        self.decrypt_failed = failed
        return tenants

    async def load(self, airfield_id: UUID) -> TenantConfig | None:
        row = await self._db.fetchrow(_CONFIG_SELECT + " WHERE c.airfield_id = $1", airfield_id)
        return row_to_tenant(row) if row else None


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

_SESSION_COLUMNS = (
    "session_id", "airfield_id", "flarm_id", "registration", "takeoff_ts",
    "landing_ts", "landing_method", "start_type_detected", "tow_registration",
    "release_ts", "release_alt_agl_m", "release_method", "tow_time_min",
    "landing_count", "conf_pairing", "conf_landing", "conf_touchgo",
    "matched_flid", "state", "review_reason", "attempts", "last_attempt",
    "created_at", "updated_at",
)
_SESSION_SELECT = f"SELECT {', '.join(_SESSION_COLUMNS)} FROM vf_sync_sessions"

_SESSION_INSERT = f"""
    INSERT INTO vf_sync_sessions ({', '.join(_SESSION_COLUMNS)})
    VALUES ({', '.join(f'${i + 1}' for i in range(len(_SESSION_COLUMNS)))})
    ON CONFLICT (airfield_id, flarm_id, takeoff_ts) DO NOTHING
    RETURNING {', '.join(_SESSION_COLUMNS)}
"""

_SESSION_UPDATE_COLUMNS = tuple(c for c in _SESSION_COLUMNS if c not in ("session_id", "created_at"))
_SESSION_UPDATE = f"""
    UPDATE vf_sync_sessions
       SET {', '.join(f'{c} = ${i + 2}' for i, c in enumerate(_SESSION_UPDATE_COLUMNS))}
     WHERE session_id = $1
"""


def row_to_session(row: Any) -> Session:
    """Map a vf_sync_sessions row to the Session dataclass."""
    return Session(
        session_id=_uuid(row["session_id"]),
        airfield_id=_uuid(row["airfield_id"]),
        flarm_id=row["flarm_id"],
        registration=row["registration"],
        takeoff_ts=row["takeoff_ts"],
        landing_ts=row["landing_ts"],
        landing_method=row["landing_method"],
        start_type_detected=row["start_type_detected"],
        tow_registration=row["tow_registration"],
        release_ts=row["release_ts"],
        release_alt_agl_m=row["release_alt_agl_m"],
        release_method=row["release_method"],
        tow_time_min=row["tow_time_min"],
        landing_count=row["landing_count"],
        conf_pairing=row["conf_pairing"],
        conf_landing=row["conf_landing"],
        conf_touchgo=row["conf_touchgo"],
        matched_flid=row["matched_flid"],
        state=SessionState(row["state"]),
        review_reason=row["review_reason"],
        attempts=row["attempts"],
        last_attempt=row["last_attempt"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def session_to_values(session: Session) -> dict[str, Any]:
    """Session dataclass -> column/value mapping (for INSERT/UPDATE)."""
    return {
        "session_id": session.session_id,
        "airfield_id": session.airfield_id,
        "flarm_id": session.flarm_id,
        "registration": session.registration,
        "takeoff_ts": session.takeoff_ts,
        "landing_ts": session.landing_ts,
        "landing_method": session.landing_method,
        "start_type_detected": session.start_type_detected,
        "tow_registration": session.tow_registration,
        "release_ts": session.release_ts,
        "release_alt_agl_m": session.release_alt_agl_m,
        "release_method": session.release_method,
        "tow_time_min": session.tow_time_min,
        "landing_count": session.landing_count,
        "conf_pairing": session.conf_pairing,
        "conf_landing": session.conf_landing,
        "conf_touchgo": session.conf_touchgo,
        "matched_flid": session.matched_flid,
        "state": session.state.value,
        "review_reason": session.review_reason,
        "attempts": session.attempts,
        "last_attempt": session.last_attempt,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


class PgSessionStore:
    """SessionStore over vf_sync_sessions."""

    def __init__(self, db: Executor) -> None:
        self._db = db

    async def _find_by_key(self, airfield_id: UUID, flarm_id: str,
                           takeoff_ts: datetime | None) -> Session | None:
        row = await self._db.fetchrow(
            _SESSION_SELECT
            + " WHERE airfield_id = $1 AND flarm_id = $2 AND takeoff_ts IS NOT DISTINCT FROM $3"
            + " ORDER BY created_at DESC LIMIT 1",
            airfield_id, flarm_id, takeoff_ts,
        )
        return row_to_session(row) if row else None

    async def upsert(self, session: Session) -> Session:
        """INSERT ... ON CONFLICT DO NOTHING, then SELECT the stored row.

        Race-safe for concurrent inserts of the same key. Sessions with
        takeoff_ts NULL are not covered by the UNIQUE constraint (SQL NULL
        semantics); for those an existing row is looked up first.
        """
        if session.takeoff_ts is None:
            existing = await self._find_by_key(session.airfield_id, session.flarm_id, None)
            if existing is not None:
                return existing
        values = session_to_values(session)
        row = await self._db.fetchrow(_SESSION_INSERT, *(values[c] for c in _SESSION_COLUMNS))
        if row is not None:
            return row_to_session(row)
        existing = await self._find_by_key(session.airfield_id, session.flarm_id, session.takeoff_ts)
        if existing is None:  # pragma: no cover - cannot happen without concurrent delete
            raise RuntimeError("vf_sync_sessions upsert: conflict but no row found")
        return existing

    async def save(self, session: Session) -> None:
        session.updated_at = utcnow()
        values = session_to_values(session)
        status = await self._db.execute(
            _SESSION_UPDATE, session.session_id, *(values[c] for c in _SESSION_UPDATE_COLUMNS)
        )
        if status == "UPDATE 0":
            raise KeyError(f"unknown session {session.session_id}")

    async def get(self, session_id: UUID) -> Session | None:
        row = await self._db.fetchrow(_SESSION_SELECT + " WHERE session_id = $1", session_id)
        return row_to_session(row) if row else None

    async def find_open_for_aircraft(self, airfield_id: UUID, flarm_id: str) -> Session | None:
        row = await self._db.fetchrow(
            _SESSION_SELECT
            + " WHERE airfield_id = $1 AND flarm_id = $2 AND state = ANY($3::text[])"
            + " ORDER BY takeoff_ts DESC NULLS LAST, created_at DESC LIMIT 1",
            airfield_id, flarm_id, list(_OPEN_STATE_VALUES),
        )
        return row_to_session(row) if row else None

    async def list_open(
        self,
        airfield_id: UUID | None = None,
        states: set[SessionState] | None = None,
        airborne_only: bool = False,
    ) -> list[Session]:
        wanted = [s.value for s in (states if states is not None else OPEN_STATES)]
        clauses = ["state = ANY($1::text[])"]
        args: list[Any] = [wanted]
        if airfield_id is not None:
            args.append(airfield_id)
            clauses.append(f"airfield_id = ${len(args)}")
        if airborne_only:
            clauses.append("landing_ts IS NULL")
        rows = await self._db.fetch(
            _SESSION_SELECT + " WHERE " + " AND ".join(clauses)
            + " ORDER BY takeoff_ts ASC NULLS FIRST, created_at ASC",
            *args,
        )
        return [row_to_session(r) for r in rows]

    async def expire_older_than(self, days: int, now: datetime,
                                airfield_id: UUID | None = None) -> int:
        status = await self._db.execute(
            """
            UPDATE vf_sync_sessions
               SET state = $1, updated_at = $2::timestamptz
             WHERE state = ANY($3::text[])
               AND COALESCE(takeoff_ts, created_at)
                   < ($2::timestamptz - make_interval(days => $4::int))
               AND ($5::uuid IS NULL OR airfield_id = $5::uuid)
            """,
            SessionState.EXPIRED.value, now, list(_OPEN_STATE_VALUES), days, airfield_id,
        )
        return int(status.split()[-1])

    async def count_today(self, airfield_id: UUID, day: date, tz: str = "UTC") -> int:
        return int(await self._db.fetchval(
            """
            SELECT COUNT(*) FROM vf_sync_sessions
             WHERE airfield_id = $1 AND (takeoff_ts AT TIME ZONE $3)::date = $2
            """,
            airfield_id, day, tz,
        ))


# ---------------------------------------------------------------------------
# Audit (append-only)
# ---------------------------------------------------------------------------

def row_to_audit(row: Any) -> AuditEntry:
    return AuditEntry(
        id=row["id"],
        airfield_id=_uuid(row["airfield_id"]),
        session_id=_uuid(row["session_id"]),
        action=row["action"],
        flid=row["flid"],
        fields_sent=_json_load(row["fields_sent"]),
        pre_state=_json_load(row["pre_state"]),
        http_status=row["http_status"],
        detail=row["detail"] or "",
        ts=row["ts"],
    )


class PgAuditStore:
    """AuditStore over vf_sync_audit. INSERT and SELECT only."""

    def __init__(self, db: Executor) -> None:
        self._db = db

    async def append(self, entry: AuditEntry) -> AuditEntry:
        row = await self._db.fetchrow(
            """
            INSERT INTO vf_sync_audit
                (session_id, airfield_id, ts, action, flid, fields_sent, pre_state, http_status, detail)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9)
            RETURNING id, session_id, airfield_id, ts, action, flid, fields_sent, pre_state,
                      http_status, detail
            """,
            entry.session_id, entry.airfield_id, entry.ts, entry.action, entry.flid,
            _json_dump(entry.fields_sent), _json_dump(entry.pre_state),
            entry.http_status, entry.detail,
        )
        return row_to_audit(row)

    async def list(
        self, airfield_id: UUID, session_id: UUID | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        args: list[Any] = [airfield_id, limit]
        clause = ""
        if session_id is not None:
            args.append(session_id)
            clause = " AND session_id = $3"
        rows = await self._db.fetch(
            "SELECT id, session_id, airfield_id, ts, action, flid, fields_sent, pre_state,"
            " http_status, detail FROM vf_sync_audit WHERE airfield_id = $1" + clause
            + " ORDER BY ts DESC, id DESC LIMIT $2",
            *args,
        )
        return [row_to_audit(r) for r in rows]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class PgBudgetStore:
    """BudgetStore over vf_sync_budget (atomic upsert increment)."""

    def __init__(self, db: Executor) -> None:
        self._db = db

    async def used(self, airfield_id: UUID, day: date) -> int:
        value = await self._db.fetchval(
            "SELECT used FROM vf_sync_budget WHERE airfield_id = $1 AND day = $2",
            airfield_id, day,
        )
        return int(value or 0)

    async def increment(self, airfield_id: UUID, day: date, n: int = 1) -> int:
        return int(await self._db.fetchval(
            """
            INSERT INTO vf_sync_budget (airfield_id, day, used) VALUES ($1, $2, $3)
            ON CONFLICT (airfield_id, day) DO UPDATE SET used = vf_sync_budget.used + EXCLUDED.used
            RETURNING used
            """,
            airfield_id, day, n,
        ))


# ---------------------------------------------------------------------------
# Recovery source: flight_status / flight_log
# ---------------------------------------------------------------------------

_FLIGHT_ROW_COLUMNS = (
    "landing_time, landing_count, landing_method, landing_confidence, launch_type, "
    "tow_plane_registration, release_altitude_agl, release_time, release_method, "
    "tow_duration_s, pairing_confidence"
)
# flight_status additionally says whether the landing is past the touch & go
# window; flight_log rows are only written once that is the case
_FLIGHT_ROW_EXTRA = {"flight_status": ", landing_final", "flight_log": ", TRUE AS landing_final"}


def make_flight_row_fetcher(db: Executor) -> FlightRowFetcher:
    """Build the recovery fetcher: rows of flight_log and flight_status for one flight.

    Each returned dict carries ``source`` ("flight_log" | "flight_status")
    plus the landing/launch columns. flight_log (archived, final) comes first.
    """

    async def fetch_flight_rows(airfield_id: UUID, flarm_id: str,
                                takeoff_ts: datetime) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for table in ("flight_log", "flight_status"):
            fetched = await db.fetch(
                f"SELECT {_FLIGHT_ROW_COLUMNS}{_FLIGHT_ROW_EXTRA[table]} FROM {table}"
                " WHERE airfield_id = $1 AND flarm_id = $2 AND takeoff_time = $3",
                airfield_id, flarm_id, takeoff_ts,
            )
            for r in fetched:
                d = dict(r)
                d["source"] = table
                rows.append(d)
        return rows

    return fetch_flight_rows
