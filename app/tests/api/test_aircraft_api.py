"""Tenant aircraft API: /api/airfields/{id}/aircraft (hermetic, fake DB / Redis).

Focus: after every successful write (POST, PUT, DELETE, CSV import) the
airfield slug is published on ``tracker:config`` so the worker reloads
its aircraft cache within seconds - and the signal stays best effort
(failed requests do not publish, a Redis error never fails the write).
Pattern taken from tests/api/test_ignored_aircraft_api.py.
"""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import aircraft as api
from app.api import airfields as airfields_api
from app.dependencies import get_current_user
from app.tracking.redis_keys import TRACKER_CONFIG_CHANNEL

TENANT = UUID("11111111-1111-1111-1111-111111111111")
OTHER_TENANT = UUID("22222222-2222-2222-2222-222222222222")
AIRFIELD_ID = UUID("33333333-3333-3333-3333-333333333333")
FOREIGN_AIRFIELD_ID = UUID("44444444-4444-4444-4444-444444444444")
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class FakeDb:
    """Just enough asyncpg for the router and _verify_airfield_ownership."""

    def __init__(self):
        self.rows: dict[tuple[UUID, str], dict] = {}
        self.airfields = {
            AIRFIELD_ID: {"id": AIRFIELD_ID, "tenant_id": TENANT, "slug": "ohlstadt",
                          "name": "Ohlstadt", "home_polygon": None},
            FOREIGN_AIRFIELD_ID: {"id": FOREIGN_AIRFIELD_ID, "tenant_id": OTHER_TENANT,
                                  "slug": "elsewhere", "name": "X", "home_polygon": None},
        }

    def _row(self, airfield_id: UUID, flarm_id: str, args) -> dict:
        return {
            "id": uuid4(), "airfield_id": airfield_id, "flarm_id": flarm_id,
            "registration": args[2], "competition_sign": args[3],
            "aircraft_model": args[4], "aircraft_type": args[5],
            "is_active": True, "created_at": NOW,
        }

    async def fetchrow(self, sql: str, *args):
        stripped = sql.lstrip()
        if "FROM airfields" in sql:
            return self.airfields.get(args[0])
        if stripped.startswith("SELECT") and "FROM tenant_aircraft" in sql:
            return self.rows.get((args[0], args[1]))          # AIRCRAFT_BY_FLARM_ID
        if stripped.startswith("INSERT INTO tenant_aircraft") and "ON CONFLICT" in sql:
            row = self._row(args[0], args[1], args)           # AIRCRAFT_UPSERT
            self.rows[(args[0], args[1])] = row
            return row
        if stripped.startswith("INSERT INTO tenant_aircraft"):
            row = self._row(args[0], args[1], args)           # AIRCRAFT_INSERT
            self.rows[(args[0], args[1])] = row
            return row
        if stripped.startswith("UPDATE tenant_aircraft"):
            row = self.rows.get((args[0], args[1]))
            if row is None:
                return None
            row.update(registration=args[2], competition_sign=args[3],
                       aircraft_model=args[4], aircraft_type=args[5])
            return row
        raise AssertionError(f"unexpected fetchrow: {sql}")

    async def fetch(self, sql: str, *args):
        assert "FROM tenant_aircraft" in sql
        return [r for (af, _), r in self.rows.items() if af == args[0]]

    async def execute(self, sql: str, *args):
        assert sql.lstrip().startswith("DELETE FROM tenant_aircraft")
        row = self.rows.pop((args[0], args[1]), None)
        return "DELETE 0" if row is None else "DELETE 1"


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


@pytest.fixture
async def client(fake_db, fake_redis):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = (
        lambda: {"tenant_id": TENANT, "email": "t@example.invalid"}
    )
    app.dependency_overrides[api.redis_client] = lambda: fake_redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


BASE = f"/api/airfields/{AIRFIELD_ID}/aircraft"
SIGNAL = (TRACKER_CONFIG_CHANNEL, "ohlstadt")
CSV_FILE = {"file": ("fleet.csv", b"flarm_id,registration\nDDA5BA,D-1234\n", "text/csv")}


def _post_body(flarm_id="DDA5BA", registration="D-1234") -> dict:
    return {"flarm_id": flarm_id, "registration": registration}


# ---------------------------------------------------------------------------
# Worker signal (tracker:config) on every successful write
# ---------------------------------------------------------------------------

async def test_post_publishes_airfield_slug(client, fake_redis):
    resp = await client.post(BASE, json=_post_body())
    assert resp.status_code == 201, resp.text
    assert fake_redis.published == [SIGNAL]
    assert TRACKER_CONFIG_CHANNEL == "tracker:config"


async def test_put_publishes_airfield_slug(client, fake_redis):
    await client.post(BASE, json=_post_body())
    resp = await client.put(f"{BASE}/DDA5BA", json={"registration": "D-5678"})
    assert resp.status_code == 200, resp.text
    assert fake_redis.published == [SIGNAL] * 2


async def test_delete_publishes_airfield_slug(client, fake_redis):
    await client.post(BASE, json=_post_body())
    resp = await client.delete(f"{BASE}/dda5ba")
    assert resp.status_code == 204
    assert fake_redis.published == [SIGNAL] * 2


async def test_csv_import_publishes_airfield_slug_once(client, fake_db, fake_redis):
    csv_body = b"flarm_id,registration\nDDA5BA,D-1234\n3E0ABC,D-KXYZ\n"
    resp = await client.post(
        f"{BASE}/import-csv",
        files={"file": ("fleet.csv", csv_body, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["imported"] == 2
    assert len(fake_db.rows) == 2
    # one signal for the whole import, not one per row
    assert fake_redis.published == [SIGNAL]


async def test_csv_import_without_valid_rows_does_not_publish(client, fake_redis):
    csv_body = b"flarm_id,registration\nDDA5BA,\n"      # registration missing
    resp = await client.post(
        f"{BASE}/import-csv",
        files={"file": ("fleet.csv", csv_body, "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["imported"] == 0
    assert fake_redis.published == []


async def test_failed_requests_do_not_publish(client, fake_redis):
    await client.post(BASE, json=_post_body())
    assert (await client.post(BASE, json=_post_body())).status_code == 409
    assert (await client.put(f"{BASE}/ABCDEF", json={"registration": "D"})).status_code == 422
    assert (await client.put(
        f"{BASE}/ABCDEF", json={"registration": "D-9999"})).status_code == 404
    assert (await client.delete(f"{BASE}/ABCDEF")).status_code == 404
    assert (await client.post(BASE, json={"flarm_id": "ZZZZ"})).status_code == 422
    assert (await client.post(
        f"{BASE}/import-csv", files={"file": ("x.txt", b"a", "text/plain")},
    )).status_code == 400
    assert fake_redis.published == [SIGNAL]            # only the first POST


async def test_write_succeeds_when_redis_signal_fails(client, fake_db, fake_redis):
    fake_redis.fail = True
    assert (await client.post(BASE, json=_post_body())).status_code == 201
    assert (await client.put(
        f"{BASE}/DDA5BA", json={"registration": "D-5678"})).status_code == 200
    assert (await client.post(f"{BASE}/import-csv", files=CSV_FILE)).status_code == 200
    assert (await client.delete(f"{BASE}/DDA5BA")).status_code == 204
    assert fake_redis.published == []
    assert fake_db.rows == {}


# ---------------------------------------------------------------------------
# CRUD sanity (response shape, uppercasing, list)
# ---------------------------------------------------------------------------

async def test_post_creates_aircraft_and_lists_it(client, fake_db):
    resp = await client.post(BASE, json={
        "flarm_id": "dda5ba", "registration": "D-1234",
        "competition_sign": "WX", "aircraft_model": "ASK 21",
        "aircraft_type": "glider",
    })
    assert resp.status_code == 201, resp.text
    item = resp.json()
    assert item["flarm_id"] == "DDA5BA"                # uppercased by the schema
    assert item["registration"] == "D-1234"
    assert item["aircraft_model"] == "ASK 21"
    assert (AIRFIELD_ID, "DDA5BA") in fake_db.rows

    resp = await client.get(BASE)
    assert resp.status_code == 200
    assert [r["flarm_id"] for r in resp.json()] == ["DDA5BA"]


async def test_foreign_tenant_airfield_is_403_and_silent(client, fake_db, fake_redis):
    base = f"/api/airfields/{FOREIGN_AIRFIELD_ID}/aircraft"
    assert (await client.post(base, json=_post_body())).status_code == 403
    assert (await client.put(
        f"{base}/DDA5BA", json={"registration": "D-1"})).status_code == 403
    assert (await client.delete(f"{base}/DDA5BA")).status_code == 403
    assert (await client.post(f"{base}/import-csv", files=CSV_FILE)).status_code == 403
    assert fake_db.rows == {} and fake_redis.published == []


async def test_unknown_airfield_is_404(client, fake_redis):
    base = f"/api/airfields/{uuid4()}/aircraft"
    assert (await client.post(base, json=_post_body())).status_code == 404
    assert fake_redis.published == []
