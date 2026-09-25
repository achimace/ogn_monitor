"""GET /api/monitor/{slug}/today against a fake Redis / fake DB.

Archived flights (flight_log) must carry the foreign-airfield / visitor
fields with the same string encoding as the live entries from Redis
(FlightState.to_redis_dict), so the SPA can treat both sources alike.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

import app.db.connection as db_connection
from app.api import monitor as monitor_api

SLUG = "test"
AIRFIELD_ID = 7


class FakePipeline:
    def __init__(self, redis: "FakeRedis"):
        self._redis = redis
        self._keys: list[str] = []

    def hgetall(self, key: str):
        self._keys.append(key)
        return self

    async def execute(self):
        return [dict(self._redis.hashes.get(k, {})) for k in self._keys]


class FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}

    async def smembers(self, key: str) -> set:
        return set(self.sets.get(key, set()))

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)


class FakeDb:
    def __init__(self):
        self.rows: list[dict] = []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args):
        assert "FROM airfields" in sql
        if args[0] != SLUG:
            return None
        return {"id": AIRFIELD_ID, "slug": SLUG, "latitude": 47.6, "longitude": 11.2,
                "elevation_m": 660, "landed_visible_minutes": 120,
                "monitor_strip_fields": ["registration"]}

    async def fetch(self, sql: str, *args):
        assert "FROM flight_log" in sql
        self.fetch_calls.append((sql, args))
        return list(self.rows)


def _log_row(**overrides) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    row = {
        "flarm_id": "DDA5BA", "registration": "D-1234", "competition_sign": "XY",
        "aircraft_model": "LS4", "takeoff_time": now - timedelta(hours=2),
        "landing_time": now - timedelta(minutes=30), "flight_duration_s": 5400,
        "max_altitude_m": 2100, "max_distance_m": 42000, "launch_type": "winch",
        "landing_type": "home", "takeoff_airfield": "Heimat",
        "landing_airfield": "Heimat", "is_visitor": False,
    }
    row.update(overrides)
    return row


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def fake_db():
    return FakeDb()


@pytest.fixture
async def client(fake_redis, fake_db, monkeypatch):
    monkeypatch.setattr(monitor_api, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(db_connection, "get_db", lambda: fake_db)
    app = FastAPI()
    app.include_router(monitor_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        yield c


async def test_archived_flights_carry_airfield_and_visitor_fields(client, fake_db):
    fake_db.rows = [
        _log_row(),
        _log_row(flarm_id="VIS001", registration="D-KXYZ", launch_type="unknown",
                 takeoff_airfield="Unterwoessen Airfield (EDPU)", is_visitor=True),
        _log_row(flarm_id="OUT001", landing_type="outlanding",
                 takeoff_airfield=None, landing_airfield=None, is_visitor=None),
    ]
    r = await client.get(f"/api/monitor/{SLUG}/today")
    assert r.status_code == 200
    by_fid = {f["flarmId"]: f for f in r.json()["flights"]}
    assert set(by_fid) == {"DDA5BA", "VIS001", "OUT001"}
    home = by_fid["DDA5BA"]
    assert home["source"] == "archived" and home["status"] == "3"
    assert home["takeoffAirfield"] == "Heimat"
    assert home["landingAirfield"] == "Heimat"
    assert home["isVisitor"] == "0"
    visitor = by_fid["VIS001"]
    assert visitor["takeoffAirfield"] == "Unterwoessen Airfield (EDPU)"
    assert visitor["isVisitor"] == "1"
    out = by_fid["OUT001"]
    assert out["landingType"] == "outlanding"
    assert out["takeoffAirfield"] == "" and out["landingAirfield"] == ""
    assert out["isVisitor"] == "0"
    # The columns are selected (parametrised query, no literals)
    sql, args = fake_db.fetch_calls[0]
    assert "takeoff_airfield, landing_airfield, is_visitor" in sql
    assert args == (AIRFIELD_ID, "120")


async def test_live_entry_wins_over_archived_and_keeps_redis_encoding(client, fake_redis, fake_db):
    fake_redis.sets[f"flights:{SLUG}"] = {"DDA5BA"}
    fake_redis.hashes[f"flight:{SLUG}:DDA5BA"] = {
        "flarm_id": "DDA5BA", "status": "2", "takeoff_time": "2026-09-25T09:00:00Z",
        "takeoff_airfield": "Heimat", "landing_airfield": "", "is_visitor": "0",
    }
    fake_db.rows = [_log_row(is_visitor=True, takeoff_airfield="Anderswo")]
    r = await client.get(f"/api/monitor/{SLUG}/today")
    assert r.status_code == 200
    flights = r.json()["flights"]
    assert len(flights) == 1
    assert flights[0]["source"] == "live"
    assert flights[0]["isVisitor"] == "0" and flights[0]["takeoffAirfield"] == "Heimat"


async def test_unknown_airfield_is_404(client):
    r = await client.get("/api/monitor/nope/today")
    assert r.status_code == 404
