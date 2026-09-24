"""Retry / housekeeping scheduler (Konzept Kap. 4.3, R-07).

- every 15 min: sessions in `awaiting_match` that are still airborne
  (a late-created VF flight gets its departure time while in the air)
- hourly: all open sessions (awaiting_match, matched, departure_written,
  tracking) - review sessions are left alone (a human decides)
- 21:00 tenant-local: closing run (same as hourly)
- 03:00 tenant-local: expire open sessions older than 7 days, log the
  previous day's budget

All decisions are made on an injected clock so the tick logic is unit
testable; `run()` just calls `tick()` once a minute.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import structlog

from app.vfsync.models import Session, SessionState, TenantConfig
from app.vfsync.stores import BudgetStore, SessionStore

log = structlog.get_logger()

RETRY_AIRBORNE_EVERY = timedelta(minutes=15)
RETRY_ALL_EVERY = timedelta(hours=1)
CLOSING_RUN_HOUR = 21
EXPIRY_RUN_HOUR = 3
SESSION_RETENTION_DAYS = 7
TICK_S = 60

RETRY_STATES = frozenset({
    SessionState.TRACKING,
    SessionState.AWAITING_MATCH,
    SessionState.MATCHED,
    SessionState.DEPARTURE_WRITTEN,
})

ProcessFn = Callable[[Session, TenantConfig, str], Awaitable[Any]]


@dataclass
class _TenantClock:
    last_airborne_retry: datetime | None = None
    last_full_retry: datetime | None = None
    closing_run_done: date | None = None
    expiry_run_done: date | None = None


@dataclass
class Scheduler:
    """Periodic retry and housekeeping runs per tenant."""
    process: ProcessFn
    sessions: SessionStore
    budget: BudgetStore
    tenants_provider: Callable[[], list[TenantConfig]]
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    retention_days: int = SESSION_RETENTION_DAYS
    _state: dict[Any, _TenantClock] = field(default_factory=dict)
    stats: dict[str, int] = field(default_factory=dict)

    async def run(self, tick_s: float = TICK_S) -> None:
        """Tick forever (cancelled on shutdown)."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("vfsync_scheduler_tick_failed")
            await asyncio.sleep(tick_s)

    async def tick(self, now: datetime | None = None) -> list[str]:
        """Run every job that is due for every enabled tenant.

        Returns the names of the jobs that ran (for tests / health).
        """
        now = now or self.clock()
        ran: list[str] = []
        for tenant in self.tenants_provider():
            st = self._state.setdefault(tenant.airfield_id, _TenantClock())
            local_now = now.astimezone(_tz(tenant))
            today = local_now.date()

            if _due(st.last_airborne_retry, now, RETRY_AIRBORNE_EVERY):
                st.last_airborne_retry = now
                n = await self.retry_airborne(tenant)
                ran.append(f"retry_airborne:{tenant.slug}:{n}")

            if _due(st.last_full_retry, now, RETRY_ALL_EVERY):
                st.last_full_retry = now
                n = await self.retry_open(tenant, trigger="retry_hourly")
                ran.append(f"retry_open:{tenant.slug}:{n}")

            if local_now.hour >= CLOSING_RUN_HOUR and st.closing_run_done != today:
                st.closing_run_done = today
                n = await self.retry_open(tenant, trigger="closing_run")
                ran.append(f"closing_run:{tenant.slug}:{n}")

            if local_now.hour >= EXPIRY_RUN_HOUR and st.expiry_run_done != today:
                st.expiry_run_done = today
                n = await self.expire(tenant, now)
                await self.log_previous_day_budget(tenant, today)
                ran.append(f"expiry:{tenant.slug}:{n}")
        return ran

    # ------------------------------------------------------------------

    async def retry_airborne(self, tenant: TenantConfig) -> int:
        """15-min run: unmatched flights still in the air."""
        sessions = await self.sessions.list_open(
            airfield_id=tenant.airfield_id,
            states={SessionState.AWAITING_MATCH},
            airborne_only=True,
        )
        return await self._process_all(sessions, tenant, "retry_airborne")

    async def retry_open(self, tenant: TenantConfig, trigger: str) -> int:
        """Hourly / 21:00 run: every open, non-review session."""
        sessions = await self.sessions.list_open(
            airfield_id=tenant.airfield_id, states=set(RETRY_STATES),
        )
        return await self._process_all(sessions, tenant, trigger)

    async def expire(self, tenant: TenantConfig, now: datetime) -> int:
        n = await self.sessions.expire_older_than(self.retention_days, now,
                                                  airfield_id=tenant.airfield_id)
        if n:
            log.info("vfsync_sessions_expired", slug=tenant.slug, count=n)
        return n

    async def log_previous_day_budget(self, tenant: TenantConfig, today: date) -> None:
        yesterday = today - timedelta(days=1)
        used = await self.budget.used(tenant.airfield_id, yesterday)
        log.info("vfsync_budget_day_closed", slug=tenant.slug, day=yesterday.isoformat(),
                 used=used, daily_budget=tenant.daily_budget)

    async def _process_all(self, sessions: list[Session], tenant: TenantConfig,
                           trigger: str) -> int:
        n = 0
        for s in sessions:
            try:
                await self.process(s, tenant, trigger)
                n += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("vfsync_retry_failed", slug=tenant.slug,
                              session_id=str(s.session_id), trigger=trigger)
        self.stats[trigger] = self.stats.get(trigger, 0) + n
        return n


def _due(last: datetime | None, now: datetime, every: timedelta) -> bool:
    return last is None or now - last >= every


def _tz(tenant: TenantConfig) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.timezone or "Europe/Berlin")
    except Exception:
        return ZoneInfo("Europe/Berlin")
