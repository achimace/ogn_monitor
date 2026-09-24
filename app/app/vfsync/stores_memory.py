"""In-memory implementations of the VF-Sync store protocols.

Used by unit tests and golden-file runs. Semantics mirror stores_pg.py:
stored sessions are copies (mutating a returned object does not change
the store until ``save``), audit entries are append-only with an
incrementing id, ordering matches the SQL queries.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from app.vfsync.models import (
    OPEN_STATES,
    AuditEntry,
    Session,
    SessionState,
    TenantConfig,
    utcnow,
)


def _takeoff_sort_key(session: Session) -> tuple[datetime, datetime]:
    """Sort key: takeoff_ts (NULL sorts oldest), then created_at."""
    ts = session.takeoff_ts or datetime.min.replace(tzinfo=timezone.utc)
    return ts, session.created_at


class InMemoryConfigStore:
    """ConfigStore over a static tenant list."""

    def __init__(self, tenants: list[TenantConfig] | None = None) -> None:
        self._tenants: list[TenantConfig] = list(tenants or [])

    async def load_enabled(self) -> list[TenantConfig]:
        return [t for t in self._tenants if t.enabled]

    async def load(self, airfield_id: UUID) -> TenantConfig | None:
        for t in self._tenants:
            if t.airfield_id == airfield_id:
                return t
        return None

    def set_tenants(self, tenants: list[TenantConfig]) -> None:
        """Replace the tenant list (simulates a config reload)."""
        self._tenants = list(tenants)


class InMemorySessionStore:
    """SessionStore keyed by session_id with the (airfield, flarm, takeoff) unique key."""

    def __init__(self) -> None:
        self._by_id: dict[UUID, Session] = {}
        self._by_key: dict[tuple[UUID, str, datetime | None], UUID] = {}

    @staticmethod
    def _key(session: Session) -> tuple[UUID, str, datetime | None]:
        return session.airfield_id, session.flarm_id, session.takeoff_ts

    async def upsert(self, session: Session) -> Session:
        key = self._key(session)
        existing_id = self._by_key.get(key)
        if existing_id is not None:
            return replace(self._by_id[existing_id])
        stored = replace(session)
        self._by_id[stored.session_id] = stored
        self._by_key[key] = stored.session_id
        return replace(stored)

    async def save(self, session: Session) -> None:
        if session.session_id not in self._by_id:
            raise KeyError(f"unknown session {session.session_id}")
        old = self._by_id[session.session_id]
        session.updated_at = utcnow()
        stored = replace(session)
        self._by_key.pop(self._key(old), None)
        self._by_id[stored.session_id] = stored
        self._by_key[self._key(stored)] = stored.session_id

    async def get(self, session_id: UUID) -> Session | None:
        stored = self._by_id.get(session_id)
        return replace(stored) if stored else None

    async def find_open_for_aircraft(self, airfield_id: UUID, flarm_id: str) -> Session | None:
        candidates = [
            s for s in self._by_id.values()
            if s.airfield_id == airfield_id and s.flarm_id == flarm_id and s.state in OPEN_STATES
        ]
        if not candidates:
            return None
        newest = max(candidates, key=_takeoff_sort_key)
        return replace(newest)

    async def list_open(
        self,
        airfield_id: UUID | None = None,
        states: set[SessionState] | None = None,
        airborne_only: bool = False,
    ) -> list[Session]:
        wanted = set(states) if states is not None else set(OPEN_STATES)
        result = [
            replace(s) for s in self._by_id.values()
            if s.state in wanted
            and (airfield_id is None or s.airfield_id == airfield_id)
            and (not airborne_only or s.landing_ts is None)
        ]
        result.sort(key=_takeoff_sort_key)
        return result

    async def expire_older_than(self, days: int, now: datetime) -> int:
        cutoff = now - timedelta(days=days)
        count = 0
        for s in self._by_id.values():
            anchor = s.takeoff_ts or s.created_at
            if s.state in OPEN_STATES and anchor < cutoff:
                s.state = SessionState.EXPIRED
                s.updated_at = now
                count += 1
        return count

    async def count_today(self, airfield_id: UUID, day: date) -> int:
        return sum(
            1 for s in self._by_id.values()
            if s.airfield_id == airfield_id
            and s.takeoff_ts is not None
            and s.takeoff_ts.astimezone(timezone.utc).date() == day
        )


class InMemoryAuditStore:
    """Append-only audit log with an incrementing id."""

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []
        self._next_id = 1

    async def append(self, entry: AuditEntry) -> AuditEntry:
        stored = replace(entry, id=self._next_id)
        self._next_id += 1
        self._entries.append(stored)
        return replace(stored)

    async def list(
        self, airfield_id: UUID, session_id: UUID | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        rows = [
            e for e in self._entries
            if e.airfield_id == airfield_id
            and (session_id is None or e.session_id == session_id)
        ]
        rows.sort(key=lambda e: (e.ts, e.id or 0), reverse=True)
        return [replace(e) for e in rows[:limit]]

    @property
    def entries(self) -> list[AuditEntry]:
        """All entries in insertion order (test helper)."""
        return [replace(e) for e in self._entries]


class InMemoryBudgetStore:
    """Per-airfield/day request counter."""

    def __init__(self) -> None:
        self._used: dict[tuple[UUID, date], int] = {}

    async def used(self, airfield_id: UUID, day: date) -> int:
        return self._used.get((airfield_id, day), 0)

    async def increment(self, airfield_id: UUID, day: date, n: int = 1) -> int:
        key = (airfield_id, day)
        self._used[key] = self._used.get(key, 0) + n
        return self._used[key]
