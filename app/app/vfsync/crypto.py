"""Credential encryption for vf_sync_config (Fernet, key from ENV).

The key lives only in VFSYNC_CRED_KEY on the host. The database holds
Fernet tokens (BYTEA); plaintext credentials exist only in process memory.
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class CredentialKeyMissing(RuntimeError):
    """VFSYNC_CRED_KEY is not configured."""


def _fernet(key: str | None = None) -> Fernet:
    key = key or settings.vfsync_cred_key
    if not key:
        raise CredentialKeyMissing("VFSYNC_CRED_KEY is not set")
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str, key: str | None = None) -> bytes:
    """Encrypt a credential string to a Fernet token."""
    return _fernet(key).encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes | memoryview | None, key: str | None = None) -> str | None:
    """Decrypt a Fernet token; None stays None.

    Raises cryptography.fernet.InvalidToken if the key does not match.
    """
    if token is None:
        return None
    if isinstance(token, memoryview):
        token = token.tobytes()
    return _fernet(key).decrypt(bytes(token)).decode("utf-8")


def generate_key() -> str:
    """Generate a new Fernet key (base64 string) for VFSYNC_CRED_KEY."""
    return Fernet.generate_key().decode()


__all__ = ["encrypt", "decrypt", "generate_key", "CredentialKeyMissing", "InvalidToken"]
