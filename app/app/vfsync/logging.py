"""structlog configuration for the VF-Sync worker with a secret filter.

Any log event key (at any nesting level of dict values) whose name looks
like a credential is replaced by "***" before rendering. This is the last
line of defence - code must still never pass secrets to the logger.
"""

import logging
from typing import Any

import structlog

from app.config import settings

SECRET_KEYS = frozenset({
    "password", "password_md5", "passwd", "appkey", "app_key",
    "accesstoken", "access_token", "token", "authorization",
    "vf_password_enc", "vf_appkey_enc", "vf_password_md5", "vf_appkey",
    "cred_key", "vfsync_cred_key", "secret",
})

MASK = "***"


def _mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (MASK if str(k).lower() in SECRET_KEYS else _mask(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_mask(v) for v in value)
    return value


def mask_secrets(_logger, _method, event_dict: dict) -> dict:
    """structlog processor: mask credential-like keys."""
    return _mask(event_dict)


def configure_logging() -> None:
    """Configure structlog with the secret filter (idempotent)."""
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            mask_secrets,
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=False,
    )
