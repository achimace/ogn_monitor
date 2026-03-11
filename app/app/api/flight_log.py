"""Flight Log API - Historical flight data with pagination and CSV export.

GET  /api/flight-log/        - Paginated flight log
GET  /api/flight-log/export/csv - CSV download (Startschreiber format)
GET  /api/flight-log/stats   - Daily/weekly statistics
"""

import csv
import io
from datetime import date, datetime, timezone

import structlog
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.db.connection import get_db
from app.dependencies import get_current_user

log = structlog.get_logger()
router = APIRouter(prefix="/api/flight-log", tags=["FlightLog"])


@router.get("/")
async def get_flight_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=100),
    date_filter: str | None = Query(None, alias="date"),
    user: dict = Depends(get_current_user),
):
    """Get paginated flight log for the current tenant."""
    db = get_db()
    tenant_id = user["tenant_id"]

    # Build query
    conditions = ["fl.tenant_id = $1"]
    params: list = [tenant_id]
    idx = 2

    if date_filter:
        conditions.append(f"fl.takeoff_time::date = ${idx}")
        params.append(date_filter)
        idx += 1

    where = " AND ".join(conditions)

    # Count total
    total = await db.fetchval(
        f"SELECT COUNT(*) FROM flight_log fl WHERE {where}", *params
    )

    # Fetch page
    offset = (page - 1) * per_page
    rows = await db.fetch(
        f"""SELECT fl.id, fl.flarm_id, fl.registration, fl.competition_sign,
                   fl.aircraft_model, fl.takeoff_time, fl.landing_time,
                   fl.flight_duration_s, fl.max_altitude_m, fl.max_distance_m,
                   fl.launch_type, fl.end_status, fl.tow_plane_reg, fl.release_alt_m
            FROM flight_log fl
            WHERE {where}
            ORDER BY fl.takeoff_time DESC
            LIMIT ${idx} OFFSET ${idx + 1}""",
        *params, per_page, offset,
    )

    pages = max(1, (total + per_page - 1) // per_page)

    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "pages": pages,
    }


@router.get("/export/csv")
async def export_csv(
    date_filter: str | None = Query(None, alias="date"),
    user: dict = Depends(get_current_user),
):
    """Export flight log as CSV (Startschreiber format).

    Includes F-Schlepp billing data (tow plane, release altitude).
    """
    db = get_db()
    tenant_id = user["tenant_id"]

    conditions = ["fl.tenant_id = $1"]
    params: list = [tenant_id]
    idx = 2

    if date_filter:
        conditions.append(f"fl.takeoff_time::date = ${idx}")
        params.append(date_filter)
        idx += 1

    where = " AND ".join(conditions)

    rows = await db.fetch(
        f"""SELECT fl.registration, fl.competition_sign, fl.aircraft_model,
                   fl.takeoff_time, fl.landing_time, fl.flight_duration_s,
                   fl.max_altitude_m, fl.max_distance_m, fl.launch_type,
                   fl.end_status, fl.tow_plane_reg, fl.release_alt_m
            FROM flight_log fl
            WHERE {where}
            ORDER BY fl.takeoff_time ASC""",
        *params,
    )

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')

    # Header
    writer.writerow([
        'Kennzeichen', 'WB-Kz', 'Typ', 'Start (UTC)', 'Landung (UTC)',
        'Dauer (h:mm)', 'Max Hoehe (m)', 'Max Distanz (km)', 'Startart',
        'Status', 'Schleppflugzeug', 'Ausklink-Hoehe (m)',
    ])

    for r in rows:
        duration = ''
        if r['flight_duration_s']:
            h = r['flight_duration_s'] // 3600
            m = (r['flight_duration_s'] % 3600) // 60
            duration = f"{h}:{m:02d}"

        takeoff = _format_time(r['takeoff_time'])
        landing = _format_time(r['landing_time'])
        launch_map = {'winch': 'Winde', 'aerotow': 'F-Schlepp', 'self': 'Eigen'}

        writer.writerow([
            r['registration'] or '',
            r['competition_sign'] or '',
            r['aircraft_model'] or '',
            takeoff,
            landing,
            duration,
            r['max_altitude_m'] or '',
            round(r['max_distance_m'] / 1000, 1) if r['max_distance_m'] else '',
            launch_map.get(r['launch_type'] or '', r['launch_type'] or ''),
            r['end_status'] or '',
            r['tow_plane_reg'] or '',
            r['release_alt_m'] or '',
        ])

    filename = f"fluglog-{date_filter or date.today().isoformat()}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/stats")
async def get_stats(
    days: int = Query(7, ge=1, le=90),
    user: dict = Depends(get_current_user),
):
    """Get daily flight statistics for the last N days."""
    db = get_db()
    tenant_id = user["tenant_id"]

    rows = await db.fetch(
        """SELECT
             takeoff_time::date AS day,
             COUNT(*) AS flights,
             COUNT(*) FILTER (WHERE launch_type = 'winch') AS winch_starts,
             COUNT(*) FILTER (WHERE launch_type = 'aerotow') AS aerotow_starts,
             COUNT(*) FILTER (WHERE launch_type = 'self') AS self_starts,
             COALESCE(AVG(flight_duration_s), 0)::int AS avg_duration_s,
             COALESCE(MAX(max_altitude_m), 0) AS max_altitude_m,
             COALESCE(MAX(max_distance_m), 0) AS max_distance_m
           FROM flight_log
           WHERE tenant_id = $1
             AND takeoff_time >= NOW() - ($2 || ' days')::interval
           GROUP BY takeoff_time::date
           ORDER BY day DESC""",
        tenant_id, str(days),
    )

    return {
        "days": days,
        "stats": [dict(r) for r in rows],
    }


def _format_time(dt) -> str:
    if dt is None:
        return ''
    if isinstance(dt, datetime):
        return dt.strftime('%H:%M')
    return str(dt)
