"""import_elevation tool: tile naming, area coverage, skip-existing plan (no network)."""

from app.tools.import_elevation import (
    plan_tiles,
    tile_name,
    tile_url,
    tiles_for_area,
)


def test_tile_name_hemispheres():
    assert tile_name(47, 11) == "Copernicus_DSM_COG_30_N47_00_E011_00_DEM"
    assert tile_name(0, 0) == "Copernicus_DSM_COG_30_N00_00_E000_00_DEM"
    # Southern / western tiles are named by their lower-left corner
    assert tile_name(-34, 151) == "Copernicus_DSM_COG_30_S34_00_E151_00_DEM"
    assert tile_name(51, -1) == "Copernicus_DSM_COG_30_N51_00_W001_00_DEM"
    assert tile_name(-1, -73) == "Copernicus_DSM_COG_30_S01_00_W073_00_DEM"


def test_tile_url_pattern():
    name = tile_name(47, 11)
    assert tile_url(name, "https://copernicus-dem-90m.s3.amazonaws.com/") == (
        "https://copernicus-dem-90m.s3.amazonaws.com/"
        "Copernicus_DSM_COG_30_N47_00_E011_00_DEM/"
        "Copernicus_DSM_COG_30_N47_00_E011_00_DEM.tif"
    )


def test_tiles_for_area_ohlstadt_small_radius_is_single_tile():
    # Ohlstadt is 0.24 deg (~18 km) east of the E010/E011 border
    assert tiles_for_area(47.6386, 11.2394, 10) == [
        "Copernicus_DSM_COG_30_N47_00_E011_00_DEM"
    ]
    assert tiles_for_area(47.6386, 11.2394, 20) == [
        "Copernicus_DSM_COG_30_N47_00_E010_00_DEM",
        "Copernicus_DSM_COG_30_N47_00_E011_00_DEM",
    ]


def test_tiles_for_area_crosses_tile_borders():
    # 60 km around Ohlstadt: 47.1..48.2 N, 10.4..12.0 E -> 2 x 2 (or 3) tiles
    names = tiles_for_area(47.6386, 11.2394, 60)
    assert "Copernicus_DSM_COG_30_N47_00_E011_00_DEM" in names
    assert "Copernicus_DSM_COG_30_N47_00_E010_00_DEM" in names
    assert "Copernicus_DSM_COG_30_N48_00_E011_00_DEM" in names
    assert "Copernicus_DSM_COG_30_N48_00_E010_00_DEM" in names
    assert 4 <= len(names) <= 6
    assert names == sorted(set(names))


def test_tiles_for_area_negative_lon_and_southern_hemisphere():
    # Sydney area, 30 km: lat -33.9 -> tiles S34 (and S33 when crossing)
    names = tiles_for_area(-33.95, 151.18, 30)
    assert "Copernicus_DSM_COG_30_S34_00_E151_00_DEM" in names
    assert all(n.startswith("Copernicus_DSM_COG_30_S3") for n in names)

    # Lasham (UK), 60 km: crosses the Greenwich meridian -> W001 and E000
    names = tiles_for_area(51.19, -1.03, 60)
    assert "Copernicus_DSM_COG_30_N51_00_W001_00_DEM" in names
    assert "Copernicus_DSM_COG_30_N51_00_W002_00_DEM" in names
    assert "Copernicus_DSM_COG_30_N50_00_W001_00_DEM" in names
    # 1.03 W + 0.86 deg = 0.17 W: E000 not reached
    assert "Copernicus_DSM_COG_30_N51_00_E000_00_DEM" not in names


def test_tiles_for_area_wraps_antimeridian():
    names = tiles_for_area(-17.7, 179.9, 40)
    assert "Copernicus_DSM_COG_30_S18_00_E179_00_DEM" in names
    assert "Copernicus_DSM_COG_30_S18_00_W180_00_DEM" in names


def test_plan_tiles_skips_existing_unless_forced():
    airfields = [
        {"slug": "ohlstadt", "latitude": 47.6587, "longitude": 11.235},
        {"slug": "dassu", "latitude": 47.7288, "longitude": 12.436},
    ]
    existing = {"Copernicus_DSM_COG_30_N47_00_E011_00_DEM"}

    to_fetch, skipped = plan_tiles(airfields, 10, existing, force=False)
    assert skipped == ["Copernicus_DSM_COG_30_N47_00_E011_00_DEM"]
    assert to_fetch == ["Copernicus_DSM_COG_30_N47_00_E012_00_DEM"]

    to_fetch, skipped = plan_tiles(airfields, 10, existing, force=True)
    assert skipped == []
    assert to_fetch == [
        "Copernicus_DSM_COG_30_N47_00_E011_00_DEM",
        "Copernicus_DSM_COG_30_N47_00_E012_00_DEM",
    ]


def test_plan_tiles_dedups_shared_tiles():
    # Two airfields in the same tile -> wanted once
    airfields = [
        {"slug": "a", "latitude": 47.6, "longitude": 11.2},
        {"slug": "b", "latitude": 47.7, "longitude": 11.3},
    ]
    to_fetch, skipped = plan_tiles(airfields, 5, set(), force=False)
    assert to_fetch == ["Copernicus_DSM_COG_30_N47_00_E011_00_DEM"]
    assert skipped == []


class _AnalyzeDb:
    def __init__(self):
        self.executed: list[str] = []

    async def execute(self, query, *args):
        self.executed.append(query)


async def test_run_import_analyzes_table_only_after_new_tiles(monkeypatch):
    from app.tools import import_elevation as mod

    db = _AnalyzeDb()
    monkeypatch.setattr(mod, "get_db", lambda: db)

    async def airfields(_db, _slug):
        return [{"slug": "a", "latitude": 47.6, "longitude": 11.2}]

    async def existing(_db):
        return set()

    async def download(_client, _name):
        return b"tif"

    async def store(_db, _name, _data, replace):
        return 361

    monkeypatch.setattr(mod, "load_airfields", airfields)
    monkeypatch.setattr(mod, "existing_tiles", existing)
    monkeypatch.setattr(mod, "download_tile", download)
    monkeypatch.setattr(mod, "store_tile", store)

    summary = await mod.run_import(None, 5.0, force=False)
    assert summary.imported == ["Copernicus_DSM_COG_30_N47_00_E011_00_DEM"]
    assert db.executed == ["ANALYZE elevation_tiles"]

    # Everything already present: no ANALYZE
    async def existing_all(_db):
        return {"Copernicus_DSM_COG_30_N47_00_E011_00_DEM"}

    monkeypatch.setattr(mod, "existing_tiles", existing_all)
    db.executed.clear()
    summary = await mod.run_import(None, 5.0, force=False)
    assert summary.skipped_existing == ["Copernicus_DSM_COG_30_N47_00_E011_00_DEM"]
    assert db.executed == []
