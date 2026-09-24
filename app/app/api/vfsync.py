"""VF-Sync API - tenant configuration, status, sessions and audit (AP-10).

GET  /api/vfsync/config/{airfield_id}   - config (credentials only as set/unset)
PUT  /api/vfsync/config/{airfield_id}   - upsert config (never touches `enabled`)
GET  /api/vfsync/status/{airfield_id}   - budget, session counters, worker health
GET  /api/vfsync/sessions               - paginated sessions of one local day
GET  /api/vfsync/audit                  - newest audit entries

The API only reads/writes the vf_sync_* tables and the worker's health
hash; all sync logic lives in the vfsync worker. Every query is scoped by
airfield_id after the tenant ownership check. Credentials are encrypted
with Fernet before they reach the database and are never logged.
"""

import json
from dataclasses import asdict
from datetime import date
from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import settings
from app.db import queries as q
from app.db.connection import get_db
from app.dependencies import get_current_user
from app.redis_client import get_redis
from app.api.vfsync_schemas import (
    VfAuditPageResponse,
    VfSessionPageResponse,
    VfSyncConfigResponse,
    VfSyncConfigUpdateRequest,
    VfSyncStatusResponse,
)
from app.vfsync import crypto
from app.vfsync.budget import BudgetGuard, stage_for
from app.vfsync.models import (
    KNOWN_FLAGS,
    OPEN_STATES,
    SessionState,
    TenantConfig,
)
from app.vfsync.stores_pg import (
    Executor,
    PgAuditStore,
    PgBudgetStore,
    row_to_session,
)

log = structlog.get_logger()
router = APIRouter(prefix="/api/vfsync", tags=["vfsync"])

VFSYNC_HEALTH_KEY = "vfsync:health"

SESSIONS_PAGE_SIZE_DEFAULT = 50
SESSIONS_PAGE_SIZE_MAX = 200
AUDIT_LIMIT_DEFAULT = 200
AUDIT_LIMIT_MAX = 1000

_CONFIG_DEFAULTS = TenantConfig(airfield_id=UUID(int=0), slug="")


# ---------------------------------------------------------------------------
# Dependencies (overridable in tests)
# ---------------------------------------------------------------------------

def db_executor() -> Executor:
    """asyncpg pool of the API process (tests override with a connection)."""
    return get_db()


def redis_client() -> Any:
    """Redis client of the API process; None when not initialized."""
    try:
        return get_redis()
    except RuntimeError:
        return None


async def _verify_airfield(db: Executor, airfield_id: UUID, tenant_id: UUID) -> dict:
    """Same rules as airfields.py: 404 unknown, 403 foreign tenant."""
    row = await db.fetchrow(q.AIRFIELD_BY_ID, airfield_id)
    if not row:
        raise HTTPException(status_code=404, detail="Flugplatz nicht gefunden")
    if row["tenant_id"] != tenant_id:
        raise HTTPException(status_code=403, detail="Kein Zugriff auf diesen Flugplatz")
    return dict(row)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_COLS = (
    "airfield_id, enabled, dry_run, vf_base_url, vf_cid, vf_username, "
    "(vf_password_enc IS NOT NULL) AS has_password, "
    "(vf_appkey_enc IS NOT NULL) AS has_appkey, "
    "flags, daily_budget, updated_at"
)
_CONFIG_SELECT = f"SELECT {_CONFIG_COLS} FROM vf_sync_config WHERE airfield_id = $1"


def _flags_dict(raw: Any) -> dict[str, bool]:
    """Normalize the JSONB flags to the four known flags (missing -> False)."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    raw = raw or {}
    return {name: bool(raw.get(name, False)) for name in KNOWN_FLAGS}


def _config_row_to_response(row: Any) -> dict:
    return {
        "airfield_id": row["airfield_id"],
        "enabled": bool(row["enabled"]),
        "dry_run": bool(row["dry_run"]),
        "vf_base_url": row["vf_base_url"],
        "vf_cid": row["vf_cid"],
        "vf_username": row["vf_username"],
        "has_password": bool(row["has_password"]),
        "has_appkey": bool(row["has_appkey"]),
        "flags": _flags_dict(row["flags"]),
        "daily_budget": int(row["daily_budget"]),
        "updated_at": row["updated_at"],
    }


@router.get("/config/{airfield_id}", response_model=VfSyncConfigResponse)
async def get_config(
    airfield_id: UUID,
    user: dict = Depends(get_current_user),
    db: Executor = Depends(db_executor),
):
    """VF-Sync configuration of an airfield (credentials only as set/unset)."""
    await _verify_airfield(db, airfield_id, user["tenant_id"])
    row = await db.fetchrow(_CONFIG_SELECT, airfield_id)
    if not row:
        raise HTTPException(status_code=404, detail="Keine VF-Sync-Konfiguration vorhanden")
    return _config_row_to_response(row)


def _encrypt_or_503(value: str) -> bytes:
    try:
        return crypto.encrypt(value)
    except crypto.CredentialKeyMissing:
        raise HTTPException(
            status_code=503,
            detail="VFSYNC_CRED_KEY ist auf dem Server nicht konfiguriert",
        )


# Column -> ($provided, $value) pairs; the upsert takes airfield_id as $1
# and then two parameters per column in this order.
_UPSERT_COLUMNS = (
    "dry_run", "vf_base_url", "vf_cid", "vf_username",
    "vf_password_enc", "vf_appkey_enc", "flags", "daily_budget",
)


def _build_upsert_sql() -> str:
    """Atomic INSERT ... ON CONFLICT DO UPDATE writing only provided columns.

    `enabled` is intentionally absent: it keeps its DB default (FALSE) on
    insert and is never touched on update (Kap. 8.6).
    """
    values: list[str] = ["$1"]
    updates: list[str] = []
    for i, col in enumerate(_UPSERT_COLUMNS):
        p_flag = f"${2 + 2 * i}"
        p_val = f"${3 + 2 * i}"
        if col == "flags":
            values.append(f"{p_val}::jsonb")
            updates.append(
                f"{col} = CASE WHEN {p_flag} THEN vf_sync_config.{col} || EXCLUDED.{col}"
                f" ELSE vf_sync_config.{col} END"
            )
        else:
            values.append(p_val)
            updates.append(
                f"{col} = CASE WHEN {p_flag} THEN EXCLUDED.{col}"
                f" ELSE vf_sync_config.{col} END"
            )
    return (
        f"INSERT INTO vf_sync_config (airfield_id, {', '.join(_UPSERT_COLUMNS)})"
        f" VALUES ({', '.join(values)})"
        f" ON CONFLICT (airfield_id) DO UPDATE SET {', '.join(updates)}, updated_at = NOW()"
        f" RETURNING {_CONFIG_COLS}"
    )


_CONFIG_UPSERT = _build_upsert_sql()


@router.put("/config/{airfield_id}", response_model=VfSyncConfigResponse)
async def update_config(
    airfield_id: UUID,
    body: VfSyncConfigUpdateRequest,
    user: dict = Depends(get_current_user),
    db: Executor = Depends(db_executor),
):
    """Create or update the VF-Sync configuration (never changes `enabled`)."""
    await _verify_airfield(db, airfield_id, user["tenant_id"])
    provided = body.model_fields_set

    # (provided, value) per column; values for the INSERT path fall back to
    # the TenantConfig defaults when the field was not sent.
    columns: dict[str, tuple[bool, Any]] = {
        "dry_run": ("dry_run" in provided,
                    body.dry_run if "dry_run" in provided else _CONFIG_DEFAULTS.dry_run),
        "vf_base_url": ("vf_base_url" in provided and body.vf_base_url is not None,
                        body.vf_base_url or _CONFIG_DEFAULTS.vf_base_url),
        "vf_cid": ("vf_cid" in provided, body.vf_cid),
        "vf_username": ("vf_username" in provided, body.vf_username),
        "vf_password_enc": (body.vf_password_md5 is not None,
                            _encrypt_or_503(body.vf_password_md5)
                            if body.vf_password_md5 is not None else None),
        "vf_appkey_enc": (body.vf_appkey is not None,
                          _encrypt_or_503(body.vf_appkey)
                          if body.vf_appkey is not None else None),
        "flags": (body.flags is not None, json.dumps(body.flags or {})),
        "daily_budget": (body.daily_budget is not None,
                         body.daily_budget or _CONFIG_DEFAULTS.daily_budget),
    }
    args: list[Any] = [airfield_id]
    for col in _UPSERT_COLUMNS:
        flag, value = columns[col]
        args.extend((flag, value))

    row = await db.fetchrow(_CONFIG_UPSERT, *args)

    # Field names only - never the values (credentials).
    log.info("vfsync_config_updated", airfield_id=str(airfield_id),
             fields=sorted(provided))
    return _config_row_to_response(row)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

_SESSION_COUNTS = """
    SELECT
        COUNT(*) FILTER (WHERE state = ANY($2::text[]))                    AS open,
        COUNT(*) FILTER (WHERE state = $3)                                  AS awaiting_match,
        COUNT(*) FILTER (WHERE state = $4)                                  AS review,
        COUNT(*) FILTER (WHERE state = $5
                           AND (takeoff_ts AT TIME ZONE $6)::date = $7)     AS completed_today,
        COUNT(*) FILTER (WHERE (takeoff_ts AT TIME ZONE $6)::date = $7)     AS movements_today
    FROM vf_sync_sessions
    WHERE airfield_id = $1
"""


def _budget_day(db: Executor, airfield_id: UUID, slug: str) -> date:
    """Local calendar day of the tenant (same rule as the worker's BudgetGuard)."""
    tenant = TenantConfig(airfield_id=airfield_id, slug=slug, timezone=settings.vfsync_timezone)
    return BudgetGuard(PgBudgetStore(db)).day_for(tenant)


async def _worker_health(redis: Any) -> dict | None:
    if redis is None:
        return None
    try:
        data = await redis.hgetall(VFSYNC_HEALTH_KEY)
    except Exception as exc:  # noqa: BLE001 - health is best effort
        log.warning("vfsync_health_read_failed", error=type(exc).__name__)
        return None
    if not data:
        return None
    return {
        "status": data.get("status") or "unknown",
        "last_event_ts": data.get("last_event_ts") or None,
        "updated_at": data.get("updated_at") or None,
    }


@router.get("/status/{airfield_id}", response_model=VfSyncStatusResponse)
async def get_status(
    airfield_id: UUID,
    user: dict = Depends(get_current_user),
    db: Executor = Depends(db_executor),
    redis: Any = Depends(redis_client),
):
    """Budget, session counters and worker health for one airfield.

    Works without a config row (enabled=false, defaults) so the frontend
    can poll it before the tenant has configured anything.
    """
    airfield = await _verify_airfield(db, airfield_id, user["tenant_id"])
    cfg = await db.fetchrow(
        "SELECT enabled, dry_run, daily_budget FROM vf_sync_config WHERE airfield_id = $1",
        airfield_id,
    )
    enabled = bool(cfg["enabled"]) if cfg else False
    dry_run = bool(cfg["dry_run"]) if cfg else _CONFIG_DEFAULTS.dry_run
    daily_budget = int(cfg["daily_budget"]) if cfg else _CONFIG_DEFAULTS.daily_budget

    day = _budget_day(db, airfield_id, airfield["slug"])
    used = await PgBudgetStore(db).used(airfield_id, day)
    counts = await db.fetchrow(
        _SESSION_COUNTS,
        airfield_id, [s.value for s in OPEN_STATES],
        SessionState.AWAITING_MATCH.value, SessionState.REVIEW.value,
        SessionState.COMPLETED.value, settings.vfsync_timezone, day,
    )
    stage = stage_for(used, daily_budget, int(counts["movements_today"]))

    return {
        "enabled": enabled,
        "dry_run": dry_run,
        "budget": {"day": day, "used": used, "daily_budget": daily_budget,
                   "stage": stage.value},
        "sessions": {
            "open": int(counts["open"]),
            "awaiting_match": int(counts["awaiting_match"]),
            "review": int(counts["review"]),
            "completed_today": int(counts["completed_today"]),
        },
        "worker": await _worker_health(redis),
    }


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

_SESSION_COLS = (
    "session_id, airfield_id, flarm_id, registration, takeoff_ts, landing_ts, "
    "landing_method, start_type_detected, tow_registration, release_ts, "
    "release_alt_agl_m, release_method, tow_time_min, landing_count, conf_pairing, "
    "conf_landing, conf_touchgo, matched_flid, state, review_reason, attempts, "
    "last_attempt, created_at, updated_at"
)


@router.get("/sessions", response_model=VfSessionPageResponse)
async def list_sessions(
    airfield_id: UUID,
    day: date | None = Query(None, alias="date",
                             description="Kalendertag in der Mandanten-Zeitzone"),
    state: SessionState | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(SESSIONS_PAGE_SIZE_DEFAULT, ge=1, le=SESSIONS_PAGE_SIZE_MAX),
    user: dict = Depends(get_current_user),
    db: Executor = Depends(db_executor),
):
    """Sessions of one local calendar day, newest takeoff first."""
    airfield = await _verify_airfield(db, airfield_id, user["tenant_id"])
    if day is None:
        day = _budget_day(db, airfield_id, airfield["slug"])

    # Sessions without takeoff_ts (landing seen first) fall on their creation day.
    conditions = [
        "airfield_id = $1",
        "(COALESCE(takeoff_ts, created_at) AT TIME ZONE $2)::date = $3",
    ]
    params: list[Any] = [airfield_id, settings.vfsync_timezone, day]
    if state is not None:
        params.append(state.value)
        conditions.append(f"state = ${len(params)}")
    where = " AND ".join(conditions)

    total = int(await db.fetchval(
        f"SELECT COUNT(*) FROM vf_sync_sessions WHERE {where}", *params
    ))
    offset = (page - 1) * page_size
    rows = await db.fetch(
        f"SELECT {_SESSION_COLS} FROM vf_sync_sessions WHERE {where}"
        f" ORDER BY takeoff_ts DESC NULLS LAST, created_at DESC"
        f" LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}",
        *params, page_size, offset,
    )
    return {
        "items": [asdict(row_to_session(r)) for r in rows],
        "total": total,
        "page": page,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@router.get("/audit", response_model=VfAuditPageResponse)
async def list_audit(
    airfield_id: UUID,
    session_id: UUID | None = Query(None),
    limit: int = Query(AUDIT_LIMIT_DEFAULT, ge=1, le=AUDIT_LIMIT_MAX),
    user: dict = Depends(get_current_user),
    db: Executor = Depends(db_executor),
):
    """Newest audit entries of an airfield, optionally for one session."""
    await _verify_airfield(db, airfield_id, user["tenant_id"])
    entries = await PgAuditStore(db).list(airfield_id, session_id=session_id, limit=limit)
    return {
        "items": [
            {
                "id": e.id,
                "ts": e.ts,
                "action": e.action,
                "session_id": e.session_id,
                "flid": e.flid,
                "fields_sent": e.fields_sent,
                "pre_state": e.pre_state,
                "http_status": e.http_status,
                "detail": e.detail,
            }
            for e in entries
        ]
    }
