"""Aircraft Resolver - In-memory FLARM-ID to aircraft info cache.

Provides O(1) lookups for FLARM-ID -> registration, model, competition sign.
Data sources (priority order):
1. Tenant-specific aircraft (highest priority)
2. OGN Device Database (DDB)
3. FlarmNet database
4. APRS beacon 'reg' field (lowest priority)

The cache is loaded at startup and reloaded hourly after DDB sync.
"""

import asyncio
import time
from dataclasses import dataclass

import structlog

from app.db.connection import get_db

log = structlog.get_logger()

# Cache settings
NEGATIVE_TTL_S = 300              # 5 minutes: re-check unknown IDs
CACHE_RELOAD_INTERVAL_S = 3600   # 1 hour: full cache reload


@dataclass(slots=True)
class AircraftInfo:
    """Resolved aircraft information."""
    flarm_id: str
    registration: str
    aircraft_model: str
    competition_sign: str
    aircraft_type: int       # 1=Glider, 2=TowPlane, 3=Helicopter, etc.
    source: str              # "tenant" / "ogn_ddb" / "flarmnet" / "aprs"
    tracked: bool
    identified: bool


class AircraftResolver:
    """In-memory aircraft lookup cache with O(1) performance."""

    def __init__(self):
        self._cache: dict[str, AircraftInfo] = {}
        self._negative_cache: dict[str, float] = {}  # flarm_id -> timestamp
        self._loaded = False

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def resolve(self, flarm_id: str) -> AircraftInfo | None:
        """Look up aircraft info by FLARM ID. O(1), no I/O.

        Args:
            flarm_id: 6-char hex FLARM/OGN device ID (uppercase).

        Returns:
            AircraftInfo if known, None if unknown.
        """
        info = self._cache.get(flarm_id)
        if info:
            return info

        # Check negative cache to avoid repeated DB queries
        neg_time = self._negative_cache.get(flarm_id)
        if neg_time and (time.monotonic() - neg_time) < NEGATIVE_TTL_S:
            return None

        return None

    def update_from_aprs(self, flarm_id: str, registration: str) -> None:
        """Update cache with registration from APRS beacon (lowest priority).

        Only adds if not already known from a better source.
        """
        if flarm_id in self._cache:
            return
        if not registration:
            return

        self._cache[flarm_id] = AircraftInfo(
            flarm_id=flarm_id,
            registration=registration,
            aircraft_model="",
            competition_sign="",
            aircraft_type=0,
            source="aprs",
            tracked=True,
            identified=True,
        )

    def mark_unknown(self, flarm_id: str) -> None:
        """Mark a FLARM ID as unknown (negative cache)."""
        self._negative_cache[flarm_id] = time.monotonic()

    async def load(self) -> None:
        """Load the full cache from database.

        Loads global aircraft_registry first, then tenant overrides.
        """
        db = get_db()
        cache: dict[str, AircraftInfo] = {}

        # 1. Global registry (OGN DDB + FlarmNet)
        rows = await db.fetch(
            "SELECT device_id, registration, aircraft_model, "
            "competition_sign, device_type, source, tracked, identified "
            "FROM aircraft_registry"
        )
        for row in rows:
            fid = row["device_id"].upper()
            cache[fid] = AircraftInfo(
                flarm_id=fid,
                registration=row["registration"] or "",
                aircraft_model=row["aircraft_model"] or "",
                competition_sign=row["competition_sign"] or "",
                aircraft_type=_parse_device_type(row["device_type"]),
                source=row["source"] or "ogn_ddb",
                tracked=row["tracked"],
                identified=row["identified"],
            )

        # 2. Tenant-specific aircraft (override global entries)
        rows = await db.fetch(
            "SELECT flarm_id, registration, aircraft_model, "
            "competition_sign, aircraft_type "
            "FROM tenant_aircraft WHERE is_active = TRUE"
        )
        for row in rows:
            fid = row["flarm_id"].upper()
            cache[fid] = AircraftInfo(
                flarm_id=fid,
                registration=row["registration"] or "",
                aircraft_model=row["aircraft_model"] or "",
                competition_sign=row["competition_sign"] or "",
                aircraft_type=_parse_aircraft_type_str(row["aircraft_type"]),
                source="tenant",
                tracked=True,
                identified=True,
            )

        self._cache = cache
        self._negative_cache.clear()
        self._loaded = True

        log.info(
            "aircraft_cache_loaded",
            total=len(cache),
            global_entries=sum(1 for v in cache.values() if v.source != "tenant"),
            tenant_entries=sum(1 for v in cache.values() if v.source == "tenant"),
        )

    async def periodic_reload(self, shutdown_event: asyncio.Event) -> None:
        """Reload cache periodically (runs as background task)."""
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=CACHE_RELOAD_INTERVAL_S,
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass

            try:
                await self.load()
                log.info("aircraft_cache_reloaded", size=self.cache_size)
            except Exception:
                log.exception("aircraft_cache_reload_failed")


def _parse_device_type(dt: str | None) -> int:
    """Convert DDB device type char to integer."""
    if not dt:
        return 0
    mapping = {"F": 1, "I": 2, "O": 3}
    return mapping.get(dt, 0)


def _parse_aircraft_type_str(at: str | None) -> int:
    """Convert tenant aircraft type string to integer."""
    if not at:
        return 0
    mapping = {
        "glider": 1, "tow_plane": 2, "motor_glider": 8,
        "tmg": 8, "helicopter": 3,
    }
    return mapping.get(at.lower(), 0)
