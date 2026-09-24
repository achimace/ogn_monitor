"""Domain models of the VF-Sync worker (see docs/dev-guides/vfsync-internals.md).

Plain dataclasses, 1:1 to the vf_sync_* tables. All datetimes are
timezone-aware UTC.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionState(str, Enum):
    TRACKING = "tracking"
    AWAITING_MATCH = "awaiting_match"
    MATCHED = "matched"
    DEPARTURE_WRITTEN = "departure_written"
    COMPLETED = "completed"
    REVIEW = "review"
    EXPIRED = "expired"


OPEN_STATES = frozenset({
    SessionState.TRACKING,
    SessionState.AWAITING_MATCH,
    SessionState.MATCHED,
    SessionState.DEPARTURE_WRITTEN,
    SessionState.REVIEW,
})

# Feature flags in vf_sync_config.flags (all default False)
FLAG_LIVE_RELEASE = "live_release"
FLAG_LIVE_TOUCHGO = "live_touchgo"
FLAG_AUTO_CREATE = "auto_create"
FLAG_JOIN_TOWFLIGHTS = "join_towflights"
KNOWN_FLAGS = (FLAG_LIVE_RELEASE, FLAG_LIVE_TOUCHGO, FLAG_AUTO_CREATE, FLAG_JOIN_TOWFLIGHTS)


@dataclass
class TenantConfig:
    """Per-airfield VF-Sync configuration (credentials decrypted, never log)."""
    airfield_id: UUID
    slug: str
    enabled: bool = False
    dry_run: bool = True
    vf_base_url: str = "https://www.vereinsflieger.de"
    vf_cid: int | None = None
    vf_username: str | None = None
    vf_password_md5: str | None = None
    vf_appkey: str | None = None
    flags: dict[str, bool] = field(default_factory=dict)
    daily_budget: int = 450
    timezone: str = "Europe/Berlin"

    def flag(self, name: str) -> bool:
        return bool(self.flags.get(name, False))

    @property
    def has_credentials(self) -> bool:
        return bool(self.vf_username and self.vf_password_md5 and self.vf_appkey)

    def __repr__(self) -> str:  # never leak credentials via repr
        return (f"TenantConfig(slug={self.slug!r}, enabled={self.enabled}, "
                f"dry_run={self.dry_run}, has_credentials={self.has_credentials})")


@dataclass
class Session:
    """One detected flight and its VF write progress (idempotency anchor)."""
    airfield_id: UUID
    flarm_id: str
    registration: str | None = None
    takeoff_ts: datetime | None = None
    session_id: UUID = field(default_factory=uuid4)
    landing_ts: datetime | None = None
    landing_method: str | None = None          # observed | silence
    start_type_detected: str | None = None     # aerotow | aerotow_ambiguous | winch | self | powered | unknown
    tow_registration: str | None = None
    release_ts: datetime | None = None
    release_alt_agl_m: int | None = None
    release_method: str | None = None          # pair_separation | towplane_max | winch_*
    tow_time_min: int | None = None
    landing_count: int = 1
    conf_pairing: float | None = None
    conf_landing: float | None = None
    conf_touchgo: float | None = None
    matched_flid: int | None = None
    state: SessionState = SessionState.TRACKING
    review_reason: str | None = None
    attempts: int = 0
    last_attempt: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def is_airborne(self) -> bool:
        return self.landing_ts is None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def is_aerotow(self) -> bool:
        return self.start_type_detected == "aerotow"

    def add_review_reason(self, reason: str) -> None:
        """Append a review reason (deduplicated, ';'-separated)."""
        reason = reason.strip()
        if not reason:
            return
        existing = [r for r in (self.review_reason or "").split(";") if r]
        if reason not in existing:
            existing.append(reason)
        self.review_reason = ";".join(existing)

    def review_reasons(self) -> list[str]:
        return [r for r in (self.review_reason or "").split(";") if r]


@dataclass
class AuditEntry:
    """Append-only record of one API read/write attempt."""
    airfield_id: UUID
    action: str                     # get | edit | match | abstain | error | dryrun_edit | recovery
    session_id: UUID | None = None
    flid: int | None = None
    fields_sent: dict | None = None
    pre_state: dict | None = None
    http_status: int | None = None
    detail: str = ""
    ts: datetime = field(default_factory=utcnow)
    id: int | None = None


AUDIT_ACTIONS = frozenset({"get", "edit", "match", "abstain", "error", "dryrun_edit", "recovery"})
