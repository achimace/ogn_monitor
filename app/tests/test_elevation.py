"""ElevationService subtile cache: hit/miss, negative cells, bounds, warm-up, DB fallback.

No network, no database: ``FakeDb`` answers the two service queries
(``CELL_SQL`` / ``WARM_SQL``) from synthetic 64x64 px subtiles laid out on
the real Copernicus GLO-90 grid (1200 px per degree).
"""

import asyncio
from math import cos, floor, radians

import pytest

from app.tracking.elevation import (
    CELL_DEG,
    CELL_SQL,
    M_PER_DEG_LAT,
    WARM_SQL,
    ElevationService,
    Tile,
    cell_bounds,
    cells_inside_circle,
    coarse_cell,
)

# Pixel size of GLO-90 below 50N (3 arcsec) and the span of a full subtile
PX = 1.0 / 1200.0
SUB = 64 * PX

OHL = (47.6386, 11.2394)


def pixel_value(r: int, c: int) -> float:
    """Default synthetic pixel content: encodes row and column."""
    return 1000.0 + r * 100.0 + c


def make_tile(tid: int, ulx: float, uly: float, w: int = 64, h: int = 64,
              values=None) -> dict:
    """A DB row as CELL_SQL / WARM_SQL return it (values = nested lists)."""
    if values is None:
        values = [[pixel_value(r, c) for c in range(w)] for r in range(h)]
    return {"id": tid, "ulx": ulx, "uly": uly, "sx": PX, "sy": -PX,
            "w": w, "h": h, "vals": values}


def subtile_origin(lat: float, lon: float) -> tuple[float, float]:
    """Upper-left corner of the grid-aligned subtile containing a position."""
    base_lon, top_lat = floor(lon), floor(lat) + 1
    i = floor((lon - base_lon) / SUB)
    j = floor((top_lat - lat) / SUB)
    return base_lon + i * SUB, top_lat - j * SUB


def tiles_around(lat: float, lon: float, n: int, start_id: int = 1) -> list[dict]:
    """(2n+1)^2 grid-aligned subtiles centred on the one containing lat/lon."""
    ulx0, uly0 = subtile_origin(lat, lon)
    tiles, tid = [], start_id
    for j in range(-n, n + 1):
        for i in range(-n, n + 1):
            tiles.append(make_tile(tid, ulx0 + i * SUB, uly0 - j * SUB))
            tid += 1
    return tiles


def bbox(t: dict) -> tuple[float, float, float, float]:
    return (t["ulx"], t["uly"] - t["h"] * PX, t["ulx"] + t["w"] * PX, t["uly"])


class FakeDb:
    """asyncpg.Pool stand-in answering the two tile queries from ``tiles``."""

    def __init__(self, tiles: list[dict] | None = None, fail: bool = False,
                 delay: float = 0.0):
        self.tiles = list(tiles or [])
        self.fail = fail
        # Simulated query duration (seconds) - for the timeout paths
        self.delay = delay
        self.fetch_calls = 0
        self.cell_queries: list[tuple[float, float, float, float]] = []
        self.warm_queries: list[tuple[float, float, float]] = []

    async def fetch(self, query, *args):
        self.fetch_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("db down")
        if query == CELL_SQL:
            xmin, ymin, xmax, ymax = args
            self.cell_queries.append(args)
            return [t for t in self.tiles if self._box_hits(t, xmin, ymin, xmax, ymax)]
        if query == WARM_SQL:
            lon, lat, radius_m = args
            self.warm_queries.append(args)
            return [t for t in self.tiles if self._circle_hits(t, lat, lon, radius_m)]
        raise AssertionError(f"unexpected query: {query[:60]}")

    @staticmethod
    def _box_hits(t, xmin, ymin, xmax, ymax) -> bool:
        txmin, tymin, txmax, tymax = bbox(t)
        return txmin <= xmax and txmax >= xmin and tymin <= ymax and tymax >= ymin

    @staticmethod
    def _circle_hits(t, lat, lon, radius_m) -> bool:
        txmin, tymin, txmax, tymax = bbox(t)
        # Distance from the centre to the closest point of the tile box
        cx = min(max(lon, txmin), txmax)
        cy = min(max(lat, tymin), tymax)
        dx = (cx - lon) * M_PER_DEG_LAT * cos(radians(lat))
        dy = (cy - lat) * M_PER_DEG_LAT
        return dx * dx + dy * dy <= radius_m * radius_m


def svc(db: FakeDb, **kw) -> ElevationService:
    kw.setdefault("enabled", True)
    kw.setdefault("backoff_s", 60)
    return ElevationService(db, **kw)


# ---------------------------------------------------------------------------
# get(): cache hit / miss / pixel addressing / negative cells
# ---------------------------------------------------------------------------

async def test_miss_fetches_the_cell_once_then_serves_from_memory():
    db = FakeDb(tiles_around(*OHL, n=1))
    s = svc(db)

    first = await s.get(*OHL)
    assert first is not None
    assert db.fetch_calls == 1
    assert db.cell_queries == [cell_bounds(coarse_cell(*OHL))]

    # Same position, another position in the same subtile, and a position
    # in a neighbouring subtile that the cell query already fetched
    assert await s.get(*OHL) == first
    assert await s.get(OHL[0] + 0.01, OHL[1] + 0.01) is not None
    assert await s.get(OHL[0] + 0.03, OHL[1] - 0.03) is not None
    assert db.fetch_calls == 1
    assert s.misses == 1 and s.hits == 3
    assert 1 <= len(s) <= 4  # tiles touching the 0.05 deg cell box


async def test_pixel_is_addressed_by_row_and_column():
    ulx, uly = subtile_origin(*OHL)
    db = FakeDb([make_tile(1, ulx, uly)])
    s = svc(db)

    # Upper-left corner -> pixel (0, 0); centre of pixel (row 5, col 7)
    assert await s.get(uly, ulx) == pixel_value(0, 0)
    assert await s.get(uly - 5.5 * PX, ulx + 7.5 * PX) == pixel_value(5, 7)
    # Last pixel (row 63, col 63), just inside the south-east corner
    assert await s.get(uly - 64 * PX + 1e-9, ulx + 64 * PX - 1e-9) == pixel_value(63, 63)
    assert db.fetch_calls <= 2  # corners may fall into two coarse cells


async def test_nodata_pixel_returns_none_but_is_cached():
    ulx, uly = subtile_origin(*OHL)
    values = [[pixel_value(r, c) for c in range(64)] for r in range(64)]
    values[3][4] = None  # NODATA
    db = FakeDb([make_tile(1, ulx, uly, values=values)])
    s = svc(db)

    lat, lon = uly - 3.5 * PX, ulx + 4.5 * PX
    assert await s.get(lat, lon) is None
    assert s.is_cached(lat, lon)
    assert await s.get(lat, lon) is None
    assert await s.get(lat, lon + PX) == pixel_value(3, 5)
    assert db.fetch_calls == 1


async def test_no_tile_marks_the_coarse_cell_negative():
    db = FakeDb([])
    s = svc(db)

    assert await s.get(*OHL) is None
    assert await s.get(*OHL) is None
    # Another position in the same 0.05 deg cell: no query either
    assert await s.get(OHL[0] + 0.004, OHL[1] + 0.004) is None
    assert db.fetch_calls == 1
    assert s.is_cached(*OHL) and s.peek(*OHL) is None
    assert s.cells_known == 1

    # A different coarse cell is asked once more
    assert await s.get(OHL[0] + 0.1, OHL[1]) is None
    assert db.fetch_calls == 2
    assert s.cells_known == 2


async def test_partially_covered_cell_answers_both_halves_correctly():
    # The cell of OHL is only covered where this single subtile lies
    ulx, uly = subtile_origin(*OHL)
    tile = make_tile(1, ulx, uly)
    db = FakeDb([tile])
    s = svc(db)
    cell = coarse_cell(*OHL)
    xmin, ymin, xmax, ymax = cell_bounds(cell)
    txmin, tymin, txmax, tymax = bbox(tile)

    # A spot inside the cell but outside the tile (the cell box is 0.05 deg,
    # the tile 0.053 deg but offset - find an uncovered corner)
    uncovered = None
    for lat, lon in ((ymin + 1e-6, xmin + 1e-6), (ymax - 1e-6, xmax - 1e-6),
                     (ymin + 1e-6, xmax - 1e-6), (ymax - 1e-6, xmin + 1e-6)):
        if not (txmin <= lon < txmax and tymin < lat <= tymax):
            uncovered = (lat, lon)
            break
    assert uncovered is not None

    assert await s.get(*uncovered) is None
    assert db.fetch_calls == 1
    # The covered part of the same cell is served without a new query
    assert await s.get(*OHL) is not None
    assert db.fetch_calls == 1
    assert s.is_cached(*uncovered) and s.is_cached(*OHL)


async def test_tile_is_found_from_every_coarse_cell_it_intersects():
    ulx, uly = subtile_origin(*OHL)
    tile = make_tile(1, ulx, uly)
    db = FakeDb([tile])
    s = svc(db)
    txmin, tymin, txmax, tymax = bbox(tile)

    assert await s.get(tymax - 1e-6, txmin + 1e-6) == pixel_value(0, 0)  # NW corner
    # SE corner lies in another coarse cell (tile 0.053 deg > cell 0.05 deg)
    assert coarse_cell(tymin + 1e-6, txmax - 1e-6) != coarse_cell(tymax - 1e-6, txmin + 1e-6)
    assert await s.get(tymin + 1e-6, txmax - 1e-6) == pixel_value(63, 63)
    assert db.fetch_calls == 1
    assert s.hits == 1


def test_tile_registers_in_all_intersecting_coarse_cells():
    # Aligned exactly on a cell border: 0.0533 deg spans 2 cells per axis
    t = Tile.from_row(make_tile(1, 11.0, 48.0))
    assert set(t.coarse_cells()) == {(220, 958), (221, 958), (220, 959), (221, 959)}
    # Just below a border: 3 cells per axis
    t = Tile.from_row(make_tile(2, 11.049, 47.951))
    assert len(t.coarse_cells()) == 9
    # Edge subtile (48 px) fits in 1 cell when placed well inside it
    t = Tile.from_row(make_tile(3, 11.001, 47.999, w=48, h=48))
    assert t.coarse_cells() == ((220, 959),)


def test_narrow_edge_subtile_addresses_pixels_by_its_own_width():
    t = Tile.from_row(make_tile(1, 11.9, 47.1, w=48, h=48))
    assert t.value(47.1 - 2.5 * PX, 11.9 + 47.5 * PX) == pixel_value(2, 47)
    assert not t.contains(47.1 - 2.5 * PX, 11.9 + 48.5 * PX)
    assert len(t.values) == 48 * 48


async def test_disabled_service_never_touches_db_or_cache():
    db = FakeDb(tiles_around(*OHL, n=1))
    s = svc(db, enabled=False)

    assert await s.get(*OHL) is None
    assert db.fetch_calls == 0
    assert len(s) == 0
    assert await s.warm(*OHL, radius_km=1) == 0
    assert db.fetch_calls == 0


async def test_cache_is_bounded_by_tiles_and_drops_oldest_half():
    # 10 subtiles far apart: each cell query returns exactly one
    ulx, uly = subtile_origin(*OHL)
    tiles = [make_tile(i + 1, ulx + i * 0.2, uly) for i in range(10)]
    centres = [(uly - SUB / 2, ulx + i * 0.2 + SUB / 2) for i in range(10)]
    db = FakeDb(tiles)
    s = svc(db, max_tiles=8)

    for p in centres[:8]:
        assert await s.get(*p) is not None
    assert len(s) == 8 and db.fetch_calls == 8

    await s.get(*centres[8])
    # 8 -> oldest 4 dropped -> +1 = 5
    assert len(s) == 5
    assert not s.is_cached(*centres[0])
    assert s.is_cached(*centres[7]) and s.is_cached(*centres[8])

    # The dropped tile's cell is unknown again and is re-fetched on demand
    assert await s.get(*centres[0]) is not None
    assert db.fetch_calls == 10
    assert len(s) == 6


async def test_db_error_falls_back_to_none_and_backs_off():
    db = FakeDb(tiles_around(*OHL, n=1), fail=True)
    s = svc(db, backoff_s=60)

    assert await s.get(*OHL) is None
    assert s.db_errors == 1
    # Not cached (unknown, not "no data") - but paused, so no query either
    assert not s.is_cached(*OHL)
    db.fail = False
    assert await s.get(*OHL) is None
    assert db.fetch_calls == 1

    # Backoff over: DB is used again
    s._db_paused_until = 0.0
    assert await s.get(*OHL) is not None
    assert db.fetch_calls == 2


async def test_lookup_timeout_is_treated_like_a_db_error():
    db = FakeDb(tiles_around(*OHL, n=1), delay=0.2)
    s = svc(db, backoff_s=60, lookup_timeout_s=0.01)

    assert await s.get(*OHL) is None
    assert s.db_errors == 1
    assert not s.is_cached(*OHL)
    # Paused: even a fast DB is not asked during the backoff
    db.delay = 0.0
    assert await s.get(*OHL) is None
    assert db.fetch_calls == 1

    s._db_paused_until = 0.0
    assert await s.get(*OHL) is not None


# ---------------------------------------------------------------------------
# warm(): one query per airfield, known cells, isolation from the beacon path
# ---------------------------------------------------------------------------

def test_cells_inside_circle_are_fully_within_the_radius():
    lat, lon = OHL
    cells = cells_inside_circle(lat, lon, 8.0)
    assert coarse_cell(lat, lon) in cells
    m_per_deg_lon = M_PER_DEG_LAT * cos(radians(lat))
    for cell in cells:
        xmin, ymin, xmax, ymax = cell_bounds(cell)
        for px, py in ((xmin, ymin), (xmax, ymax), (xmin, ymax), (xmax, ymin)):
            dx = (px - lon) * m_per_deg_lon
            dy = (py - lat) * M_PER_DEG_LAT
            assert (dx * dx + dy * dy) ** 0.5 <= 8000.0
    # Roughly pi r^2 / cell area (~10), minus the ragged border
    cell_m2 = (CELL_DEG * M_PER_DEG_LAT) * (CELL_DEG * m_per_deg_lon)
    assert 0.3 * 3.14159 * 8000 ** 2 / cell_m2 < len(cells) <= 3.14159 * 8000 ** 2 / cell_m2
    assert cells_inside_circle(lat, lon, 0) == []
    # A 0.05 deg cell has a ~6.7 km diagonal: nothing fits in a 3 km circle
    assert cells_inside_circle(lat, lon, 3.0) == []


async def test_warm_loads_all_tiles_in_radius_with_one_query():
    db = FakeDb(tiles_around(*OHL, n=3))  # 49 tiles, ~0.37 deg square
    s = svc(db)

    loaded = await s.warm(*OHL, radius_km=8.0)

    assert db.fetch_calls == 1
    assert db.warm_queries == [(OHL[1], OHL[0], 8000.0)]
    assert 9 <= loaded < 49
    assert len(s) == loaded
    assert s.cells_known == len(cells_inside_circle(*OHL, 8.0)) > 0

    # Lookups inside the circle are served from memory
    for dlat, dlon in ((0, 0), (0.02, 0.03), (-0.03, -0.01)):
        assert await s.get(OHL[0] + dlat, OHL[1] + dlon) is not None
    assert db.fetch_calls == 1
    assert s.misses == 0 and s.hits == 3

    # A second warm-up returns the same tiles but adds nothing
    assert await s.warm(*OHL, radius_km=8.0) == loaded
    assert len(s) == loaded and db.fetch_calls == 2


async def test_warm_marks_uncovered_cells_inside_the_circle_negative():
    db = FakeDb([])  # no tiles at all
    s = svc(db)

    assert await s.warm(*OHL, radius_km=8.0) == 0
    assert len(s) == 0 and s.cells_known > 0
    # Inside the circle: negative without a query; far outside: one query
    assert await s.get(*OHL) is None
    assert db.fetch_calls == 1
    assert await s.get(OHL[0] + 0.5, OHL[1]) is None
    assert db.fetch_calls == 2


async def test_warm_airfields_runs_once_per_airfield_position():
    class Cfg:
        def __init__(self, lat, lon):
            self.latitude, self.longitude = lat, lon

    db = FakeDb(tiles_around(*OHL, n=2))
    s = svc(db)
    configs = {"a": Cfg(*OHL), "b": Cfg(47.7288, 12.436)}

    await s.warm_airfields(configs, radius_km=2.0)
    assert db.fetch_calls == 2
    assert len(s._warmed) == 2

    # Reload with the same airfields: nothing new
    await s.warm_airfields(configs, radius_km=2.0)
    assert db.fetch_calls == 2

    # A new airfield is warmed
    configs["c"] = Cfg(48.0, 11.5)
    await s.warm_airfields(configs, radius_km=2.0)
    assert db.fetch_calls == 3


async def test_warm_survives_db_error_without_pausing_beacon_lookups():
    db = FakeDb(tiles_around(*OHL, n=1), fail=True)
    s = svc(db)
    assert await s.warm(*OHL, radius_km=2.0) == 0
    assert s.warm_errors == 1
    assert s.db_errors == 0
    assert len(s) == 0 and s.cells_known == 0

    # The beacon path is untouched: a working DB is queried right away
    db.fail = False
    assert await s.get(*OHL) is not None
    assert db.fetch_calls == 2


async def test_warm_timeout_aborts_warmup_but_not_beacon_lookups():
    db = FakeDb(tiles_around(*OHL, n=1), delay=0.2)
    s = svc(db, warm_timeout_s=0.01, lookup_timeout_s=1.0)

    assert await s.warm(*OHL, radius_km=2.0) == 0
    assert db.fetch_calls == 1
    assert s.warm_errors == 1 and s.db_errors == 0
    assert len(s) == 0

    # Per-beacon lookups keep working (own timeout, no backoff)
    db.delay = 0.0
    assert await s.get(*OHL) is not None
    assert db.fetch_calls == 2


async def test_warm_airfields_marks_airfield_only_after_error_free_run():
    class Cfg:
        def __init__(self, lat, lon):
            self.latitude, self.longitude = lat, lon

    db = FakeDb(tiles_around(*OHL, n=1), fail=True)
    s = svc(db)
    configs = {"a": Cfg(*OHL), "b": Cfg(47.7288, 12.436)}

    await s.warm_airfields(configs, radius_km=2.0)
    # First airfield failed -> run aborted, second one not even attempted
    assert db.fetch_calls == 1
    assert s.warm_errors == 1 and s.db_errors == 0
    assert s._warmed == set()

    # Next reload (DB back): both airfields are warmed and remembered
    db.fail = False
    await s.warm_airfields(configs, radius_km=2.0)
    assert db.fetch_calls == 3
    assert len(s._warmed) == 2
    await s.warm_airfields(configs, radius_km=2.0)
    assert db.fetch_calls == 3


async def test_warm_larger_than_cache_is_truncated_to_half_the_bound():
    db = FakeDb(tiles_around(*OHL, n=3))  # 49 tiles
    s = svc(db, max_tiles=10)
    loaded = await s.warm(*OHL, radius_km=30.0)
    assert loaded == 5
    assert len(s) == 5


async def test_clear_forgets_tiles_cells_and_warmed_airfields():
    db = FakeDb(tiles_around(*OHL, n=1))
    s = svc(db)
    await s.warm(*OHL, radius_km=2.0)
    assert await s.get(*OHL) is not None
    s.clear()
    assert len(s) == 0 and s.cells_known == 0 and not s.is_cached(*OHL)
    with pytest.raises(AssertionError):
        # sanity: FakeDb rejects unknown queries (guards the fake itself)
        await db.fetch("SELECT 1")
