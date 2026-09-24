"""API budget guard with degradation stages (Konzept Kap. 5.6, R-09).

The VF REST API allows 500 requests per AppKey and day; the guard stops
at the tenant's daily_budget (default 450). Every HTTP call - successful
or not - is counted via `on_request` (hook for VfClient).
"""

from datetime import date, datetime, timezone
from enum import Enum
from typing import Callable
from zoneinfo import ZoneInfo

from app.vfsync.models import TenantConfig
from app.vfsync.stores import BudgetStore

# Degradation thresholds as fraction of daily_budget
STAGE_NO_LIVE_DEPARTURE_RATIO = 0.6
STAGE_AEROTOW_ONLY_RATIO = 0.9
STAGE_HARD_STOP_RATIO = 1.0
# ... or when the day is busier than this many movements (Kap. 5.6)
MANY_MOVEMENTS_THRESHOLD = 80
# get + edit
CALLS_PER_WRITE = 2


class Stage(Enum):
    """Budget degradation stage of a tenant for the current day."""
    NORMAL = "normal"                         # live departure + landing bundle
    NO_LIVE_DEPARTURE = "no_live_departure"   # landing bundle only
    AEROTOW_ONLY = "aerotow_only"             # only aerotow sessions (billing priority)
    HARD_STOP = "hard_stop"                   # nothing, queue everything


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stage_for(used: int, daily_budget: int, movements_today: int) -> Stage:
    """Pure stage decision (Kap. 5.6)."""
    if daily_budget <= 0 or used >= daily_budget * STAGE_HARD_STOP_RATIO:
        return Stage.HARD_STOP
    if used >= daily_budget * STAGE_AEROTOW_ONLY_RATIO:
        return Stage.AEROTOW_ONLY
    if (used >= daily_budget * STAGE_NO_LIVE_DEPARTURE_RATIO
            or movements_today > MANY_MOVEMENTS_THRESHOLD):
        return Stage.NO_LIVE_DEPARTURE
    return Stage.NORMAL


class BudgetGuard:
    """Counts API requests per tenant/day and decides what may be spent."""

    def __init__(self, store: BudgetStore, clock: Callable[[], datetime] = _utcnow):
        self._store = store
        self._clock = clock

    def day_for(self, tenant: TenantConfig) -> date:
        """Budget day = calendar day in the tenant's timezone (VF server day)."""
        try:
            tz = ZoneInfo(tenant.timezone or "Europe/Berlin")
        except Exception:
            tz = ZoneInfo("Europe/Berlin")
        return self._clock().astimezone(tz).date()

    async def used(self, tenant: TenantConfig) -> int:
        return await self._store.used(tenant.airfield_id, self.day_for(tenant))

    async def remaining(self, tenant: TenantConfig) -> int:
        return max(0, tenant.daily_budget - await self.used(tenant))

    async def stage(self, tenant: TenantConfig, movements_today: int = 0) -> Stage:
        return stage_for(await self.used(tenant), tenant.daily_budget, movements_today)

    async def reserve(self, tenant: TenantConfig, n: int = CALLS_PER_WRITE) -> bool:
        """Is there budget for n more calls? Does not consume anything -
        consumption happens per request via on_request()."""
        used = await self.used(tenant)
        if stage_for(used, tenant.daily_budget, 0) is Stage.HARD_STOP:
            return False
        return tenant.daily_budget - used >= n

    async def on_request(self, tenant: TenantConfig) -> None:
        """VfClient hook: one HTTP request is about to be sent."""
        await self._store.increment(tenant.airfield_id, self.day_for(tenant), 1)

    def hook_for(self, tenant: TenantConfig):
        """Bind on_request to a tenant for VfClient(on_request=...)."""
        async def _hook() -> None:
            await self.on_request(tenant)
        return _hook
