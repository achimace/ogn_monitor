"""AP-3: asyncpg stores against a real PostgreSQL (skipped without one).

Connects with settings.database_url (VFSYNC_TEST_DATABASE_URL overrides).
Every test runs inside one transaction that is rolled back; a temporary
tenant + airfield row provides the FK target.
"""

import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.config import settings
from app.vfsync import crypto
from app.vfsync.models import AuditEntry, Session, SessionState
from app.vfsync.stores_pg import (
    PgAuditStore,
    PgBudgetStore,
    PgConfigStore,
    PgSessionStore,
    make_flight_row_fetcher,
)

T0 = datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc)
CONNECT_TIMEOUT_S = 2.0


def _db_url() -> str:
    url = os.environ.get("VFSYNC_TEST_DATABASE_URL") or settings.database_url
    return url.replace("postgresql+asyncpg://", "postgresql://")


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


@pytest.fixture
async def airfield_id(conn):
    """Temporary tenant + airfield (rolled back with the transaction)."""
    tag = uuid4().hex[:10]
    tenant_id = await conn.fetchval(
        "INSERT INTO tenants (name, slug, email, password_hash) VALUES ($1, $2, $3, $4)"
        " RETURNING id",
        f"vfsync-test-{tag}", f"vfsync-test-{tag}", f"vfsync-{tag}@example.invalid", "x",
    )
    af_id = await conn.fetchval(
        "INSERT INTO airfields (tenant_id, name, slug, latitude, longitude, elevation_m)"
        " VALUES ($1, $2, $3, 47.64, 11.23, 660) RETURNING id",
        tenant_id, f"Test {tag}", f"test-{tag}",
    )
    return af_id


def mk(af, flarm="DDA5BA", takeoff=T0, **kw) -> Session:
    return Session(airfield_id=af, flarm_id=flarm, takeoff_ts=takeoff, **kw)


async def test_upsert_conflict_returns_existing(conn, airfield_id):
    store = PgSessionStore(conn)
    a = await store.upsert(mk(airfield_id, registration="D-1234"))
    b = await store.upsert(mk(airfield_id, registration="OTHER"))
    assert a.session_id == b.session_id
    assert b.registration == "D-1234"
    assert b.state == SessionState.TRACKING
    assert b.takeoff_ts == T0
    c = await store.upsert(mk(airfield_id, takeoff=T0 + timedelta(hours=1)))
    assert c.session_id != a.session_id
    assert await conn.fetchval(
        "SELECT COUNT(*) FROM vf_sync_sessions WHERE airfield_id = $1", airfield_id) == 2


async def test_save_and_get_round_trip(conn, airfield_id):
    store = PgSessionStore(conn)
    s = await store.upsert(mk(airfield_id))
    s.landing_ts = T0 + timedelta(hours=1)
    s.landing_method = "observed"
    s.start_type_detected = "aerotow"
    s.tow_registration = "D-EKKW"
    s.release_ts = T0 + timedelta(minutes=8)
    s.release_alt_agl_m = 600
    s.release_method = "pair_separation"
    s.tow_time_min = 8
    s.landing_count = 2
    s.conf_pairing = 0.87
    s.conf_landing = 0.95
    s.conf_touchgo = 0.5
    s.matched_flid = 123456
    s.state = SessionState.MATCHED
    s.add_review_reason("x")
    s.attempts = 2
    s.last_attempt = T0 + timedelta(minutes=30)
    before = s.updated_at
    await store.save(s)
    assert s.updated_at > before

    r = await store.get(s.session_id)
    assert r is not None
    for f in ("airfield_id", "flarm_id", "landing_ts", "landing_method", "start_type_detected",
              "tow_registration", "release_ts", "release_alt_agl_m", "release_method",
              "tow_time_min", "landing_count", "matched_flid", "state", "review_reason",
              "attempts", "last_attempt"):
        assert getattr(r, f) == getattr(s, f), f
    assert r.conf_pairing == pytest.approx(0.87, abs=1e-6)
    assert r.conf_landing == pytest.approx(0.95, abs=1e-6)
    assert r.conf_touchgo == pytest.approx(0.5, abs=1e-6)
    assert r.updated_at == s.updated_at
    assert await store.get(uuid4()) is None
    with pytest.raises(KeyError):
        await store.save(mk(airfield_id, flarm="NOPE"))


async def test_find_open_and_list_open_filters(conn, airfield_id):
    store = PgSessionStore(conn)
    s1 = await store.upsert(mk(airfield_id, flarm="A", takeoff=T0))
    s2 = await store.upsert(mk(airfield_id, flarm="A", takeoff=T0 + timedelta(hours=2)))
    s3 = await store.upsert(mk(airfield_id, flarm="B", takeoff=T0 + timedelta(minutes=5)))
    s3.state = SessionState.AWAITING_MATCH
    s3.landing_ts = T0 + timedelta(hours=1)
    await store.save(s3)
    s4 = await store.upsert(mk(airfield_id, flarm="C", takeoff=T0 + timedelta(minutes=9)))
    s4.state = SessionState.COMPLETED
    await store.save(s4)

    assert (await store.find_open_for_aircraft(airfield_id, "A")).session_id == s2.session_id
    s2.state = SessionState.EXPIRED
    await store.save(s2)
    assert (await store.find_open_for_aircraft(airfield_id, "A")).session_id == s1.session_id
    assert await store.find_open_for_aircraft(uuid4(), "A") is None

    ids = lambda rows: [r.session_id for r in rows]  # noqa: E731
    assert ids(await store.list_open(airfield_id=airfield_id)) == [s1.session_id, s3.session_id]
    assert ids(await store.list_open(airfield_id=airfield_id, airborne_only=True)) == [s1.session_id]
    assert ids(await store.list_open(airfield_id=airfield_id,
                                     states={SessionState.AWAITING_MATCH})) == [s3.session_id]
    assert ids(await store.list_open(airfield_id=airfield_id,
                                     states={SessionState.COMPLETED})) == [s4.session_id]
    assert s1.session_id in ids(await store.list_open())      # unscoped includes ours


async def test_expire_and_count_today(conn, airfield_id):
    store = PgSessionStore(conn)
    now = datetime(2025, 5, 10, 3, 0, tzinfo=timezone.utc)
    old = await store.upsert(mk(airfield_id, flarm="A", takeoff=now - timedelta(days=8)))
    fresh = await store.upsert(mk(airfield_id, flarm="B", takeoff=now - timedelta(days=2)))
    late = await store.upsert(mk(airfield_id, flarm="C",
                                 takeoff=datetime(2025, 5, 8, 23, 30, tzinfo=timezone.utc)))
    assert await store.expire_older_than(7, now=now) == 1
    assert (await store.get(old.session_id)).state == SessionState.EXPIRED
    assert (await store.get(fresh.session_id)).state == SessionState.TRACKING
    assert await store.expire_older_than(7, now=now) == 0
    assert await store.count_today(airfield_id, date(2025, 5, 8)) == 2   # fresh + late (UTC)
    assert await store.count_today(airfield_id, date(2025, 5, 9)) == 0
    assert await store.count_today(uuid4(), date(2025, 5, 8)) == 0
    assert late.takeoff_ts.date() == date(2025, 5, 8)


async def test_audit_append_and_list(conn, airfield_id):
    sessions = PgSessionStore(conn)
    audit = PgAuditStore(conn)
    s = await sessions.upsert(mk(airfield_id))
    e1 = await audit.append(AuditEntry(airfield_id=airfield_id, action="get",
                                       session_id=s.session_id, flid=42,
                                       pre_state={"departuretime": ""}, http_status=200))
    e2 = await audit.append(AuditEntry(airfield_id=airfield_id, action="edit",
                                       session_id=s.session_id, flid=42,
                                       fields_sent={"departuretime": "2025-05-01 10:00"},
                                       http_status=200, detail="ok"))
    e3 = await audit.append(AuditEntry(airfield_id=airfield_id, action="abstain"))
    assert e1.id is not None and e2.id > e1.id and e3.id > e2.id
    assert e1.pre_state == {"departuretime": ""} and e1.fields_sent is None
    assert e2.fields_sent == {"departuretime": "2025-05-01 10:00"}

    rows = await audit.list(airfield_id)
    assert [r.id for r in rows] == [e3.id, e2.id, e1.id]
    assert [r.id for r in await audit.list(airfield_id, session_id=s.session_id)] == [e2.id, e1.id]
    assert [r.id for r in await audit.list(airfield_id, limit=1)] == [e3.id]
    assert await audit.list(uuid4()) == []
    assert rows[1].detail == "ok" and rows[2].detail == ""


async def test_budget_increment(conn, airfield_id):
    store = PgBudgetStore(conn)
    day = date(2025, 5, 1)
    assert await store.used(airfield_id, day) == 0
    assert await store.increment(airfield_id, day) == 1
    assert await store.increment(airfield_id, day, 4) == 5
    assert await store.used(airfield_id, day) == 5
    assert await store.used(airfield_id, date(2025, 5, 2)) == 0


async def test_config_decrypt_round_trip(conn, airfield_id, monkeypatch):
    key = crypto.generate_key()
    monkeypatch.setattr(settings, "vfsync_cred_key", key)
    await conn.execute(
        """
        INSERT INTO vf_sync_config
            (airfield_id, enabled, dry_run, vf_cid, vf_username, vf_password_enc,
             vf_appkey_enc, flags, daily_budget)
        VALUES ($1, TRUE, FALSE, 4711, 'tech-user', $2, $3, $4::jsonb, 300)
        """,
        airfield_id, crypto.encrypt("5f4dcc3b5aa765d61d8327deb882cf99"),
        crypto.encrypt("app-key-secret"), '{"live_release": true, "auto_create": false}',
    )
    store = PgConfigStore(conn)
    tenants = [t for t in await store.load_enabled() if t.airfield_id == airfield_id]
    assert len(tenants) == 1
    t = tenants[0]
    assert t.slug.startswith("test-")
    assert t.enabled and not t.dry_run
    assert t.vf_cid == 4711 and t.vf_username == "tech-user"
    assert t.vf_password_md5 == "5f4dcc3b5aa765d61d8327deb882cf99"
    assert t.vf_appkey == "app-key-secret"
    assert t.flag("live_release") and not t.flag("auto_create")
    assert t.daily_budget == 300
    assert "5f4dcc3b" not in repr(t)

    single = await store.load(airfield_id)
    assert single is not None and single.vf_appkey == "app-key-secret"
    assert await store.load(uuid4()) is None

    # wrong key -> tenant skipped, not crashed
    monkeypatch.setattr(settings, "vfsync_cred_key", crypto.generate_key())
    assert [t for t in await store.load_enabled() if t.airfield_id == airfield_id] == []


async def test_flight_row_fetcher_reads_log_and_status(conn, airfield_id):
    landing = T0 + timedelta(hours=1)
    await conn.execute(
        "INSERT INTO flight_log (airfield_id, flarm_id, takeoff_time, landing_time, landing_count,"
        " landing_method, launch_type, release_altitude_agl, tow_duration_s)"
        " VALUES ($1, 'DDA5BA', $2, $3, 2, 'observed', 'aerotow', 610, 420)",
        airfield_id, T0, landing,
    )
    await conn.execute(
        "INSERT INTO flight_status (airfield_id, flarm_id, status, takeoff_time, landing_time)"
        " VALUES ($1, 'DDA5BA', 3, $2, $3)",
        airfield_id, T0, landing,
    )
    fetch = make_flight_row_fetcher(conn)
    rows = await fetch(airfield_id, "DDA5BA", T0)
    assert [r["source"] for r in rows] == ["flight_log", "flight_status"]
    assert rows[0]["landing_time"] == landing and rows[0]["landing_count"] == 2
    assert rows[0]["launch_type"] == "aerotow" and rows[0]["release_altitude_agl"] == 610
    assert await fetch(airfield_id, "DDA5BA", T0 + timedelta(minutes=1)) == []
    assert await fetch(uuid4(), "DDA5BA", T0) == []
