"""Settings validation.

The track retention is bounded by the OGN data usage policy (no
re-distribution of OGN data older than 24 h) - the API serves the track
stream to browsers, so a longer retention would violate the licence.
"""

import pytest
from pydantic import ValidationError

from app.config import OGN_MAX_REDISTRIBUTION_AGE_S, Settings


def _settings(**overrides) -> Settings:
    # _env_file=None: ignore a local .env so the test is hermetic
    return Settings(_env_file=None, **overrides)


def test_track_retention_default_is_within_ogn_policy():
    assert OGN_MAX_REDISTRIBUTION_AGE_S == 86400
    assert _settings().track_retention_s <= OGN_MAX_REDISTRIBUTION_AGE_S


@pytest.mark.parametrize("value", [3600, 86399, 86400])
def test_track_retention_accepts_values_up_to_24h(value):
    assert _settings(track_retention_s=value).track_retention_s == value


@pytest.mark.parametrize("value", [86401, 172800, 7 * 86400])
def test_track_retention_rejects_values_over_24h(value):
    with pytest.raises(ValidationError) as exc:
        _settings(track_retention_s=value)
    assert "24 hours" in str(exc.value)
    assert "ogn-data-usage" in str(exc.value)


def test_track_retention_rejects_env_override_over_24h(monkeypatch):
    monkeypatch.setenv("TRACK_RETENTION_S", "100000")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# ---------------------------------------------------------------------------
# Aircraft type filter
# ---------------------------------------------------------------------------

def test_ignored_categories_default_drops_helicopters_but_not_powered():
    from app.tracking.aircraft_category import AircraftCategory as C

    cats = _settings().ignored_categories()
    assert C.HELICOPTER in cats and C.UAV in cats and C.STATIC in cats
    assert C.BALLOON_AIRSHIP in cats and C.PARACHUTE_DROP in cats
    for kept in (C.GLIDER, C.TOW_PLANE, C.MOTOR_GLIDER, C.POWERED, C.JET, C.ULTRALIGHT):
        assert kept not in cats


def test_ignored_categories_accepts_custom_list_and_empty():
    from app.tracking.aircraft_category import AircraftCategory as C

    s = _settings(ignored_aircraft_categories="Helicopter, jet")
    assert s.ignored_categories() == frozenset({C.HELICOPTER, C.JET})
    assert _settings(ignored_aircraft_categories="").ignored_categories() == frozenset()


@pytest.mark.parametrize("value", ["helicopter,spaceship", "unknown", "heli copter"])
def test_ignored_categories_rejects_unknown_names(value):
    with pytest.raises(ValidationError) as exc:
        _settings(ignored_aircraft_categories=value)
    assert "categor" in str(exc.value)


def test_ignored_categories_env_override(monkeypatch):
    from app.tracking.aircraft_category import AircraftCategory as C

    monkeypatch.setenv("IGNORED_AIRCRAFT_CATEGORIES", "uav")
    assert Settings(_env_file=None).ignored_categories() == frozenset({C.UAV})
