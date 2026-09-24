"""Audit log of every VF API read/write attempt (R-10, append-only).

`fields_sent` never contains authentication parameters - they are
stripped here as a safety net even if a caller passes them.
"""

from uuid import UUID

import structlog

from app.vfsync.models import AUDIT_ACTIONS, AuditEntry
from app.vfsync.stores import AuditStore

log = structlog.get_logger()

AUTH_PARAMS = frozenset({"accesstoken", "username", "password", "appkey", "cid"})


def strip_auth(payload: dict | None) -> dict | None:
    """Remove authentication parameters from a payload copy."""
    if payload is None:
        return None
    return {k: v for k, v in payload.items() if k not in AUTH_PARAMS}


class AuditLog:
    """Thin append-only wrapper around an AuditStore."""

    def __init__(self, store: AuditStore):
        self._store = store

    async def record(
        self,
        airfield_id: UUID,
        action: str,
        *,
        session_id: UUID | None = None,
        flid: int | None = None,
        fields_sent: dict | None = None,
        pre_state: dict | None = None,
        http_status: int | None = None,
        detail: str = "",
    ) -> AuditEntry:
        """Append one entry. Unknown actions are rejected loudly."""
        if action not in AUDIT_ACTIONS:
            raise ValueError(f"unknown audit action: {action}")
        entry = AuditEntry(
            airfield_id=airfield_id,
            action=action,
            session_id=session_id,
            flid=flid,
            fields_sent=strip_auth(fields_sent),
            pre_state=strip_auth(pre_state),
            http_status=http_status,
            detail=detail[:2000] if detail else "",
        )
        stored = await self._store.append(entry)
        log.info(
            "vfsync_audit",
            action=action,
            session_id=str(session_id) if session_id else None,
            flid=flid,
            http_status=http_status,
            fields=sorted(entry.fields_sent.keys()) if entry.fields_sent else [],
            detail=entry.detail[:120],
        )
        return stored

    async def list(self, airfield_id: UUID, session_id: UUID | None = None,
                   limit: int = 200) -> list[AuditEntry]:
        return await self._store.list(airfield_id, session_id=session_id, limit=limit)
