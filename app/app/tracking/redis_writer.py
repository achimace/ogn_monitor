"""Redis Hot State writer for live flight data.

Writes flight state to Redis using HSET and publishes events via PubSub.
This is the bridge between the Worker's flight logic and the API Server.

Redis Key Schema:
  flight:{airfield_slug}:{flarm_id}  -> Hash with all flight fields
  flights:{airfield_slug}            -> Set of active FLARM IDs
  positions:{airfield_slug}          -> Stream of position updates
  track:{airfield_slug}:{flarm_id}   -> Stream of thinned track points (id = beacon ms)
  ogn:health                         -> Hash with OGN connection status
  event:{airfield_slug}              -> PubSub channel for status change events
  beacon:{airfield_slug}             -> PubSub channel for beacon updates
"""

import json
from typing import Any

import redis.asyncio as aioredis
import structlog
from redis.exceptions import ResponseError

from app.config import settings

log = structlog.get_logger()

# Optional field in the flight hash marking a synthetic flight written by
# ``app.vfsync.simulate``. The API shows such flights like real ones; the
# APRS worker's recovery skips them so they never reach the state machine,
# flight_status or flight_log (the entry expires via its TTL).
SIMULATED_FIELD = "simulated"
SIMULATED_VALUE = "1"

# Redis' error text for XADD with an explicit id that is not greater than
# the stream's current top id (out-of-order beacon). Only this case is
# swallowed in ``add_track_point``.
XADD_OUT_OF_ORDER_MSG = "equal or smaller than the target stream top item"


class RedisWriter:
    """Writes flight state to Redis Hot State."""

    def __init__(self, redis: aioredis.Redis):
        self._redis = redis
        self._ttl = settings.redis_hot_state_ttl

    async def update_flight(self, airfield_slug: str, flarm_id: str,
                            data: dict[str, Any], ttl: int | None = None) -> None:
        """Write flight state hash and update active flights set.

        Args:
            airfield_slug: Airfield identifier.
            flarm_id: FLARM device ID.
            data: Dict of field->value pairs for HSET.
            ttl: Optional override (seconds). If not given, the default
                 hot-state TTL is used. Sticky-landed flights pass a longer
                 value to keep the entry alive while no beacons arrive.
        """
        key = f"flight:{airfield_slug}:{flarm_id}"

        # Convert all values to strings for Redis
        str_data = {k: str(v) for k, v in data.items()}

        pipe = self._redis.pipeline()
        pipe.hset(key, mapping=str_data)
        pipe.expire(key, ttl if ttl is not None else self._ttl)
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

    async def add_track_point(self, airfield_slug: str, flarm_id: str, ts_ms: int,
                              lat: float, lon: float, alt_m: float, alt_agl_m: float,
                              speed_kmh: float, vs_ms: float, track_deg: float,
                              retention_s: int, min_interval_s: int) -> None:
        """Append a point to the per-aircraft track stream.

        The stream id is the beacon time (``{ts_ms}-*``) so the API can
        XRANGE by time. Every write refreshes the sliding TTL; MAXLEN is
        derived from retention / thinning interval as a memory guard.

        Out-of-order beacons (ts_ms not greater than the last entry) make
        XADD fail with a ResponseError; only that case is skipped, any
        other Redis error propagates.

        Args:
            airfield_slug: Airfield identifier.
            flarm_id: FLARM device ID.
            ts_ms: Beacon time as Unix epoch milliseconds.
            lat, lon: WGS84 position.
            alt_m: Altitude MSL (m).
            alt_agl_m: Altitude above ground (m): terrain elevation under
                the aircraft when known, otherwise the airfield elevation.
            speed_kmh: Ground speed (km/h).
            vs_ms: Vertical speed (m/s).
            track_deg: Course (degrees).
            retention_s: Sliding TTL of the stream (seconds).
            min_interval_s: Thinning interval (seconds); with retention_s
                it bounds the number of entries (MAXLEN).
        """
        key = f"track:{airfield_slug}:{flarm_id}"
        maxlen = max(1, retention_s // max(1, min_interval_s))

        pipe = self._redis.pipeline()
        pipe.xadd(
            key,
            {
                "lat": str(round(lat, 5)),
                "lon": str(round(lon, 5)),
                "alt": str(round(alt_m)),
                "agl": str(round(alt_agl_m)),
                "speed": str(round(speed_kmh)),
                "vs": str(round(vs_ms, 1)),
                "track": str(round(track_deg)),
            },
            id=f"{ts_ms}-*",
            maxlen=maxlen,
            approximate=True,
        )
        pipe.expire(key, retention_s)
        try:
            await pipe.execute()
        except ResponseError as exc:
            if XADD_OUT_OF_ORDER_MSG not in str(exc):
                raise
            log.debug(
                "track_point_skipped",
                airfield=airfield_slug,
                flarm_id=flarm_id,
                ts_ms=ts_ms,
                error=str(exc),
            )

    async def remove_flight(self, airfield_slug: str, flarm_id: str) -> None:
        """Remove a flight from the active set (after landing/archive)."""
        pipe = self._redis.pipeline()
        pipe.srem(f"flights:{airfield_slug}", flarm_id)
        pipe.delete(f"flight:{airfield_slug}:{flarm_id}")
        await pipe.execute()

    async def delete_track(self, airfield_slug: str, flarm_id: str) -> None:
        """Drop the per-aircraft track stream (DDB tracked=N eviction).

        Not called on a normal archive: the 24 h track deliberately
        outlives the flight for the monitor map.
        """
        await self._redis.delete(f"track:{airfield_slug}:{flarm_id}")

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
