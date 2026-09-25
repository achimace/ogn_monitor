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
