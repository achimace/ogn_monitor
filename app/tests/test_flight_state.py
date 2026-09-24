"""FlightState Redis round trip and aircraft role mapping."""

from app.data.aircraft_resolver import role_from_row
from app.tracking.flight_state import FlightState, FlightStatus


def test_redis_round_trip_keeps_tracking_hardening_fields():
    f = FlightState(
        flarm_id="DDA5BA",
        airfield_slug="test",
        status=FlightStatus.LANDING,
        aircraft_role="glider",
        flarm_aircraft_type=1,
        launch_type="aerotow",
        tow_plane_flarm_id="TOW001",
        tow_plane_reg="D-ETOW",
        release_alt_m=1095.0,
        release_alt_agl_m=435.0,
        release_time="2026-09-24T10:30:00Z",
        release_method="pair_separation",
        tow_duration_s=420,
        pairing_confidence=0.93,
        landing_count=3,
        landing_method="silence",
        landing_confidence=0.8,
        touch_go_confidence=0.9,
        landing_final=True,
        landing_time="2026-09-24T12:00:00Z",
    )

    data = f.to_redis_dict()
    assert all(isinstance(v, str) for v in data.values())

    g = FlightState.from_redis(data, "test")
    assert g.status == FlightStatus.LANDING
    assert g.aircraft_role == "glider"
    assert g.flarm_aircraft_type == 1
    assert g.launch_type == "aerotow"
    assert g.tow_plane_reg == "D-ETOW"
    assert g.release_alt_m == 1095.0
    assert g.release_alt_agl_m == 435.0
    assert g.release_time == "2026-09-24T10:30:00Z"
    assert g.release_method == "pair_separation"
    assert g.tow_duration_s == 420
    assert g.pairing_confidence == 0.93
    assert g.landing_count == 3
    assert g.landing_method == "silence"
    assert g.landing_confidence == 0.8
    assert g.touch_go_confidence == 0.9
    assert g.landing_final is True


def test_from_redis_defaults_for_legacy_hashes():
    """Hashes written before the hardening must still restore."""
    legacy = {
        "flarm_id": "DDA5BA",
        "status": "2",
        "launch_type": "winch",
        "release_alt_m": "1000",
    }
    f = FlightState.from_redis(legacy, "test")
    assert f.landing_count == 1
    assert f.landing_final is False
    assert f.landing_method == ""
    assert f.release_alt_agl_m == 0.0
    assert f.pairing_confidence == 0.0


def test_role_from_row_prefers_explicit_role():
    assert role_from_row("towplane", "glider") == "towplane"
    assert role_from_row("MotorGlider_SL", None) == "motorglider_sl"
    assert role_from_row("bogus", "tow_plane") == "towplane"
    assert role_from_row(None, "tmg") == "motorglider_sl"
    assert role_from_row(None, "motor_glider") == "motorglider_sl"
    assert role_from_row(None, "glider") == "glider"
    assert role_from_row(None, None) == ""
    assert role_from_row("", "unknown") == ""
