"""Ignore-list API: /api/airfields/{id}/ignored-aircraft (hermetic, fake DB / Redis).

Auth (401), tenant ownership (403 / 404), validation (422), duplicates
(409), uppercasing, the tracker:config Redis signal and its best-effort
nature.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import airfields as airfields_api
from app.api import ignored_aircraft as api
from app.dependencies import get_current_user
from app.tracking.redis_keys import TRACKER_CONFIG_CHANNEL

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
AIRFIELD_ID = UUID("33333333-3333-3333-3333-333333333333")
FOREIGN_AIRFIELD_ID = UUID("44444444-4444-4444-4444-444444444444")
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


class FakeDb:
    """Just enough asyncpg for the router and _verify_airfield_ownership."""

    def __init__(self):
        self.rows: dict[tuple[UUID, str], dict] = {}
        self.queries: list[tuple[str, tuple]] = []
        self.airfields = {
            AIRFIELD_ID: {"id": AIRFIELD_ID, "tenant_id": TENANT, "slug": "ohlstadt",
                          "name": "Ohlstadt", "home_polygon": None},
            FOREIGN_AIRFIELD_ID: {"id": FOREIGN_AIRFIELD_ID, "tenant_id": OTHER_TENANT,
                                  "slug": "elsewhere", "name": "X", "home_polygon": None},
        }

    async def fetchrow(self, sql: str, *args):
        self.queries.append((sql, args))
        if "FROM airfields" in sql:
            return self.airfields.get(args[0])
        if sql.lstrip().startswith("INSERT INTO airfield_ignored_aircraft"):
            airfield_id, flarm_id, note = args
            key = (airfield_id, flarm_id)
            if key in self.rows:
                return None                       # ON CONFLICT DO NOTHING
            row = {"id": uuid4(), "flarm_id": flarm_id, "note": note, "created_at": NOW}
            self.rows[key] = row
            return row
        if sql.lstrip().startswith("DELETE FROM airfield_ignored_aircraft"):
            row = self.rows.pop((args[0], args[1]), None)
            return {"id": row["id"]} if row else None
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(self, sql: str, *args):
        self.queries.append((sql, args))
        assert "FROM airfield_ignored_aircraft" in sql
        return [r for (af, _), r in self.rows.items() if af == args[0]]


class FakeRedis:
    def __init__(self, fail=False):
        self.fail = fail
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, payload):
        if self.fail:
            raise ConnectionError("redis gone")
        self.published.append((channel, payload))
        return 1


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(api, "get_db", lambda: db)
    monkeypatch.setattr(airfields_api, "get_db", lambda: db)
    return db


@pytest.fixture
def fake_redis():
    return FakeRedis()


def _app(fake_redis, user: dict | None) -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[api.redis_client] = lambda: fake_redis
    return app


@pytest.fixture
async def client(fake_db, fake_redis):
    app = _app(fake_redis, {"tenant_id": TENANT, "email": "t@example.invalid"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.fixture
async def anon_client(fake_db, fake_redis):
    app = _app(fake_redis, None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


BASE = f"/api/airfields/{AIRFIELD_ID}/ignored-aircraft"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_post_creates_item_uppercased_and_lists_it(client, fake_db, fake_redis):
    resp = await client.post(BASE, json={"flarm_id": "3e0abc", "note": " Rettungsheli Murnau "})
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["flarmId"] == "3E0ABC"
    assert item["note"] == "Rettungsheli Murnau"
    assert item["createdAt"].startswith("2026-09-25T12:00:00")
    UUID(item["id"])
    assert set(item) == {"id", "flarmId", "note", "createdAt"}

    resp = await client.get(BASE)
    assert resp.status_code == 200
    assert resp.json() == [item]
    # parametrised, airfield-scoped
    sql, args = fake_db.queries[-1]
    assert "WHERE airfield_id = $1" in sql and args == (AIRFIELD_ID,)


async def test_post_without_note(client):
    resp = await client.post(BASE, json={"flarm_id": "DDA5BA"})
    assert resp.status_code == 201
    assert resp.json()["note"] is None


async def test_delete_removes_item_case_insensitively(client, fake_db):
    await client.post(BASE, json={"flarm_id": "DDA5BA"})
    resp = await client.delete(f"{BASE}/dda5ba")
    assert resp.status_code == 204 and resp.content == b""
    assert fake_db.rows == {}
    assert (await client.get(BASE)).json() == []


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

async def test_duplicate_is_409(client):
    assert (await client.post(BASE, json={"flarm_id": "DDA5BA"})).status_code == 201
    resp = await client.post(BASE, json={"flarm_id": "dda5ba", "note": "again"})
    assert resp.status_code == 409
    assert "DDA5BA" in resp.json()["detail"]


async def test_delete_unknown_is_404(client):
    resp = await client.delete(f"{BASE}/ABCDEF")
    assert resp.status_code == 404


@pytest.mark.parametrize("body", [
    {"flarm_id": "ZZZZ"},          # not hex
    {"flarm_id": "ABC"},           # too short
    {"flarm_id": "A" * 17},        # too long
    {"flarm_id": ""},
    {},
    {"flarm_id": "DDA5BA", "note": "x" * 121},
])
async def test_invalid_body_is_422(client, body):
    assert (await client.post(BASE, json=body)).status_code == 422


async def test_unauthenticated_is_401(anon_client, fake_db):
    assert (await anon_client.get(BASE)).status_code == 401
    assert (await anon_client.post(BASE, json={"flarm_id": "DDA5BA"})).status_code == 401
    assert (await anon_client.delete(f"{BASE}/DDA5BA")).status_code == 401
    assert fake_db.rows == {}


async def test_foreign_tenant_airfield_is_403(client, fake_db):
    base = f"/api/airfields/{FOREIGN_AIRFIELD_ID}/ignored-aircraft"
    assert (await client.get(base)).status_code == 403
    assert (await client.post(base, json={"flarm_id": "DDA5BA"})).status_code == 403
    assert (await client.delete(f"{base}/DDA5BA")).status_code == 403
    assert fake_db.rows == {}


async def test_unknown_airfield_is_404(client):
    base = f"/api/airfields/{uuid4()}/ignored-aircraft"
    assert (await client.get(base)).status_code == 404
    assert (await client.post(base, json={"flarm_id": "DDA5BA"})).status_code == 404


# ---------------------------------------------------------------------------
# Worker signal (tracker:config)
# ---------------------------------------------------------------------------

async def test_post_and_delete_publish_airfield_slug(client, fake_redis):
    await client.post(BASE, json={"flarm_id": "DDA5BA"})
    await client.delete(f"{BASE}/DDA5BA")
    assert fake_redis.published == [(TRACKER_CONFIG_CHANNEL, "ohlstadt")] * 2
    assert TRACKER_CONFIG_CHANNEL == "tracker:config"


async def test_failed_requests_do_not_publish(client, fake_redis):
    await client.post(BASE, json={"flarm_id": "DDA5BA"})
    await client.post(BASE, json={"flarm_id": "DDA5BA"})      # 409
    await client.delete(f"{BASE}/ABCDEF")                      # 404
    await client.post(BASE, json={"flarm_id": "ZZZZ"})         # 422
    assert len(fake_redis.published) == 1


async def test_write_succeeds_when_redis_signal_fails(client, fake_redis, fake_db):
    fake_redis.fail = True
    resp = await client.post(BASE, json={"flarm_id": "DDA5BA"})
    assert resp.status_code == 201
    assert len(fake_db.rows) == 1
    assert (await client.delete(f"{BASE}/DDA5BA")).status_code == 204
    assert fake_redis.published == []


async def test_publish_helper_is_best_effort():
    assert await api.publish_tracker_config_changed(None, "ohlstadt") is False
    assert await api.publish_tracker_config_changed(FakeRedis(fail=True), "x") is False
    ok = FakeRedis()
    assert await api.publish_tracker_config_changed(ok, "ohlstadt") is True
    assert ok.published == [(TRACKER_CONFIG_CHANNEL, "ohlstadt")]
