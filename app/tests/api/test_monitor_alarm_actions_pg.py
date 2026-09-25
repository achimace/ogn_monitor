"""Alarm-action history against a real PostgreSQL (skipped without one).

Covers what the fake DB in test_monitor_alarm_actions.py cannot: the SQL
of the history endpoints (newest first, UTC day filter, tenant isolation)
and the migration's CHECK constraints. One asyncpg connection per test,
transaction rolled back afterwards (pattern of test_vfsync_api.py).
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

import app.db.connection as db_connection
from app.api import monitor as monitor_api
from app.config import settings
from app.dependencies import get_current_user

CONNECT_TIMEOUT_S = 2.0
FID = "DDA5BA"
FID2 = "DD1234"
EMAIL = "fl@example.invalid"
DAY = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _db_url() -> str:
    url = os.environ.get("VFSYNC_TEST_DATABASE_URL") or settings.database_url
    return url.replace("postgresql+asyncpg://", "postgresql://")


class NoHotStateRedis:
    async def hgetall(self, key: str) -> dict:
        return {}


@pytest.fixture
async def conn():
    asyncpg = pytest.importorskip("asyncpg")
    try:
        connection = await asyncio.wait_for(asyncpg.connect(_db_url()), CONNECT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - any failure -> skip
        pytest.skip(f"PostgreSQL not reachable: {type(exc).__name__}")
    tx = connection.transaction()
    await tx.start()
    try:
        yield connection
    finally:
        await tx.rollback()
        await connection.close()


async def _make_tenant_with_airfield(conn) -> SimpleNamespace:
    tag = uuid4().hex[:10]
    name = f"alarm-test-{tag}"
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (name, slug, email, password_hash) VALUES ($1, $2, $3, $4)"
        " RETURNING id",
        name, f"alarm-test-{tag}", f"alarm-{tag}@example.invalid", "x",
    )
    slug = f"test-{tag}"
    airfield_id = await conn.fetchval(
        "INSERT INTO airfields (tenant_id, name, slug, latitude, longitude, elevation_m)"
        " VALUES ($1, $2, $3, 47.64, 11.23, 660) RETURNING id",
        tenant_id, f"Test {tag}", slug,
    )
    return SimpleNamespace(tenant_id=tenant_id, airfield_id=airfield_id, slug=slug,
                           name=name)


@pytest.fixture
async def own(conn):
    return await _make_tenant_with_airfield(conn)


@pytest.fixture
async def foreign(conn):
    return await _make_tenant_with_airfield(conn)


@pytest.fixture
async def client(conn, own, monkeypatch):
    monkeypatch.setattr(monitor_api, "get_redis", lambda: NoHotStateRedis())
    monkeypatch.setattr(db_connection, "get_db", lambda: conn)
    app = FastAPI()
    app.include_router(monitor_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "tenant_id": own.tenant_id, "email": EMAIL, "email_verified": True,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        yield c


async def _insert(conn, airfield_id, flarm_id: str, state: str, created_at: datetime,
                  kind: str = "alarm", comment: str | None = None) -> None:
    await conn.execute(
        "INSERT INTO flight_alarm_actions"
        " (airfield_id, flarm_id, flight_takeoff_ts, alarm_kind, state, comment, set_by, created_at)"
        " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
        airfield_id, flarm_id, DAY + timedelta(hours=8), kind, state, comment, EMAIL, created_at,
    )


async def test_post_round_trip_persists_row(client, conn, own):
    r = await client.post(f"/api/monitor/{own.slug}/flights/{FID}/actions",
                          json={"state": "acknowledged", "comment": "Pilot erreicht",
                                "alarm_kind": "signal_lost"})
    assert r.status_code == 201, r.text
    action = r.json()["action"]
    # public label resolved from tenants.name via the airfield row
    assert r.json()["alarmState"]["alarmSetBy"] == own.name

    row = await conn.fetchrow("SELECT * FROM flight_alarm_actions WHERE id = $1", UUID(action["id"]))
    assert row["airfield_id"] == own.airfield_id
    assert row["flarm_id"] == FID
    assert row["alarm_kind"] == "signal_lost"
    assert row["state"] == "acknowledged"
    assert row["comment"] == "Pilot erreicht"
    assert row["set_by"] == EMAIL
    assert row["flight_takeoff_ts"] is None
    assert action["createdAt"] == row["created_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    r = await client.get(f"/api/monitor/{own.slug}/flights/{FID}/actions")
    assert r.status_code == 200
    assert r.json()["count"] == 1 and r.json()["items"][0]["id"] == action["id"]


async def test_history_is_newest_first_and_filtered_by_utc_day(client, conn, own):
    await _insert(conn, own.airfield_id, FID, "acknowledged", DAY + timedelta(hours=9))
    await _insert(conn, own.airfield_id, FID, "retrieval_underway", DAY + timedelta(hours=11),
                  comment="Rückholer unterwegs")
    await _insert(conn, own.airfield_id, FID, "resolved", DAY + timedelta(hours=23, minutes=59))
    # outside the day (previous / next UTC day) and another aircraft
    await _insert(conn, own.airfield_id, FID, "false_alarm", DAY - timedelta(seconds=1))
    await _insert(conn, own.airfield_id, FID, "false_alarm", DAY + timedelta(days=1))
    await _insert(conn, own.airfield_id, FID2, "acknowledged", DAY + timedelta(hours=10))

    r = await client.get(f"/api/monitor/{own.slug}/flights/{FID}/actions",
                         params={"date": "2026-05-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert [i["state"] for i in body["items"]] == ["resolved", "retrieval_underway", "acknowledged"]
    assert body["items"][1]["comment"] == "Rückholer unterwegs"
    assert body["items"][0]["createdAt"] == "2026-05-01T23:59:00Z"
    assert body["items"][0]["flightTakeoffTs"] == "2026-05-01T08:00:00Z"
    assert body["items"][0]["alarmKind"] == "alarm" and body["items"][0]["setBy"] == EMAIL

    # neighbouring days only see their own rows
    r = await client.get(f"/api/monitor/{own.slug}/flights/{FID}/actions",
                         params={"date": "2026-04-30"})
    assert [i["createdAt"] for i in r.json()["items"]] == ["2026-04-30T23:59:59Z"]
    r = await client.get(f"/api/monitor/{own.slug}/flights/{FID}/actions",
                         params={"date": "2026-05-02"})
    assert r.json()["count"] == 1


async def test_airfield_history_spans_all_aircraft(client, conn, own, foreign):
    await _insert(conn, own.airfield_id, FID, "acknowledged", DAY + timedelta(hours=9))
    await _insert(conn, own.airfield_id, FID2, "resolved", DAY + timedelta(hours=10))
    await _insert(conn, foreign.airfield_id, FID, "acknowledged", DAY + timedelta(hours=11))

    r = await client.get(f"/api/monitor/{own.slug}/actions", params={"date": "2026-05-01"})
    assert r.status_code == 200
    assert [i["state"] for i in r.json()["items"]] == ["resolved", "acknowledged"]
    assert r.json()["count"] == 2


async def test_cross_tenant_history_is_403(client, conn, own, foreign):
    await _insert(conn, foreign.airfield_id, FID, "acknowledged", DAY + timedelta(hours=9))
    r = await client.get(f"/api/monitor/{foreign.slug}/flights/{FID}/actions",
                         params={"date": "2026-05-01"})
    assert r.status_code == 403
    r = await client.get(f"/api/monitor/{foreign.slug}/actions", params={"date": "2026-05-01"})
    assert r.status_code == 403
    r = await client.post(f"/api/monitor/{foreign.slug}/flights/{FID}/actions",
                          json={"state": "acknowledged"})
    assert r.status_code == 403
    assert await conn.fetchval(
        "SELECT count(*) FROM flight_alarm_actions WHERE airfield_id = $1", foreign.airfield_id
    ) == 1


async def test_default_day_is_today_utc(client, conn, own):
    now = datetime.now(timezone.utc)
    await _insert(conn, own.airfield_id, FID, "acknowledged", now - timedelta(days=1))
    await _insert(conn, own.airfield_id, FID, "resolved",
                  datetime(now.year, now.month, now.day, tzinfo=timezone.utc))
    r = await client.get(f"/api/monitor/{own.slug}/flights/{FID}/actions")
    assert [i["state"] for i in r.json()["items"]] == ["resolved"]


@pytest.mark.parametrize("kind,state", [("fire", "acknowledged"), ("alarm", "done")])
async def test_check_constraints_reject_unknown_enums(conn, own, kind, state):
    asyncpg = pytest.importorskip("asyncpg")
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert(conn, own.airfield_id, FID, state, DAY, kind=kind)
