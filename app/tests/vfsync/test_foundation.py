"""AP-0: crypto round trip, secret masking, session model helpers."""

from uuid import uuid4

import pytest
from cryptography.fernet import InvalidToken

from app.vfsync import crypto
from app.vfsync.logging import mask_secrets
from app.vfsync.models import Session, SessionState, TenantConfig

KEY_A = crypto.generate_key()
KEY_B = crypto.generate_key()


def test_encrypt_decrypt_round_trip():
    token = crypto.encrypt("5f4dcc3b5aa765d61d8327deb882cf99", key=KEY_A)
    assert isinstance(token, bytes)
    assert b"5f4dcc3b" not in token
    assert crypto.decrypt(token, key=KEY_A) == "5f4dcc3b5aa765d61d8327deb882cf99"
    assert crypto.decrypt(memoryview(token), key=KEY_A) == "5f4dcc3b5aa765d61d8327deb882cf99"
    assert crypto.decrypt(None, key=KEY_A) is None


def test_decrypt_with_wrong_key_fails():
    token = crypto.encrypt("appkey-value", key=KEY_A)
    with pytest.raises(InvalidToken):
        crypto.decrypt(token, key=KEY_B)


def test_missing_key_is_an_error(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "vfsync_cred_key", "")
    with pytest.raises(crypto.CredentialKeyMissing):
        crypto.encrypt("x")


def test_log_processor_masks_secrets_at_any_depth():
    event = {
        "event": "signin",
        "username": "tech-user",
        "password": "hunter2",
        "payload": {"appkey": "abc", "accesstoken": "tok", "callsign": "D-1234"},
        "items": [{"token": "t"}],
    }
    masked = mask_secrets(None, None, event)
    assert masked["username"] == "tech-user"
    assert masked["password"] == "***"
    assert masked["payload"]["appkey"] == "***"
    assert masked["payload"]["accesstoken"] == "***"
    assert masked["payload"]["callsign"] == "D-1234"
    assert masked["items"][0]["token"] == "***"


def test_tenant_repr_never_contains_credentials():
    t = TenantConfig(airfield_id=uuid4(), slug="test", vf_username="u",
                     vf_password_md5="deadbeef", vf_appkey="secret-key")
    assert "deadbeef" not in repr(t)
    assert "secret-key" not in repr(t)
    assert t.has_credentials
    assert t.flag("live_release") is False


def test_session_review_reasons_are_deduplicated():
    s = Session(airfield_id=uuid4(), flarm_id="DDA5BA")
    assert s.is_open and s.is_airborne
    s.add_review_reason("ambiguous_match")
    s.add_review_reason("ambiguous_match")
    s.add_review_reason("towheight_low_confidence")
    assert s.review_reasons() == ["ambiguous_match", "towheight_low_confidence"]
    s.state = SessionState.COMPLETED
    assert not s.is_open
