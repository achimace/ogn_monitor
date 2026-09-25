"""Synthetic flight publisher: event sequence, payload shape and hot state."""

from datetime import datetime, timezone

from app.tracking.flight_state import FlightStatus
from app.tracking.redis_writer import SIMULATED_FIELD, SIMULATED_VALUE
from app.vfsync import simulate
from app.vfsync.simulate import LANDED_TTL_CUSHION_S, build_events, build_parser, hot_state_ttl

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _events(*argv: str):
    args = build_parser().parse_args(["--slug", "test", "--registration", "D-1234", *argv])
    return build_events(args, now=NOW)


def test_aerotow_sequence_and_snapshots_are_independent():
    events = _events("--type", "aerotow", "--touch-go", "1", "--duration-min", "45",
                     "--release-agl", "450", "--tow-minutes", "7")
    assert [e[0] for e in events] == [
        "takeoff", "launch_type_detected", "touch_and_go", "landing", "landing_final",
    ]
    takeoff, launch, tg, landing, final = [e[1] for e in events]
    assert takeoff.status == FlightStatus.TAKEOFF and takeoff.launch_type == "unknown"
    assert takeoff.takeoff_time == "2026-09-24T11:15:00Z"
    assert launch.launch_type == "aerotow" and launch.release_alt_agl_m == 450
    assert launch.tow_duration_s == 420 and launch.release_time == "2026-09-24T11:22:00Z"
    assert launch.landing_count == 1                      # snapshot, not mutated later
    assert tg.landing_count == 2 and tg.touch_go_confidence == 1.0
    assert landing.status == FlightStatus.LANDING and not landing.landing_final
    assert final.landing_final and final.landing_count == 2
    assert final.landing_time == "2026-09-24T11:58:00Z"
    # payload as published by the worker
    d = final.to_redis_dict()
    assert d["landing_count"] == "2" and d["release_alt_agl_m"] == "450"
    assert all(isinstance(v, str) for v in d.values())


def test_winch_has_no_tow_plane_and_only_takeoff_option():
    events = _events("--type", "winch")
    launch = events[1][1]
    assert launch.launch_type == "winch" and launch.tow_plane_reg == ""
    assert launch.release_method == "winch_vs_drop"


class RecordingWriter:
    """Records update_flight/publish_event in call order (fake RedisWriter)."""

    def __init__(self, redis):
        self.redis = redis
        self.calls: list[tuple] = []

    async def update_flight(self, slug, flarm_id, data, ttl=None):
        self.calls.append(("update_flight", slug, flarm_id, dict(data), ttl))

    async def publish_event(self, slug, event_type, flarm_id, data=None, message=""):
        self.calls.append(("publish_event", slug, event_type, flarm_id, dict(data or {})))


async def test_main_writes_hot_state_before_every_event(monkeypatch):
    """UAT Kap. 3: a reloaded monitor tab must see the simulated flight, so
    the flight hash is written (like FlightTracker) before each event and
    the landed flight stays in the hot state with the extended TTL."""
    writer_box: list[RecordingWriter] = []
    closed: list[bool] = []

    async def fake_init_redis():
        return object()

    async def fake_close_redis():
        closed.append(True)

    def make_writer(redis):
        w = RecordingWriter(redis)
        writer_box.append(w)
        return w

    monkeypatch.setattr(simulate, "init_redis", fake_init_redis)
    monkeypatch.setattr(simulate, "close_redis", fake_close_redis)
    monkeypatch.setattr(simulate, "RedisWriter", make_writer)

    await simulate.main(["--slug", "test", "--registration", "D-1234", "--flarm", "sim001",
                         "--type", "aerotow", "--touch-go", "1", "--delay", "0",
                         "--landed-visible-min", "120"])

    writer = writer_box[0]
    updates = [c for c in writer.calls if c[0] == "update_flight"]
    events = [c for c in writer.calls if c[0] == "publish_event"]
    assert [e[2] for e in events] == [
        "takeoff", "launch_type_detected", "touch_and_go", "landing", "landing_final",
    ]
    assert len(updates) == len(events) == 5
    # strict alternation: hash first, then the event with the same payload
    for i, (upd, ev) in enumerate(zip(updates, events)):
        assert writer.calls.index(upd) < writer.calls.index(ev)
        assert upd[1] == ev[1] == "test"
        assert upd[2] == ev[3] == "SIM001"
        assert upd[3] == ev[4]
        assert isinstance(upd[3], dict) and upd[3]["registration"] == "D-1234"
    # TTL semantics of FlightTracker: default while airborne, extended once landed
    assert [u[4] for u in updates[:3]] == [None, None, None]
    assert updates[3][4] == updates[4][4] == 120 * 60 + LANDED_TTL_CUSHION_S
    # landing_final keeps the flight in the hot state (no remove_flight);
    # to_redis_dict encodes booleans as "1"/"0"
    assert updates[3][3]["landing_final"] == "0"
    assert updates[4][3]["landing_final"] == "1"
    assert not any(c[0] == "remove_flight" for c in writer.calls)
    # every hash is marked so the APRS worker's recovery never adopts it
    assert all(u[3][SIMULATED_FIELD] == SIMULATED_VALUE for u in updates)
    assert closed == [True]


def test_marker_is_not_part_of_flight_state_payload():
    """The marker is added on the copy in main(), not in FlightState."""
    events = _events("--type", "winch")
    assert SIMULATED_FIELD not in events[-1][1].to_redis_dict()


def test_hot_state_ttl_follows_status():
    events = _events("--type", "winch")
    airborne, landed = events[1][1], events[-1][1]
    assert hot_state_ttl(airborne, 1440) is None
    assert hot_state_ttl(landed, 1440) == 1440 * 60 + LANDED_TTL_CUSHION_S
