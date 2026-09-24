"""Synthetic flight publisher: event sequence and payload shape."""

from datetime import datetime, timezone

from app.tracking.flight_state import FlightStatus
from app.vfsync.simulate import build_events, build_parser

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
