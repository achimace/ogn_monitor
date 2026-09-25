"""ElevationService: cache behaviour, warm-up grid, DB fallback (no network, fake DB)."""

import asyncio
from math import cos, pi, radians

import pytest

from app.tracking.elevation import (
    CELL_DEG,
    M_PER_DEG_LAT,
    ElevationService,
    cache_key,
    grid_points,
)


class FakeDb:
    """asyncpg.Pool stand-in: ``values`` maps cache cells to elevations."""

    def __init__(self, values: dict[tuple[float, float], float | None] | None = None,
                 fail: bool = False, delay: float = 0.0):
        self.values = values or {}
        self.fail = fail
        # Simulated query duration (seconds) - for the timeout paths
        self.delay = delay
        self.point_queries: list[tuple[float, float]] = []
        self.batch_sizes: list[int] = []
        self.fetch_calls = 0

    async def fetchval(self, query, lon, lat):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("db down")
        self.point_queries.append((lat, lon))
        return self.values.get(cache_key(lat, lon))

    async def fetch(self, query, lons, lats):
        self.fetch_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("db down")
        self.batch_sizes.append(len(lons))
        return [
            {"i": i + 1, "elev": self.values.get(cache_key(lat, lon))}
            for i, (lon, lat) in enumerate(zip(lons, lats))
        ]


OHL = (47.6386, 11.2394)


def svc(db: FakeDb, **kw) -> ElevationService:
    kw.setdefault("enabled", True)
    kw.setdefault("backoff_s", 60)
    return ElevationService(db, **kw)


# ---------------------------------------------------------------------------
# get(): cache hit / miss / negative cache
# ---------------------------------------------------------------------------

async def test_miss_queries_db_once_then_hits_cache():
    db = FakeDb({cache_key(*OHL): 676.0})
    s = svc(db)

    assert await s.get(*OHL) == 676.0
    assert await s.get(*OHL) == 676.0
    # Same ~100 m cell: no second query
    assert await s.get(OHL[0] + 0.0004, OHL[1] - 0.0004) == 676.0

    assert len(db.point_queries) == 1
    assert s.misses == 1 and s.hits == 2


async def test_no_tile_is_cached_as_negative_entry():
    db = FakeDb({})
    s = svc(db)

    assert await s.get(*OHL) is None
    assert await s.get(*OHL) is None

    assert len(db.point_queries) == 1
    assert s.is_cached(*OHL)
    assert s.peek(*OHL) is None


async def test_disabled_service_never_touches_db_or_cache():
    db = FakeDb({cache_key(*OHL): 676.0})
    s = svc(db, enabled=False)

    assert await s.get(*OHL) is None
    assert db.point_queries == []
    assert len(s) == 0
    assert await s.warm(*OHL, radius_km=1) == 0


async def test_cache_is_bounded_and_drops_oldest_half():
    values = {}
    points = [(47.0 + i * CELL_DEG, 11.0) for i in range(10)]
    for p in points:
        values[cache_key(*p)] = float(100 + p[0])
    s = svc(FakeDb(values), max_entries=8)

    for p in points[:8]:
        await s.get(*p)
    assert len(s) == 8

    await s.get(*points[8])
    # 8 -> trimmed to 4 oldest dropped -> +1 = 5
    assert len(s) == 5
    assert not s.is_cached(*points[0])
    assert s.is_cached(*points[7])
    assert s.is_cached(*points[8])


async def test_db_error_falls_back_to_none_and_backs_off():
    db = FakeDb(fail=True)
    s = svc(db, backoff_s=60)

    assert await s.get(*OHL) is None
    assert s.db_errors == 1
    # Not cached (unknown, not "no data") - but paused, so no query either
    assert not s.is_cached(*OHL)
    db.fail = False
    db.values[cache_key(*OHL)] = 676.0
    assert await s.get(*OHL) is None
    assert db.point_queries == []

    # Backoff over: DB is used again
    s._db_paused_until = 0.0
    assert await s.get(*OHL) == 676.0


async def test_lookup_timeout_is_treated_like_a_db_error():
    db = FakeDb({cache_key(*OHL): 676.0}, delay=0.2)
    s = svc(db, backoff_s=60, lookup_timeout_s=0.01)

    assert await s.get(*OHL) is None
    assert s.db_errors == 1
    assert not s.is_cached(*OHL)
    # Paused: even a fast DB is not asked during the backoff
    db.delay = 0.0
    assert await s.get(*OHL) is None
    assert db.point_queries == []

    s._db_paused_until = 0.0
    assert await s.get(*OHL) == 676.0


# ---------------------------------------------------------------------------
# warm(): grid size and batching
# ---------------------------------------------------------------------------

def test_grid_points_cover_the_circle_once_per_cell():
    lat, lon = OHL
    radius_km, step_m = 2.0, 100.0
    pts = grid_points(lat, lon, radius_km, step_m)

    # Roughly pi r^2 / step^2 points (grid inside a circle), unique cells
    expected = pi * (radius_km * 1000) ** 2 / step_m ** 2
    assert 0.7 * expected < len(pts) < 1.05 * expected
    assert len(pts) == len(set(pts))
    assert all(p == cache_key(*p) for p in pts)

    # All within the radius (with one cell of slack for the snapping)
    m_per_deg_lon = M_PER_DEG_LAT * cos(radians(lat))
    for plat, plon in pts:
        dy = (plat - lat) * M_PER_DEG_LAT
        dx = (plon - lon) * m_per_deg_lon
        assert (dx * dx + dy * dy) ** 0.5 <= radius_km * 1000 + 120


def test_grid_points_step_larger_than_cell_leaves_gaps():
    pts_100 = grid_points(*OHL, 1.0, 100.0)
    pts_200 = grid_points(*OHL, 1.0, 200.0)
    assert len(pts_200) < len(pts_100)
    assert len(pts_200) == pytest.approx(len(pts_100) / 4, rel=0.15)


def test_grid_points_degenerate_inputs():
    assert grid_points(*OHL, 0, 100) == []
    assert grid_points(*OHL, 1, 0) == []
    assert grid_points(*OHL, 0.05, 100) == [cache_key(*OHL)]


async def test_warm_fills_cache_in_batches_and_skips_cached(monkeypatch):
    from app.tracking import elevation as mod
    monkeypatch.setattr(mod.settings, "terrain_warm_batch", 50)

    pts = grid_points(*OHL, 1.0, 100.0)
    values = {p: 600.0 + i for i, p in enumerate(pts)}
    values[pts[0]] = None  # one NODATA cell inside the area
    db = FakeDb(values)
    s = svc(db)

    filled = await s.warm(*OHL, radius_km=1.0, step_m=100.0)

    assert filled == len(pts)
    assert len(s) == len(pts)
    assert all(size <= 50 for size in db.batch_sizes)
    assert sum(db.batch_sizes) == len(pts)
    assert s.peek(*pts[1]) == 601.0
    assert s.is_cached(*pts[0]) and s.peek(*pts[0]) is None

    # Everything cached: a second warm queries nothing
    db.batch_sizes.clear()
    assert await s.warm(*OHL, radius_km=1.0, step_m=100.0) == 0
    assert db.batch_sizes == []
    # And get() is served from the cache
    assert await s.get(*pts[2]) == 602.0
    assert db.point_queries == []


async def test_warm_airfields_runs_once_per_airfield_position(monkeypatch):
    from app.tracking import elevation as mod
    monkeypatch.setattr(mod.settings, "terrain_warm_step_m", 200)

    class Cfg:
        def __init__(self, lat, lon):
            self.latitude, self.longitude = lat, lon

    db = FakeDb({})
    s = svc(db)
    configs = {"a": Cfg(*OHL), "b": Cfg(47.7288, 12.436)}

    await s.warm_airfields(configs, radius_km=0.5)
    n_batches = len(db.batch_sizes)
    assert n_batches >= 2

    # Reload with the same airfields: nothing new
    await s.warm_airfields(configs, radius_km=0.5)
    assert len(db.batch_sizes) == n_batches

    # A new airfield is warmed
    configs["c"] = Cfg(48.0, 11.5)
    await s.warm_airfields(configs, radius_km=0.5)
    assert len(db.batch_sizes) > n_batches


async def test_warm_survives_db_error_without_pausing_beacon_lookups():
    db = FakeDb(fail=True)
    s = svc(db)
    assert await s.warm(*OHL, radius_km=0.5, step_m=100.0) == 0
    assert s.warm_errors == 1
    assert s.db_errors == 0
    assert len(s) == 0

    # The beacon path is untouched: a working DB is queried right away
    db.fail = False
    db.values[cache_key(*OHL)] = 676.0
    assert await s.get(*OHL) == 676.0
    assert len(db.point_queries) == 1


async def test_warm_timeout_aborts_warmup_but_not_beacon_lookups(monkeypatch):
    from app.tracking import elevation as mod
    monkeypatch.setattr(mod.settings, "terrain_warm_batch", 20)

    pts = grid_points(*OHL, 0.5, 100.0)
    db = FakeDb({p: 650.0 for p in pts}, delay=0.2)
    s = svc(db, warm_timeout_s=0.01, lookup_timeout_s=1.0)

    assert await s.warm(*OHL, radius_km=0.5, step_m=100.0) == 0
    # First batch timed out, the remaining batches were skipped
    assert db.fetch_calls == 1
    assert s.warm_errors == 1 and s.db_errors == 0
    assert len(s) == 0

    # Per-beacon lookups keep working (own timeout, no backoff)
    db.delay = 0.0
    assert await s.get(*pts[0]) == 650.0
    assert db.point_queries == [pts[0]]


async def test_warm_airfields_marks_airfield_only_after_error_free_run(monkeypatch):
    from app.tracking import elevation as mod
    monkeypatch.setattr(mod.settings, "terrain_warm_step_m", 200)

    class Cfg:
        def __init__(self, lat, lon):
            self.latitude, self.longitude = lat, lon

    db = FakeDb({}, fail=True)
    s = svc(db)
    configs = {"a": Cfg(*OHL), "b": Cfg(47.7288, 12.436)}

    await s.warm_airfields(configs, radius_km=0.5)
    # First airfield failed -> run aborted, second one not even attempted
    assert db.fetch_calls == 1
    assert s.warm_errors == 1 and s.db_errors == 0
    assert s._warmed == set()

    # Next reload (DB back): both airfields are warmed and remembered
    db.fail = False
    await s.warm_airfields(configs, radius_km=0.5)
    assert db.fetch_calls > 1
    assert len(s._warmed) == 2
    calls = db.fetch_calls
    await s.warm_airfields(configs, radius_km=0.5)
    assert db.fetch_calls == calls
