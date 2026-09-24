"""VF-Sync worker wiring (filled in by AP-3/AP-6/AP-11).

Skeleton: validates configuration and idles. The real run loop wires
stores, event consumer, scheduler, health and alerting.
"""

import asyncio
import signal

import structlog

from app.config import settings
from app.vfsync.logging import configure_logging

log = structlog.get_logger()


async def run() -> None:
    """Run the VF-Sync worker until SIGTERM/SIGINT."""
    configure_logging()
    if not settings.vfsync_enabled:
        log.warning("vfsync_disabled", hint="set VFSYNC_ENABLED=true")
        return
    if not settings.vfsync_cred_key:
        log.error("vfsync_cred_key_missing")
        return

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    log.info("vfsync_starting", health_port=settings.vfsync_health_port)
    await shutdown.wait()
    log.info("vfsync_stopped")
