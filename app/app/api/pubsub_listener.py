"""Redis PubSub Listener for API Server.

Subscribes to beacon and event channels published by the APRS Worker.
Routes messages to the WebSocket ConnectionManager for broadcast to clients.

Each API worker process runs its own PubSub listener.
"""

import asyncio
import json

import structlog

from app.redis_client import get_redis
from app.api.connection_manager import manager

log = structlog.get_logger()


async def start_pubsub_listener() -> None:
    """Start listening for Redis PubSub messages from the APRS Worker.

    Subscribes to:
    - beacon:{airfield_slug} - Position updates (every ~3s per flight)
    - event:{airfield_slug} - Status changes (takeoff, landing, alarm, etc.)

    This runs as a background task in each API worker process.
    """
    redis = get_redis()
    pubsub = redis.pubsub()

    try:
        await pubsub.psubscribe("beacon:*", "event:*")
        log.info("pubsub_listener_started", patterns=["beacon:*", "event:*"])

        async for message in pubsub.listen():
            if message["type"] not in ("pmessage",):
                continue

            try:
                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8")

                data_raw = message["data"]
                if isinstance(data_raw, bytes):
                    data_raw = data_raw.decode("utf-8")

                data = json.loads(data_raw)

                # Extract airfield slug from channel name
                # Format: "beacon:ohlstadt" or "event:ohlstadt"
                parts = channel.split(":", 1)
                if len(parts) != 2:
                    continue

                channel_type = parts[0]
                airfield_slug = parts[1]

                if channel_type == "beacon":
                    # Position update - broadcast delta
                    flarm_id = data.get("flarm_id", "")
                    beacon_data = data.get("data", {})
                    if flarm_id and beacon_data:
                        await manager.broadcast_beacon(
                            airfield_slug, flarm_id, beacon_data
                        )

                elif channel_type == "event":
                    # Status change event - broadcast specialized message
                    await manager.broadcast_event(airfield_slug, data)

            except json.JSONDecodeError:
                log.debug("pubsub_invalid_json", channel=channel)
            except Exception:
                log.exception("pubsub_message_error")

    except asyncio.CancelledError:
        log.info("pubsub_listener_stopping")
    except Exception:
        log.exception("pubsub_listener_crashed")
    finally:
        await pubsub.punsubscribe("beacon:*", "event:*")
        await pubsub.close()
