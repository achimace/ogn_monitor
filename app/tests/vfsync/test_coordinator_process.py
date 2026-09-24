"""End-to-end of the coordinator's match + write path (AP-5 wiring).

Events -> session -> matcher (list/today cache) -> writer (mock VF).
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.tracking.flight_state import FlightState, FlightStatus
from app.vfsync.audit import AuditLog
from app.vfsync.budget import BudgetGuard
from app.vfsync.coordinator import SyncCoordinator
from app.vfsync.models import SessionState, TenantConfig
from app.vfsync.stores_memory import (
    InMemoryAuditStore,
    InMemoryBudgetStore,
    InMemoryConfigStore,
    InMemorySessionStore,
)
from app.vfsync.vf_client.mock import MockVfStore, mock_client
from app.vfsync.writer import VfWriter

AF = uuid4()
T_OFF = datetime(2026, 9, 24, 10, 0, 12, tzinfo=timezone.utc)
T_LAND = T_OFF + timedelta(hours=2, minutes=30)


def _event(etype: str, **flight_kw) -> dict:
    base = dict(flarm_id="DDA5BA", airfield_slug="test", registration="D-1234",
                status=FlightStatus.FLYING, takeoff_time=T_OFF.strftime("%Y-%m-%dT%H:%M:%SZ"))
    base.update(flight_kw)
    f = FlightState(**base)
    return {"type": etype, "flarm_id": "DDA5BA", "message": "", "data": f.to_redis_dict()}


class World:
    def __init__(self, dry_run=False, daily_budget=450):
        self.now = T_OFF
        self.vf = MockVfStore()
        self.tenant = TenantConfig(airfield_id=AF, slug="test", enabled=True, dry_run=dry_run,
                                   daily_budget=daily_budget, vf_username="u",
                                   vf_password_md5="m", vf_appkey="k")
        self.sessions = InMemorySessionStore()
        self.audit_store = InMemoryAuditStore()
        self.budget_store = InMemoryBudgetStore()
        clock = lambda: self.now  # noqa: E731
        guard = BudgetGuard(self.budget_store, clock=clock)
        self.coord = SyncCoordinator(
            sessions=self.sessions, audit=self.audit_store, budget=self.budget_store,
            config=InMemoryConfigStore([self.tenant]), clock=clock, guard=guard,
            client_for=lambda t: mock_client(self.vf, on_request=guard.hook_for(t)),
            list_cache_s=300,
        )
        self.coord.writer = VfWriter(self.coord.client_for, self.sessions,
                                     AuditLog(self.audit_store), guard)

    async def event(self, etype, **kw):
        return await self.coord.on_event(self.tenant, _event(etype, **kw))

    async def session(self):
        return (await self.sessions.list_open(airfield_id=AF) or
                [s for s in await self.sessions.list_open(airfield_id=AF, states=set(SessionState))])[0]

    def actions(self):
        return [e.action for e in self.audit_store.entries] if hasattr(self.audit_store, "entries") else None


@pytest.fixture
def w():
    return World()


async def _all_sessions(w: World):
    return await w.sessions.list_open(airfield_id=AF, states=set(SessionState))


async def test_takeoff_writes_live_departure_and_landing_writes_bundle(w: World):
    flid = w.vf.add_flight(callsign="D-1234")

    await w.event("takeoff")
    s = (await _all_sessions(w))[0]
    assert s.matched_flid == flid
    assert s.state == SessionState.DEPARTURE_WRITTEN
    assert w.vf.flight(flid)["departuretime"] == "2026-09-24 10:00"
    assert w.vf.flight(flid)["arrivaltime"] == ""

    await w.event("launch_type_detected", launch_type="aerotow", tow_plane_reg="D-ETOW",
                  release_alt_agl_m=435.0, release_time="2026-09-24T10:07:00Z",
                  release_method="pair_separation", tow_duration_s=408,
                  pairing_confidence=0.96)
    s = (await _all_sessions(w))[0]
    assert s.release_alt_agl_m == 435 and s.tow_time_min == 7
    assert w.vf.flight(flid)["towheight"] == ""      # live_release is off: internal only

    w.now = T_LAND + timedelta(minutes=2)
    await w.event("landing_final", status=FlightStatus.LANDING,
                  landing_time=T_LAND.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  landing_method="observed", landing_confidence=1.0, landing_count=1,
                  launch_type="aerotow", release_alt_agl_m=435.0, pairing_confidence=0.96,
                  tow_plane_reg="D-ETOW")
    s = (await _all_sessions(w))[0]
    assert s.state == SessionState.COMPLETED
    f = w.vf.flight(flid)
    assert f["arrivaltime"] == "2026-09-24 12:30"
    assert f["towheight"] == "435" and f["towtime"] == "7"
    assert f["landingcount"] == "1"
    # exactly one list/today (cached), one get+edit per write
    assert sum(1 for _, p, _ in w.vf.requests if "list/today" in p) == 1
    assert w.vf.order_of(flid) == ["get", "edit", "get", "edit"]


async def test_no_vf_flight_waits_and_gets_matched_on_retry(w: World):
    await w.event("takeoff")
    s = (await _all_sessions(w))[0]
    assert s.state == SessionState.AWAITING_MATCH and s.matched_flid is None

    # pilot creates the flight later; cache must expire before the retry
    flid = w.vf.add_flight(callsign="D-1234")
    w.now += timedelta(minutes=6)
    r = await w.coord.process(s, w.tenant, "retry_airborne")
    assert r.status == "written" and r.sent == {"departuretime": "2026-09-24 10:00"}
    s = (await _all_sessions(w))[0]
    assert s.matched_flid == flid and s.state == SessionState.DEPARTURE_WRITTEN


async def test_two_open_vf_flights_same_callsign_go_to_review(w: World):
    w.vf.add_flight(callsign="D-1234")
    w.vf.add_flight(callsign="D-1234")
    await w.event("takeoff")
    s = (await _all_sessions(w))[0]
    assert s.state == SessionState.REVIEW
    assert any(r.startswith("ambiguous_match") for r in s.review_reasons())
    assert w.vf.edits == []


async def test_a_vf_flight_is_never_matched_twice(w: World):
    """Two gliders of the same registration? No - but two sessions of one
    aircraft on one day with a single VF flight: the second must not
    steal the flight of the first."""
    flid = w.vf.add_flight(callsign="D-1234")
    await w.event("takeoff")
    first = (await _all_sessions(w))[0]
    assert first.matched_flid == flid

    second_off = (T_OFF + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    w.now = T_OFF + timedelta(hours=3)
    await w.event("takeoff", takeoff_time=second_off)
    sessions = await _all_sessions(w)
    second = [s for s in sessions if s.session_id != first.session_id][0]
    assert second.matched_flid is None
    assert second.state == SessionState.AWAITING_MATCH


async def test_degraded_budget_skips_live_departure_but_writes_bundle(w: World):
    flid = w.vf.add_flight(callsign="D-1234")
    for _ in range(280):                       # 62 % of 450 -> NO_LIVE_DEPARTURE
        await w.budget_store.increment(AF, w.coord.guard.day_for(w.tenant))
    await w.event("takeoff")
    s = (await _all_sessions(w))[0]
    assert s.state == SessionState.TRACKING and w.vf.requests == []

    w.now = T_LAND + timedelta(minutes=2)
    await w.event("landing_final", status=FlightStatus.LANDING,
                  landing_time=T_LAND.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  landing_method="observed", landing_confidence=1.0, launch_type="winch")
    s = (await _all_sessions(w))[0]
    assert s.state == SessionState.COMPLETED
    assert w.vf.flight(flid)["departuretime"] == "2026-09-24 10:00"   # caught up in the bundle
    assert w.vf.flight(flid)["arrivaltime"] == "2026-09-24 12:30"


async def test_aerotow_only_stage_defers_winch_flights(w: World):
    w.vf.add_flight(callsign="D-1234")
    for _ in range(410):                       # 91 %
        await w.budget_store.increment(AF, w.coord.guard.day_for(w.tenant))
    w.now = T_LAND
    await w.event("landing_final", status=FlightStatus.LANDING,
                  landing_time=T_LAND.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  landing_method="observed", landing_confidence=1.0, launch_type="winch")
    s = (await _all_sessions(w))[0]
    assert s.state == SessionState.TRACKING and w.vf.requests == []


async def test_dry_run_matches_but_never_edits():
    w = World(dry_run=True)
    flid = w.vf.add_flight(callsign="D-1234")
    await w.event("takeoff")
    s = (await _all_sessions(w))[0]
    assert s.matched_flid == flid and s.state == SessionState.DEPARTURE_WRITTEN
    assert w.vf.edits == []
    actions = [e.action for e in await w.audit_store.list(AF, limit=50)]
    assert "dryrun_edit" in actions and "edit" not in actions


async def test_login_forbidden_pauses_tenant_until_resume(w: World):
    w.vf.add_flight(callsign="D-1234")
    w.vf.fail_next("auth/signin", 403, times=5)
    await w.event("takeoff")
    assert "test" in w.coord.paused
    s = (await _all_sessions(w))[0]
    assert s.state == SessionState.TRACKING
    n_requests = len(w.vf.requests)

    await w.coord.process(s, w.tenant, "retry_airborne")
    assert len(w.vf.requests) == n_requests        # paused: no calls at all

    w.vf._failures.clear()                         # operator fixed the credentials
    w.coord.resume(w.tenant)
    r = await w.coord.process(s, w.tenant, "retry_airborne")
    assert r.status == "written"


async def test_write_error_streak_is_tracked(w: World):
    w.vf.add_flight(callsign="D-1234")
    w.vf.fail_next("flight/get", 503, times=30)
    await w.event("takeoff")
    s = (await _all_sessions(w))[0]
    for _ in range(2):
        await w.coord.process(s, w.tenant, "retry_airborne")
    assert w.coord.write_error_streak["test"] == 3
    w.vf._failures.clear()
    await w.coord.process(s, w.tenant, "retry_airborne")
    assert w.coord.write_error_streak["test"] == 0


async def test_completed_and_review_sessions_are_left_alone(w: World):
    w.vf.add_flight(callsign="D-1234")
    await w.event("takeoff")
    s = (await _all_sessions(w))[0]
    s.state = SessionState.REVIEW
    await w.sessions.save(s)
    assert await w.coord.process(s, w.tenant, "retry_hourly") is None
