"""GET /api/monitor/{slug}/flights/{flarm_id}/track against a fake Redis.

The app's Redis client runs with decode_responses=True, so XRANGE returns
``(str_id, {str: str})`` tuples - the fake mirrors that.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI

import app.db.connection as db_connection
from app.api import monitor as monitor_api

SLUG = "test"
FID = "DDA5BA"


class FakeRedis:
    def __init__(self, streams: dict[str, list[tuple[str, dict]]] | None = None,
                 active_airfields: set[str] | None = None):
        self.streams = streams or {}
        self.active_airfields = active_airfields or set()
        self.xrange_calls: list[tuple[str, str, str]] = []

    async def xrange(self, key: str, min: str = "-", max: str = "+", count=None):
        self.xrange_calls.append((key, min, max))
        lo = int(min.split("-")[0]) if min != "-" else 0
        return [
            (eid, fields) for eid, fields in self.streams.get(key, [])
            if int(eid.split("-")[0]) >= lo
        ]

    async def sismember(self, key: str, member: str) -> bool:
        return key == "active_airfields" and member in self.active_airfields


class FakeDb:
    def __init__(self, slugs: set[str]):
        self.slugs = slugs

    async def fetchrow(self, sql: str, *args):
        return {"slug": args[0]} if args[0] in self.slugs else None


def _entry(ts: datetime, **fields) -> tuple[str, dict]:
    base = {"lat": "47.6", "lon": "11.2", "alt": "1200", "agl": "540",
            "speed": "95", "vs": "1.2", "track": "180"}
    base.update({k: str(v) for k, v in fields.items()})
    return (f"{int(ts.timestamp() * 1000)}-0", base)


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def fake_redis(now):
    return FakeRedis(streams={
        f"track:{SLUG}:{FID}": [
            _entry(now - timedelta(hours=23), alt=700, agl=40, speed=60, vs=2.5, track=90),
            _entry(now - timedelta(hours=2)),
            _entry(now - timedelta(minutes=5), alt=1500, agl=840, vs=-0.4),
        ],
    })


@pytest.fixture
async def client(fake_redis, monkeypatch):
    monkeypatch.setattr(monitor_api, "get_redis", lambda: fake_redis)
    monkeypatch.setattr(db_connection, "get_db", lambda: FakeDb({"in-db-only"}))
    app = FastAPI()
    app.include_router(monitor_api.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        yield c


async def test_track_returns_points_ascending_as_numbers(client, fake_redis, now):
    r = await client.get(f"/api/monitor/{SLUG}/flights/{FID}/track")
    assert r.status_code == 200
    body = r.json()
    assert body["airfield"] == SLUG
    assert body["flarmId"] == FID
    assert set(body) == {"airfield", "flarmId", "since", "points"}
    assert len(body["points"]) == 3

    first = body["points"][0]
    assert first == {
        "t": (now - timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lat": 47.6, "lon": 11.2, "alt": 700, "agl": 40,
        "speed": 60, "vs": 2.5, "track": 90,
    }
    assert isinstance(first["lat"], float) and isinstance(first["alt"], int)
    times = [p["t"] for p in body["points"]]
    assert times == sorted(times)

    # window: since ~ now - 24h, stream read from that ms id up to "+"
    since = datetime.strptime(body["since"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((now - timedelta(hours=24)) - since) <= timedelta(seconds=5)
    key, lo, hi = fake_redis.xrange_calls[0]
    assert key == f"track:{SLUG}:{FID}" and hi == "+"
    assert lo.endswith("-0") and abs(int(lo[:-2]) - int(since.timestamp() * 1000)) < 5000


async def test_hours_limits_the_window(client, now):
    r = await client.get(f"/api/monitor/{SLUG}/flights/{FID}/track", params={"hours": 3})
    assert r.status_code == 200
    assert len(r.json()["points"]) == 2
    r = await client.get(f"/api/monitor/{SLUG}/flights/{FID}/track", params={"hours": 0.5})
    assert len(r.json()["points"]) == 1


@pytest.mark.parametrize("hours", ["0", "0.05", "25", "-1", "abc"])
async def test_hours_out_of_bounds_is_422(client, hours):
    r = await client.get(f"/api/monitor/{SLUG}/flights/{FID}/track", params={"hours": hours})
    assert r.status_code == 422


async def test_hours_upper_bound_follows_track_retention(client):
    from app.config import settings

    assert monitor_api._TRACK_MAX_HOURS == settings.track_retention_s / 3600
    r = await client.get(f"/api/monitor/{SLUG}/flights/{FID}/track",
                         params={"hours": monitor_api._TRACK_MAX_HOURS})
    assert r.status_code == 200


async def test_flarm_id_is_uppercased(client):
    r = await client.get(f"/api/monitor/{SLUG}/flights/{FID.lower()}/track")
    assert r.status_code == 200
    assert r.json()["flarmId"] == FID
    assert len(r.json()["points"]) == 3


async def test_unknown_aircraft_at_known_airfield_is_empty(client, fake_redis):
    fake_redis.active_airfields.add(SLUG)
    r = await client.get(f"/api/monitor/{SLUG}/flights/NOPE01/track")
    assert r.status_code == 200
    assert r.json()["points"] == []


async def test_airfield_only_in_db_is_empty_not_404(client):
    r = await client.get("/api/monitor/in-db-only/flights/NOPE01/track")
    assert r.status_code == 200
    assert r.json() == {
        "airfield": "in-db-only", "flarmId": "NOPE01",
        "since": r.json()["since"], "points": [],
    }


async def test_unknown_airfield_is_404(client):
    r = await client.get("/api/monitor/nowhere/flights/NOPE01/track")
    assert r.status_code == 404
