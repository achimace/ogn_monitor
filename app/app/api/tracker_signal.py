"""Best-effort tracker:config signal from the API to the APRS worker.

After a write that changes what the worker must know immediately
(ignore list, tenant aircraft) the airfield slug is published on
``tracker:config``. The worker's listener then reloads the airfield
configs and the aircraft cache within seconds instead of waiting for
its periodic reloads. Shared by ``app.api.ignored_aircraft`` and
``app.api.aircraft`` (single definition, no copies).
"""

from typing import Any

import structlog

from app.redis_client import get_redis
from app.tracking.redis_keys import TRACKER_CONFIG_CHANNEL

log = structlog.get_logger()


def redis_client() -> Any:
    """Redis client of the API process; None when not initialized (tests)."""
    try:
        return get_redis()
    except RuntimeError:
        return None


async def publish_tracker_config_changed(redis: Any, slug: str) -> bool:
    """Tell the APRS worker to reload its airfield configs right away.

    Best effort: without Redis or on a Redis error the DB write stays
    valid - the worker picks the change up with its periodic reload.
    Returns True if the message was published.
    """
    if redis is None:
        return False
    try:
        await redis.publish(TRACKER_CONFIG_CHANNEL, slug)
    except Exception as exc:  # noqa: BLE001 - never fail the write
        log.warning("tracker_config_signal_failed", slug=slug, error=type(exc).__name__)
        return False
    return True
