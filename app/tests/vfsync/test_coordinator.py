"""AP-3: SyncCoordinator event dispatch and recovery (in-memory stores).

Event payloads are built from FlightState.to_redis_dict() exactly like
RedisWriter.publish_event does, so the parsing is tested against the real
wire format (all values strings).
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.tracking.flight_state import FlightState, FlightStatus
from app.vfsync.coordinator import SyncCoordinator, parse_iso
from app.vfsync.models import SessionState, TenantConfig
from app.vfsync.stores_memory import (
    InMemoryAuditStore,
    InMemoryBudgetStore,
    InMemoryConfigStore,
    InMemorySessionStore,
)

AF = uuid4()
SLUG = "ohlstadt"
FID = "DDA5BA"
TAKEOFF = "2025-05-01T10:00:00Z"
RELEASE = "2025-05-01T10:07:30Z"
LANDING = "2025-05-01T11:15:00Z"
NOW = datetime(2025, 5, 1, 12, 0, tzinfo=timezone.utc)


def tenant(**kw) -> TenantConfig:
    return TenantConfig(airfield_id=AF, slug=SLUG, enabled=True, **kw)


def make_flight(**overrides) -> FlightState:
    f = FlightState(flarm_id=FID, airfield_slug=SLUG, registration="D-1234",
                    status=FlightStatus.FLYING, takeoff_time=TAKEOFF)
    for k, v in overrides.items():
        setattr(f, k, v)
    return f


def event(etype: str, flight: FlightState, message: str = "") -> dict:
    """Same shape as RedisWriter.publish_event (after JSON round trip)."""
    return {
        "type": etype,
        "flarm_id": flight.flarm_id,
        "message": message,
        "data": {k: str(v) for k, v in flight.to_redis_dict().items()},
    }


@pytest.fixture
def stores():
    return {
        "sessions": InMemorySessionStore(),
        "audit": InMemoryAuditStore(),
        "budget": InMemoryBudgetStore(),
        "config": InMemoryConfigStore([tenant()]),
    }


@pytest.fixture
def coord(stores) -> SyncCoordinator:
    return SyncCoordinator(**stores, clock=lambda: NOW)


# ---------------------------------------------------------------------------

async def test_full_event_sequence(coord, stores):
    t = tenant()

    # takeoff
    s = await coord.on_event(t, event("takeoff", make_flight(status=FlightStatus.TAKEOFF)))
    assert s.state == SessionState.TRACKING
    assert s.registration == "D-1234"
    assert s.takeoff_ts == parse_iso(TAKEOFF)
    assert s.is_airborne and s.landing_count == 1
    sid = s.session_id

    # launch_type_detected (aerotow)
    f = make_flight(status=FlightStatus.FLYING, launch_type="aerotow", tow_plane_reg="D-EKKW",
                    tow_plane_flarm_id="3E1234", release_time=RELEASE, release_alt_m=1260.4,
                    release_alt_agl_m=600.4, release_method="pair_separation",
                    tow_duration_s=450, pairing_confidence=0.87)
    s = await coord.on_event(t, event("launch_type_detected", f, "pair_separation"))
    assert s.session_id == sid
    assert s.start_type_detected == "aerotow"
    assert s.tow_registration == "D-EKKW"
    assert s.release_ts == parse_iso(RELEASE)
    assert s.release_alt_agl_m == 600
    assert s.release_method == "pair_separation"
    assert s.tow_time_min == 8            # 450 s -> 7.5 -> half-up
    assert s.conf_pairing == pytest.approx(0.87)

    # touch_and_go
    f = make_flight(status=FlightStatus.FLYING, launch_type="aerotow", landing_count=2,
                    touch_go_confidence=0.9)
    s = await coord.on_event(t, event("touch_and_go", f))
    assert s.landing_count == 2 and s.conf_touchgo == pytest.approx(0.9)
    assert s.is_airborne

    # landing (not final) is ignored
    f = make_flight(status=FlightStatus.LANDING, landing_time=LANDING, landing_count=2,
                    landing_method="observed", landing_confidence=0.95)
    assert await coord.on_event(t, event("landing", f)) is None
    assert (await stores["sessions"].get(sid)).landing_ts is None

    # landing_final
    f.landing_final = True
    s = await coord.on_event(t, event("landing_final", f))
    assert s.landing_ts == parse_iso(LANDING)
    assert s.landing_method == "observed"
    assert s.conf_landing == pytest.approx(0.95)
    assert s.landing_count == 2
    assert not s.is_airborne

    # landing_retracted
    f = make_flight(status=FlightStatus.FLYING, landing_count=2)
    s = await coord.on_event(t, event("landing_retracted", f))
    assert s.landing_ts is None and s.landing_method is None and s.conf_landing is None
    assert s.landing_count == 2
    assert s.start_type_detected == "aerotow"     # launch data survives

    stored = await stores["sessions"].get(sid)
    assert stored.landing_ts is None
    assert len(await stores["sessions"].list_open(airfield_id=AF)) == 1
    assert stores["audit"].entries == []           # no writer -> no audit


async def test_duplicate_takeoff_does_not_create_second_session(coord, stores):
    t = tenant()
    a = await coord.on_event(t, event("takeoff", make_flight()))
    b = await coord.on_event(t, event("takeoff", make_flight(registration="D-9999")))
    assert a.session_id == b.session_id
    assert b.registration == "D-1234"
    assert len(await stores["sessions"].list_open()) == 1


async def test_winch_launch_has_no_tow_time(coord):
    f = make_flight(launch_type="winch", release_time=RELEASE, release_alt_agl_m=380,
                    release_method="winch_vs_drop", tow_duration_s=0, pairing_confidence=0.0)
    s = await coord.on_event(tenant(), event("launch_type_detected", f))
    assert s.start_type_detected == "winch"
    assert s.release_alt_agl_m == 380
    assert s.tow_time_min is None
    assert s.tow_registration is None


async def test_landing_final_without_takeoff_creates_session(coord, stores):
    f = make_flight(status=FlightStatus.LANDING, landing_time=LANDING, landing_final=True,
                    landing_method="silence", landing_confidence=0.6, landing_count=1)
    s = await coord.on_event(tenant(), event("landing_final", f))
    assert s is not None
    assert s.takeoff_ts == parse_iso(TAKEOFF)
    assert s.landing_ts == parse_iso(LANDING)
    assert s.landing_method == "silence"
    assert s.registration == "D-1234"
    assert len(await stores["sessions"].list_open(airfield_id=AF)) == 1


async def test_launch_without_takeoff_creates_session(coord, stores):
    f = make_flight(launch_type="aerotow", tow_plane_reg="D-EKKW", release_time=RELEASE,
                    release_alt_agl_m=500, tow_duration_s=360, pairing_confidence=0.7)
    s = await coord.on_event(tenant(), event("launch_type_detected", f))
    assert s.takeoff_ts == parse_iso(TAKEOFF)
    assert s.tow_time_min == 6
    assert (await stores["sessions"].get(s.session_id)).start_type_detected == "aerotow"


async def test_landing_of_second_flight_does_not_touch_older_open_session(coord, stores):
    t = tenant()
    first = await coord.on_event(t, event("takeoff", make_flight()))
    second_takeoff = "2025-05-01T12:30:00Z"
    f = make_flight(takeoff_time=second_takeoff, landing_time="2025-05-01T13:00:00Z",
                    landing_final=True, landing_method="observed", landing_confidence=0.9)
    s = await coord.on_event(t, event("landing_final", f))
    assert s.session_id != first.session_id
    assert (await stores["sessions"].get(first.session_id)).landing_ts is None
    assert len(await stores["sessions"].list_open(airfield_id=AF)) == 2


async def test_events_without_takeoff_time_are_ignored(coord, stores):
    """Never attach an event without takeoff_time to another open session."""
    t = tenant()
    first = await coord.on_event(t, event("takeoff", make_flight()))
    for etype, extra in (
        ("landing_final", dict(landing_time=LANDING, landing_final=True,
                               landing_method="observed", landing_confidence=0.9)),
        ("landing", dict(landing_time=LANDING)),
        ("touch_and_go", dict(landing_count=2, touch_go_confidence=0.8)),
        ("launch_type_detected", dict(launch_type="winch")),
        ("landing_retracted", dict()),
    ):
        f = make_flight(takeoff_time="", **extra)
        assert await coord.on_event(t, event(etype, f)) is None, etype
    kept = await stores["sessions"].get(first.session_id)
    assert kept.landing_ts is None and kept.landing_count == 1
    assert kept.start_type_detected is None and kept.state == SessionState.TRACKING
    assert len(await stores["sessions"].list_open()) == 1

    # no open session at all and no takeoff time -> ignored as well
    f = make_flight(flarm_id="000000", takeoff_time="", landing_time=LANDING, landing_final=True)
    assert await coord.on_event(t, event("landing_final", f)) is None
    assert len(await stores["sessions"].list_open()) == 1


async def test_visitor_events_are_ignored(coord, stores):
    t = tenant()
    for etype, extra in (
        ("takeoff", dict(status=FlightStatus.TAKEOFF)),
        ("launch_type_detected", dict(launch_type="aerotow")),
        ("touch_and_go", dict(landing_count=2)),
        ("landing_final", dict(landing_time=LANDING, landing_final=True,
                               status=FlightStatus.LANDING)),
        ("landing_retracted", dict()),
    ):
        f = make_flight(is_visitor=True, takeoff_airfield="Unterwoessen Airfield (EDPU)",
                        **extra)
        assert await coord.on_event(t, event(etype, f)) is None, etype
    assert await stores["sessions"].list_open() == []
    # The same aircraft as a home flight is booked normally
    s = await coord.on_event(t, event("takeoff", make_flight()))
    assert s is not None and s.state == SessionState.TRACKING


async def test_landing_retracted_after_completion_goes_to_review(coord, stores):
    t = tenant()
    s = await coord.on_event(t, event("takeoff", make_flight()))
    s.state = SessionState.COMPLETED
    s.landing_ts = parse_iso(LANDING)
    await stores["sessions"].save(s)
    s = await coord.on_event(t, event("landing_retracted", make_flight()))
    assert s.state == SessionState.REVIEW
    assert "landing_retracted_after_completion" in s.review_reasons()
    assert s.landing_ts is None


async def test_ignored_and_malformed_events(coord, stores):
    t = tenant()
    for etype in ("flight_restarted", "sticky_landed_expired", "alarm", "beacon", None):
        assert await coord.on_event(t, {"type": etype, "flarm_id": FID, "data": {}}) is None
    assert await coord.on_event(t, {"type": "takeoff", "flarm_id": "", "data": {}}) is None
    assert await coord.on_event(t, {"type": "takeoff"}) is None
    assert await stores["sessions"].list_open() == []


async def test_takeoff_without_time_is_ignored(coord, stores):
    """No clock fallback: a takeoff without takeoff_time creates no session."""
    f = make_flight(takeoff_time="")
    assert await coord.on_event(tenant(), event("takeoff", f)) is None
    assert await stores["sessions"].list_open() == []


async def test_process_hook_is_noop_without_writer(coord):
    s = await coord.on_event(tenant(), event("takeoff", make_flight()))
    assert await coord.process(s, tenant(), trigger="test") is None


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

async def test_recovery_takes_landing_from_flight_log(coord, stores):
    t = tenant()
    s = await coord.on_event(t, event("takeoff", make_flight()))
    other = await coord.on_event(t, event("takeoff", make_flight(flarm_id="3E1234",
                                                                  registration="D-EKKW")))
    calls: list[tuple] = []

    async def fetch(airfield_id, flarm_id, takeoff_ts):
        calls.append((airfield_id, flarm_id, takeoff_ts))
        if flarm_id != FID:
            return []
        return [{
            "source": "flight_log",
            "landing_time": parse_iso(LANDING),
            "landing_count": 3,
            "landing_method": "observed",
            "landing_confidence": 0.8,
            "launch_type": "aerotow",
            "tow_plane_registration": "D-EKKW",
            "release_altitude_agl": 610,
            "release_time": parse_iso(RELEASE),
            "release_method": "towplane_max",
            "tow_duration_s": 420,
            "pairing_confidence": 0.75,
        }, {
            "source": "flight_status",
            "landing_time": parse_iso(LANDING),
            "landing_count": 3,
            "landing_method": "observed",
            "landing_confidence": 0.8,
            "launch_type": "aerotow",
            "tow_plane_registration": "D-EKKW",
            "release_altitude_agl": 610,
            "release_time": parse_iso(RELEASE),
            "release_method": "towplane_max",
            "tow_duration_s": 420,
            "pairing_confidence": 0.75,
        }]

    assert await coord.recover(t, fetch_flight_rows=fetch) == 1
    assert {c[1] for c in calls} == {FID, "3E1234"}
    assert all(c[0] == AF for c in calls)

    r = await stores["sessions"].get(s.session_id)
    assert r.landing_ts == parse_iso(LANDING)
    assert r.landing_count == 3
    assert r.landing_method == "observed"
    assert r.conf_landing == pytest.approx(0.8)
    assert r.start_type_detected == "aerotow"
    assert r.tow_registration == "D-EKKW"
    assert r.release_alt_agl_m == 610
    assert r.release_ts == parse_iso(RELEASE)
    assert r.tow_time_min == 7
    assert r.conf_pairing == pytest.approx(0.75)
    assert (await stores["sessions"].get(other.session_id)).landing_ts is None

    entries = await stores["audit"].list(AF, session_id=s.session_id)
    assert len(entries) == 1
    assert entries[0].action == "recovery"
    assert "flight_log" in entries[0].detail
    assert "landing_ts" in entries[0].fields_sent

    # idempotent: second run finds nothing new
    assert await coord.recover(t, fetch_flight_rows=fetch) == 0
    assert len(await stores["audit"].list(AF)) == 1


async def test_recovery_does_not_overwrite_existing_session_data(coord, stores):
    t = tenant()
    f = make_flight(landing_time=LANDING, landing_final=True, landing_method="observed",
                    landing_confidence=0.9, landing_count=2)
    s = await coord.on_event(t, event("landing_final", f))

    async def fetch(airfield_id, flarm_id, takeoff_ts):
        return [{"source": "flight_status", "landing_time": parse_iso("2025-05-01T11:30:00Z"),
                 "landing_count": 1, "landing_method": "silence", "landing_confidence": 0.5,
                 "launch_type": "winch"}]

    assert await coord.recover(t, fetch_flight_rows=fetch) == 1     # only launch_type is new
    r = await stores["sessions"].get(s.session_id)
    assert r.landing_ts == parse_iso(LANDING)
    assert r.landing_method == "observed"
    assert r.landing_count == 2
    assert r.start_type_detected == "winch"


async def test_recovery_uses_constructor_fetcher_and_skips_completed(stores):
    async def fetch(airfield_id, flarm_id, takeoff_ts):
        return [{"source": "flight_log", "landing_time": parse_iso(LANDING)}]

    coord = SyncCoordinator(**stores, clock=lambda: NOW, fetch_flight_rows=fetch)
    t = tenant()
    s = await coord.on_event(t, event("takeoff", make_flight()))
    s.state = SessionState.COMPLETED
    await stores["sessions"].save(s)
    assert await coord.recover(t) == 0

    coord_without = SyncCoordinator(**stores)
    with pytest.raises(RuntimeError):
        await coord_without.recover(t)


def test_parse_iso_variants():
    assert parse_iso("2025-05-01T10:00:00Z") == datetime(2025, 5, 1, 10, tzinfo=timezone.utc)
    assert parse_iso("2025-05-01T12:00:00+02:00") == datetime(2025, 5, 1, 10, tzinfo=timezone.utc)
    assert parse_iso("2025-05-01T10:00:00") == datetime(2025, 5, 1, 10, tzinfo=timezone.utc)
    assert parse_iso("") is None and parse_iso(None) is None and parse_iso("garbage") is None
    naive = datetime(2025, 5, 1, 10)
    assert parse_iso(naive) == naive.replace(tzinfo=timezone.utc)
    assert parse_iso(NOW + timedelta(0)) == NOW
