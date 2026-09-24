"""AP-13: end-to-end golden-file test of a synthetic flight day.

Synthetic beacons -> FlightStateMachine + LaunchDetector (real tracking
code) -> events exactly as the APRS worker publishes them -> VF-Sync
coordinator (in-memory stores) -> VF mock. The resulting VF records and
the audit trail are compared with tests/vfsync/golden/flight_day.json.

Regenerate the golden file deliberately with UPDATE_GOLDEN=1 after a
reviewed behaviour change - never to make a red test green.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

import tests.conftest as tc
from app.tracking.flight_state import FlightStatus
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
from tests.conftest import AF_ELEV, Sim, approach_and_land, beacon, fly_away, ground_roll
from tests.test_launch_detector import _interleave, pair_climb

GOLDEN = Path(__file__).parent / "golden" / "flight_day.json"
AF = UUID("00000000-0000-0000-0000-00000000a1f1")
DAY_START = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)   # fixed, replaces conftest.T0

GLD = "GLD001"
GLD2 = "GLD002"
TOW = "TOW001"
REGS = {GLD: "D-1234", GLD2: "D-5678", TOW: "D-ETOW"}


class EventSim(Sim):
    """Sim that serialises events at emission time, like FlightTracker does."""

    def __init__(self, sink, **kw):
        super().__init__(**kw)
        self.sink = sink
        self.serialised: list[dict] = []

    def feed(self, b):
        before = len(self.events)
        flight = super().feed(b)
        for e in self.events[before:]:
            self.serialised.append({
                "type": e["event_type"],
                "flarm_id": e["flarm_id"],
                "message": e.get("message", ""),
                "data": e["flight"].to_redis_dict(),
                "_ts": b.timestamp,
            })
        return flight


class SyncWorld:
    def __init__(self, tenant: TenantConfig):
        self.now = DAY_START
        self.vf = MockVfStore()
        self.tenant = tenant
        self.sessions = InMemorySessionStore()
        self.audit_store = InMemoryAuditStore()
        clock = lambda: self.now  # noqa: E731
        guard = BudgetGuard(InMemoryBudgetStore(), clock=clock)
        self.coord = SyncCoordinator(
            sessions=self.sessions, audit=self.audit_store, budget=guard._store,
            config=InMemoryConfigStore([tenant]), clock=clock, guard=guard,
            client_for=lambda t: mock_client(self.vf, on_request=guard.hook_for(t)),
        )
        self.coord.writer = VfWriter(self.coord.client_for, self.sessions,
                                     AuditLog(self.audit_store), guard)


def _flight_day(sim: EventSim) -> None:
    """Aerotow (D-1234 behind D-ETOW), winch launch with a touch & go
    (D-5678), everybody lands, landings become final."""
    sim.feed_all(_interleave(ground_roll(TOW), ground_roll(GLD)))
    n = 50
    sim.feed_all(_interleave(pair_climb(TOW, 18, n, east_offset=50), pair_climb(GLD, 18, n)))
    last_t, last_alt, last_east = 18 + 3 * (n - 1), AF_ELEV + 60 + 7.5 * n, 150 + 80 * n
    sim.feed(beacon(TOW, last_t + 3, east=last_east + 450, north=200, alt=last_alt - 40,
                    speed=140, vs=-3.0, track=45))
    sim.feed(beacon(GLD, last_t + 3, east=last_east + 60, alt=last_alt + 2, speed=85, vs=0.5))

    # Winch launch of the second glider at 08:20
    sim.feed_all(ground_roll(GLD2, 1200))
    alt = AF_ELEV + 60
    for i, t in enumerate(range(1218, 1248, 3)):
        alt += 36
        sim.feed(beacon(GLD2, t, east=150 + 30 * (i + 1), alt=alt, speed=95, vs=12.0))
    sim.feed(beacon(GLD2, 1248, east=500, alt=alt + 10, speed=80, vs=1.0))
    sim.feed_all(fly_away(GLD2, 1300))

    # Tow plane lands at 08:15 and becomes final
    sim.feed_all(fly_away(TOW, 500))
    sim.feed_all(approach_and_land(TOW, 900))
    sim.feed(beacon(TOW, 1020, speed=0))

    # Second glider: touch & go at ~09:00, then final landing at 09:30
    sim.feed_all(approach_and_land(GLD2, 3600))
    sim.feed(beacon(GLD2, 3640, east=30, alt=AF_ELEV + 2, speed=40))
    sim.feed(beacon(GLD2, 3650, east=120, alt=AF_ELEV + 20, speed=70, vs=2.0))
    sim.feed(beacon(GLD2, 3655, east=250, alt=AF_ELEV + 60, speed=90, vs=3.0))
    sim.feed_all(fly_away(GLD2, 3700))
    sim.feed_all(approach_and_land(GLD2, 5400))
    sim.feed(beacon(GLD2, 5520, speed=0))          # > 90 s after touchdown: final

    # First glider lands at 10:30
    sim.feed_all(fly_away(GLD, 2000))
    sim.feed_all(approach_and_land(GLD, 9000))
    sim.feed(beacon(GLD, 9120, speed=0))


@pytest.fixture
def fixed_day(monkeypatch):
    monkeypatch.setattr(tc, "T0", DAY_START.timestamp())
    yield


async def _run_day(dry_run: bool) -> dict:
    tenant = TenantConfig(airfield_id=AF, slug="test", enabled=True, dry_run=dry_run,
                          vf_username="u", vf_password_md5="m", vf_appkey="k")
    world = SyncWorld(tenant)
    # The pilots created their flights empty in VF before the day
    world.vf.add_flight(callsign="D-ETOW", flid=101)
    world.vf.add_flight(callsign="D-1234", flid=102, starttype="F")
    world.vf.add_flight(callsign="D-5678", flid=103)

    sim = EventSim(sink=None, roles={TOW: "towplane", GLD: "glider", GLD2: "glider"},
                   registrations=REGS)
    _flight_day(sim)

    for ev in sim.serialised:
        world.now = datetime.fromtimestamp(ev["_ts"], tz=timezone.utc)
        await world.coord.on_event(tenant, {k: v for k, v in ev.items() if k != "_ts"})

    sessions = await world.sessions.list_open(airfield_id=AF, states=set(SessionState))
    sessions.sort(key=lambda s: (s.takeoff_ts, s.flarm_id))
    audit = await world.audit_store.list(AF, limit=1000)
    audit.sort(key=lambda e: e.id)
    return {
        "vf_flights": {str(flid): {k: v for k, v in rec.items() if k != "flid"}
                       for flid, rec in sorted(world.vf.flights.items())},
        "vf_edits": [[flid, payload] for flid, payload in world.vf.edits],
        "sessions": [
            {
                "flarm_id": s.flarm_id, "registration": s.registration,
                "takeoff": s.takeoff_ts.isoformat() if s.takeoff_ts else None,
                "landing": s.landing_ts.isoformat() if s.landing_ts else None,
                "start_type": s.start_type_detected, "tow": s.tow_registration,
                "release_agl": s.release_alt_agl_m, "tow_min": s.tow_time_min,
                "landing_count": s.landing_count, "state": s.state.value,
                "flid": s.matched_flid, "review": s.review_reasons(),
            }
            for s in sessions
        ],
        "audit": [
            {"action": e.action, "flid": e.flid, "fields": e.fields_sent,
             "http": e.http_status, "detail": e.detail}
            for e in audit
        ],
        "events": [e["type"] for e in sim.serialised],
        "requests": len(world.vf.requests),
    }


async def test_flight_day_matches_golden(fixed_day):
    result = await _run_day(dry_run=False)

    # Sanity on the substance before any golden comparison
    by_reg = {s["registration"]: s for s in result["sessions"]}
    assert by_reg["D-1234"]["start_type"] == "aerotow"
    assert by_reg["D-1234"]["state"] == "completed"
    assert by_reg["D-5678"]["landing_count"] == 2
    assert by_reg["D-ETOW"]["start_type"] == "powered"
    f_glider = result["vf_flights"]["102"]
    assert f_glider["departuretime"] == "2026-09-24 08:00"
    assert f_glider["arrivaltime"] == "2026-09-24 10:30"
    assert f_glider["towheight"] == str(int(AF_ELEV + 60 + 7.5 * 50 - AF_ELEV))
    assert f_glider["towtime"] == "3"
    assert result["vf_flights"]["103"]["landingcount"] == "2"
    assert result["vf_flights"]["103"]["towheight"] == ""      # winch: no height
    assert result["requests"] < 40                               # budget-friendly

    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
        pytest.skip("golden file written - re-run to compare")

    assert GOLDEN.exists(), "golden file missing - generate deliberately with UPDATE_GOLDEN=1"
    expected = json.loads(GOLDEN.read_text())
    assert result == expected


async def test_flight_day_dry_run_writes_nothing(fixed_day):
    result = await _run_day(dry_run=True)
    assert result["vf_edits"] == []
    assert all(v["arrivaltime"] == "" for v in result["vf_flights"].values())
    actions = [a["action"] for a in result["audit"]]
    assert "dryrun_edit" in actions and "edit" not in actions
    assert {s["state"] for s in result["sessions"]} == {"completed"}
