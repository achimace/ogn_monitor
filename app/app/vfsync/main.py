"""VF-Sync worker wiring: stores, coordinator, event consumer, health.

``python -m app.vfsync`` -> ``run()``. Scheduler (AP-6), HTTP health and
alerting (AP-11) plug in here later. Importing this module has no side
effects; connections are opened inside ``run()``.
"""

import asyncio
import signal
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from app.config import settings
from app.db.connection import close_db, init_db
from app.redis_client import close_redis, init_redis
from app.vfsync.coordinator import SyncCoordinator
from app.vfsync.event_consumer import EventConsumer
from app.vfsync.logging import configure_logging
from app.vfsync.models import TenantConfig
from app.vfsync.stores import ConfigStore, SessionStore
from app.vfsync.stores_pg import (
    PgAuditStore,
    PgBudgetStore,
    PgConfigStore,
    PgSessionStore,
    make_flight_row_fetcher,
)

log = structlog.get_logger()

HEALTH_KEY = "vfsync:health"


@dataclass
class RuntimeState:
    """Mutable worker state shared between the loops."""
    tenants: list[TenantConfig] = field(default_factory=list)
    status: str = "starting"
    consumer: EventConsumer | None = None

    def enabled_tenants(self) -> list[TenantConfig]:
        return self.tenants

    @property
    def last_event_ts(self) -> datetime | None:
        return self.consumer.last_event_ts if self.consumer else None


async def recover_tenants(coordinator: SyncCoordinator, tenants: list[TenantConfig]) -> None:
    """Run recovery for each tenant; one failing tenant does not stop the rest."""
    for tenant in tenants:
        try:
            await coordinator.recover(tenant)
        except Exception:
            log.exception("vfsync_recovery_failed", slug=tenant.slug)


async def reload_config(state: RuntimeState, config: ConfigStore,
                        coordinator: SyncCoordinator) -> None:
    """Reload enabled tenants; newly enabled tenants get a recovery run."""
    tenants = await config.load_enabled()
    known = {t.airfield_id for t in state.tenants}
    added = [t for t in tenants if t.airfield_id not in known]
    removed = [t.slug for t in state.tenants if t.airfield_id not in {x.airfield_id for x in tenants}]
    state.tenants = tenants
    if added or removed:
        log.info("vfsync_config_changed", added=[t.slug for t in added], removed=removed,
                 enabled=[t.slug for t in tenants])
    if added:
        await recover_tenants(coordinator, added)


async def config_reload_loop(state: RuntimeState, config: ConfigStore,
                             coordinator: SyncCoordinator, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await reload_config(state, config, coordinator)
        except Exception:
            log.exception("vfsync_config_reload_failed")


async def publish_health(redis: Any, state: RuntimeState, sessions: SessionStore) -> None:
    """Write the vfsync:health hash (read by health.py / AP-11)."""
    try:
        open_sessions = len(await sessions.list_open())
    except Exception:
        log.exception("vfsync_health_sessions_failed")
        open_sessions = -1
    await redis.hset(HEALTH_KEY, mapping={
        "status": state.status,
        "last_event_ts": state.last_event_ts.isoformat() if state.last_event_ts else "",
        "open_sessions": str(open_sessions),
        "tenants": ",".join(t.slug for t in state.tenants),
        "updated_at": datetime.now(tz=timezone.utc).isoformat(),
    })


async def health_loop(redis: Any, state: RuntimeState, sessions: SessionStore,
                      interval_s: float) -> None:
    while True:
        try:
            await publish_health(redis, state, sessions)
        except Exception:
            log.exception("vfsync_health_publish_failed")
        await asyncio.sleep(interval_s)


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
    pool = await init_db()
    redis = await init_redis()
    tasks: list[asyncio.Task] = []
    try:
        config_store = PgConfigStore(pool)
        session_store = PgSessionStore(pool)
        coordinator = SyncCoordinator(
            sessions=session_store,
            audit=PgAuditStore(pool),
            budget=PgBudgetStore(pool),
            config=config_store,
            fetch_flight_rows=make_flight_row_fetcher(pool),
        )
        state = RuntimeState()
        await reload_config(state, config_store, coordinator)
        log.info("vfsync_tenants_loaded", enabled=[t.slug for t in state.tenants])

        consumer = EventConsumer(
            redis,
            tenants_provider=state.enabled_tenants,
            coordinator=coordinator,
        )
        state.consumer = consumer
        state.status = "ok"
        tasks = [
            asyncio.create_task(consumer.run(), name="vfsync-consumer"),
            asyncio.create_task(
                config_reload_loop(state, config_store, coordinator,
                                   settings.vfsync_config_reload_s),
                name="vfsync-config-reload",
            ),
            asyncio.create_task(
                health_loop(redis, state, session_store, settings.vfsync_health_interval_s),
                name="vfsync-health",
            ),
        ]
        await shutdown.wait()
        state.status = "stopping"
    finally:
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await close_redis()
        await close_db()
        log.info("vfsync_stopped")
