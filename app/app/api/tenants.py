"""Tenant API - own tenant CRUD."""

import structlog
from fastapi import APIRouter, Depends, HTTPException

from app.db.connection import get_db
from app.db import queries as q
from app.api.schemas import TenantResponse, TenantUpdateRequest
from app.dependencies import get_current_user

log = structlog.get_logger()
router = APIRouter(prefix="/api/tenant", tags=["tenant"])


@router.get("", response_model=TenantResponse)
async def get_tenant(user: dict = Depends(get_current_user)):
    """Get own tenant data."""
    pool = get_db()
    row = await pool.fetchrow(q.TENANT_BY_ID, user["tenant_id"])
    if not row:
        raise HTTPException(status_code=404, detail="Mandant nicht gefunden")
    return dict(row)


@router.put("", response_model=TenantResponse)
async def update_tenant(
    body: TenantUpdateRequest,
    user: dict = Depends(get_current_user),
):
    """Update own tenant (name, slug)."""
    pool = get_db()

    # Get current data for unchanged fields
    current = await pool.fetchrow(q.TENANT_BY_ID, user["tenant_id"])
    if not current:
        raise HTTPException(status_code=404, detail="Mandant nicht gefunden")

    name = body.name if body.name is not None else current["name"]
    slug = body.slug if body.slug is not None else current["slug"]

    # Check slug uniqueness if changed
    if slug != current["slug"]:
        existing = await pool.fetchrow(q.TENANT_BY_SLUG, slug)
        if existing:
            raise HTTPException(status_code=409, detail="Slug bereits vergeben")

    row = await pool.fetchrow(q.TENANT_UPDATE, user["tenant_id"], name, slug)
    log.info("tenant_updated", tenant_id=str(user["tenant_id"]))
    return dict(row)


@router.delete("", status_code=204)
async def delete_tenant(user: dict = Depends(get_current_user)):
    """Delete own tenant and all associated data (CASCADE)."""
    pool = get_db()
    await pool.execute(q.TENANT_DELETE, user["tenant_id"])
    log.info("tenant_deleted", tenant_id=str(user["tenant_id"]))
