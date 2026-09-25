"""Terrain elevation lookup (Copernicus GLO-90 DEM in ``elevation_tiles``).

The worker asks this service for the ground elevation under an aircraft
so that ``FlightState.altitude_agl`` and the outlanding logic use the real
terrain instead of the airfield elevation (over the Alps the difference
is hundreds of metres). The tiles are imported once by the operator with
``python -m app.tools.import_elevation`` (see DEPLOYMENT.md).

Design (subtile cache):

- ``elevation_tiles.rast`` holds 64x64 px subtiles of the 1x1 degree
  source rasters (~0.053 deg, i.e. ~6 km x 4 km at 47N; edge subtiles are
  narrower). A subtile is fetched from the database *once* - including
  all its pixel values (``ST_DumpValues``) - and kept in memory as a flat
  ``array('f')`` (16 KB per full subtile). Every later position inside it
  is a pure in-memory pixel lookup, so a cache miss costs one query per
  ~24 km^2, not one per 100 m as before.
- A coarse grid of ``CELL_DEG`` (0.05 deg) cells is the spatial index:
  every cached tile is registered in each coarse cell it intersects, and
  a lookup only scans the (at most a handful of) tiles of its own cell.
- A miss fetches *all* tiles intersecting the point's coarse cell (a cell
  box touches at most 2x2 subtiles) and marks the cell as *known*. A known
  cell without a covering tile is a negative entry: regions without tiles
  cost one query per coarse cell, and partially covered cells (edge of the
  imported area) stay correct.
- The cache is bounded by the number of tiles
  (``settings.terrain_cache_max_tiles``, 4000 x 16 KB ~ 65 MB worst case);
  when full, the oldest half is dropped (dict insertion order) and the
  coarse cells that referenced a dropped tile become unknown again.
- ``warm()`` fetches every subtile within a radius around a point with a
  single query (10 km ~ 20-40 rows) and marks the coarse cells lying
  entirely inside the circle as known. It is meant to run as a background
  task at worker start; until it is done, misses simply hit the database.
- Any database error or timeout (``settings.terrain_lookup_timeout_s``)
  of a per-beacon lookup falls back to ``None`` (caller uses the airfield
  elevation) and pauses lookups for ``settings.terrain_db_backoff_s``.
- Warm-up queries have their own timeout (``settings.terrain_warm_timeout_s``);
  a failing warm-up aborts the current run but never pauses the beacon
  path - it is retried at the next config reload.

Copernicus GLO-90 is a *surface* model (DSM): over forest the value is
the canopy (~20-30 m above ground), over buildings the roof. At
airfields and open fields it equals the ground.
"""

import asyncio
import time
from array import array
from dataclasses import dataclass
from math import cos, floor, isnan, nan, radians
from typing import Any, Protocol

import structlog

from app.config import settings

log = structlog.get_logger()

# Coarse spatial-index cell (degrees). Slightly smaller than a full 64 px
# GLO-90 subtile (~0.053 deg), so a cell box intersects at most 2x2 tiles
# and a tile is registered in at most 3x3 cells.
CELL_DEG = 0.05

# Metres per degree of latitude (spherical mean)
M_PER_DEG_LAT = 111_320.0

# Tile columns fetched by both queries: georeference + all band-1 pixel
# values as a 2-D array (asyncpg: nested lists, None where NODATA).
_TILE_COLS = (
    "SELECT id, ST_UpperLeftX(rast) AS ulx, ST_UpperLeftY(rast) AS uly, "
    "ST_ScaleX(rast) AS sx, ST_ScaleY(rast) AS sy, "
    "ST_Width(rast) AS w, ST_Height(rast) AS h, "
    "ST_DumpValues(rast, 1) AS vals FROM elevation_tiles "
)

# Cache miss: all subtiles touching the coarse cell box (xmin, ymin, xmax, ymax).
CELL_SQL = _TILE_COLS + "WHERE ST_Intersects(rast, ST_MakeEnvelope($1, $2, $3, $4, 4326))"

# warm(): all subtiles within radius_m (geodesic buffer) of a point (lon, lat).
WARM_SQL = _TILE_COLS + (
    "WHERE ST_Intersects(rast, ST_Buffer("
    "ST_SetSRID(ST_Point($1, $2), 4326)::geography, $3)::geometry)"
)

Cell = tuple[int, int]


class _Db(Protocol):
    """The subset of asyncpg.Pool this service uses."""

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...


def coarse_cell(lat: float, lon: float) -> Cell:
    """Coarse-index cell of a position: (floor(lon / CELL_DEG), floor(lat / CELL_DEG))."""
    return (floor(lon / CELL_DEG), floor(lat / CELL_DEG))


def cell_bounds(cell: Cell) -> tuple[float, float, float, float]:
    """Bounding box (xmin, ymin, xmax, ymax) of a coarse cell in degrees."""
    cx, cy = cell
    return (cx * CELL_DEG, cy * CELL_DEG, (cx + 1) * CELL_DEG, (cy + 1) * CELL_DEG)


def cells_inside_circle(lat: float, lon: float, radius_km: float) -> list[Cell]:
    """Coarse cells whose box lies entirely within ``radius_km`` of a point.

    Equirectangular distance with 1 % safety margin, so a cell reported
    here is also inside PostGIS' geodesic buffer of the same radius.
    """
    if radius_km <= 0:
        return []
    radius_m = radius_km * 1000.0
    m_per_deg_lon = M_PER_DEG_LAT * cos(radians(lat))
    if m_per_deg_lon <= 1.0:
        return []
    d_lat = radius_m / M_PER_DEG_LAT
    d_lon = radius_m / m_per_deg_lon
    r2 = (radius_m * 0.99) ** 2
    out: list[Cell] = []
    for cy in range(floor((lat - d_lat) / CELL_DEG), floor((lat + d_lat) / CELL_DEG) + 1):
        for cx in range(floor((lon - d_lon) / CELL_DEG), floor((lon + d_lon) / CELL_DEG) + 1):
            xmin, ymin, xmax, ymax = cell_bounds((cx, cy))
            inside = True
            for px, py in ((xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)):
                dx = (px - lon) * m_per_deg_lon
                dy = (py - lat) * M_PER_DEG_LAT
                if dx * dx + dy * dy > r2:
                    inside = False
                    break
            if inside:
                out.append((cx, cy))
    return out


@dataclass(slots=True)
class Tile:
    """One cached raster subtile (georeference + flat pixel values).

    ``values`` is row-major, ``w * h`` entries, NaN for NODATA.
    """

    id: int
    ulx: float
    uly: float
    sx: float
    sy: float  # negative: rows go south
    w: int
    h: int
    values: array
    cells: tuple[Cell, ...] = ()

    @classmethod
    def from_row(cls, row: Any) -> "Tile":
        """Build a tile from a CELL_SQL / WARM_SQL row (values: nested lists)."""
        w, h = int(row["w"]), int(row["h"])
        flat = array("f", [nan]) * (w * h)
        vals = row["vals"] or []
        for r, line in enumerate(vals[:h]):
            base = r * w
            for c, v in enumerate(line[:w]):
                if v is not None:
                    flat[base + c] = v
        return cls(
            id=int(row["id"]), ulx=float(row["ulx"]), uly=float(row["uly"]),
            sx=float(row["sx"]), sy=float(row["sy"]), w=w, h=h, values=flat,
        )

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) in degrees."""
        x2 = self.ulx + self.w * self.sx
        y2 = self.uly + self.h * self.sy
        return (min(self.ulx, x2), min(self.uly, y2), max(self.ulx, x2), max(self.uly, y2))

    def contains(self, lat: float, lon: float) -> bool:
        xmin, ymin, xmax, ymax = self.bounds
        # Half-open on the east/south edge so a point on a shared edge
        # belongs to exactly one tile (consistent with the pixel index).
        return xmin <= lon < xmax and ymin < lat <= ymax

    def value(self, lat: float, lon: float) -> float | None:
        """Pixel value under a position (must be inside), None for NODATA."""
        col = int((lon - self.ulx) / self.sx)
        row = int((lat - self.uly) / self.sy)
        col = min(max(col, 0), self.w - 1)
        row = min(max(row, 0), self.h - 1)
        v = self.values[row * self.w + col]
        return None if isnan(v) else float(v)

    def coarse_cells(self) -> tuple[Cell, ...]:
        """All coarse cells this tile intersects."""
        xmin, ymin, xmax, ymax = self.bounds
        # Shrink by a hair so a tile ending exactly on a cell border is not
        # registered in the next cell.
        eps = 1e-9
        return tuple(
            (cx, cy)
            for cy in range(floor((ymin + eps) / CELL_DEG), floor((ymax - eps) / CELL_DEG) + 1)
            for cx in range(floor((xmin + eps) / CELL_DEG), floor((xmax - eps) / CELL_DEG) + 1)
        )


class ElevationService:
    """Cached terrain elevation lookups backed by ``elevation_tiles``.

    Args:
        db: asyncpg pool (or anything with ``fetch``). May be None at
            construction; ``get_db()`` is used lazily then.
        max_tiles: cache bound in subtiles (default
            ``settings.terrain_cache_max_tiles``).
        enabled: kill switch (default ``settings.terrain_agl_enabled``); when
            False every lookup returns None without touching the cache/DB.
        backoff_s: pause of the beacon path after a lookup error/timeout
            (default ``settings.terrain_db_backoff_s``).
        lookup_timeout_s: per-beacon query timeout
            (default ``settings.terrain_lookup_timeout_s``).
        warm_timeout_s: warm-up query timeout
            (default ``settings.terrain_warm_timeout_s``).
    """

    def __init__(
        self,
        db: _Db | None = None,
        max_tiles: int | None = None,
        enabled: bool | None = None,
        backoff_s: float | None = None,
        lookup_timeout_s: float | None = None,
        warm_timeout_s: float | None = None,
    ) -> None:
        self._db = db
        self.max_tiles = max(2, max_tiles if max_tiles is not None
                             else settings.terrain_cache_max_tiles)
        self.enabled = settings.terrain_agl_enabled if enabled is None else enabled
        self.backoff_s = (settings.terrain_db_backoff_s if backoff_s is None
                          else backoff_s)
        self.lookup_timeout_s = (settings.terrain_lookup_timeout_s
                                 if lookup_timeout_s is None else lookup_timeout_s)
        self.warm_timeout_s = (settings.terrain_warm_timeout_s
                               if warm_timeout_s is None else warm_timeout_s)
        # tile id -> Tile, insertion order == age
        self._tiles: dict[int, Tile] = {}
        # coarse cell -> ids of cached tiles intersecting it
        self._index: dict[Cell, list[int]] = {}
        # Coarse cells fully resolved against the DB (all intersecting tiles
        # cached; empty = negative entry). Insertion order == age; bounded
        # by max_cells (a cell is a few dozen bytes, tiles are the cost).
        self._known: dict[Cell, None] = {}
        self.max_cells = self.max_tiles * 4
        # Monotonic time until which DB lookups are skipped after an error
        self._db_paused_until = 0.0
        # (lat, lon, radius_km) already warmed - config reloads skip them
        self._warmed: set[tuple[float, float, float]] = set()
        self.hits = 0
        self.misses = 0
        # Beacon-path lookup errors/timeouts (these pause the DB path)
        self.db_errors = 0
        # Warm-up errors/timeouts (abort the warm-up only)
        self.warm_errors = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        """Number of cached tiles."""
        return len(self._tiles)

    @property
    def cells_known(self) -> int:
        return len(self._known)

    def peek(self, lat: float, lon: float) -> float | None:
        """Cached value only (no DB); None also when not cached."""
        tile = self._find_tile(lat, lon)
        return tile.value(lat, lon) if tile else None

    def is_cached(self, lat: float, lon: float) -> bool:
        """True when a lookup here would not touch the DB (tile or negative)."""
        return (self._find_tile(lat, lon) is not None
                or coarse_cell(lat, lon) in self._known)

    async def get(self, lat: float, lon: float) -> float | None:
        """Terrain elevation (m MSL) under a position, or None.

        None means "unknown" (disabled, no tile, NODATA, DB error) and the
        caller falls back to the airfield elevation.
        """
        if not self.enabled:
            return None
        tile = self._find_tile(lat, lon)
        if tile is not None:
            self.hits += 1
            return tile.value(lat, lon)
        cell = coarse_cell(lat, lon)
        if cell in self._known:
            self.hits += 1
            return None
        self.misses += 1

        if time.monotonic() < self._db_paused_until:
            return None
        try:
            rows = await asyncio.wait_for(
                self._db_conn().fetch(CELL_SQL, *cell_bounds(cell)),
                timeout=self.lookup_timeout_s,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - never break the beacon path
            self._on_db_error(exc)
            return None
        for row in rows:
            self._store(row)
        self._mark_known(cell)
        tile = self._find_tile(lat, lon)
        return tile.value(lat, lon) if tile else None

    async def warm(self, lat: float, lon: float, radius_km: float) -> int:
        """Fetch every subtile within ``radius_km`` of a point (one query).

        Returns the number of tiles returned by the database (cached ones
        included). Never raises; on a DB error/timeout 0 is returned and
        ``warm_errors`` counts the failure.
        """
        loaded, _new, _ok = await self._warm_circle(lat, lon, radius_km)
        return loaded

    async def _warm_circle(self, lat: float, lon: float,
                           radius_km: float) -> tuple[int, int, bool]:
        """``warm()`` returning ``(tiles, new_tiles, ok)``; ok=False after an error."""
        if not self.enabled or radius_km <= 0:
            return 0, 0, True
        try:
            rows = await asyncio.wait_for(
                self._db_conn().fetch(WARM_SQL, lon, lat, radius_km * 1000.0),
                timeout=self.warm_timeout_s,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            self._on_warm_error(exc, radius_km)
            return 0, 0, False
        if len(rows) > self.max_tiles // 2:
            # Warming more than the cache can hold would just evict itself
            log.warning("terrain_warmup_truncated", tiles=len(rows),
                        kept=self.max_tiles // 2, max_tiles=self.max_tiles)
            rows = rows[: self.max_tiles // 2]
        new = 0
        for row in rows:
            if self._store(row):
                new += 1
        for cell in cells_inside_circle(lat, lon, radius_km):
            self._mark_known(cell)
        return len(rows), new, True

    async def warm_airfields(self, configs: dict[str, Any],
                             radius_km: float | None = None) -> None:
        """Warm the cache around every airfield config not warmed before.

        ``configs`` maps slug -> object with ``latitude``/``longitude``
        (AirfieldConfig). Meant to run as a background task; logs count
        and duration per airfield. An airfield counts as warmed only after
        an error-free run; a DB error/timeout aborts the whole run (the
        remaining airfields are retried at the next call).
        """
        if not self.enabled:
            return
        radius = settings.terrain_warm_radius_km if radius_km is None else radius_km
        for slug, cfg in list(configs.items()):
            key = (round(cfg.latitude, 4), round(cfg.longitude, 4), float(radius))
            if key in self._warmed:
                continue
            t0 = time.monotonic()
            loaded, new, ok = await self._warm_circle(cfg.latitude, cfg.longitude, radius)
            if not ok:
                log.warning(
                    "terrain_cache_warmup_aborted",
                    airfield=slug,
                    duration_s=round(time.monotonic() - t0, 1),
                )
                return
            self._warmed.add(key)
            log.info(
                "terrain_cache_warmed",
                airfield=slug,
                radius_km=radius,
                tiles=loaded,
                tiles_new=new,
                cache_tiles=len(self._tiles),
                cells_known=len(self._known),
                duration_s=round(time.monotonic() - t0, 2),
            )
            if not loaded:
                log.warning(
                    "terrain_no_tiles_for_airfield",
                    airfield=slug,
                    hint="run: python -m app.tools.import_elevation --slug " + slug,
                )
            # Give the event loop a chance between airfields
            await asyncio.sleep(0)

    def clear(self) -> None:
        self._tiles.clear()
        self._index.clear()
        self._known.clear()
        self._warmed.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _db_conn(self) -> _Db:
        if self._db is None:
            from app.db.connection import get_db
            self._db = get_db()
        return self._db

    def _find_tile(self, lat: float, lon: float) -> Tile | None:
        ids = self._index.get(coarse_cell(lat, lon))
        if not ids:
            return None
        for tid in ids:
            tile = self._tiles[tid]
            if tile.contains(lat, lon):
                return tile
        return None

    def _store(self, row: Any) -> bool:
        """Cache a tile row (no-op when already cached). Returns True if new."""
        tid = int(row["id"])
        if tid in self._tiles:
            return False
        if len(self._tiles) >= self.max_tiles:
            self._trim_tiles()
        tile = Tile.from_row(row)
        tile.cells = tile.coarse_cells()
        self._tiles[tid] = tile
        for cell in tile.cells:
            self._index.setdefault(cell, []).append(tid)
        return True

    def _trim_tiles(self) -> None:
        """Drop the oldest half of the tiles; their cells become unknown."""
        drop = len(self._tiles) // 2
        for tid in list(self._tiles)[:drop]:
            tile = self._tiles.pop(tid)
            for cell in tile.cells:
                ids = self._index.get(cell)
                if ids:
                    ids.remove(tid)
                    if not ids:
                        del self._index[cell]
                self._known.pop(cell, None)
        log.info("terrain_cache_trimmed", dropped=drop, tiles=len(self._tiles),
                 cells_known=len(self._known))

    def _mark_known(self, cell: Cell) -> None:
        if cell in self._known:
            return
        if len(self._known) >= self.max_cells:
            drop = len(self._known) // 2
            for c in list(self._known)[:drop]:
                del self._known[c]
            log.info("terrain_cells_trimmed", dropped=drop, cells_known=len(self._known))
        self._known[cell] = None

    def _on_db_error(self, exc: BaseException) -> None:
        """Beacon-path lookup failed: pause the DB path for backoff_s."""
        self.db_errors += 1
        self._db_paused_until = time.monotonic() + self.backoff_s
        log.warning(
            "terrain_lookup_failed",
            error=type(exc).__name__,
            detail=str(exc)[:200],
            backoff_s=self.backoff_s,
        )

    def _on_warm_error(self, exc: BaseException, radius_km: float) -> None:
        """Warm-up query failed: count + log, but do not touch the beacon path."""
        self.warm_errors += 1
        log.warning(
            "terrain_warmup_failed",
            error=type(exc).__name__,
            detail=str(exc)[:200],
            radius_km=radius_km,
            timeout_s=self.warm_timeout_s,
        )
