"""Auth API - Register, Login, JWT, E-Mail-Verifikation, Disclaimer."""

import secrets
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import settings
from app.db.connection import get_db
from app.db import queries as q
from app.api.schemas import (
    AcceptDisclaimerRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    VerifyEmailRequest,
)
from app.dependencies import get_current_user

log = structlog.get_logger()
router = APIRouter(prefix="/api/auth", tags=["auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _create_token(tenant_id: str, email: str, email_verified: bool) -> tuple[str, int]:
    """Create a JWT access token."""
    expire_minutes = settings.jwt_expire_minutes
    expire = datetime.now(timezone.utc) + timedelta(minutes=expire_minutes)
    payload = {
        "sub": str(tenant_id),
        "email": email,
        "email_verified": email_verified,
        "exp": expire,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expire_minutes * 60


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest):
    """Register a new tenant (Verein)."""
    pool = get_db()

    # Check if email or slug already exists
    existing = await pool.fetchrow(q.TENANT_BY_EMAIL, body.email)
    if existing:
        raise HTTPException(status_code=409, detail="E-Mail bereits registriert")

    existing_slug = await pool.fetchrow(q.TENANT_BY_SLUG, body.slug)
    if existing_slug:
        raise HTTPException(status_code=409, detail="Slug bereits vergeben")

    password_hash = pwd_context.hash(body.password)
    verification_token = secrets.token_urlsafe(32)

    row = await pool.fetchrow(
        q.TENANT_INSERT,
        body.name, body.slug, body.email, password_hash, verification_token,
    )

    log.info("tenant_registered", tenant_id=str(row["id"]), email=body.email)

    # TODO: Send verification email via SMTP when configured

    token, expires_in = _create_token(row["id"], body.email, False)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        tenant_id=row["id"],
        email_verified=False,
        disclaimer_accepted=False,
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    """Login with email and password."""
    pool = get_db()
    row = await pool.fetchrow(q.TENANT_BY_EMAIL, body.email)

    if not row or not pwd_context.verify(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Ungueltige Anmeldedaten")

    token, expires_in = _create_token(row["id"], row["email"], row["email_verified"])
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        tenant_id=row["id"],
        email_verified=row["email_verified"],
        disclaimer_accepted=row["disclaimer_accepted_at"] is not None,
    )


@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest):
    """Verify email with token from verification link."""
    pool = get_db()
    row = await pool.fetchrow(q.TENANT_VERIFY_EMAIL, body.token)
    if not row:
        raise HTTPException(status_code=400, detail="Ungueltiger oder abgelaufener Token")

    log.info("email_verified", tenant_id=str(row["id"]))
    return {"message": "E-Mail erfolgreich verifiziert"}


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(user: dict = Depends(get_current_user)):
    """Refresh JWT token (requires valid current token)."""
    pool = get_db()
    row = await pool.fetchrow(q.TENANT_BY_ID, user["tenant_id"])
    if not row:
        raise HTTPException(status_code=404, detail="Mandant nicht gefunden")

    token, expires_in = _create_token(row["id"], row["email"], row["email_verified"])
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        tenant_id=row["id"],
        email_verified=row["email_verified"],
        disclaimer_accepted=row["disclaimer_accepted_at"] is not None,
    )


@router.post("/accept-disclaimer")
async def accept_disclaimer(
    body: AcceptDisclaimerRequest,
    user: dict = Depends(get_current_user),
):
    """Accept the liability disclaimer (required before using the monitor)."""
    pool = get_db()
    row = await pool.fetchrow(q.TENANT_ACCEPT_DISCLAIMER, user["tenant_id"], body.version)
    if not row:
        raise HTTPException(status_code=404, detail="Mandant nicht gefunden")

    log.info("disclaimer_accepted", tenant_id=str(user["tenant_id"]), version=body.version)
    return {"message": "Haftungsausschluss akzeptiert", "version": row["disclaimer_version"]}
