"""REST Monitor endpoint - fallback for clients without WebSocket.

GET /api/monitor/{slug} returns the current flight state from Redis.
No authentication required (public monitor).
"""

import json
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.schemas import (
    AlarmActionCreated,
    AlarmActionItem,
    AlarmActionList,
    AlarmActionRequest,
    AlarmHotState,
    TrackPoint,
    TrackResponse,
)
from app.config import settings
from app.dependencies import get_current_user
from app.redis_client import get_redis
from app.tracking.flight_state import FlightStatus

log = structlog.get_logger()

router = APIRouter(prefix="/api/monitor", tags=["Monitor"])

# Hot-state status -> alarm_kind of a Flugleiter action (everything else: "other").
_STATUS_TO_ALARM_KIND: dict[int, str] = {
    FlightStatus.ALARM: "alarm",
    FlightStatus.EMERGENCY: "emergency",
    FlightStatus.OUTLANDING: "outlanding",
    FlightStatus.OUTLANDING_PENDING: "outlanding",
    FlightStatus.SIGNAL_LOST: "signal_lost",
}

# German wording of the alarm_action event message (state -> text).
_ALARM_STATE_TEXT: dict[str, str] = {
    "acknowledged": "Alarm quittiert",
    "retrieval_underway": "Rückholung läuft",
    "resolved": "Alarm erledigt",
    "false_alarm": "Fehlalarm",
}

_ACTION_COLUMNS = (
    "id, state, comment, alarm_kind, set_by, created_at, flight_takeoff_ts"
)

# Display label for alarm_set_by in the public hot state when the tenant has
# no name. The Flugleiter's e-mail never leaves the auth-only history.
_ALARM_SET_BY_FALLBACK = "Flugleiter"


def _alarm_set_by_label(tenant_name: str | None) -> str:
    """Non-personal label shown to (anonymous) monitor clients."""
    return (tenant_name or "").strip() or _ALARM_SET_BY_FALLBACK


@router.get("/{slug}")
async def get_monitor_status(slug: str):
    """Get current flight status for an airfield.

    Returns all active flights with QDR, distance, altitude, etc.
    Data comes directly from Redis Hot State (< 5ms latency).

    Args:
        slug: Airfield slug (e.g. "ohlstadt").
    """
    redis = get_redis()

    # Check if airfield has active flights
    flarm_ids = await redis.smembers(f"flights:{slug}")

    # If no flights, check if airfield exists at all
    if not flarm_ids:
        is_active = await redis.sismember("active_airfields", slug)
        if not is_active:
            from app.db.connection import get_db
            db = get_db()
            row = await db.fetchrow(
                "SELECT slug FROM airfields WHERE slug = $1 AND is_active = TRUE",
                slug,
            )
            if not row:
                raise HTTPException(status_code=404, detail="Airfield not found")

        return {
            "airfield": slug,
            "timestamp": _utcnow_iso(),
            "flights": [],
            "stats": {"flying": 0, "landed": 0, "alarm": 0, "outlanding": 0},
        }

    # Pipeline all HGETALL calls for efficiency
    pipe = redis.pipeline()
    for fid in flarm_ids:
        pipe.hgetall(f"flight:{slug}:{fid}")
    results = await pipe.execute()

    flights = []
    for data in results:
        if data:
            flights.append(_to_camel_case(data))

    stats = {
        "flying": sum(1 for f in flights if int(f.get("status", 0)) == 2),
        "landed": sum(1 for f in flights if int(f.get("status", 0)) == 3),
        "alarm": sum(1 for f in flights if int(f.get("status", 0)) == 5),
        "outlanding": sum(1 for f in flights if int(f.get("status", 0)) in (4, 7)),
    }

    return {
        "airfield": slug,
        "timestamp": _utcnow_iso(),
        "flights": flights,
        "stats": stats,
    }


@router.get("/{slug}/today")
async def get_today(slug: str):
    """Get the full picture of TODAY's flights for an airfield.

    Merges:
      - active flights from Redis hot state (incl. sticky-landed)
      - flights already archived to flight_log earlier today

    Returns one entry per FLARM-ID (the most recent run wins).
    """
    from app.db.connection import get_db

    redis = get_redis()
    db = get_db()

    # 1. Resolve airfield
    row = await db.fetchrow(
        "SELECT id, slug, latitude, longitude, elevation_m, "
        "landed_visible_minutes, monitor_strip_fields "
        "FROM airfields WHERE slug = $1 AND is_active = TRUE",
        slug,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Airfield not found")
    airfield_id = row["id"]
    strip_fields = list(row["monitor_strip_fields"] or [])
    landed_visible_minutes = int(row["landed_visible_minutes"] or 1440)

    # 2. Active flights from Redis (full hot-state picture)
    flights_by_fid: dict[str, dict] = {}
    flarm_ids = await redis.smembers(f"flights:{slug}")
    if flarm_ids:
        pipe = redis.pipeline()
        for fid in flarm_ids:
            pipe.hgetall(f"flight:{slug}:{fid}")
        for data in await pipe.execute():
            if data:
                f = _to_camel_case(data)
                f["source"] = "live"
                flights_by_fid[data.get("flarm_id", "")] = f

    # 3. Recently landed flights from PostgreSQL.
    # Two filters:
    #   a) the flight must have landed within the visibility window
    #      (landed_visible_minutes ago at most), so a 120-minute setting
    #      really means "show landings from the last 2h"
    #   b) AND the flight must be from the current UTC day, so we never
    #      pull yesterday's leftovers even if the minutes window is huge
    archived = await db.fetch(
        """
        SELECT flarm_id, registration, competition_sign, aircraft_model,
               takeoff_time, landing_time, flight_duration_s,
               max_altitude_m, max_distance_m, launch_type, landing_type,
               takeoff_airfield, landing_airfield, is_visitor
        FROM flight_log
        WHERE airfield_id = $1
          AND landing_time IS NOT NULL
          AND landing_time >= NOW() - ($2 || ' minutes')::interval
          AND landing_time >= (NOW() AT TIME ZONE 'UTC')::date
        ORDER BY landing_time DESC
        """,
        airfield_id,
        str(landed_visible_minutes),
    )
    for r in archived:
        fid = r["flarm_id"]
        # Live entry takes precedence (current run is more recent)
        if fid in flights_by_fid:
            continue
        flights_by_fid[fid] = {
            "flarmId": fid,
            "registration": r["registration"] or "",
            "competitionSign": r["competition_sign"] or "",
            "aircraftModel": r["aircraft_model"] or "",
            "status": "3",  # LANDING / archived
            "takeoffTime": _iso(r["takeoff_time"]),
            "landingTime": _iso(r["landing_time"]),
            "flightDurationS": str(r["flight_duration_s"] or 0),
            "maxAltitudeM": str(r["max_altitude_m"] or 0),
            "maxDistanceM": str(r["max_distance_m"] or 0),
            "launchType": r["launch_type"] or "unknown",
            "landingType": r["landing_type"] or "home",
            # Same string encoding as the Redis hash (see FlightState.to_redis_dict)
            "takeoffAirfield": r["takeoff_airfield"] or "",
            "landingAirfield": r["landing_airfield"] or "",
            "isVisitor": "1" if r["is_visitor"] else "0",
            "source": "archived",
        }

    flights = list(flights_by_fid.values())
    # Sort by max(landing_time, takeoff_time) desc
    flights.sort(
        key=lambda f: (f.get("landingTime") or f.get("takeoffTime") or ""),
        reverse=True,
    )

    # 4. Day statistics
    starts_today = len(flights)
    landed_today = sum(
        1 for f in flights
        if f.get("landingTime") or int(f.get("status", 0) or 0) == 3
    )
    in_air = sum(
        1 for f in flights
        if int(f.get("status", 0) or 0) in (1, 2, 6, 10)  # TAKEOFF/FLYING/TOWING/SIGNAL_LOST
    )
    by_launch: dict[str, int] = {}
    longest = {"reg": "", "duration_s": 0}
    highest = {"reg": "", "altitude_m": 0}
    for f in flights:
        lt = f.get("launchType") or "unknown"
        by_launch[lt] = by_launch.get(lt, 0) + 1
        dur = int(float(f.get("flightDurationS") or 0))
        if dur > longest["duration_s"]:
            longest = {"reg": f.get("registration") or f.get("flarmId", ""),
                       "duration_s": dur}
        alt = int(float(f.get("maxAltitudeM") or 0))
        if alt > highest["altitude_m"]:
            highest = {"reg": f.get("registration") or f.get("flarmId", ""),
                       "altitude_m": alt}

    return {
        "airfield": slug,
        "timestamp": _utcnow_iso(),
        "flights": flights,
        "day_stats": {
            "starts_today": starts_today,
            "landed_today": landed_today,
            "in_air": in_air,
            "by_launch": by_launch,
            "longest": longest,
            "highest": highest,
        },
        "config": {
            "strip_fields": strip_fields,
            "landed_visible_minutes": landed_visible_minutes,
        },
    }


# Longest look-back the track stream can serve (its sliding TTL).
_TRACK_MAX_HOURS = settings.track_retention_s / 3600


@router.get("/{slug}/flights/{flarm_id}/track", response_model=TrackResponse)
async def get_flight_track(
    slug: str,
    flarm_id: str,
    hours: float = Query(
        _TRACK_MAX_HOURS, ge=0.1, le=_TRACK_MAX_HOURS,
        description="Look-back window in hours",
    ),
):
    """Get an aircraft's flight track of the last ``hours`` hours.

    Reads the thinned per-aircraft track stream written by the worker
    (``track:{slug}:{flarm_id}``, stream id = beacon time in ms). Points are
    returned in ascending time order. No authentication (public monitor).

    Args:
        slug: Airfield slug.
        flarm_id: FLARM device ID (case-insensitive).
        hours: Look-back window in hours, 0.1 .. stream retention
            (``settings.track_retention_s``, default 24 h = default value).
    """
    redis = get_redis()
    flarm_id = flarm_id.upper()

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_ms = int(since.timestamp() * 1000)

    entries = await redis.xrange(f"track:{slug}:{flarm_id}", min=f"{since_ms}-0", max="+")

    # No data: make sure the airfield exists at all (same rule as the
    # status endpoint), otherwise answer with an empty track.
    if not entries:
        is_active = await redis.sismember("active_airfields", slug)
        if not is_active:
            from app.db.connection import get_db
            db = get_db()
            row = await db.fetchrow(
                "SELECT slug FROM airfields WHERE slug = $1 AND is_active = TRUE",
                slug,
            )
            if not row:
                raise HTTPException(status_code=404, detail="Airfield not found")

    points = [_track_point(entry_id, fields) for entry_id, fields in entries]

    return TrackResponse(
        airfield=slug,
        flarm_id=flarm_id,
        since=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        points=points,
    )


def _track_point(entry_id: str, fields: dict[str, str]) -> TrackPoint:
    """Build a TrackPoint from a stream entry (id ``<ms>-<seq>``).

    The Redis client runs with ``decode_responses=True``, so id and field
    values arrive as ``str``.
    """
    ts_ms = int(entry_id.split("-", 1)[0])
    t = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _get(name: str) -> str:
        return fields.get(name, "0")

    return TrackPoint(
        t=t,
        lat=float(_get("lat")),
        lon=float(_get("lon")),
        alt=int(float(_get("alt"))),
        agl=int(float(_get("agl"))),
        speed=int(float(_get("speed"))),
        vs=float(_get("vs")),
        track=int(float(_get("track"))),
    )


@router.post("/{slug}/flights/{flarm_id}/dismiss")
async def dismiss_flight(
    slug: str,
    flarm_id: str,
    user: dict = Depends(get_current_user),
):
    """Dismiss a flight from the active monitor.

    Removes the flight from the Redis hot state. If the flight was airborne
    or landed, it is first archived to flight_log so nothing is lost.
    The change is broadcast to all connected monitors via the standard
    flight_removed WebSocket message.

    Auth + ownership: only the tenant owning this airfield may dismiss.
    """
    from app.db.connection import get_db
    from app.tracking.flight_state import FlightState
    from app.tracking.state_synchronizer import StateSynchronizer
    db = get_db()

    # 1. Verify airfield ownership
    row = await db.fetchrow(
        "SELECT id, tenant_id FROM airfields WHERE slug = $1",
        slug,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Flugplatz nicht gefunden")
    if row["tenant_id"] != user["tenant_id"]:
        raise HTTPException(status_code=403, detail="Kein Zugriff auf diesen Flugplatz")

    redis = get_redis()

    # 2. Load flight from hot state
    data = await redis.hgetall(f"flight:{slug}:{flarm_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Flug nicht im Hot-State")

    # 3. Archive to flight_log so the flight is preserved.
    # Best-effort: failures here must not block the dismiss.
    try:
        flight = FlightState.from_redis(data, slug)
        flight.airfield_id = row["id"]
        await StateSynchronizer()._write_flight_log(db, flight, flight.status)
    except Exception:
        log.exception("dismiss_archive_failed", slug=slug, flarm_id=flarm_id)

    # 4. Remove from hot state (set + hash)
    pipe = redis.pipeline()
    pipe.srem(f"flights:{slug}", flarm_id)
    pipe.delete(f"flight:{slug}:{flarm_id}")
    await pipe.execute()

    # 5. Publish dismissed event so all monitors update live
    import json
    await redis.publish(
        f"event:{slug}",
        json.dumps({
            "type": "dismissed",
            "flarm_id": flarm_id,
            "message": "Vom Flugleiter aus der Liste entfernt",
        }),
    )

    log.info(
        "flight_dismissed",
        slug=slug,
        flarm_id=flarm_id,
        by=user["email"],
    )
    return {"ok": True, "flarm_id": flarm_id}


# ---------------------------------------------------------------------------
# Tower alarm workflow: acknowledge / annotate an alarm without removing the
# flight. Every action is appended to flight_alarm_actions (history); the
# latest one is mirrored into the flight hash as alarm_* fields (HSET only -
# the worker's state machine never touches these fields, and they vanish
# together with the hash when the flight is archived).
# ---------------------------------------------------------------------------

@router.post(
    "/{slug}/flights/{flarm_id}/actions",
    response_model=AlarmActionCreated,
    status_code=status.HTTP_201_CREATED,
)
async def create_alarm_action(
    slug: str,
    flarm_id: str,
    body: AlarmActionRequest,
    user: dict = Depends(get_current_user),
):
    """Record how the Flugleiter handled an alarm for a flight.

    Auth + ownership as in ``dismiss_flight``. If the flight is in the hot
    state, ``alarm_kind`` is derived from its status and
    ``flight_takeoff_ts`` from its ``takeoff_time``; the hash then gets the
    ``alarm_state``/``alarm_comment``/``alarm_set_by``/``alarm_set_at``
    fields and an ``alarm_action`` event is published for the monitors.
    Without hot state the action is still stored (``alarm_kind`` from the
    body, default ``other``).

    Privacy: the hash and the event are readable by anonymous monitor
    clients, so ``alarm_set_by`` there is a *display label* (tenant name,
    fallback "Flugleiter"). The user's e-mail is stored only in
    ``flight_alarm_actions.set_by`` (auth-only history / ``action.setBy``).
    """
    from app.db.connection import get_db
    db = get_db()

    airfield = await _owned_airfield(db, slug, user)
    airfield_id = airfield["id"]
    set_by_label = _alarm_set_by_label(airfield.get("tenant_name"))
    redis = get_redis()
    flarm_id = flarm_id.upper()
    key = f"flight:{slug}:{flarm_id}"

    # 2. Hot state (optional)
    hot = await redis.hgetall(key)
    if hot:
        alarm_kind = _alarm_kind_from_status(hot.get("status"))
        takeoff_ts = _parse_iso(hot.get("takeoff_time"))
    else:
        alarm_kind = body.alarm_kind or "other"
        takeoff_ts = None

    # 3. History row
    row = await db.fetchrow(
        f"""
        INSERT INTO flight_alarm_actions
            (airfield_id, flarm_id, flight_takeoff_ts, alarm_kind, state, comment, set_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING {_ACTION_COLUMNS}
        """,
        airfield_id, flarm_id, takeoff_ts, alarm_kind, body.state, body.comment,
        user["email"],
    )
    action = _action_item(row)
    alarm_state = AlarmHotState(
        alarm_state=body.state,
        alarm_comment=body.comment or "",
        alarm_set_by=set_by_label,
        alarm_set_at=action.created_at,
    )

    # 4. Mirror into the hot state + notify monitors
    mirrored = False
    if hot:
        fields = alarm_state.model_dump(by_alias=False)
        await redis.hset(key, mapping=fields)
        # Race: the worker may have archived the flight (DEL hash, SREM set)
        # between HGETALL and HSET - the HSET then re-created the hash
        # without TTL. Undo that instead of leaving an orphan behind.
        if await redis.ttl(key) == -1:
            await redis.delete(key)
            log.warning(
                "alarm_action_flight_gone",
                slug=slug,
                flarm_id=flarm_id,
                state=body.state,
            )
        else:
            mirrored = True
            message = f"{_ALARM_STATE_TEXT[body.state]} ({set_by_label})"
            await redis.publish(
                f"event:{slug}",
                json.dumps({
                    "type": "alarm_action",
                    "flarm_id": flarm_id,
                    "data": fields,
                    "message": message,
                }),
            )

    log.info(
        "alarm_action_recorded",
        slug=slug,
        flarm_id=flarm_id,
        state=body.state,
        alarm_kind=alarm_kind,
        in_hot_state=mirrored,
        by=user["email"],
    )
    return AlarmActionCreated(action=action, alarm_state=alarm_state)


@router.get("/{slug}/flights/{flarm_id}/actions", response_model=AlarmActionList)
async def list_flight_alarm_actions(
    slug: str,
    flarm_id: str,
    date: date_type | None = Query(None, description="UTC day, default today"),
    user: dict = Depends(get_current_user),
):
    """Alarm-action history of one aircraft for a UTC day, newest first."""
    from app.db.connection import get_db
    db = get_db()

    airfield_id = await _owned_airfield_id(db, slug, user)
    start, end = _utc_day_bounds(date)
    rows = await db.fetch(
        f"""
        SELECT {_ACTION_COLUMNS}
        FROM flight_alarm_actions
        WHERE airfield_id = $1 AND flarm_id = $2
          AND created_at >= $3 AND created_at < $4
        ORDER BY created_at DESC, id DESC
        """,
        airfield_id, flarm_id.upper(), start, end,
    )
    items = [_action_item(r) for r in rows]
    return AlarmActionList(items=items, count=len(items))


@router.get("/{slug}/actions", response_model=AlarmActionList)
async def list_airfield_alarm_actions(
    slug: str,
    date: date_type | None = Query(None, description="UTC day, default today"),
    user: dict = Depends(get_current_user),
):
    """Alarm-action history of all aircraft of an airfield for a UTC day."""
    from app.db.connection import get_db
    db = get_db()

    airfield_id = await _owned_airfield_id(db, slug, user)
    start, end = _utc_day_bounds(date)
    rows = await db.fetch(
        f"""
        SELECT {_ACTION_COLUMNS}
        FROM flight_alarm_actions
        WHERE airfield_id = $1
          AND created_at >= $2 AND created_at < $3
        ORDER BY created_at DESC, id DESC
        """,
        airfield_id, start, end,
    )
    items = [_action_item(r) for r in rows]
    return AlarmActionList(items=items, count=len(items))


async def _owned_airfield(db, slug: str, user: dict):
    """Resolve the airfield and enforce tenant ownership (404 / 403).

    Returns:
        Record with ``id``, ``tenant_id`` and ``tenant_name`` (may be None).
    """
    row = await db.fetchrow(
        """
        SELECT a.id, a.tenant_id, t.name AS tenant_name
        FROM airfields a
        LEFT JOIN tenants t ON t.id = a.tenant_id
        WHERE a.slug = $1
        """,
        slug,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Flugplatz nicht gefunden")
    if row["tenant_id"] != user["tenant_id"]:
        raise HTTPException(status_code=403, detail="Kein Zugriff auf diesen Flugplatz")
    return row


async def _owned_airfield_id(db, slug: str, user: dict) -> UUID:
    """Airfield id after the ownership check of ``_owned_airfield``."""
    return (await _owned_airfield(db, slug, user))["id"]


def _alarm_kind_from_status(raw_status: str | None) -> str:
    """Map a hot-state ``status`` value to an alarm_kind ("other" if unknown)."""
    try:
        code = int(raw_status or 0)
    except ValueError:
        return "other"
    return _STATUS_TO_ALARM_KIND.get(code, "other")


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 UTC timestamp from the hot state (``""`` -> None)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_day_bounds(day: date_type | None) -> tuple[datetime, datetime]:
    """[start, end) of a UTC day as aware datetimes (default: today)."""
    if day is None:
        day = datetime.now(timezone.utc).date()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _action_item(row) -> AlarmActionItem:
    return AlarmActionItem(
        id=row["id"],
        state=row["state"],
        comment=row["comment"],
        alarm_kind=row["alarm_kind"],
        set_by=row["set_by"],
        created_at=_iso(row["created_at"]),
        flight_takeoff_ts=_iso(row["flight_takeoff_ts"]) or None,
    )


def _iso(ts) -> str:
    if ts is None:
        return ""
    if isinstance(ts, datetime) and ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc)
    if hasattr(ts, "strftime"):
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ts)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_camel_case(data: dict) -> dict:
    """Convert snake_case dict keys to camelCase."""
    result = {}
    for key, val in data.items():
        parts = key.split("_")
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        result[camel] = val
    return result
