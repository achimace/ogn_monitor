"""WebSocket endpoint for real-time flight monitoring.

Endpoint: ws://host/ws/monitor/{slug}

No authentication required (public monitor).
Clients receive:
- full_state on connect
- flight_update (delta) on each beacon
- flight_added / flight_removed on status changes
- alarm on emergencies
- ping/pong heartbeat every 15s
"""

import asyncio

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.connection_manager import manager, HEARTBEAT_INTERVAL
from app.redis_client import get_redis

log = structlog.get_logger()

router = APIRouter()


@router.websocket("/ws/monitor/{slug}")
async def monitor_websocket(websocket: WebSocket, slug: str):
    """WebSocket endpoint for real-time flight monitoring of an airfield.

    Args:
        slug: Airfield slug (e.g. "ohlstadt").
    """
    # Validate airfield exists (check if slug is in Redis active_airfields)
    redis = get_redis()
    is_active = await redis.sismember("active_airfields", slug)

    # Also check if there's at least flight data or the slug is known
    # Allow connection even if no flights yet (new airfield)
    if not is_active:
        # Check database as fallback
        from app.db.connection import get_db
        db = get_db()
        row = await db.fetchrow(
            "SELECT slug FROM airfields WHERE slug = $1 AND is_active = TRUE",
            slug,
        )
        if not row:
            await websocket.close(code=4004, reason="Airfield not found")
            return

    await manager.connect(slug, websocket)

    # Start heartbeat task
    heartbeat_task = asyncio.create_task(
        _heartbeat_loop(websocket, HEARTBEAT_INTERVAL),
    )

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "pong":
                # Heartbeat response - connection is alive
                pass
            else:
                log.debug("ws_unknown_message", type=msg_type)

    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("ws_connection_error", airfield=slug)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        manager.disconnect(slug, websocket)


async def _heartbeat_loop(websocket: WebSocket, interval: int) -> None:
    """Send periodic ping messages to keep connection alive."""
    from datetime import datetime, timezone

    while True:
        await asyncio.sleep(interval)
        try:
            await websocket.send_json({
                "type": "ping",
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
        except Exception:
            break
