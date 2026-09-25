"""AP-10: /api/vfsync/* against a real PostgreSQL (skipped without one).

A minimal FastAPI app includes only the vfsync router. The JWT dependency
is overridden with a fixed tenant, the DB dependency with one asyncpg
connection whose transaction is rolled back after every test (same
pattern as tests/vfsync/test_stores_pg.py), Redis with a tiny fake.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import vfsync as vfsync_api
from app.config import settings
from app.dependencies import get_current_user
from app.vfsync import crypto
from app.vfsync.models import AuditEntry, Session, SessionState
from app.vfsync.stores_pg import PgAuditStore, PgSessionStore

CONNECT_TIMEOUT_S = 2.0
# 2025-05-01 10:00Z = 12:00 Europe/Berlin (local day 2025-05-01)
T0 = datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc)
# 2025-05-01 22:30Z = 00:30 Europe/Berlin on 2025-05-02
T_LATE = datetime(2025, 5, 1, 22, 30, tzinfo=timezone.utc)
MD5 = "5f4dcc3b5aa765d61d8327deb882cf99"


def _db_url() -> str:
    url = os.environ.get("VFSYNC_TEST_DATABASE_URL") or settings.database_url
    return url.replace("postgresql+asyncpg://", "postgresql://")


class FakeRedis:
    def __init__(self, data: dict | None = None, fail_publish: bool = False) -> None:
        self.data = data or {}
        self.fail_publish = fail_publish
        self.published: list[tuple[str, str]] = []

    async def hgetall(self, key: str) -> dict:
        return dict(self.data) if key == vfsync_api.VFSYNC_HEALTH_KEY else {}

    async def publish(self, channel: str, payload: str) -> int:
        if self.fail_publish:
            raise ConnectionError("redis gone")
        self.published.append((channel, payload))
        return 1


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
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (name, slug, email, password_hash) VALUES ($1, $2, $3, $4)"
        " RETURNING id",
        f"vfapi-test-{tag}", f"vfapi-test-{tag}", f"vfapi-{tag}@example.invalid", "x",
    )
    airfield_id = await conn.fetchval(
        "INSERT INTO airfields (tenant_id, name, slug, latitude, longitude, elevation_m)"
        " VALUES ($1, $2, $3, 47.64, 11.23, 660) RETURNING id",
        tenant_id, f"Test {tag}", f"test-{tag}",
    )
    return SimpleNamespace(tenant_id=tenant_id, airfield_id=airfield_id)


@pytest.fixture
async def ids(conn):
    """Temporary tenant + airfield (rolled back with the transaction)."""
    return await _make_tenant_with_airfield(conn)


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
async def client(conn, ids, fake_redis):
    app = FastAPI()
    app.include_router(vfsync_api.router)
    app.dependency_overrides[get_current_user] = lambda: {
        "tenant_id": ids.tenant_id, "email": "t@example.invalid", "email_verified": True,
    }
    app.dependency_overrides[vfsync_api.db_executor] = lambda: conn
    app.dependency_overrides[vfsync_api.redis_client] = lambda: fake_redis
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        yield c


@pytest.fixture
def cred_key(monkeypatch):
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "vfsync_cred_key", key)
    return key


def mk(af, flarm="DDA5BA", takeoff=T0, **kw) -> Session:
    return Session(airfield_id=af, flarm_id=flarm, takeoff_ts=takeoff, **kw)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

async def test_config_404_without_row(client, ids):
    r = await client.get(f"/api/vfsync/config/{ids.airfield_id}")
    assert r.status_code == 404


async def test_config_unknown_airfield_404(client):
    r = await client.get(f"/api/vfsync/config/{uuid4()}")
    assert r.status_code == 404
    r = await client.put(f"/api/vfsync/config/{uuid4()}", json={"dry_run": True})
    assert r.status_code == 404


async def test_put_creates_row_with_encrypted_credentials(client, conn, ids, cred_key):
    body = {
        "dry_run": False,
        "vf_cid": 4711,
        "vf_username": "tech-user",
        "vf_password_md5": MD5,
        "vf_appkey": "app-key-secret",
        "flags": {"live_release": True},
        "daily_budget": 300,
    }
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["airfield_id"] == str(ids.airfield_id)
    assert data["enabled"] is False            # never set by the API
    assert data["dry_run"] is False
    assert data["vf_base_url"] == "https://www.vereinsflieger.de"
    assert data["vf_cid"] == 4711
    assert data["vf_username"] == "tech-user"
    assert data["has_password"] is True
    assert data["has_appkey"] is True
    assert data["flags"] == {"live_release": True, "live_touchgo": False,
                             "auto_create": False, "join_towflights": False}
    assert data["daily_budget"] == 300
    assert data["updated_at"] is not None
    # credentials never appear in the response
    assert MD5 not in r.text and "app-key-secret" not in r.text
    assert "vf_password" not in data and "vf_appkey" not in data

    row = await conn.fetchrow(
        "SELECT enabled, vf_password_enc, vf_appkey_enc FROM vf_sync_config"
        " WHERE airfield_id = $1", ids.airfield_id)
    assert row["enabled"] is False
    assert isinstance(row["vf_password_enc"], (bytes, memoryview))
    assert bytes(row["vf_password_enc"]) != MD5.encode()
    assert crypto.decrypt(row["vf_password_enc"]) == MD5
    assert crypto.decrypt(row["vf_appkey_enc"]) == "app-key-secret"

    # GET returns the same shape
    r = await client.get(f"/api/vfsync/config/{ids.airfield_id}")
    assert r.status_code == 200
    assert r.json() == data


async def test_put_signals_worker_via_redis(client, conn, ids, fake_redis, cred_key):
    """Every successful PUT publishes the slug on vfsync:config so the worker
    reloads immediately (UAT T-04); the GET does not."""
    slug = await conn.fetchval("SELECT slug FROM airfields WHERE id = $1", ids.airfield_id)
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json={"dry_run": True})
    assert r.status_code == 200, r.text
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json={"vf_appkey": "k"})
    assert r.status_code == 200, r.text
    assert fake_redis.published == [(vfsync_api.VFSYNC_CONFIG_CHANNEL, slug)] * 2

    await client.get(f"/api/vfsync/config/{ids.airfield_id}")
    assert len(fake_redis.published) == 2


async def test_put_succeeds_when_redis_signal_fails(client, ids, fake_redis):
    fake_redis.fail_publish = True
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json={"dry_run": True})
    assert r.status_code == 200, r.text
    assert fake_redis.published == []


async def test_publish_config_changed_is_best_effort():
    """No Redis (tests / not initialised) and Redis errors never raise."""
    assert await vfsync_api.publish_config_changed(None, "ohlstadt") is False
    assert await vfsync_api.publish_config_changed(FakeRedis(fail_publish=True), "x") is False
    ok = FakeRedis()
    assert await vfsync_api.publish_config_changed(ok, "ohlstadt") is True
    assert ok.published == [(vfsync_api.VFSYNC_CONFIG_CHANNEL, "ohlstadt")]


async def test_put_partial_update_keeps_other_fields(client, conn, ids, cred_key):
    await client.put(f"/api/vfsync/config/{ids.airfield_id}", json={
        "vf_username": "u1", "vf_password_md5": MD5, "vf_appkey": "k1",
        "flags": {"live_release": True, "auto_create": True}, "daily_budget": 200,
    })
    # enabled set by an operator directly in the DB must survive a PUT
    await conn.execute(
        "UPDATE vf_sync_config SET enabled = TRUE WHERE airfield_id = $1", ids.airfield_id)

    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json={
        "flags": {"auto_create": False}, "vf_cid": None, "dry_run": True,
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["enabled"] is True
    assert data["dry_run"] is True
    assert data["vf_username"] == "u1"
    assert data["has_password"] is True and data["has_appkey"] is True
    assert data["flags"]["live_release"] is True and data["flags"]["auto_create"] is False
    assert data["daily_budget"] == 200
    assert data["vf_cid"] is None
    assert crypto.decrypt(await conn.fetchval(
        "SELECT vf_appkey_enc FROM vf_sync_config WHERE airfield_id = $1", ids.airfield_id
    )) == "k1"

    # rotate only the appkey
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json={"vf_appkey": "k2"})
    assert r.status_code == 200
    assert crypto.decrypt(await conn.fetchval(
        "SELECT vf_appkey_enc FROM vf_sync_config WHERE airfield_id = $1", ids.airfield_id
    )) == "k2"
    assert crypto.decrypt(await conn.fetchval(
        "SELECT vf_password_enc FROM vf_sync_config WHERE airfield_id = $1", ids.airfield_id
    )) == MD5


async def test_put_rejects_enabled_and_unknown_flags(client, ids):
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json={"enabled": True})
    assert r.status_code == 422
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}",
                         json={"flags": {"live_release": True, "nuke": True}})
    assert r.status_code == 422
    assert "nuke" in r.text
    # nothing was created by the rejected requests
    r = await client.get(f"/api/vfsync/config/{ids.airfield_id}")
    assert r.status_code == 404


@pytest.mark.parametrize("body", [
    {"vf_password_md5": "not-a-hash"},
    {"vf_password_md5": MD5[:-1]},
    {"daily_budget": 0},
    {"daily_budget": 501},
    {"vf_base_url": "ftp://x"},
    {"vf_cid": 0},
])
async def test_put_validation_422(client, ids, body):
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json=body)
    assert r.status_code == 422, body


async def test_put_credentials_without_key_503(client, ids, monkeypatch):
    monkeypatch.setattr(settings, "vfsync_cred_key", "")
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json={"vf_appkey": "k"})
    assert r.status_code == 503
    assert "k" != r.json()["detail"]
    # non-credential updates still work without the key
    r = await client.put(f"/api/vfsync/config/{ids.airfield_id}", json={"dry_run": True})
    assert r.status_code == 200 and r.json()["has_appkey"] is False


async def test_foreign_airfield_403(client, conn):
    other = await _make_tenant_with_airfield(conn)
    af = other.airfield_id
    assert (await client.get(f"/api/vfsync/config/{af}")).status_code == 403
    assert (await client.put(f"/api/vfsync/config/{af}", json={"dry_run": True})).status_code == 403
    assert (await client.get(f"/api/vfsync/status/{af}")).status_code == 403
    assert (await client.get("/api/vfsync/sessions", params={"airfield_id": str(af)})).status_code == 403
    assert (await client.get("/api/vfsync/audit", params={"airfield_id": str(af)})).status_code == 403
    assert await conn.fetchval(
        "SELECT COUNT(*) FROM vf_sync_config WHERE airfield_id = $1", af) == 0


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

async def test_status_shape_without_config_and_worker(client, ids):
    r = await client.get(f"/api/vfsync/status/{ids.airfield_id}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["enabled"] is False and data["dry_run"] is True
    assert data["worker"] is None
    budget = data["budget"]
    assert budget["used"] == 0 and budget["daily_budget"] == 450
    assert budget["stage"] == "normal"
    assert datetime.strptime(budget["day"], "%Y-%m-%d")
    assert data["sessions"] == {"open": 0, "awaiting_match": 0, "review": 0,
                                "completed_today": 0}


async def test_status_counts_budget_and_worker(client, conn, ids, fake_redis, cred_key):
    await client.put(f"/api/vfsync/config/{ids.airfield_id}",
                     json={"dry_run": False, "daily_budget": 100})
    await conn.execute(
        "UPDATE vf_sync_config SET enabled = TRUE WHERE airfield_id = $1", ids.airfield_id)
    now = datetime.now(timezone.utc)
    store = PgSessionStore(conn)
    s_open = await store.upsert(mk(ids.airfield_id, flarm="A", takeoff=now - timedelta(hours=1)))
    s_wait = await store.upsert(mk(ids.airfield_id, flarm="B", takeoff=now - timedelta(hours=2)))
    s_wait.state = SessionState.AWAITING_MATCH
    await store.save(s_wait)
    s_rev = await store.upsert(mk(ids.airfield_id, flarm="C", takeoff=now - timedelta(hours=3)))
    s_rev.state = SessionState.REVIEW
    await store.save(s_rev)
    s_done = await store.upsert(mk(ids.airfield_id, flarm="D", takeoff=now - timedelta(minutes=5)))
    s_done.state = SessionState.COMPLETED
    await store.save(s_done)
    s_old = await store.upsert(mk(ids.airfield_id, flarm="E", takeoff=now - timedelta(days=40)))
    s_old.state = SessionState.COMPLETED
    await store.save(s_old)
    assert s_open.state == SessionState.TRACKING

    r = await client.get(f"/api/vfsync/status/{ids.airfield_id}")
    day = r.json()["budget"]["day"]
    await conn.execute(
        "INSERT INTO vf_sync_budget (airfield_id, day, used) VALUES ($1, $2::date, 65)",
        ids.airfield_id, datetime.strptime(day, "%Y-%m-%d").date())
    fake_redis.data = {"status": "ok", "last_event_ts": now.isoformat(),
                       "open_sessions": "3", "tenants": "x", "updated_at": now.isoformat()}

    r = await client.get(f"/api/vfsync/status/{ids.airfield_id}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["enabled"] is True and data["dry_run"] is False
    assert data["budget"] == {"day": day, "used": 65, "daily_budget": 100,
                              "stage": "no_live_departure"}
    assert data["sessions"] == {"open": 3, "awaiting_match": 1, "review": 1,
                                "completed_today": 1}
    assert data["worker"] == {"status": "ok", "last_event_ts": now.isoformat(),
                              "updated_at": now.isoformat()}


async def test_status_worker_empty_fields_become_null(client, ids, fake_redis):
    fake_redis.data = {"status": "starting", "last_event_ts": "", "updated_at": ""}
    r = await client.get(f"/api/vfsync/status/{ids.airfield_id}")
    assert r.json()["worker"] == {"status": "starting", "last_event_ts": None,
                                  "updated_at": None}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

async def test_sessions_date_filter_pagination_and_state(client, conn, ids):
    store = PgSessionStore(conn)
    for i in range(5):
        s = await store.upsert(mk(ids.airfield_id, flarm=f"F{i}",
                                  takeoff=T0 + timedelta(minutes=10 * i),
                                  registration=f"D-{i:04d}"))
        if i == 0:
            s.state = SessionState.COMPLETED
            s.landing_ts = T0 + timedelta(hours=1)
            s.matched_flid = 999
            s.conf_landing = 0.9
            await store.save(s)
    late = await store.upsert(mk(ids.airfield_id, flarm="LATE", takeoff=T_LATE))

    # local day 2025-05-01: 5 sessions, page size 2 -> 3 pages, newest first
    r = await client.get("/api/vfsync/sessions", params={
        "airfield_id": str(ids.airfield_id), "date": "2025-05-01", "page_size": 2})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total"] == 5 and data["page"] == 1 and data["pages"] == 3
    assert [i["flarm_id"] for i in data["items"]] == ["F4", "F3"]
    first = data["items"][0]
    assert set(first) == {
        "session_id", "airfield_id", "flarm_id", "registration", "takeoff_ts", "landing_ts",
        "landing_method", "start_type_detected", "tow_registration", "release_ts",
        "release_alt_agl_m", "release_method", "tow_time_min", "landing_count",
        "conf_pairing", "conf_landing", "conf_touchgo", "matched_flid", "state",
        "review_reason", "attempts", "last_attempt", "created_at", "updated_at",
    }
    assert first["state"] == "tracking"
    assert datetime.fromisoformat(first["takeoff_ts"]) == T0 + timedelta(minutes=40)
    assert UUID(first["session_id"])

    r = await client.get("/api/vfsync/sessions", params={
        "airfield_id": str(ids.airfield_id), "date": "2025-05-01", "page_size": 2, "page": 3})
    assert [i["flarm_id"] for i in r.json()["items"]] == ["F0"]
    done = r.json()["items"][0]
    assert done["state"] == "completed" and done["matched_flid"] == 999
    assert done["conf_landing"] == pytest.approx(0.9, abs=1e-6)
    assert datetime.fromisoformat(done["landing_ts"]) == T0 + timedelta(hours=1)

    # 22:30Z is already 2025-05-02 in Europe/Berlin
    r = await client.get("/api/vfsync/sessions", params={
        "airfield_id": str(ids.airfield_id), "date": "2025-05-02"})
    assert [i["session_id"] for i in r.json()["items"]] == [str(late.session_id)]

    # state filter
    r = await client.get("/api/vfsync/sessions", params={
        "airfield_id": str(ids.airfield_id), "date": "2025-05-01", "state": "completed"})
    assert r.json()["total"] == 1 and r.json()["items"][0]["flarm_id"] == "F0"
    r = await client.get("/api/vfsync/sessions", params={
        "airfield_id": str(ids.airfield_id), "date": "2025-05-01", "state": "bogus"})
    assert r.status_code == 422

    # default date = today (local) -> nothing from 2025
    r = await client.get("/api/vfsync/sessions", params={"airfield_id": str(ids.airfield_id)})
    assert r.status_code == 200 and r.json() == {"items": [], "total": 0, "page": 1, "pages": 1}

    # limits
    r = await client.get("/api/vfsync/sessions", params={
        "airfield_id": str(ids.airfield_id), "page_size": 201})
    assert r.status_code == 422
    r = await client.get("/api/vfsync/sessions")
    assert r.status_code == 422


async def test_sessions_are_tenant_scoped(client, conn, ids):
    other = await _make_tenant_with_airfield(conn)
    await PgSessionStore(conn).upsert(mk(other.airfield_id, flarm="X"))
    await PgSessionStore(conn).upsert(mk(ids.airfield_id, flarm="Y"))
    r = await client.get("/api/vfsync/sessions", params={
        "airfield_id": str(ids.airfield_id), "date": "2025-05-01"})
    assert [i["flarm_id"] for i in r.json()["items"]] == ["Y"]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

async def test_audit_listing(client, conn, ids):
    sessions = PgSessionStore(conn)
    audit = PgAuditStore(conn)
    s = await sessions.upsert(mk(ids.airfield_id))
    other = await _make_tenant_with_airfield(conn)
    for i in range(3):
        await audit.append(AuditEntry(
            airfield_id=ids.airfield_id, action="get", session_id=s.session_id,
            flid=100 + i, http_status=200, ts=T0 + timedelta(minutes=i),
            pre_state={"arrivaltime": ""}, detail=f"e{i}",
        ))
    await audit.append(AuditEntry(
        airfield_id=ids.airfield_id, action="abstain", ts=T0 + timedelta(hours=1),
        detail="no session",
    ))
    await audit.append(AuditEntry(airfield_id=other.airfield_id, action="get", flid=1))

    r = await client.get("/api/vfsync/audit", params={"airfield_id": str(ids.airfield_id)})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["action"] for i in items] == ["abstain", "get", "get", "get"]
    assert [i["flid"] for i in items] == [None, 102, 101, 100]
    assert set(items[1]) == {"id", "ts", "action", "session_id", "flid", "fields_sent",
                             "pre_state", "http_status", "detail"}
    assert items[1]["session_id"] == str(s.session_id)
    assert items[1]["pre_state"] == {"arrivaltime": ""}
    assert items[1]["fields_sent"] is None
    assert items[1]["http_status"] == 200
    assert datetime.fromisoformat(items[1]["ts"]) == T0 + timedelta(minutes=2)

    r = await client.get("/api/vfsync/audit", params={
        "airfield_id": str(ids.airfield_id), "session_id": str(s.session_id), "limit": 2})
    items = r.json()["items"]
    assert [i["flid"] for i in items] == [102, 101]

    r = await client.get("/api/vfsync/audit", params={
        "airfield_id": str(ids.airfield_id), "limit": 1001})
    assert r.status_code == 422
