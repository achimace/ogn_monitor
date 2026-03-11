"""Pydantic v2 models for API request/response validation."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator
import re


# =============================================
# AUTH
# =============================================

class RegisterRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, description="Vereinsname")
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    slug: str = Field(..., min_length=2, max_length=50, pattern=r"^[a-z0-9-]+$")

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if v.startswith("-") or v.endswith("-"):
            raise ValueError("Slug darf nicht mit Bindestrich beginnen oder enden")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    tenant_id: UUID
    email_verified: bool
    disclaimer_accepted: bool


class VerifyEmailRequest(BaseModel):
    token: str


class AcceptDisclaimerRequest(BaseModel):
    version: int = 1


class RefreshRequest(BaseModel):
    pass  # Token comes from Authorization header


# =============================================
# TENANT
# =============================================

class TenantResponse(BaseModel):
    id: UUID
    name: str
    slug: str
    email: str
    email_verified: bool
    disclaimer_accepted_at: datetime | None
    disclaimer_version: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TenantUpdateRequest(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=100)
    slug: str | None = Field(None, min_length=2, max_length=50, pattern=r"^[a-z0-9-]+$")


# =============================================
# AIRFIELD
# =============================================

class AirfieldCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    slug: str = Field(..., min_length=2, max_length=50, pattern=r"^[a-z0-9-]+$")
    icao_code: str | None = Field(None, max_length=4)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    elevation_m: int = Field(..., ge=0, le=5000)
    home_radius_m: int = Field(800, ge=200, le=5000)
    ogn_filter_radius_km: int = Field(500, ge=50, le=1000)
    alarm_timeout_s: int = Field(600, ge=60, le=3600)
    signal_loss_timeout_s: int = Field(300, ge=60, le=1800)
    takeoff_speed_kmh: int = Field(40, ge=20, le=100)
    takeoff_alt_offset_m: int = Field(50, ge=10, le=200)
    tow_plane_flarm_ids: list[str] = Field(default_factory=list)
    winch_vs_threshold_ms: float = Field(8.0, ge=3.0, le=15.0)


class AirfieldUpdateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    slug: str = Field(..., min_length=2, max_length=50, pattern=r"^[a-z0-9-]+$")
    icao_code: str | None = Field(None, max_length=4)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)
    elevation_m: int = Field(..., ge=0, le=5000)
    home_radius_m: int = Field(800, ge=200, le=5000)
    ogn_filter_radius_km: int = Field(500, ge=50, le=1000)
    alarm_timeout_s: int = Field(600, ge=60, le=3600)
    signal_loss_timeout_s: int = Field(300, ge=60, le=1800)
    takeoff_speed_kmh: int = Field(40, ge=20, le=100)
    takeoff_alt_offset_m: int = Field(50, ge=10, le=200)
    tow_plane_flarm_ids: list[str] = Field(default_factory=list)
    winch_vs_threshold_ms: float = Field(8.0, ge=3.0, le=15.0)
    is_active: bool = True


class AirfieldResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    slug: str
    icao_code: str | None = None
    latitude: float
    longitude: float
    elevation_m: int
    home_radius_m: int
    ogn_filter_radius_km: int
    alarm_timeout_s: int
    signal_loss_timeout_s: int
    takeoff_speed_kmh: int
    takeoff_alt_offset_m: int
    tow_plane_flarm_ids: list[str]
    winch_vs_threshold_ms: float
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


# =============================================
# AIRCRAFT
# =============================================

class AircraftCreateRequest(BaseModel):
    flarm_id: str = Field(..., min_length=4, max_length=16, pattern=r"^[A-Fa-f0-9]+$")
    registration: str = Field(..., min_length=2, max_length=16)
    competition_sign: str | None = Field(None, max_length=4)
    aircraft_model: str | None = Field(None, max_length=64)
    aircraft_type: str | None = Field(None, max_length=32)

    @field_validator("flarm_id")
    @classmethod
    def uppercase_flarm(cls, v: str) -> str:
        return v.upper()


class AircraftUpdateRequest(BaseModel):
    registration: str = Field(..., min_length=2, max_length=16)
    competition_sign: str | None = Field(None, max_length=4)
    aircraft_model: str | None = Field(None, max_length=64)
    aircraft_type: str | None = Field(None, max_length=32)


class AircraftResponse(BaseModel):
    id: UUID
    airfield_id: UUID
    flarm_id: str
    registration: str
    competition_sign: str | None = None
    aircraft_model: str | None = None
    aircraft_type: str | None = None
    is_active: bool
    created_at: datetime | None = None


class CsvImportResponse(BaseModel):
    imported: int
    skipped: int
    errors: list[str]
