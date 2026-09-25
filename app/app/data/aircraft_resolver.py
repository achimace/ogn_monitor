"""Aircraft Resolver - In-memory FLARM-ID to aircraft info cache.

Provides O(1) lookups for FLARM-ID -> registration, model, competition sign.
Data sources (priority order):
1. Tenant-specific aircraft (highest priority)
2. OGN Device Database (DDB)
3. FlarmNet database
4. APRS beacon 'reg' field (lowest priority)

The cache is loaded at startup and reloaded hourly after DDB sync.

OGN DDB privacy flags (ODbL condition: "you must follow DDB tracking
privacy choices", see docs/dev-guides/implement-flight-logic.md):

* ``tracked = N``: the device owner opted out of tracking. The flag is
  passed through as ``AircraftInfo.tracked = False`` and the FlightTracker
  drops every beacon of that device. It wins over a tenant_aircraft entry.
* ``identified = N``: the device may be tracked but not identified. The
  DDB registration / competition sign are blanked at load time, so no
  consumer can leak them, and ``update_from_aprs`` never fills them in
  again. The only exception is a tenant_aircraft entry for that FLARM-ID:
  the operator entered its own fleet, which is explicit consent, so the
  tenant registration is used (``identified = True``, ``source = tenant``).
"""

import asyncio
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import structlog

from app.db.connection import get_db

log = structlog.get_logger()

# Cache settings
NEGATIVE_TTL_S = 300              # 5 minutes: re-check unknown IDs
CACHE_RELOAD_INTERVAL_S = 3600   # 1 hour: full cache reload


@dataclass(slots=True)
class AircraftInfo:
    """Resolved aircraft information.

    ``tracked`` / ``identified`` carry the OGN DDB privacy choices (see
    module docstring). For an unidentified device ``registration`` and
    ``competition_sign`` are always empty unless ``source == "tenant"``.
    """
    flarm_id: str
    registration: str
    aircraft_model: str
    competition_sign: str
    aircraft_type: int       # 1=Glider, 2=TowPlane, 3=Helicopter, etc.
    source: str              # "tenant" / "ogn_ddb" / "flarmnet" / "aprs"
    tracked: bool            # False = DDB opt-out, drop all beacons
    identified: bool         # False = show the FLARM-ID only (no reg / CN)
    # Launch-detection role: towplane / glider / motorglider_sl / powered / ""
    role: str = ""


def build_cache(registry_rows: Iterable[Mapping], tenant_rows: Iterable[Mapping],
                ) -> dict[str, AircraftInfo]:
    """Merge aircraft_registry and tenant_aircraft rows into the lookup cache.

    Pure function (no I/O) so the DDB privacy rules can be unit-tested:

    * registry ``tracked``/``identified`` are honoured; a NULL counts as
      TRUE (the DDB default).
    * ``identified = FALSE`` blanks registration and competition sign.
    * a tenant row overrides the registry data (explicit consent for the
      tenant's own fleet) but inherits ``tracked``: the DDB opt-out wins.

    Args:
        registry_rows: rows with device_id, registration, aircraft_model,
            competition_sign, device_type, source, tracked, identified.
        tenant_rows: rows with flarm_id, registration, aircraft_model,
            competition_sign, aircraft_type, role.

    Returns:
        flarm_id (uppercase) -> AircraftInfo.
    """
    cache: dict[str, AircraftInfo] = {}

    # 1. Global registry (OGN DDB + FlarmNet)
    for row in registry_rows:
        fid = row["device_id"].upper()
        tracked = row["tracked"] is not False
        identified = row["identified"] is not False
        cache[fid] = AircraftInfo(
            flarm_id=fid,
            registration=(row["registration"] or "") if identified else "",
            aircraft_model=row["aircraft_model"] or "",
            competition_sign=(row["competition_sign"] or "") if identified else "",
            aircraft_type=_parse_device_type(row["device_type"]),
            source=row["source"] or "ogn_ddb",
            tracked=tracked,
            identified=identified,
        )

    # 2. Tenant-specific aircraft (override global entries, keep the
    #    DDB tracked flag: the device owner's opt-out is stronger than the
    #    operator's fleet list)
    for row in tenant_rows:
        fid = row["flarm_id"].upper()
        prev = cache.get(fid)
        cache[fid] = AircraftInfo(
            flarm_id=fid,
            registration=row["registration"] or "",
            aircraft_model=row["aircraft_model"] or "",
            competition_sign=row["competition_sign"] or "",
            aircraft_type=_parse_aircraft_type_str(row["aircraft_type"]),
            source="tenant",
            tracked=prev.tracked if prev is not None else True,
            identified=True,
            role=role_from_row(row["role"], row["aircraft_type"]),
        )

    return cache


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

        Only adds if not already known from a better source. A DDB entry
        with ``identified = N`` is such a source (with blank registration):
        it stays as it is, so the APRS ``reg`` field can never re-identify
        a device whose owner opted out of identification.
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

        registry_rows = await db.fetch(
            "SELECT device_id, registration, aircraft_model, "
            "competition_sign, device_type, source, tracked, identified "
            "FROM aircraft_registry"
        )
        tenant_rows = await db.fetch(
            "SELECT flarm_id, registration, aircraft_model, "
            "competition_sign, aircraft_type, role "
            "FROM tenant_aircraft WHERE is_active = TRUE"
        )
        cache = build_cache(registry_rows, tenant_rows)

        self._cache = cache
        self._negative_cache.clear()
        self._loaded = True

        log.info(
            "aircraft_cache_loaded",
            total=len(cache),
            global_entries=sum(1 for v in cache.values() if v.source != "tenant"),
            tenant_entries=sum(1 for v in cache.values() if v.source == "tenant"),
            untracked=sum(1 for v in cache.values() if not v.tracked),
            unidentified=sum(1 for v in cache.values() if not v.identified),
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


# Launch-detection roles, see app.tracking.launch_detector
VALID_ROLES = {"towplane", "glider", "motorglider_sl", "powered"}

# Fallback: derive the role from the legacy tenant aircraft_type
_ROLE_FROM_AIRCRAFT_TYPE = {
    "glider": "glider",
    "tow_plane": "towplane",
    "motor_glider": "motorglider_sl",
    "tmg": "motorglider_sl",
    "helicopter": "powered",
    "powered": "powered",
}


def role_from_row(role: str | None, aircraft_type: str | None) -> str:
    """Effective launch-detection role of a tenant aircraft.

    An explicit ``role`` wins; otherwise the legacy ``aircraft_type`` is
    mapped. Unknown values yield "" (no a-priori knowledge).
    """
    if role and role.lower() in VALID_ROLES:
        return role.lower()
    if aircraft_type:
        return _ROLE_FROM_AIRCRAFT_TYPE.get(aircraft_type.lower(), "")
    return ""
