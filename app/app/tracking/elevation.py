"""Terrain elevation lookup (Copernicus GLO-90 DEM in ``elevation_tiles``).

The worker asks this service for the ground elevation under an aircraft
so that ``FlightState.altitude_agl`` and the outlanding logic use the real
terrain instead of the airfield elevation (over the Alps the difference
is hundreds of metres). The tiles are imported once by the operator with
``python -m app.tools.import_elevation`` (see DEPLOYMENT.md).

Design:

- In-memory dict cache keyed by (round(lat, 3), round(lon, 3)), i.e.
  ~100 m cells. The cache is bounded (``settings.terrain_cache_max_entries``);
  when full, the oldest half is dropped (dict insertion order).
- A miss costs one parametrised ``ST_Value`` query. The result - also a
  *negative* one (no tile / NODATA) - is cached under the same key, so a
  region without tiles is not queried per beacon.
- ``warm()`` fills the cache around an airfield with a few batched
  ``unnest`` queries. It is meant to run as a background task at worker
  start; until it is done, misses simply hit the database.
- Any database error or timeout (``settings.terrain_lookup_timeout_s``)
  of a per-beacon lookup falls back to ``None`` (caller uses the airfield
  elevation) and pauses lookups for ``settings.terrain_db_backoff_s``.
- Warm-up batches have their own timeout (``settings.terrain_warm_timeout_s``);
  a failing warm-up aborts the current run but never pauses the beacon
  path - it is retried at the next config reload.

Copernicus GLO-90 is a *surface* model (DSM): over forest the value is
the canopy (~20-30 m above ground), over buildings the roof. At
airfields and open fields it equals the ground.
"""

import asyncio
import time
from math import cos, radians
from typing import Any, Protocol

import structlog

from app.config import settings

log = structlog.get_logger()

# Cache cell size: 0.001 deg ~ 111 m in latitude, ~75 m in longitude at 47N.
CELL_DECIMALS = 3
CELL_DEG = 10 ** -CELL_DECIMALS

# Metres per degree of latitude (spherical mean)
M_PER_DEG_LAT = 111_320.0

# One lookup per cache miss. The IS NOT NULL guard skips a tile whose
# extent touches the point but whose pixel there is NODATA (tile border),
# so a neighbouring tile with data still wins.
POINT_SQL = (
    "SELECT ST_Value(rast, pt) FROM elevation_tiles, "
    "ST_SetSRID(ST_Point($1, $2), 4326) AS pt "
    "WHERE ST_Intersects(rast, pt) AND ST_Value(rast, pt) IS NOT NULL LIMIT 1"
)

# Batched lookup for warm(): one row per input point, NULL when no tile
# covers it (or NODATA). ``WITH ORDINALITY`` keeps the input order.
WARM_SQL = (
    "SELECT p.i, "
    "       (SELECT ST_Value(t.rast, p.pt) FROM elevation_tiles t "
    "         WHERE ST_Intersects(t.rast, p.pt) "
    "           AND ST_Value(t.rast, p.pt) IS NOT NULL LIMIT 1) AS elev "
    "FROM ("
    "    SELECT i, ST_SetSRID(ST_Point(lon, lat), 4326) AS pt "
    "    FROM unnest($1::float8[], $2::float8[]) WITH ORDINALITY AS u(lon, lat, i)"
    ") AS p ORDER BY p.i"
)


class _Db(Protocol):
    """The subset of asyncpg.Pool this service uses."""

    async def fetchval(self, query: str, *args: Any) -> Any: ...

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...


def cache_key(lat: float, lon: float) -> tuple[float, float]:
    """Cache cell of a position (~100 m)."""
    return (round(lat, CELL_DECIMALS), round(lon, CELL_DECIMALS))


def grid_points(lat: float, lon: float, radius_km: float,
                step_m: float) -> list[tuple[float, float]]:
    """Cache-cell centres of a ``step_m`` grid within ``radius_km`` of a point.

    Points are snapped to cache cells and de-duplicated, so the result has
    at most one entry per cell. Order: row by row from south-west.

    Returns:
        List of (lat, lon) tuples.
    """
    if radius_km <= 0 or step_m <= 0:
        return []
    radius_m = radius_km * 1000.0
    m_per_deg_lon = M_PER_DEG_LAT * cos(radians(lat))
    if m_per_deg_lon <= 1.0:
        # Poles: degenerate, warm just the centre cell
        return [cache_key(lat, lon)]
    n = int(radius_m // step_m)
    r2 = radius_m * radius_m
    seen: dict[tuple[float, float], None] = {}
    for j in range(-n, n + 1):
        dy = j * step_m
        for i in range(-n, n + 1):
            dx = i * step_m
            if dx * dx + dy * dy > r2:
                continue
            key = cache_key(lat + dy / M_PER_DEG_LAT, lon + dx / m_per_deg_lon)
            seen[key] = None
    return list(seen)


class ElevationService:
    """Cached terrain elevation lookups backed by ``elevation_tiles``.

    Args:
        db: asyncpg pool (or anything with ``fetchval``/``fetch``). May be
            None at construction; ``get_db()`` is used lazily then.
        max_entries: cache bound (default ``settings.terrain_cache_max_entries``).
        enabled: kill switch (default ``settings.terrain_agl_enabled``); when
            False every lookup returns None without touching the cache/DB.
        backoff_s: pause of the beacon path after a lookup error/timeout
            (default ``settings.terrain_db_backoff_s``).
        lookup_timeout_s: per-beacon query timeout
            (default ``settings.terrain_lookup_timeout_s``).
        warm_timeout_s: per-batch warm-up query timeout
            (default ``settings.terrain_warm_timeout_s``).
    """

    def __init__(
        self,
        db: _Db | None = None,
        max_entries: int | None = None,
        enabled: bool | None = None,
        backoff_s: float | None = None,
        lookup_timeout_s: float | None = None,
        warm_timeout_s: float | None = None,
    ) -> None:
        self._db = db
        self.max_entries = max(2, max_entries if max_entries is not None
                               else settings.terrain_cache_max_entries)
        self.enabled = settings.terrain_agl_enabled if enabled is None else enabled
        self.backoff_s = (settings.terrain_db_backoff_s if backoff_s is None
                          else backoff_s)
        self.lookup_timeout_s = (settings.terrain_lookup_timeout_s
                                 if lookup_timeout_s is None else lookup_timeout_s)
        self.warm_timeout_s = (settings.terrain_warm_timeout_s
                               if warm_timeout_s is None else warm_timeout_s)
        self._cache: dict[tuple[float, float], float | None] = {}
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
        return len(self._cache)

    def peek(self, lat: float, lon: float) -> float | None:
        """Cached value only (no DB); None also when not cached."""
        return self._cache.get(cache_key(lat, lon))

    def is_cached(self, lat: float, lon: float) -> bool:
        return cache_key(lat, lon) in self._cache

    async def get(self, lat: float, lon: float) -> float | None:
        """Terrain elevation (m MSL) under a position, or None.

        None means "unknown" (disabled, no tile, NODATA, DB error) and the
        caller falls back to the airfield elevation.
        """
        if not self.enabled:
            return None
        key = cache_key(lat, lon)
        if key in self._cache:
            self.hits += 1
            return self._cache[key]
        self.misses += 1

        if time.monotonic() < self._db_paused_until:
            return None
        try:
            value = await asyncio.wait_for(
                self._db_conn().fetchval(POINT_SQL, key[1], key[0]),
                timeout=self.lookup_timeout_s,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - never break the beacon path
            self._on_db_error(exc)
            return None
        elev = float(value) if value is not None else None
        self._store(key, elev)
        return elev

    async def warm(self, lat: float, lon: float, radius_km: float,
                   step_m: float | None = None) -> int:
        """Fill the cache on a grid around a point (batched queries).

        Returns the number of cells filled (including negative entries).
        Cells already cached are not queried again. Never raises; on a DB
        error/timeout the remaining batches are skipped and 0/partial is
        returned (``warm_errors`` counts the failures).
        """
        filled, _ok = await self._warm_grid(lat, lon, radius_km, step_m)
        return filled

    async def _warm_grid(self, lat: float, lon: float, radius_km: float,
                         step_m: float | None = None) -> tuple[int, bool]:
        """``warm()`` returning ``(filled, ok)``; ok=False after an error."""
        if not self.enabled:
            return 0, True
        step = settings.terrain_warm_step_m if step_m is None else step_m
        points = [p for p in grid_points(lat, lon, radius_km, step)
                  if p not in self._cache]
        if not points:
            return 0, True
        if len(points) > self.max_entries:
            # Warming more than the cache can hold would just evict itself
            points = points[: self.max_entries // 2]
        filled = 0
        batch = max(1, settings.terrain_warm_batch)
        for start in range(0, len(points), batch):
            chunk = points[start:start + batch]
            lats = [p[0] for p in chunk]
            lons = [p[1] for p in chunk]
            try:
                rows = await asyncio.wait_for(
                    self._db_conn().fetch(WARM_SQL, lons, lats),
                    timeout=self.warm_timeout_s,
                )
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                self._on_warm_error(exc, start // batch, len(points))
                return filled, False
            for row in rows:
                idx = int(row["i"]) - 1
                if 0 <= idx < len(chunk):
                    val = row["elev"]
                    self._store(chunk[idx], float(val) if val is not None else None)
                    filled += 1
            # Give the event loop a chance between batches
            await asyncio.sleep(0)
        return filled, True

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
            filled, ok = await self._warm_grid(cfg.latitude, cfg.longitude, radius)
            if not ok:
                log.warning(
                    "terrain_cache_warmup_aborted",
                    airfield=slug,
                    cells=filled,
                    duration_s=round(time.monotonic() - t0, 1),
                )
                return
            self._warmed.add(key)
            covered = sum(
                1 for p in grid_points(cfg.latitude, cfg.longitude, radius,
                                       settings.terrain_warm_step_m)
                if self._cache.get(p) is not None
            )
            log.info(
                "terrain_cache_warmed",
                airfield=slug,
                radius_km=radius,
                cells=filled,
                cells_with_data=covered,
                cache_size=len(self._cache),
                duration_s=round(time.monotonic() - t0, 1),
            )
            if filled and not covered:
                log.warning(
                    "terrain_no_tiles_for_airfield",
                    airfield=slug,
                    hint="run: python -m app.tools.import_elevation --slug " + slug,
                )

    def clear(self) -> None:
        self._cache.clear()
        self._warmed.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _db_conn(self) -> _Db:
        if self._db is None:
            from app.db.connection import get_db
            self._db = get_db()
        return self._db

    def _store(self, key: tuple[float, float], value: float | None) -> None:
        if len(self._cache) >= self.max_entries:
            # Drop the oldest half (insertion order == age)
            drop = len(self._cache) // 2
            for k in list(self._cache)[:drop]:
                del self._cache[k]
            log.info("terrain_cache_trimmed", dropped=drop, size=len(self._cache))
        self._cache[key] = value

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

    def _on_warm_error(self, exc: BaseException, batch_no: int,
                       total_points: int) -> None:
        """Warm-up batch failed: count + log, but do not touch the beacon path."""
        self.warm_errors += 1
        log.warning(
            "terrain_warmup_batch_failed",
            error=type(exc).__name__,
            detail=str(exc)[:200],
            batch=batch_no,
            points=total_points,
            timeout_s=self.warm_timeout_s,
        )
