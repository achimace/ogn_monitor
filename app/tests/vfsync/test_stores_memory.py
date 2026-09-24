"""AP-3: in-memory store semantics (mirror of the PG stores)."""

from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.vfsync.models import AuditEntry, Session, SessionState, TenantConfig
from app.vfsync.stores_memory import (
    InMemoryAuditStore,
    InMemoryBudgetStore,
    InMemoryConfigStore,
    InMemorySessionStore,
)

AF = uuid4()
AF2 = uuid4()
T0 = datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc)


def mk(flarm="DDA5BA", takeoff=T0, airfield=AF, **kw) -> Session:
    return Session(airfield_id=airfield, flarm_id=flarm, takeoff_ts=takeoff, **kw)


async def test_upsert_returns_existing_for_same_key():
    store = InMemorySessionStore()
    a = await store.upsert(mk(registration="D-1234"))
    b = await store.upsert(mk(registration="OTHER"))
    assert a.session_id == b.session_id
    assert b.registration == "D-1234"
    c = await store.upsert(mk(takeoff=T0 + timedelta(hours=1)))
    assert c.session_id != a.session_id


async def test_returned_sessions_are_copies_until_saved():
    store = InMemorySessionStore()
    s = await store.upsert(mk())
    s.landing_count = 3
    assert (await store.get(s.session_id)).landing_count == 1
    before = (await store.get(s.session_id)).updated_at
    await store.save(s)
    stored = await store.get(s.session_id)
    assert stored.landing_count == 3
    assert stored.updated_at >= before


async def test_save_unknown_session_raises():
    store = InMemorySessionStore()
    with pytest.raises(KeyError):
        await store.save(mk())


async def test_find_open_for_aircraft_prefers_newest_open():
    store = InMemorySessionStore()
    old = await store.upsert(mk(takeoff=T0))
    new = await store.upsert(mk(takeoff=T0 + timedelta(hours=2)))
    assert (await store.find_open_for_aircraft(AF, "DDA5BA")).session_id == new.session_id
    new.state = SessionState.COMPLETED
    await store.save(new)
    assert (await store.find_open_for_aircraft(AF, "DDA5BA")).session_id == old.session_id
    assert await store.find_open_for_aircraft(AF2, "DDA5BA") is None
    assert await store.find_open_for_aircraft(AF, "XXXXXX") is None


async def test_list_open_filters():
    store = InMemorySessionStore()
    s1 = await store.upsert(mk(flarm="A", takeoff=T0))
    s2 = await store.upsert(mk(flarm="B", takeoff=T0 + timedelta(minutes=5), airfield=AF2))
    s3 = await store.upsert(mk(flarm="C", takeoff=T0 + timedelta(minutes=10)))
    s3.state = SessionState.AWAITING_MATCH
    s3.landing_ts = T0 + timedelta(hours=1)
    await store.save(s3)
    s4 = await store.upsert(mk(flarm="D", takeoff=T0 + timedelta(minutes=15)))
    s4.state = SessionState.COMPLETED
    await store.save(s4)

    ids = lambda rows: [r.session_id for r in rows]  # noqa: E731
    assert ids(await store.list_open()) == [s1.session_id, s2.session_id, s3.session_id]
    assert ids(await store.list_open(airfield_id=AF)) == [s1.session_id, s3.session_id]
    assert ids(await store.list_open(states={SessionState.AWAITING_MATCH})) == [s3.session_id]
    assert ids(await store.list_open(airfield_id=AF, airborne_only=True)) == [s1.session_id]
    assert ids(await store.list_open(states={SessionState.COMPLETED})) == [s4.session_id]


async def test_expire_older_than():
    store = InMemorySessionStore()
    old = await store.upsert(mk(flarm="A", takeoff=T0 - timedelta(days=10)))
    fresh = await store.upsert(mk(flarm="B", takeoff=T0 - timedelta(days=2)))
    done = await store.upsert(mk(flarm="C", takeoff=T0 - timedelta(days=30)))
    done.state = SessionState.COMPLETED
    await store.save(done)
    assert await store.expire_older_than(7, now=T0) == 1
    assert (await store.get(old.session_id)).state == SessionState.EXPIRED
    assert (await store.get(fresh.session_id)).state == SessionState.TRACKING
    assert (await store.get(done.session_id)).state == SessionState.COMPLETED
    assert await store.expire_older_than(7, now=T0) == 0


async def test_count_today_uses_utc_date():
    store = InMemorySessionStore()
    await store.upsert(mk(flarm="A", takeoff=datetime(2025, 5, 1, 23, 30, tzinfo=timezone.utc)))
    await store.upsert(mk(flarm="B", takeoff=datetime(2025, 5, 2, 0, 30, tzinfo=timezone.utc)))
    await store.upsert(mk(flarm="C", takeoff=datetime(2025, 5, 2, 8, 0, tzinfo=timezone.utc)))
    await store.upsert(mk(flarm="D", takeoff=datetime(2025, 5, 2, 8, 0, tzinfo=timezone.utc),
                          airfield=AF2))
    assert await store.count_today(AF, date(2025, 5, 1)) == 1
    assert await store.count_today(AF, date(2025, 5, 2)) == 2
    assert await store.count_today(AF2, date(2025, 5, 2)) == 1


async def test_audit_is_append_only_with_incrementing_ids():
    store = InMemoryAuditStore()
    sid = uuid4()
    e1 = await store.append(AuditEntry(airfield_id=AF, action="get", session_id=sid, flid=1))
    e2 = await store.append(AuditEntry(airfield_id=AF, action="edit", session_id=sid, flid=1,
                                       fields_sent={"departuretime": "2025-05-01 10:00"}))
    e3 = await store.append(AuditEntry(airfield_id=AF2, action="get"))
    assert (e1.id, e2.id, e3.id) == (1, 2, 3)
    rows = await store.list(AF)
    assert [r.id for r in rows] == [2, 1]           # newest first
    assert [r.id for r in await store.list(AF, session_id=sid, limit=1)] == [2]
    assert [r.id for r in await store.list(AF2)] == [3]
    rows[0].detail = "mutated"
    assert (await store.list(AF))[0].detail == ""


async def test_budget_increment_returns_new_value():
    store = InMemoryBudgetStore()
    day = date(2025, 5, 1)
    assert await store.used(AF, day) == 0
    assert await store.increment(AF, day) == 1
    assert await store.increment(AF, day, 5) == 6
    assert await store.used(AF, day) == 6
    assert await store.used(AF, date(2025, 5, 2)) == 0
    assert await store.used(AF2, day) == 0


async def test_config_store_filters_enabled():
    on = TenantConfig(airfield_id=AF, slug="on", enabled=True)
    off = TenantConfig(airfield_id=AF2, slug="off", enabled=False)
    store = InMemoryConfigStore([on, off])
    assert [t.slug for t in await store.load_enabled()] == ["on"]
    assert (await store.load(AF2)).slug == "off"
    assert await store.load(uuid4()) is None
