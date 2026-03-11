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
