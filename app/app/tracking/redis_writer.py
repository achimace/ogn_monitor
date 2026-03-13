"""Redis Hot State writer for live flight data.

Writes flight state to Redis using HSET and publishes events via PubSub.
This is the bridge between the Worker's flight logic and the API Server.

Redis Key Schema:
  flight:{airfield_slug}:{flarm_id}  -> Hash with all flight fields
  flights:{airfield_slug}            -> Set of active FLARM IDs
  positions:{airfield_slug}          -> Stream of position updates
  ogn:health                         -> Hash with OGN connection status
  event:{airfield_slug}              -> PubSub channel for status change events
  beacon:{airfield_slug}             -> PubSub channel for beacon updates
"""

import json
from typing import Any

import redis.asyncio as aioredis
import structlog

from app.config import settings

log = structlog.get_logger()


class RedisWriter:
    """Writes flight state to Redis Hot State."""

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis
        self._ttl = settings.redis_hot_state_ttl

    async def update_flight(self, airfield_slug: str, flarm_id: str, data: dict[str, Any]) -> None:
        """Write flight state hash and update active flights set.

        Args:
            airfield_slug: Airfield identifier.
            flarm_id: FLARM device ID.
            data: Dict of field->value pairs for HSET.
        """
        key = f"flight:{airfield_slug}:{flarm_id}"

        # Convert all values to strings for Redis
        str_data = {k: str(v) for k, v in data.items()}

        pipe = self._redis.pipeline()
        pipe.hset(key, mapping=str_data)
        pipe.expire(key, self._ttl)
        pipe.sadd(f"flights:{airfield_slug}", flarm_id)
        pipe.sadd("active_airfields", airfield_slug)
        await pipe.execute()

    async def publish_beacon(self, airfield_slug: str, flarm_id: str, data: dict[str, Any]) -> None:
        """Publish beacon update notification via PubSub.

        The API server subscribes to this channel for WebSocket broadcast.
        """
        message = json.dumps({
            "type": "beacon",
            "flarm_id": flarm_id,
            "data": data,
        })
        await self._redis.publish(f"beacon:{airfield_slug}", message)

    async def publish_event(self, airfield_slug: str, event_type: str, flarm_id: str,
                            data: dict[str, Any] | None = None, message: str = "") -> None:
        """Publish flight event (takeoff, landing, alarm, etc.) via PubSub.

        Args:
            airfield_slug: Airfield identifier.
            event_type: Event type (takeoff, landing, alarm, outlanding, emergency, etc.).
            flarm_id: FLARM device ID.
            data: Additional event data.
            message: Human-readable event description.
        """
        event = {
            "type": event_type,
            "flarm_id": flarm_id,
            "message": message,
        }
        if data:
            event["data"] = {k: str(v) for k, v in data.items()}

        await self._redis.publish(
            f"event:{airfield_slug}",
            json.dumps(event),
        )

    async def add_position(self, airfield_slug: str, flarm_id: str,
                           lat: float, lon: float, alt: float,
                           speed: float, vs: float, track: float) -> None:
        """Add position to the position stream (for flight tracks).

        Uses Redis Streams with MAXLEN to limit memory usage.
        """
        await self._redis.xadd(
            f"positions:{airfield_slug}",
            {
                "flarm_id": flarm_id,
                "lat": str(round(lat, 5)),
                "lon": str(round(lon, 5)),
                "alt": str(round(alt)),
                "speed": str(round(speed)),
                "vs": str(round(vs, 1)),
                "track": str(round(track)),
            },
            maxlen=5000,
            approximate=True,
        )

    async def remove_flight(self, airfield_slug: str, flarm_id: str) -> None:
        """Remove a flight from the active set (after landing/archive)."""
        pipe = self._redis.pipeline()
        pipe.srem(f"flights:{airfield_slug}", flarm_id)
        pipe.delete(f"flight:{airfield_slug}:{flarm_id}")
        await pipe.execute()

    async def update_health(self, health: dict[str, Any]) -> None:
        """Update OGN connection health status."""
        str_health = {k: str(v) for k, v in health.items()}
        await self._redis.hset("ogn:health", mapping=str_health)

    async def get_active_flights(self, airfield_slug: str) -> set[str]:
        """Get set of active FLARM IDs for an airfield."""
        return await self._redis.smembers(f"flights:{airfield_slug}")

    async def get_flight(self, airfield_slug: str, flarm_id: str) -> dict[str, str] | None:
        """Get flight state hash from Redis."""
        data = await self._redis.hgetall(f"flight:{airfield_slug}:{flarm_id}")
        return data if data else None
