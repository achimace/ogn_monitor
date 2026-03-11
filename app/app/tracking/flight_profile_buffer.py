"""Flight Profile Ring Buffer.

Stores the last ~10 minutes of beacon data per flight for profile analysis.
Used to classify flight-end scenarios when signal is lost (Step 19).

Each flight has a deque of beacon snapshots. Old entries are pruned
automatically. Data stays in memory (Worker process only).
"""

import time
from collections import deque
from dataclasses import dataclass

MAX_AGE_S = 600  # 10 minutes
MAX_POINTS = 200  # ~1 beacon per 3-5s


@dataclass(slots=True)
class ProfilePoint:
    """Single point in the flight profile buffer."""
    timestamp: float  # Unix timestamp
    lat: float
    lon: float
    alt: float        # MSL meters
    speed: float      # km/h
    vs: float         # m/s
    track: float      # degrees


class FlightProfileBuffer:
    """Ring buffer of recent beacon data per flight."""

    def __init__(self):
        self._buffers: dict[str, deque[ProfilePoint]] = {}

    def add(self, flarm_id: str, timestamp: float, lat: float, lon: float,
            alt: float, speed: float, vs: float, track: float) -> None:
        """Add a beacon snapshot to the flight's profile buffer."""
        if flarm_id not in self._buffers:
            self._buffers[flarm_id] = deque(maxlen=MAX_POINTS)

        self._buffers[flarm_id].append(ProfilePoint(
            timestamp=timestamp,
            lat=lat,
            lon=lon,
            alt=alt,
            speed=speed,
            vs=vs,
            track=track,
        ))

        self._prune(flarm_id)

    def get(self, flarm_id: str) -> list[ProfilePoint]:
        """Get all current profile points for a flight."""
        buf = self._buffers.get(flarm_id)
        if not buf:
            return []
        return list(buf)

    def remove(self, flarm_id: str) -> None:
        """Remove a flight's buffer (after landing/archive)."""
        self._buffers.pop(flarm_id, None)

    def _prune(self, flarm_id: str) -> None:
        """Remove entries older than MAX_AGE_S."""
        buf = self._buffers.get(flarm_id)
        if not buf:
            return

        cutoff = time.time() - MAX_AGE_S
        while buf and buf[0].timestamp < cutoff:
            buf.popleft()

    def clear(self) -> None:
        """Clear all buffers."""
        self._buffers.clear()
