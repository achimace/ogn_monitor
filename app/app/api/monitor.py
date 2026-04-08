"""REST Monitor endpoint - fallback for clients without WebSocket.

GET /api/monitor/{slug} returns the current flight state from Redis.
No authentication required (public monitor).
"""

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, HTTPException

from app.redis_client import get_redis

log = structlog.get_logger()

router = APIRouter(prefix="/api/monitor", tags=["Monitor"])


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
        "SELECT id, slug, latitude, longitude, elevation_m "
        "FROM airfields WHERE slug = $1 AND is_active = TRUE",
        slug,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Airfield not found")
    airfield_id = row["id"]

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

    # 3. Today's archived flights from PostgreSQL (UTC day)
    archived = await db.fetch(
        """
        SELECT flarm_id, registration, competition_sign, aircraft_model,
               takeoff_time, landing_time, flight_duration_s,
               max_altitude_m, max_distance_m, launch_type, landing_type
        FROM flight_log
        WHERE airfield_id = $1
          AND takeoff_time >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
        ORDER BY takeoff_time DESC
        """,
        airfield_id,
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
    }


def _iso(ts) -> str:
    if ts is None:
        return ""
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
