"""Aircraft API - CRUD for tenant aircraft + CSV import."""

import csv
import io
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from app.db.connection import get_db
from app.db import queries as q
from app.api.schemas import (
    AircraftCreateRequest,
    AircraftResponse,
    AircraftUpdateRequest,
    CsvImportResponse,
)
from app.api.airfields import _verify_airfield_ownership
from app.dependencies import get_current_user

log = structlog.get_logger()
router = APIRouter(prefix="/api/airfields/{airfield_id}/aircraft", tags=["aircraft"])


@router.get("", response_model=list[AircraftResponse])
async def list_aircraft(airfield_id: UUID, user: dict = Depends(get_current_user)):
    """List all aircraft of an airfield."""
    await _verify_airfield_ownership(airfield_id, user["tenant_id"])
    pool = get_db()
    rows = await pool.fetch(q.AIRCRAFT_LIST_BY_AIRFIELD, airfield_id)
    return [dict(r) for r in rows]


@router.post("", response_model=AircraftResponse, status_code=201)
async def create_aircraft(
    airfield_id: UUID,
    body: AircraftCreateRequest,
    user: dict = Depends(get_current_user),
):
    """Add an aircraft to the airfield fleet."""
    await _verify_airfield_ownership(airfield_id, user["tenant_id"])
    pool = get_db()

    # Check if FLARM ID already exists for this airfield
    existing = await pool.fetchrow(q.AIRCRAFT_BY_FLARM_ID, airfield_id, body.flarm_id)
    if existing:
        raise HTTPException(status_code=409, detail=f"FLARM-ID {body.flarm_id} bereits registriert")

    row = await pool.fetchrow(
        q.AIRCRAFT_INSERT,
        airfield_id, body.flarm_id, body.registration,
        body.competition_sign, body.aircraft_model, body.aircraft_type,
    )
    log.info("aircraft_added", airfield_id=str(airfield_id), flarm_id=body.flarm_id)
    return dict(row)


@router.put("/{flarm_id}", response_model=AircraftResponse)
async def update_aircraft(
    airfield_id: UUID,
    flarm_id: str,
    body: AircraftUpdateRequest,
    user: dict = Depends(get_current_user),
):
    """Update an aircraft."""
    await _verify_airfield_ownership(airfield_id, user["tenant_id"])
    pool = get_db()

    row = await pool.fetchrow(
        q.AIRCRAFT_UPDATE,
        airfield_id, flarm_id.upper(),
        body.registration, body.competition_sign,
        body.aircraft_model, body.aircraft_type,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Flugzeug nicht gefunden")

    log.info("aircraft_updated", airfield_id=str(airfield_id), flarm_id=flarm_id)
    return dict(row)


@router.delete("/{flarm_id}", status_code=204)
async def delete_aircraft(
    airfield_id: UUID,
    flarm_id: str,
    user: dict = Depends(get_current_user),
):
    """Remove an aircraft from the airfield fleet."""
    await _verify_airfield_ownership(airfield_id, user["tenant_id"])
    pool = get_db()
    result = await pool.execute(q.AIRCRAFT_DELETE, airfield_id, flarm_id.upper())
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Flugzeug nicht gefunden")
    log.info("aircraft_deleted", airfield_id=str(airfield_id), flarm_id=flarm_id)


@router.post("/import-csv", response_model=CsvImportResponse)
async def import_csv(
    airfield_id: UUID,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    """Import aircraft from CSV file.

    Expected CSV format (with or without header):
    flarm_id,registration,competition_sign,aircraft_model,aircraft_type

    Minimum required columns: flarm_id, registration
    Uses UPSERT - existing FLARM IDs are updated.
    """
    await _verify_airfield_ownership(airfield_id, user["tenant_id"])

    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Nur CSV-Dateien erlaubt")

    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # Handle BOM
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.reader(io.StringIO(text), delimiter=",")
    rows = list(reader)

    if not rows:
        raise HTTPException(status_code=400, detail="CSV-Datei ist leer")

    # Detect header row
    first_row = [c.strip().lower() for c in rows[0]]
    has_header = "flarm_id" in first_row or "flarm" in first_row or "registration" in first_row
    data_rows = rows[1:] if has_header else rows

    # Map column indices
    if has_header:
        col_map = {name: idx for idx, name in enumerate(first_row)}
        flarm_idx = col_map.get("flarm_id", col_map.get("flarm", 0))
        reg_idx = col_map.get("registration", col_map.get("kennzeichen", 1))
        cs_idx = col_map.get("competition_sign", col_map.get("wettbewerb", None))
        model_idx = col_map.get("aircraft_model", col_map.get("model", col_map.get("muster", None)))
        type_idx = col_map.get("aircraft_type", col_map.get("type", col_map.get("typ", None)))
    else:
        flarm_idx, reg_idx = 0, 1
        cs_idx = 2 if len(rows[0]) > 2 else None
        model_idx = 3 if len(rows[0]) > 3 else None
        type_idx = 4 if len(rows[0]) > 4 else None

    pool = get_db()
    imported = 0
    skipped = 0
    errors: list[str] = []

    for line_num, row in enumerate(data_rows, start=2 if has_header else 1):
        if not row or all(c.strip() == "" for c in row):
            continue  # Skip empty rows

        try:
            flarm_id = row[flarm_idx].strip().upper()
            registration = row[reg_idx].strip()

            if not flarm_id or not registration:
                errors.append(f"Zeile {line_num}: FLARM-ID oder Kennzeichen fehlt")
                skipped += 1
                continue

            cs = row[cs_idx].strip() if cs_idx is not None and cs_idx < len(row) else None
            model = row[model_idx].strip() if model_idx is not None and model_idx < len(row) else None
            atype = row[type_idx].strip() if type_idx is not None and type_idx < len(row) else None

            await pool.fetchrow(
                q.AIRCRAFT_UPSERT,
                airfield_id, flarm_id, registration, cs or None, model or None, atype or None,
            )
            imported += 1
        except IndexError:
            errors.append(f"Zeile {line_num}: Zu wenige Spalten")
            skipped += 1
        except Exception as e:
            errors.append(f"Zeile {line_num}: {str(e)}")
            skipped += 1

    log.info("csv_imported", airfield_id=str(airfield_id), imported=imported, skipped=skipped)
    return CsvImportResponse(imported=imported, skipped=skipped, errors=errors)
