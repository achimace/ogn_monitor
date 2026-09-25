"""Ignored aircraft API - per-airfield ignore list (airfield_ignored_aircraft).

FLARM-IDs on this list are never tracked at the airfield (rescue
helicopter of the clinic next door, a neighbour's drone, ...). The worker
loads the list with the airfield configs; after every change the airfield
slug is published on ``tracker:config`` so the worker reloads within
seconds instead of waiting for its periodic reload (best effort - the
write is valid even without Redis).

Routes (auth + tenant ownership like the aircraft routes):

- ``GET    /api/airfields/{airfield_id}/ignored-aircraft``            -> [{id, flarmId, note, createdAt}]
- ``POST   /api/airfields/{airfield_id}/ignored-aircraft``            -> 201 item (409 duplicate)
- ``DELETE /api/airfields/{airfield_id}/ignored-aircraft/{flarm_id}`` -> 204 (404 unknown)
"""

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.airfields import _verify_airfield_ownership
from app.api.schemas import IgnoredAircraftCreateRequest, IgnoredAircraftItem
from app.db import queries as q
from app.db.connection import get_db
from app.dependencies import get_current_user
from app.redis_client import get_redis
from app.tracking.redis_keys import TRACKER_CONFIG_CHANNEL

log = structlog.get_logger()
router = APIRouter(
    prefix="/api/airfields/{airfield_id}/ignored-aircraft", tags=["ignored-aircraft"]
)


def redis_client() -> Any:
    """Redis client of the API process; None when not initialized (tests)."""
    try:
        return get_redis()
    except RuntimeError:
        return None


async def publish_tracker_config_changed(redis: Any, slug: str) -> bool:
    """Tell the APRS worker to reload its airfield configs right away.

    Best effort: without Redis or on a Redis error the DB write stays
    valid - the worker picks the change up with its periodic reload.
    Returns True if the message was published.
    """
    if redis is None:
        return False
    try:
        await redis.publish(TRACKER_CONFIG_CHANNEL, slug)
    except Exception as exc:  # noqa: BLE001 - never fail the write
        log.warning("tracker_config_signal_failed", slug=slug, error=type(exc).__name__)
        return False
    return True


@router.get("", response_model=list[IgnoredAircraftItem])
async def list_ignored_aircraft(
    airfield_id: UUID, user: dict = Depends(get_current_user)
) -> list[dict]:
    """List the ignored FLARM-IDs of an airfield."""
    await _verify_airfield_ownership(airfield_id, user["tenant_id"])
    rows = await get_db().fetch(q.IGNORED_AIRCRAFT_LIST, airfield_id)
    return [dict(r) for r in rows]


@router.post("", response_model=IgnoredAircraftItem, status_code=201)
async def add_ignored_aircraft(
    airfield_id: UUID,
    body: IgnoredAircraftCreateRequest,
    user: dict = Depends(get_current_user),
    redis: Any = Depends(redis_client),
) -> dict:
    """Put a FLARM-ID on the airfield's ignore list (409 if already there)."""
    airfield = await _verify_airfield_ownership(airfield_id, user["tenant_id"])
    row = await get_db().fetchrow(
        q.IGNORED_AIRCRAFT_INSERT, airfield_id, body.flarm_id, body.note
    )
    if row is None:
        raise HTTPException(
            status_code=409, detail=f"FLARM-ID {body.flarm_id} steht bereits auf der Ignorierliste"
        )
    log.info("ignored_aircraft_added", airfield_id=str(airfield_id), flarm_id=body.flarm_id)
    await publish_tracker_config_changed(redis, airfield["slug"])
    return dict(row)


@router.delete("/{flarm_id}", status_code=204, response_class=Response)
async def remove_ignored_aircraft(
    airfield_id: UUID,
    flarm_id: str,
    user: dict = Depends(get_current_user),
    redis: Any = Depends(redis_client),
) -> Response:
    """Remove a FLARM-ID from the airfield's ignore list (404 if not on it)."""
    airfield = await _verify_airfield_ownership(airfield_id, user["tenant_id"])
    row = await get_db().fetchrow(q.IGNORED_AIRCRAFT_DELETE, airfield_id, flarm_id.upper())
    if row is None:
        raise HTTPException(status_code=404, detail="FLARM-ID nicht auf der Ignorierliste")
    log.info("ignored_aircraft_removed", airfield_id=str(airfield_id), flarm_id=flarm_id.upper())
    await publish_tracker_config_changed(redis, airfield["slug"])
    return Response(status_code=204)
