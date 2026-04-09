"""WebSocket Connection Manager for real-time flight monitoring.

Manages per-airfield WebSocket connections and broadcasts flight updates.
Calculates deltas (only changed fields) to minimize bandwidth.

Each API worker has its own ConnectionManager instance.
Redis PubSub ensures all workers receive updates from the single APRS Worker.
"""

import asyncio
import json
from datetime import datetime, timezone

import structlog
from starlette.websockets import WebSocket, WebSocketState

from app.redis_client import get_redis

log = structlog.get_logger()

# Heartbeat settings
HEARTBEAT_INTERVAL = 15   # Send ping every 15s
HEARTBEAT_TIMEOUT = 30    # Close if no pong within 30s


class MonitorConnectionManager:
    """Manages WebSocket connections grouped by airfield slug."""

    def __init__(self):
        # airfield_slug -> list of WebSocket connections
        self._connections: dict[str, list[WebSocket]] = {}
        # Per-client previous state for delta calculation
        # ws_id -> { flarm_id -> { field: value } }
        self._client_state: dict[int, dict[str, dict]] = {}

    @property
    def total_connections(self) -> int:
        return sum(len(conns) for conns in self._connections.values())

    async def connect(self, slug: str, websocket: WebSocket) -> None:
        """Accept a new WebSocket connection and send full state."""
        await websocket.accept()

        if slug not in self._connections:
            self._connections[slug] = []
        self._connections[slug].append(websocket)
        self._client_state[id(websocket)] = {}

        log.info(
            "ws_client_connected",
            airfield=slug,
            clients=len(self._connections[slug]),
        )

        # Send initial full state
        await self._send_full_state(slug, websocket)

    def disconnect(self, slug: str, websocket: WebSocket) -> None:
        """Remove a WebSocket connection."""
        conns = self._connections.get(slug, [])
        if websocket in conns:
            conns.remove(websocket)
        self._client_state.pop(id(websocket), None)

        log.info(
            "ws_client_disconnected",
            airfield=slug,
            clients=len(conns),
        )

    async def broadcast_beacon(self, slug: str, flarm_id: str, data: dict) -> None:
        """Broadcast a beacon update as delta to all clients of an airfield.

        Args:
            slug: Airfield slug.
            flarm_id: FLARM ID that was updated.
            data: Current flight data dict from Redis PubSub message.
        """
        conns = self._connections.get(slug, [])
        if not conns:
            return

        # Build per-client delta messages
        disconnected = []
        for ws in conns:
            ws_id = id(ws)
            prev_state = self._client_state.get(ws_id, {}).get(flarm_id, {})

            # Calculate delta
            delta = {}
            for key, val in data.items():
                if prev_state.get(key) != val:
                    delta[key] = val

            if not delta:
                continue

            # Update stored state
            if ws_id not in self._client_state:
                self._client_state[ws_id] = {}
            if flarm_id not in self._client_state[ws_id]:
                self._client_state[ws_id][flarm_id] = {}
            self._client_state[ws_id][flarm_id].update(data)

            message = {
                "type": "flight_update",
                "flarmId": flarm_id,
                "ts": _utcnow_iso(),
                "d": _to_camel_case(delta),
            }

            if not await self._send_json(ws, message):
                disconnected.append(ws)

        for ws in disconnected:
            self.disconnect(slug, ws)

    async def broadcast_event(self, slug: str, event: dict) -> None:
        """Broadcast an event (takeoff, landing, alarm, etc.) to all clients."""
        conns = self._connections.get(slug, [])
        if not conns:
            return

        event_type = event.get("type", "")
        flarm_id = event.get("flarm_id", "")
        flight_data = event.get("data", {})
        message_text = event.get("message", "")

        # Build appropriate WebSocket message
        if event_type == "takeoff":
            ws_msg = {
                "type": "flight_added",
                "flight": _to_camel_case(flight_data),
            }
        elif event_type == "landing":
            # Sticky landed: the flight stays in the hot state and only
            # transitions to status LANDING. Push a regular update so the
            # client moves it to the "Gelandet" section without removing
            # it. Removal happens later via sticky_landed_expired,
            # flight_restarted or an explicit dismiss.
            ws_msg = {
                "type": "flight_update",
                "flarmId": flarm_id,
                "ts": _utcnow_iso(),
                "d": _to_camel_case(flight_data),
            }
        elif event_type in ("sticky_landed_expired", "flight_restarted"):
            ws_msg = {
                "type": "flight_removed",
                "flarmId": flarm_id,
                "reason": event_type,
                "summary": _build_summary(flight_data),
            }
        elif event_type == "dismissed":
            ws_msg = {
                "type": "flight_removed",
                "flarmId": flarm_id,
                "reason": "dismissed",
                "summary": _build_summary(flight_data),
            }
        elif event_type in ("alarm", "emergency", "outlanding", "diverted", "signal_lost"):
            ws_msg = self._build_alarm_message(event_type, flarm_id, flight_data, message_text)
        elif event_type == "signal_recovered":
            ws_msg = {
                "type": "flight_update",
                "flarmId": flarm_id,
                "ts": _utcnow_iso(),
                "d": _to_camel_case(flight_data),
            }
        else:
            ws_msg = {
                "type": "event",
                "eventType": event_type,
                "flarmId": flarm_id,
                "message": message_text,
            }

        # Broadcast to all clients
        disconnected = []
        for ws in conns:
            if not await self._send_json(ws, ws_msg):
                disconnected.append(ws)

        # Update client state on flight_added
        if event_type == "takeoff" and flight_data:
            for ws in conns:
                ws_id = id(ws)
                if ws_id in self._client_state:
                    self._client_state[ws_id][flarm_id] = dict(flight_data)

        # Keep client state in sync after a sticky landing so subsequent
        # delta diffs are computed against the LANDING snapshot.
        if event_type == "landing" and flight_data:
            for ws in conns:
                ws_id = id(ws)
                if ws_id in self._client_state:
                    self._client_state[ws_id][flarm_id] = dict(flight_data)

        # Remove flight from client state on real removal events
        if event_type in ("dismissed", "sticky_landed_expired", "flight_restarted"):
            for ws in conns:
                ws_id = id(ws)
                if ws_id in self._client_state:
                    self._client_state[ws_id].pop(flarm_id, None)

        for ws in disconnected:
            self.disconnect(slug, ws)

    async def _send_full_state(self, slug: str, websocket: WebSocket) -> None:
        """Send complete flight list to a single client."""
        redis = get_redis()

        flarm_ids = await redis.smembers(f"flights:{slug}")
        flights = []

        if flarm_ids:
            pipe = redis.pipeline()
            for fid in flarm_ids:
                pipe.hgetall(f"flight:{slug}:{fid}")
            results = await pipe.execute()

            for data in results:
                if data:
                    flights.append(_to_camel_case(data))

        # Cache state for this client
        ws_id = id(websocket)
        self._client_state[ws_id] = {}
        for flight in flights:
            fid = flight.get("flarmId", "")
            if fid:
                # Store original snake_case for delta comparison
                self._client_state[ws_id][fid] = {}

        # Count by status
        stats = _count_stats(flights)

        message = {
            "type": "full_state",
            "airfield": slug,
            "timestamp": _utcnow_iso(),
            "flights": flights,
            "stats": stats,
        }
        await self._send_json(websocket, message)

    def _build_alarm_message(self, event_type: str, flarm_id: str,
                             data: dict, message: str) -> dict:
        """Build an alarm WebSocket message."""
        severity_map = {
            "emergency": "CRITICAL",
            "alarm": "HIGH",
            "outlanding": "MEDIUM",
            "diverted": "LOW",
            "signal_lost": "LOW",
        }

        return {
            "type": "alarm",
            "severity": severity_map.get(event_type, "HIGH"),
            "flarmId": flarm_id,
            "registration": data.get("registration", ""),
            "competitionSign": data.get("competition_sign", ""),
            "aircraftModel": data.get("aircraft_model", ""),
            "scenario": event_type.upper(),
            "message": message,
            "lastPosition": {
                "latitude": float(data.get("latitude", 0)),
                "longitude": float(data.get("longitude", 0)),
                "altitudeM": int(float(data.get("altitude_m", 0))),
                "qdrDeg": int(float(data.get("qdr_deg", 0))),
                "bearingText": data.get("bearing_text", ""),
                "distanceKm": round(float(data.get("distance_m", 0)) / 1000, 1),
                "lastSeen": data.get("last_seen", ""),
            },
            "timestamp": _utcnow_iso(),
        }

    async def _send_json(self, websocket: WebSocket, data: dict) -> bool:
        """Send JSON to a WebSocket, return False if failed."""
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json(data)
                return True
        except Exception:
            pass
        return False


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_camel_case(data: dict) -> dict:
    """Convert snake_case dict keys to camelCase for frontend."""
    result = {}
    for key, val in data.items():
        parts = key.split("_")
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        result[camel] = val
    return result


def _build_summary(data: dict) -> dict:
    return {
        "landingTime": data.get("landing_time", ""),
        "maxAltitudeM": data.get("max_altitude_m", ""),
        "maxDistanceM": data.get("max_distance_m", ""),
        "launchType": data.get("launch_type", ""),
    }


def _count_stats(flights: list[dict]) -> dict:
    stats = {"flying": 0, "landed": 0, "alarm": 0, "outlanding": 0}
    for f in flights:
        status = int(f.get("status", 0))
        if status == 2:
            stats["flying"] += 1
        elif status == 3:
            stats["landed"] += 1
        elif status == 5:
            stats["alarm"] += 1
        elif status in (4, 7):
            stats["outlanding"] += 1
    return stats


# Global instance (per API worker process)
manager = MonitorConnectionManager()
