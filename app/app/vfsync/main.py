"""VF-Sync worker wiring.

``python -m app.vfsync`` -> ``run()``. Builds the PostgreSQL stores, the
budget guard, writer and coordinator, then runs these loops until
SIGTERM/SIGINT:

- event consumer  (Redis PubSub event:* -> coordinator)
- scheduler       (15 min / hourly / 21:00 / 03:00 runs)
- config reload   (every VFSYNC_CONFIG_RELOAD_S; new tenants get recovery)
- config listener (Redis PubSub vfsync:config -> immediate reload; the
                   periodic reload stays as fallback)
- health          (Redis hash vfsync:health + HTTP /healthz)
- monitoring      (alert rules R-12 every minute)

Importing this module has no side effects; connections are opened
inside ``run()``.
"""

import asyncio
import inspect
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog

from app.config import settings
from app.db.connection import close_db, init_db
from app.redis_client import close_redis, init_redis
from app.vfsync.alerts import AlertSink, MonitoringSnapshot, TenantSnapshot, evaluate
from app.vfsync.audit import AuditLog
from app.vfsync.budget import BudgetGuard
from app.vfsync.coordinator import SyncCoordinator
from app.vfsync.event_consumer import EventConsumer
from app.vfsync.health import build_health_app, serve_health
from app.vfsync.logging import configure_logging
from app.vfsync.models import TenantConfig, utcnow
from app.vfsync.redis_keys import VFSYNC_CONFIG_CHANNEL, VFSYNC_HEALTH_KEY
from app.vfsync.scheduler import Scheduler
from app.vfsync.stores import ConfigStore, SessionStore
from app.vfsync.stores_pg import (
    PgAuditStore,
    PgBudgetStore,
    PgConfigStore,
    PgSessionStore,
    make_flight_row_fetcher,
)
from app.vfsync.writer import VfWriter

log = structlog.get_logger()

HEALTH_KEY = VFSYNC_HEALTH_KEY
MONITORING_INTERVAL_S = 60


@dataclass
class RuntimeState:
    """Mutable worker state shared between the loops."""
    tenants: list[TenantConfig] = field(default_factory=list)
    status: str = "starting"
    started_at: datetime = field(default_factory=utcnow)
    consumer: EventConsumer | None = None
    coordinator: SyncCoordinator | None = None
    guard: BudgetGuard | None = None
    sessions: SessionStore | None = None
    config: ConfigStore | None = None
    redis: Any = None
    aprs_down_since: datetime | None = None
    last_snapshot: dict[str, Any] = field(default_factory=dict)
    # Serialises reload_config: the periodic loop and the PubSub listener
    # may fire at the same time; without the lock both would see the same
    # tenant as "added" and run its recovery twice.
    reload_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def config_errors(self) -> list[str]:
        return list(getattr(self.config, "decrypt_failed", []) or [])

    def enabled_tenants(self) -> list[TenantConfig]:
        return self.tenants

    @property
    def last_event_ts(self) -> datetime | None:
        return self.consumer.last_event_ts if self.consumer else None


# ---------------------------------------------------------------------------
# Config / recovery
# ---------------------------------------------------------------------------

async def recover_tenants(coordinator: SyncCoordinator, tenants: list[TenantConfig]) -> None:
    """Run recovery for each tenant; one failing tenant does not stop the rest."""
    for tenant in tenants:
        try:
            await coordinator.recover(tenant)
        except Exception:
            log.exception("vfsync_recovery_failed", slug=tenant.slug)


async def reload_config(state: RuntimeState, config: ConfigStore,
                        coordinator: SyncCoordinator) -> None:
    """Reload enabled tenants; newly enabled tenants get a recovery run,
    reloaded credentials lift a login pause.

    Serialised via ``state.reload_lock`` (periodic loop and PubSub
    listener share it), so each newly enabled tenant is recovered once.
    """
    async with state.reload_lock:
        tenants = await config.load_enabled()
        known = {t.airfield_id: t for t in state.tenants}
        added = [t for t in tenants if t.airfield_id not in known]
        removed = [t.slug for t in state.tenants
                   if t.airfield_id not in {x.airfield_id for x in tenants}]
        for t in tenants:
            old = known.get(t.airfield_id)
            if old and (old.vf_password_md5, old.vf_appkey, old.vf_username) != (
                    t.vf_password_md5, t.vf_appkey, t.vf_username):
                coordinator.resume(t)
        state.tenants = tenants
        if added or removed:
            log.info("vfsync_config_changed", added=[t.slug for t in added], removed=removed,
                     enabled=[t.slug for t in tenants])
        if added:
            await recover_tenants(coordinator, added)


async def config_reload_loop(state: RuntimeState, config: ConfigStore,
                             coordinator: SyncCoordinator, interval_s: float) -> None:
    """Periodic fallback reload (the PubSub listener below is the fast path)."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            await reload_config(state, config, coordinator)
        except Exception:
            log.exception("vfsync_config_reload_failed")


async def _close_pubsub(pubsub: Any, channel: str) -> None:
    for step in (lambda: pubsub.unsubscribe(channel), pubsub.close):
        try:
            result = step()
            if inspect.isawaitable(result):
                await result
        except Exception:  # pragma: no cover - best effort on shutdown
            log.debug("vfsync_config_listener_close_failed", exc_info=True)


async def config_change_listener(redis: Any, state: RuntimeState, config: ConfigStore,
                                 coordinator: SyncCoordinator,
                                 channel: str = VFSYNC_CONFIG_CHANNEL,
                                 reconnect_delay_s: float | None = None) -> None:
    """Reload the tenant config as soon as a ``vfsync:config`` message arrives.

    The CLI tools and the config API publish the airfield slug after every
    successful write. Same subscribe/reconnect/cancel behaviour as
    ``EventConsumer.run``: one failing reload never stops the listener, a
    Redis error re-subscribes after ``reconnect_delay_s``.
    """
    delay = settings.vfsync_pubsub_reconnect_s if reconnect_delay_s is None else reconnect_delay_s
    while True:
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(channel)
            log.info("vfsync_config_listener_started", channel=channel)
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                payload = message.get("data")
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8", "replace")
                log.info("vfsync_config_reload_triggered", payload=payload)
                try:
                    await reload_config(state, config, coordinator)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("vfsync_config_reload_failed", payload=payload)
            log.warning("vfsync_config_listener_stream_ended", retry_in_s=delay)
        except asyncio.CancelledError:
            log.info("vfsync_config_listener_stopping")
            raise
        except Exception:
            log.exception("vfsync_config_listener_error", retry_in_s=delay)
        finally:
            await _close_pubsub(pubsub, channel)
        await asyncio.sleep(delay)


# ---------------------------------------------------------------------------
# Health / monitoring
# ---------------------------------------------------------------------------

OGN_HEALTH_KEY = "ogn:health"


async def aprs_state(state: RuntimeState) -> tuple[bool | None, timedelta | None]:
    """(connected, down_for) from the APRS worker's ogn:health hash."""
    if state.redis is None:
        return None, None
    try:
        h = await state.redis.hgetall(OGN_HEALTH_KEY)
    except Exception:
        log.exception("vfsync_ogn_health_read_failed")
        return None, None
    if not h:
        return None, None
    connected = str(h.get("connected", "")).lower() == "true"
    now = utcnow()
    if connected:
        state.aprs_down_since = None
        return True, None
    state.aprs_down_since = state.aprs_down_since or now
    return False, now - state.aprs_down_since


async def build_snapshot(state: RuntimeState) -> dict[str, Any]:
    """Current health snapshot (Redis hash + /healthz body)."""
    connected, down_for = await aprs_state(state)
    snap: dict[str, Any] = {
        "status": state.status,
        "started_at": state.started_at,
        "last_event_ts": state.last_event_ts,
        "tenants": [t.slug for t in state.tenants],
        "updated_at": utcnow(),
        "open_sessions": -1,
        "budget": {},
        "paused": sorted(state.coordinator.paused) if state.coordinator else [],
        "config_errors": state.config_errors,
        "aprs_connected": connected,
        "aprs_down_for_s": int(down_for.total_seconds()) if down_for else 0,
    }
    try:
        if state.sessions is not None:
            snap["open_sessions"] = len(await state.sessions.list_open())
        if state.guard is not None and state.sessions is not None:
            for t in state.tenants:
                movements = await state.sessions.count_today(
                    t.airfield_id, state.guard.day_for(t), t.timezone,
                )
                stage = await state.guard.stage(t, movements)
                snap["budget"][t.slug] = {
                    "used": await state.guard.used(t),
                    "daily_budget": t.daily_budget,
                    "stage": stage.value,
                    "movements": movements,
                }
    except Exception:
        log.exception("vfsync_snapshot_failed")
    state.last_snapshot = snap
    return snap


async def publish_health(redis: Any, state: RuntimeState) -> None:
    """Write the vfsync:health hash (read by the API status endpoint)."""
    snap = await build_snapshot(state)
    mapping = {
        "status": snap["status"],
        "last_event_ts": snap["last_event_ts"].isoformat() if snap["last_event_ts"] else "",
        "open_sessions": str(snap["open_sessions"]),
        "tenants": ",".join(snap["tenants"]),
        "updated_at": snap["updated_at"].isoformat(),
        "aprs_connected": "" if snap["aprs_connected"] is None else str(snap["aprs_connected"]).lower(),
        "config_errors": ",".join(snap["config_errors"]),
    }
    for slug, b in snap["budget"].items():
        mapping[f"budget_used:{slug}"] = str(b["used"])
        mapping[f"stage:{slug}"] = b["stage"]
    await redis.hset(HEALTH_KEY, mapping=mapping)


async def health_loop(redis: Any, state: RuntimeState, interval_s: float) -> None:
    while True:
        try:
            await publish_health(redis, state)
        except Exception:
            log.exception("vfsync_health_publish_failed")
        await asyncio.sleep(interval_s)


async def monitoring_snapshot(state: RuntimeState) -> MonitoringSnapshot:
    """Collect the inputs of the alert rules (R-12)."""
    now = utcnow()
    tenants: list[TenantSnapshot] = []
    coord = state.coordinator
    for t in state.tenants:
        used = await state.guard.used(t) if state.guard else 0
        oldest: timedelta | None = None
        if state.sessions is not None:
            open_sessions = await state.sessions.list_open(airfield_id=t.airfield_id)
            pending = [s for s in open_sessions if s.landing_ts is not None]
            if pending:
                oldest = now - min(s.created_at for s in pending)
        tenants.append(TenantSnapshot(
            slug=t.slug, budget_used=used, daily_budget=t.daily_budget,
            oldest_pending_age=oldest,
            write_error_streak=coord.write_error_streak.get(t.slug, 0) if coord else 0,
            login_forbidden=(t.slug in coord.paused) if coord else False,
        ))
    connected, down_for = await aprs_state(state)
    return MonitoringSnapshot(now=now, last_event_ts=state.last_event_ts,
                              worker_started_at=state.started_at, tenants=tenants,
                              aprs_connected=connected, aprs_down_for=down_for,
                              config_errors=state.config_errors)


async def monitoring_loop(state: RuntimeState, sink: AlertSink, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            snap = await monitoring_snapshot(state)
            await sink.send_all(evaluate(snap))
        except Exception:
            log.exception("vfsync_monitoring_failed")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

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
        audit_store = PgAuditStore(pool)
        budget_store = PgBudgetStore(pool)
        guard = BudgetGuard(budget_store)
        coordinator = SyncCoordinator(
            sessions=session_store,
            audit=audit_store,
            budget=budget_store,
            config=config_store,
            fetch_flight_rows=make_flight_row_fetcher(pool),
            guard=guard,
            list_cache_s=settings.vfsync_list_cache_s,
        )
        coordinator.writer = VfWriter(
            coordinator.client_for, session_store, AuditLog(audit_store), guard,
        )
        state = RuntimeState(coordinator=coordinator, guard=guard, sessions=session_store,
                             config=config_store, redis=redis)
        await reload_config(state, config_store, coordinator)
        log.info("vfsync_tenants_loaded", enabled=[t.slug for t in state.tenants])

        consumer = EventConsumer(
            redis,
            tenants_provider=state.enabled_tenants,
            coordinator=coordinator,
        )
        state.consumer = consumer
        scheduler = Scheduler(
            process=coordinator.process,
            sessions=session_store,
            budget=budget_store,
            tenants_provider=state.enabled_tenants,
        )
        sink = AlertSink()
        log.info("vfsync_alert_channels", channels=sink.channels)
        health_app = build_health_app(
            lambda: state.last_snapshot or {"status": state.status, "tenants": []}
        )
        state.status = "ok"
        tasks = [
            asyncio.create_task(consumer.run(), name="vfsync-consumer"),
            asyncio.create_task(scheduler.run(), name="vfsync-scheduler"),
            asyncio.create_task(
                config_reload_loop(state, config_store, coordinator,
                                   settings.vfsync_config_reload_s),
                name="vfsync-config-reload",
            ),
            asyncio.create_task(
                config_change_listener(redis, state, config_store, coordinator),
                name="vfsync-config-listener",
            ),
            asyncio.create_task(
                health_loop(redis, state, settings.vfsync_health_interval_s),
                name="vfsync-health",
            ),
            asyncio.create_task(
                monitoring_loop(state, sink, MONITORING_INTERVAL_S),
                name="vfsync-monitoring",
            ),
            asyncio.create_task(
                serve_health(health_app, settings.vfsync_health_port, shutdown),
                name="vfsync-health-http",
            ),
        ]
        log.info("vfsync_started", tasks=[t.get_name() for t in tasks])
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
