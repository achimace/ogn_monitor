"""vf_base_url policy: https only, allow-listed hosts (Kap. 8.4)."""

import pytest

from app.config import settings
from app.vfsync.urls import validate_base_url


def test_default_host_is_accepted_and_normalised():
    assert validate_base_url("https://www.vereinsflieger.de/") == "https://www.vereinsflieger.de"
    assert validate_base_url("HTTPS://WWW.Vereinsflieger.DE") == "https://WWW.Vereinsflieger.DE"


@pytest.mark.parametrize("url", [
    "http://www.vereinsflieger.de",          # no TLS
    "https://api:8000",                       # internal host (SSRF)
    "https://evil.example",                   # not allow-listed
    "https://www.vereinsflieger.de/interface",  # path
    "ftp://www.vereinsflieger.de",
    "",
])
def test_rejected(url):
    with pytest.raises(ValueError):
        validate_base_url(url)


def test_allow_list_and_insecure_switch(monkeypatch):
    monkeypatch.setattr(settings, "vfsync_allowed_hosts", "www.vereinsflieger.de, test.vf.example")
    assert validate_base_url("https://test.vf.example") == "https://test.vf.example"
    with pytest.raises(ValueError):
        validate_base_url("http://vfmock:8099")
    monkeypatch.setattr(settings, "vfsync_allow_insecure_base_url", True)
    assert validate_base_url("http://vfmock:8099") == "http://vfmock:8099"
