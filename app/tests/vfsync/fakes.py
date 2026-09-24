"""Minimal in-memory fakes shared by vfsync unit tests."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from app.vfsync.models import AuditEntry, Session, SessionState, OPEN_STATES


class FakeSessionStore:
    def __init__(self):
        self.sessions: dict[UUID, Session] = {}
        self.saves = 0

    async def upsert(self, session: Session) -> Session:
        for s in self.sessions.values():
            if (s.airfield_id, s.flarm_id, s.takeoff_ts) == (
                session.airfield_id, session.flarm_id, session.takeoff_ts
            ):
                return s
        self.sessions[session.session_id] = session
        return session

    async def save(self, session: Session) -> None:
        self.saves += 1
        self.sessions[session.session_id] = session

    async def get(self, session_id: UUID) -> Session | None:
        return self.sessions.get(session_id)

    async def find_open_for_aircraft(self, airfield_id, flarm_id):
        cands = [s for s in self.sessions.values()
                 if s.airfield_id == airfield_id and s.flarm_id == flarm_id and s.is_open]
        cands.sort(key=lambda s: s.takeoff_ts or datetime.min, reverse=True)
        return cands[0] if cands else None

    async def list_open(self, airfield_id=None, states=None, airborne_only=False):
        out = []
        for s in self.sessions.values():
            if airfield_id and s.airfield_id != airfield_id:
                continue
            if states is None and s.state not in OPEN_STATES:
                continue
            if states is not None and s.state not in states:
                continue
            if airborne_only and not s.is_airborne:
                continue
            out.append(s)
        return out

    async def expire_older_than(self, days: int, now: datetime) -> int:
        n = 0
        for s in self.sessions.values():
            if s.is_open and s.created_at < now.replace(day=now.day) and (now - s.created_at).days >= days:
                s.state = SessionState.EXPIRED
                n += 1
        return n

    async def count_today(self, airfield_id, day: date) -> int:
        return sum(1 for s in self.sessions.values()
                   if s.airfield_id == airfield_id and s.takeoff_ts and s.takeoff_ts.date() == day)


class FakeAuditStore:
    def __init__(self):
        self.entries: list[AuditEntry] = []

    async def append(self, entry: AuditEntry) -> AuditEntry:
        entry.id = len(self.entries) + 1
        self.entries.append(entry)
        return entry

    async def list(self, airfield_id, session_id=None, limit=200):
        out = [e for e in self.entries if e.airfield_id == airfield_id
               and (session_id is None or e.session_id == session_id)]
        return out[-limit:]

    def actions(self) -> list[str]:
        return [e.action for e in self.entries]


class FakeBudgetStore:
    def __init__(self):
        self.data: dict[tuple, int] = {}

    async def used(self, airfield_id, day):
        return self.data.get((airfield_id, day), 0)

    async def increment(self, airfield_id, day, n=1):
        self.data[(airfield_id, day)] = self.data.get((airfield_id, day), 0) + n
        return self.data[(airfield_id, day)]
