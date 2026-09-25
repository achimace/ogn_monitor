"""In-memory index of known airports (table ``airports``, OurAirports data).

The worker uses it to tell a landing at a *foreign airfield* from an
outlanding and to remember ground contact / takeoffs of aircraft at foreign
airports (visitors). The table is filled once by the operator with
``python -m app.tools.import_airports`` (see DEPLOYMENT.md 9b); the worker
loads every airport within ``settings.airports_index_radius_km`` of each
active airfield at start and on every config reload.

Design:

- ``nearest(lat, lon, max_m)`` must be cheap (it is called from the beacon
  path for slow beacons of untracked aircraft): airports are bucketed in a
  ``CELL_DEG`` (0.1 deg) grid dict and a lookup scans only the cells that
  can contain a point within ``max_m`` (3x3 for radii up to a few km).
- Loading builds a new grid and swaps it in atomically; a failing load
  (table missing, DB error) keeps the previous index. An empty index means
  "no known airports": every landing away from home is an outlanding and
  no foreign ground contact is recorded - the behaviour before this feature.
"""

import asyncio
import time
from dataclasses import dataclass
from math import ceil, cos, floor, radians
from typing import Any, Protocol

import structlog

from app.config import settings
from app.tracking.geo_calc import haversine

log = structlog.get_logger()

# Grid cell size (degrees). 0.1 deg = ~11 km N-S, ~7.5 km E-W at 47N.
CELL_DEG = 0.1
M_PER_DEG_LAT = 111_320.0

# Idents that are 4 upper-case letters are ICAO codes (OurAirports uses
# synthetic idents like "DE-0123" for fields without one).
_ICAO_LEN = 4

LOAD_SQL = (
    "SELECT ident, icao_code, name, type, latitude, longitude, elevation_m, "
    "municipality FROM airports "
    "WHERE ST_DWithin(location, ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography, $3)"
)

Cell = tuple[int, int]


class _Db(Protocol):
    """The subset of asyncpg.Pool this index uses."""

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...


@dataclass(frozen=True, slots=True)
class Airport:
    """One row of ``airports`` as the worker needs it."""
    ident: str
    name: str
    latitude: float
    longitude: float
    elevation_m: float | None = None
    icao_code: str = ""
    type: str = ""
    municipality: str = ""

    @property
    def display_name(self) -> str:
        """``"Gundelfingen (EDMU)"`` when an ICAO code is known, else the name."""
        code = (self.icao_code or "").strip().upper()
        if not code and len(self.ident) == _ICAO_LEN and self.ident.isalpha():
            code = self.ident.upper()
        if code and code not in self.name.upper():
            return f"{self.name} ({code})"
        return self.name


def grid_cell(lat: float, lon: float) -> Cell:
    """Grid cell of a position: (floor(lat / CELL_DEG), floor(lon / CELL_DEG))."""
    return (floor(lat / CELL_DEG), floor(lon / CELL_DEG))


class AirportIndex:
    """Grid-indexed airports around the active airfields.

    Args:
        db: asyncpg pool (or anything with ``fetch``). May be None at
            construction; ``get_db()`` is used lazily then.
        radius_km: load radius around each airfield (default
            ``settings.airports_index_radius_km``).
    """

    def __init__(self, db: _Db | None = None, radius_km: float | None = None) -> None:
        self._db = db
        self.radius_km = (settings.airports_index_radius_km
                          if radius_km is None else float(radius_km))
        self._grid: dict[Cell, list[Airport]] = {}
        self._count = 0
        self.loaded_at: float = 0.0
        self.load_errors = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return self._count

    def __bool__(self) -> bool:
        """An index without airports behaves like "no index" (fallback)."""
        return self._count > 0

    def add(self, airport: Airport) -> None:
        """Insert one airport (used by the loader and by tests)."""
        self._grid.setdefault(grid_cell(airport.latitude, airport.longitude), []).append(airport)
        self._count += 1

    def clear(self) -> None:
        self._grid = {}
        self._count = 0

    def nearest(self, lat: float, lon: float, max_m: float) -> Airport | None:
        """Closest airport within ``max_m`` metres of a position, or None.

        Scans the grid cells that can contain a point within ``max_m``
        ((2n+1)^2 cells, n = 1 for radii up to one cell width) and
        compares great-circle distances.
        """
        if self._count == 0 or max_m <= 0:
            return None
        m_per_deg_lon = M_PER_DEG_LAT * cos(radians(lat))
        n_lat = max(1, ceil(max_m / (CELL_DEG * M_PER_DEG_LAT)))
        n_lon = max(1, ceil(max_m / (CELL_DEG * m_per_deg_lon))) if m_per_deg_lon > 1.0 else 1
        c_lat, c_lon = grid_cell(lat, lon)
        best: Airport | None = None
        best_d = max_m
        for dy in range(-n_lat, n_lat + 1):
            for dx in range(-n_lon, n_lon + 1):
                for ap in self._grid.get((c_lat + dy, c_lon + dx), ()):
                    d = haversine(lat, lon, ap.latitude, ap.longitude)
                    if d <= best_d:
                        best_d = d
                        best = ap
        return best

    async def load_for_airfields(self, configs: dict[str, Any],
                                 radius_km: float | None = None) -> int:
        """(Re)load all airports within ``radius_km`` of every airfield.

        ``configs`` maps slug -> object with ``latitude``/``longitude``
        (AirfieldConfig). One query per airfield, merged by ident, then the
        new grid replaces the old one. On any error the previous index is
        kept (and an empty index stays empty = legacy behaviour).

        Returns:
            Number of airports in the index after the call.
        """
        radius = self.radius_km if radius_km is None else float(radius_km)
        t0 = time.monotonic()
        seen: dict[str, Airport] = {}
        try:
            db = self._db_conn()
            for slug, cfg in list(configs.items()):
                rows = await db.fetch(LOAD_SQL, cfg.longitude, cfg.latitude, radius * 1000.0)
                for row in rows:
                    ap = airport_from_row(row)
                    seen.setdefault(ap.ident, ap)
                await asyncio.sleep(0)
        except Exception as exc:  # noqa: BLE001 - keep the old index, log
            self.load_errors += 1
            log.warning(
                "airport_index_load_failed",
                error=str(exc)[:200],
                kept=self._count,
                hint="run: python -m app.tools.import_airports",
            )
            return self._count

        fresh = AirportIndex(self._db, radius)
        for ap in seen.values():
            fresh.add(ap)
        self._grid = fresh._grid
        self._count = fresh._count
        self.loaded_at = time.time()
        log.info(
            "airport_index_loaded",
            airports=self._count,
            airfields=len(configs),
            radius_km=radius,
            duration_s=round(time.monotonic() - t0, 2),
        )
        if configs and not self._count:
            log.warning(
                "airport_index_empty",
                hint="run: python -m app.tools.import_airports",
            )
        return self._count

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _db_conn(self) -> _Db:
        if self._db is None:
            from app.db.connection import get_db
            self._db = get_db()
        return self._db


def airport_from_row(row: Any) -> Airport:
    """Build an ``Airport`` from a ``LOAD_SQL`` row (asyncpg Record or dict)."""
    elev = row["elevation_m"]
    return Airport(
        ident=str(row["ident"]),
        name=str(row["name"] or row["ident"]),
        latitude=float(row["latitude"]),
        longitude=float(row["longitude"]),
        elevation_m=float(elev) if elev is not None else None,
        icao_code=str(row["icao_code"] or ""),
        type=str(row["type"] or ""),
        municipality=str(row["municipality"] or ""),
    )
