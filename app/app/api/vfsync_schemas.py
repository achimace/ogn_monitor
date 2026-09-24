"""Pydantic v2 models for the VF-Sync API (/api/vfsync/*).

Credentials are write-only: the update request accepts them, the responses
only report whether they are set (Konzept Kap. 8.3). ``enabled`` is not part
of the update model on purpose (no self-activation, Kap. 8.6) - the request
model forbids extra fields, so a body containing ``enabled`` is a 422.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.vfsync.models import KNOWN_FLAGS, SessionState

# Upper bound of the VF REST API (500 requests/AppKey/day, Kap. 8.6)
VF_DAILY_BUDGET_MAX = 500


# =============================================
# CONFIG
# =============================================

class VfSyncFlags(BaseModel):
    live_release: bool = False
    live_touchgo: bool = False
    auto_create: bool = False
    join_towflights: bool = False


class VfSyncConfigResponse(BaseModel):
    airfield_id: UUID
    enabled: bool
    dry_run: bool
    vf_base_url: str
    vf_cid: int | None
    vf_username: str | None
    has_password: bool
    has_appkey: bool
    flags: VfSyncFlags
    daily_budget: int
    updated_at: datetime | None


class VfSyncConfigUpdateRequest(BaseModel):
    """PUT body - every field optional, only provided fields are written."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool | None = None
    vf_base_url: str | None = Field(None, min_length=8, max_length=255)
    vf_cid: int | None = Field(None, ge=1)
    vf_username: str | None = Field(None, max_length=255)
    vf_password_md5: str | None = Field(
        None, pattern=r"^[0-9a-fA-F]{32}$",
        description="MD5-Hex des Passworts (nie das Klartextpasswort)",
    )
    vf_appkey: str | None = Field(None, min_length=1, max_length=255)
    flags: dict[str, bool] | None = None
    daily_budget: int | None = Field(None, ge=1, le=VF_DAILY_BUDGET_MAX)

    @field_validator("vf_password_md5")
    @classmethod
    def lowercase_md5(cls, v: str | None) -> str | None:
        return v.lower() if v is not None else None

    @field_validator("vf_base_url")
    @classmethod
    def https_and_allowed_host(cls, v: str | None) -> str | None:
        # Kap. 8.4: TLS mandatory, host allow-list (settings.vfsync_allowed_hosts)
        from app.vfsync.urls import validate_base_url
        return validate_base_url(v) if v is not None else None

    @field_validator("vf_username")
    @classmethod
    def empty_username_is_null(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("flags")
    @classmethod
    def only_known_flags(cls, v: dict[str, bool] | None) -> dict[str, bool] | None:
        if v is None:
            return None
        unknown = sorted(set(v) - set(KNOWN_FLAGS))
        if unknown:
            raise ValueError(f"Unbekannte Flags: {', '.join(unknown)}")
        return v


# =============================================
# STATUS
# =============================================

class VfSyncBudgetStatus(BaseModel):
    day: date
    used: int
    daily_budget: int
    stage: str


class VfSyncSessionCounts(BaseModel):
    open: int
    awaiting_match: int
    review: int
    completed_today: int


class VfSyncWorkerStatus(BaseModel):
    status: str
    last_event_ts: str | None
    updated_at: str | None


class VfSyncStatusResponse(BaseModel):
    enabled: bool
    dry_run: bool
    budget: VfSyncBudgetStatus
    sessions: VfSyncSessionCounts
    worker: VfSyncWorkerStatus | None


# =============================================
# SESSIONS / AUDIT
# =============================================

class VfSessionResponse(BaseModel):
    session_id: UUID
    airfield_id: UUID
    flarm_id: str
    registration: str | None
    takeoff_ts: datetime | None
    landing_ts: datetime | None
    landing_method: str | None
    start_type_detected: str | None
    tow_registration: str | None
    release_ts: datetime | None
    release_alt_agl_m: int | None
    release_method: str | None
    tow_time_min: int | None
    landing_count: int
    conf_pairing: float | None
    conf_landing: float | None
    conf_touchgo: float | None
    matched_flid: int | None
    state: SessionState
    review_reason: str | None
    attempts: int
    last_attempt: datetime | None
    created_at: datetime
    updated_at: datetime


class VfSessionPageResponse(BaseModel):
    items: list[VfSessionResponse]
    total: int
    page: int
    pages: int


class VfAuditEntryResponse(BaseModel):
    id: int
    ts: datetime
    action: str
    session_id: UUID | None
    flid: int | None
    fields_sent: dict | None
    pre_state: dict | None
    http_status: int | None
    detail: str | None


class VfAuditPageResponse(BaseModel):
    items: list[VfAuditEntryResponse]
