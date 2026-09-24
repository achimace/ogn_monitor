"""Storage protocols of the VF-Sync worker.

Implementations: stores_memory.py (tests, golden-file runs) and
stores_pg.py (asyncpg, vf_sync_* tables). Every query is scoped by
airfield_id - tenant separation is enforced at this layer.
"""

from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from app.vfsync.models import AuditEntry, Session, SessionState, TenantConfig


class ConfigStore(Protocol):
    async def load_enabled(self) -> list[TenantConfig]:
        """All tenants with enabled=true, credentials decrypted."""
        ...

    async def load(self, airfield_id: UUID) -> TenantConfig | None:
        ...


class SessionStore(Protocol):
    async def upsert(self, session: Session) -> Session:
        """Insert or return the existing session for
        (airfield_id, flarm_id, takeoff_ts). Returns the stored session."""
        ...

    async def save(self, session: Session) -> None:
        """Persist all fields of an existing session (by session_id)."""
        ...

    async def get(self, session_id: UUID) -> Session | None:
        ...

    async def find_open_for_aircraft(self, airfield_id: UUID, flarm_id: str) -> Session | None:
        """Most recent open session (state not completed/expired) of an aircraft."""
        ...

    async def list_open(
        self,
        airfield_id: UUID | None = None,
        states: set[SessionState] | None = None,
        airborne_only: bool = False,
    ) -> list[Session]:
        ...

    async def expire_older_than(self, days: int, now: datetime,
                                airfield_id: UUID | None = None) -> int:
        """Open sessions older than `days` -> EXPIRED. Returns the count.
        With airfield_id only that tenant's sessions are touched."""
        ...

    async def count_today(self, airfield_id: UUID, day: date, tz: str = "UTC") -> int:
        """Number of sessions (flight movements) whose takeoff falls on the
        calendar day `day` in timezone `tz` (tenant timezone)."""
        ...


class AuditStore(Protocol):
    async def append(self, entry: AuditEntry) -> AuditEntry:
        ...

    async def list(
        self, airfield_id: UUID, session_id: UUID | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        ...


class BudgetStore(Protocol):
    async def used(self, airfield_id: UUID, day: date) -> int:
        ...

    async def increment(self, airfield_id: UUID, day: date, n: int = 1) -> int:
        """Atomically add n, return the new value."""
        ...
