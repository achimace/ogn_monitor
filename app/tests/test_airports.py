"""AirportIndex: grid lookup, radius limit, empty-index fallback, loader (fake DB)."""

from math import cos, radians

import pytest

from app.tracking.airports import (
    CELL_DEG,
    LOAD_SQL,
    Airport,
    AirportIndex,
    airport_from_row,
    grid_cell,
)
from app.tracking.flight_state_machine import _bounded_put
from tests.conftest import AF_LAT, AF_LON, make_config, pos


def ap(ident: str, east_m: float, north_m: float = 0.0, elev: float | None = 600.0,
       name: str | None = None, icao: str = "") -> Airport:
    lat, lon = pos(east_m, north_m)
    return Airport(ident=ident, name=name or ident, latitude=lat, longitude=lon,
                   elevation_m=elev, icao_code=icao)


def test_grid_cell_is_floor_of_tenth_degree():
    assert grid_cell(47.64, 11.23) == (476, 112)
    assert grid_cell(-0.05, -0.05) == (-1, -1)
    assert CELL_DEG == 0.1


def test_nearest_returns_closest_within_radius():
    idx = AirportIndex()
    idx.add(ap("NEAR", 1500))
    idx.add(ap("FAR", 1900, 300))
    lat, lon = pos(0, 0)
    found = idx.nearest(lat, lon, 2000)
    assert found is not None and found.ident == "NEAR"
    assert len(idx) == 2
    assert bool(idx) is True


def test_nearest_respects_radius_limit():
    idx = AirportIndex()
    idx.add(ap("A", 2500))
    lat, lon = pos(0, 0)
    assert idx.nearest(lat, lon, 2000) is None
    assert idx.nearest(lat, lon, 3000).ident == "A"
    assert idx.nearest(lat, lon, 0) is None


def test_nearest_crosses_grid_cell_borders():
    """An airport just across a 0.1 deg border must be found (3x3 scan)."""
    idx = AirportIndex()
    # Position 200 m south of the 47.6 border, airport 200 m north of it
    lat0 = 47.6 - 200 / 111_320.0
    lat1 = 47.6 + 200 / 111_320.0
    idx.add(Airport(ident="B", name="Border", latitude=lat1, longitude=11.23, elevation_m=None))
    assert grid_cell(lat0, 11.23) != grid_cell(lat1, 11.23)
    assert idx.nearest(lat0, 11.23, 2000).ident == "B"


def test_nearest_with_large_radius_scans_more_cells():
    idx = AirportIndex()
    idx.add(ap("FAR", 25_000))
    lat, lon = pos(0, 0)
    assert idx.nearest(lat, lon, 2000) is None
    assert idx.nearest(lat, lon, 30_000).ident == "FAR"


def test_empty_index_is_falsy_and_returns_none():
    idx = AirportIndex()
    assert len(idx) == 0
    assert bool(idx) is False
    assert idx.nearest(AF_LAT, AF_LON, 5000) is None
    idx.add(ap("X", 100))
    idx.clear()
    assert idx.nearest(AF_LAT, AF_LON, 5000) is None


def test_display_name_appends_icao_code_once():
    assert ap("EDMU", 0, name="Gundelfingen", icao="EDMU").display_name == "Gundelfingen (EDMU)"
    # ICAO-looking ident without icao_code column
    assert ap("EDPU", 0, name="Unterwoessen Airfield").display_name == "Unterwoessen Airfield (EDPU)"
    # Synthetic ident: name only
    assert ap("DE-0123", 0, name="Wiese").display_name == "Wiese"
    # Code already part of the name is not repeated
    assert ap("EDMU", 0, name="Gundelfingen EDMU", icao="EDMU").display_name == "Gundelfingen EDMU"


def test_airport_from_row_handles_nulls():
    a = airport_from_row({
        "ident": "DE-0001", "icao_code": None, "name": None, "type": None,
        "latitude": 47.0, "longitude": 11.0, "elevation_m": None, "municipality": None,
    })
    assert a.name == "DE-0001"
    assert a.elevation_m is None
    assert a.icao_code == "" and a.type == "" and a.municipality == ""


class FakeDb:
    """Answers LOAD_SQL with rows within the requested radius of the point."""

    def __init__(self, rows: list[dict], fail: bool = False):
        self.rows = rows
        self.fail = fail
        self.calls: list[tuple] = []

    async def fetch(self, sql: str, *args):
        assert sql == LOAD_SQL
        assert len(args) == 3
        self.calls.append(args)
        if self.fail:
            raise RuntimeError("relation \"airports\" does not exist")
        lon, lat, radius_m = args
        m_per_deg_lon = 111_320.0 * cos(radians(lat))
        out = []
        for r in self.rows:
            dx = (r["longitude"] - lon) * m_per_deg_lon
            dy = (r["latitude"] - lat) * 111_320.0
            if (dx * dx + dy * dy) ** 0.5 <= radius_m:
                out.append(r)
        return out


def _row(ident: str, east_m: float, north_m: float = 0.0) -> dict:
    lat, lon = pos(east_m, north_m)
    return {"ident": ident, "icao_code": None, "name": ident, "type": "small_airport",
            "latitude": lat, "longitude": lon, "elevation_m": 600.0, "municipality": None}


async def test_load_for_airfields_queries_each_airfield_and_dedupes():
    rows = [_row("A", 5_000), _row("B", 20_000), _row("C", 400_000)]
    db = FakeDb(rows)
    idx = AirportIndex(db, radius_km=300)
    cfg1 = make_config(slug="one")
    cfg2 = make_config(slug="two", latitude=AF_LAT + 0.05)
    n = await idx.load_for_airfields({"one": cfg1, "two": cfg2})
    assert n == 2  # A and B (twice) merged by ident, C out of range
    assert len(db.calls) == 2
    assert db.calls[0] == (AF_LON, AF_LAT, 300_000.0)
    lat, lon = pos(0, 0)
    assert idx.nearest(lat, lon, 6000).ident == "A"
    assert idx.loaded_at > 0


async def test_load_failure_keeps_previous_index():
    idx = AirportIndex(FakeDb([], fail=True))
    idx.add(ap("OLD", 100))
    n = await idx.load_for_airfields({"one": make_config()})
    assert n == 1
    assert idx.load_errors == 1
    assert idx.nearest(AF_LAT, AF_LON, 2000).ident == "OLD"


async def test_reload_replaces_index_atomically():
    db = FakeDb([_row("A", 1000)])
    idx = AirportIndex(db, radius_km=10)
    await idx.load_for_airfields({"one": make_config()})
    assert idx.nearest(AF_LAT, AF_LON, 2000).ident == "A"
    db.rows = [_row("B", 1000)]
    await idx.load_for_airfields({"one": make_config()})
    assert idx.nearest(AF_LAT, AF_LON, 2000).ident == "B"
    assert len(idx) == 1


def test_bounded_put_evicts_oldest():
    d: dict[str, int] = {}
    for i in range(5):
        _bounded_put(d, f"k{i}", i, 3)
    assert list(d) == ["k2", "k3", "k4"]
    # Updating an existing key does not evict
    _bounded_put(d, "k3", 33, 3)
    assert list(d) == ["k2", "k3", "k4"] and d["k3"] == 33
    # Degenerate bound still holds one entry
    e: dict[str, int] = {}
    _bounded_put(e, "a", 1, 0)
    _bounded_put(e, "b", 2, 0)
    assert list(e) == ["b"]
