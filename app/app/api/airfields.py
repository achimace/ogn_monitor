"""Airfield API - CRUD for tenant airfields."""

import json
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException

from app.db.connection import get_db
from app.db import queries as q
from app.api.schemas import (
    AirfieldCreateRequest,
    AirfieldResponse,
    AirfieldUpdateRequest,
)
from app.dependencies import get_current_user

log = structlog.get_logger()
router = APIRouter(prefix="/api/airfields", tags=["airfields"])


def _row_to_dict(row) -> dict:
    """Convert a DB row to a dict, parsing the GeoJSON polygon string."""
    d = dict(row)
    poly = d.get("home_polygon")
    d["home_polygon"] = json.loads(poly) if isinstance(poly, str) else None
    return d


async def _verify_airfield_ownership(airfield_id: UUID, tenant_id: UUID) -> dict:
    """Verify the airfield belongs to the tenant. Returns airfield row."""
    pool = get_db()
    row = await pool.fetchrow(q.AIRFIELD_BY_ID, airfield_id)
    if not row:
        raise HTTPException(status_code=404, detail="Flugplatz nicht gefunden")
    if row["tenant_id"] != tenant_id:
        raise HTTPException(status_code=403, detail="Kein Zugriff auf diesen Flugplatz")
    return _row_to_dict(row)


@router.get("", response_model=list[AirfieldResponse])
async def list_airfields(user: dict = Depends(get_current_user)):
    """List all airfields of the current tenant."""
    pool = get_db()
    rows = await pool.fetch(q.AIRFIELD_LIST_BY_TENANT, user["tenant_id"])
    return [_row_to_dict(r) for r in rows]


@router.post("", response_model=AirfieldResponse, status_code=201)
async def create_airfield(
    body: AirfieldCreateRequest,
    user: dict = Depends(get_current_user),
):
    """Create a new airfield for the current tenant."""
    pool = get_db()
    try:
        row = await pool.fetchrow(
            q.AIRFIELD_INSERT,
            user["tenant_id"],
            body.name, body.slug, body.icao_code,
            body.latitude, body.longitude, body.elevation_m,
            body.home_radius_m, body.ogn_filter_radius_km,
            body.alarm_timeout_s, body.signal_loss_timeout_s,
            body.takeoff_speed_kmh, body.takeoff_alt_offset_m,
            body.tow_plane_flarm_ids, body.winch_vs_threshold_ms,
            json.dumps(body.home_polygon) if body.home_polygon else None,
        )
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Slug bereits fuer diesen Mandanten vergeben")
        raise

    log.info("airfield_created", airfield_id=str(row["id"]), name=body.name)
    return _row_to_dict(row)


@router.get("/{airfield_id}", response_model=AirfieldResponse)
async def get_airfield(airfield_id: UUID, user: dict = Depends(get_current_user)):
    """Get airfield details."""
    return await _verify_airfield_ownership(airfield_id, user["tenant_id"])


@router.put("/{airfield_id}", response_model=AirfieldResponse)
async def update_airfield(
    airfield_id: UUID,
    body: AirfieldUpdateRequest,
    user: dict = Depends(get_current_user),
):
    """Update an airfield."""
    await _verify_airfield_ownership(airfield_id, user["tenant_id"])
    pool = get_db()

    try:
        row = await pool.fetchrow(
            q.AIRFIELD_UPDATE,
            airfield_id,
            body.name, body.slug, body.icao_code,
            body.latitude, body.longitude, body.elevation_m,
            body.home_radius_m, body.ogn_filter_radius_km,
            body.alarm_timeout_s, body.signal_loss_timeout_s,
            body.takeoff_speed_kmh, body.takeoff_alt_offset_m,
            body.tow_plane_flarm_ids, body.winch_vs_threshold_ms,
            body.is_active,
            json.dumps(body.home_polygon) if body.home_polygon else None,
        )
    except Exception as e:
        if "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Slug bereits fuer diesen Mandanten vergeben")
        raise

    log.info("airfield_updated", airfield_id=str(airfield_id))
    return _row_to_dict(row)


@router.delete("/{airfield_id}", status_code=204)
async def delete_airfield(airfield_id: UUID, user: dict = Depends(get_current_user)):
    """Delete an airfield and all associated data."""
    await _verify_airfield_ownership(airfield_id, user["tenant_id"])
    pool = get_db()
    await pool.execute(q.AIRFIELD_DELETE, airfield_id, user["tenant_id"])
    log.info("airfield_deleted", airfield_id=str(airfield_id))
