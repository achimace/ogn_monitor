"""Scheduler cadence (Kap. 4.3) and health rule."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from app.vfsync.health import build_health_app, is_healthy
from app.vfsync.models import Session, SessionState, TenantConfig
from app.vfsync.scheduler import Scheduler
from tests.vfsync.fakes import FakeBudgetStore, FakeSessionStore

AF = uuid4()
# 08:00 UTC = 10:00 Europe/Berlin (CEST)
T0 = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


def _tenant() -> TenantConfig:
    return TenantConfig(airfield_id=AF, slug="test", enabled=True)


class Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, session, tenant, trigger):
        self.calls.append((str(session.session_id)[:8], trigger))


def _scheduler(sessions: FakeSessionStore, rec: Recorder) -> Scheduler:
    return Scheduler(process=rec, sessions=sessions, budget=FakeBudgetStore(),
                     tenants_provider=lambda: [_tenant()], clock=lambda: T0)


async def test_first_tick_runs_both_retries_once_then_respects_cadence():
    sessions = FakeSessionStore()
    airborne = Session(airfield_id=AF, flarm_id="A", takeoff_ts=T0,
                       state=SessionState.AWAITING_MATCH)
    landed = Session(airfield_id=AF, flarm_id="B", takeoff_ts=T0 - timedelta(hours=2),
                     landing_ts=T0 - timedelta(hours=1), state=SessionState.MATCHED)
    review = Session(airfield_id=AF, flarm_id="C", takeoff_ts=T0, state=SessionState.REVIEW)
    for s in (airborne, landed, review):
        sessions.sessions[s.session_id] = s
    rec = Recorder()
    sch = _scheduler(sessions, rec)

    ran = await sch.tick(T0)
    assert any(r.startswith("retry_airborne:test:1") for r in ran)
    assert any(r.startswith("retry_open:test:2") for r in ran)
    assert any(r.startswith("expiry:test") for r in ran)      # 10:00 local > 03:00
    assert not any(r.startswith("closing_run") for r in ran)  # before 21:00 local
    triggers = [t for _, t in rec.calls]
    assert triggers.count("retry_airborne") == 1
    assert triggers.count("retry_hourly") == 2                 # review excluded
    assert str(review.session_id)[:8] not in [s for s, _ in rec.calls]

    # 5 minutes later: nothing due
    assert await sch.tick(T0 + timedelta(minutes=5)) == []
    # 15 minutes later: airborne retry only
    ran = await sch.tick(T0 + timedelta(minutes=15))
    assert [r.split(":")[0] for r in ran] == ["retry_airborne"]
    # an hour later: both
    ran = await sch.tick(T0 + timedelta(hours=1))
    assert [r.split(":")[0] for r in ran] == ["retry_airborne", "retry_open"]


async def test_closing_run_at_21_local_once_per_day():
    sessions = FakeSessionStore()
    rec = Recorder()
    sch = _scheduler(sessions, rec)
    await sch.tick(T0)
    evening = datetime(2026, 9, 24, 19, 5, tzinfo=timezone.utc)   # 21:05 Berlin
    ran = await sch.tick(evening)
    assert any(r.startswith("closing_run:test") for r in ran)
    ran = await sch.tick(evening + timedelta(minutes=1))
    assert not any(r.startswith("closing_run") for r in ran)
    # next day again
    ran = await sch.tick(evening + timedelta(days=1))
    assert any(r.startswith("closing_run") for r in ran)


async def test_expiry_marks_old_sessions():
    sessions = FakeSessionStore()
    old = Session(airfield_id=AF, flarm_id="A", takeoff_ts=T0 - timedelta(days=9),
                  state=SessionState.AWAITING_MATCH, created_at=T0 - timedelta(days=9))
    fresh = Session(airfield_id=AF, flarm_id="B", takeoff_ts=T0 - timedelta(days=1),
                    state=SessionState.AWAITING_MATCH, created_at=T0 - timedelta(days=1))
    sessions.sessions[old.session_id] = old
    sessions.sessions[fresh.session_id] = fresh
    sch = _scheduler(sessions, Recorder())
    n = await sch.expire(_tenant(), T0)
    assert n == 1
    assert old.state == SessionState.EXPIRED and fresh.state == SessionState.AWAITING_MATCH


async def test_process_errors_do_not_stop_the_run():
    sessions = FakeSessionStore()
    for fid in ("A", "B"):
        s = Session(airfield_id=AF, flarm_id=fid, takeoff_ts=T0, state=SessionState.MATCHED)
        sessions.sessions[s.session_id] = s

    calls = []

    async def flaky(session, tenant, trigger):
        calls.append(session.flarm_id)
        if session.flarm_id == "A":
            raise RuntimeError("boom")

    sch = Scheduler(process=flaky, sessions=sessions, budget=FakeBudgetStore(),
                    tenants_provider=lambda: [_tenant()], clock=lambda: T0)
    n = await sch.retry_open(_tenant(), "retry_hourly")
    assert sorted(calls) == ["A", "B"] and n == 1


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------

def test_health_rule():
    assert is_healthy({"status": "ok", "tenants": []})
    # no flight events for hours is fine as long as the APRS worker is connected
    assert is_healthy({"status": "ok", "tenants": ["x"], "aprs_connected": True,
                       "last_event_ts": None})
    assert is_healthy({"status": "ok", "tenants": ["x"], "aprs_connected": False,
                       "aprs_down_for_s": 600})
    assert not is_healthy({"status": "ok", "tenants": ["x"], "aprs_connected": False,
                           "aprs_down_for_s": 1801})
    assert not is_healthy({"status": "stopping", "tenants": []})
    assert is_healthy({"status": "starting", "tenants": ["x"]})


async def test_healthz_endpoint_status_codes():
    snap = {"status": "ok", "tenants": ["x"], "aprs_connected": True,
            "last_event_ts": datetime.now(timezone.utc) - timedelta(minutes=2)}
    app = build_health_app(lambda: snap)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://h") as c:
        r = await c.get("/healthz")
        assert r.status_code == 200 and r.json()["healthy"] is True
        snap["aprs_connected"] = False
        snap["aprs_down_for_s"] = 3600
        r = await c.get("/healthz")
        assert r.status_code == 503 and r.json()["healthy"] is False
