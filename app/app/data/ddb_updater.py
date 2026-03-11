"""OGN Device Database (DDB) and FlarmNet sync.

Downloads and imports aircraft registrations from:
1. OGN DDB (daily): http://ddb.glidernet.org/download/?j=1
2. FlarmNet (weekly): https://www.flarmnet.org/files/downloads/data.fln

Results are stored in the aircraft_registry table and trigger
an AircraftResolver cache reload.
"""

import asyncio
from datetime import datetime, timezone

import httpx
import structlog

from app.db.connection import get_db

log = structlog.get_logger()

DDB_URL = "http://ddb.glidernet.org/download/?j=1"
FLARMNET_URL = "https://www.flarmnet.org/files/downloads/data.fln"

# DDB JSON fields
_DDB_FIELDS = ["DEVICE_TYPE", "DEVICE_ID", "AIRCRAFT_MODEL", "REGISTRATION", "CN", "TRACKED", "IDENTIFIED"]


async def sync_ddb() -> dict:
    """Download and import OGN Device Database.

    Returns:
        Dict with counts: inserted, updated, errors.
    """
    log.info("ddb_sync_start")
    stats = {"inserted": 0, "updated": 0, "errors": 0, "total": 0}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(DDB_URL)
            resp.raise_for_status()

        data = resp.json()
        devices = data.get("devices", [])
        stats["total"] = len(devices)

        db = get_db()
        async with db.acquire() as conn:
            for dev in devices:
                try:
                    device_id = dev.get("device_id", "").strip().upper()
                    if not device_id or len(device_id) < 6:
                        continue

                    result = await conn.execute(
                        """
                        INSERT INTO aircraft_registry
                            (device_type, device_id, aircraft_model, registration,
                             competition_sign, tracked, identified, source, updated_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, 'ogn_ddb', NOW())
                        ON CONFLICT (device_id) DO UPDATE SET
                            device_type = EXCLUDED.device_type,
                            aircraft_model = EXCLUDED.aircraft_model,
                            registration = EXCLUDED.registration,
                            competition_sign = EXCLUDED.competition_sign,
                            tracked = EXCLUDED.tracked,
                            identified = EXCLUDED.identified,
                            source = CASE WHEN aircraft_registry.source = 'tenant'
                                         THEN aircraft_registry.source
                                         ELSE EXCLUDED.source END,
                            updated_at = NOW()
                        """,
                        dev.get("device_type", "F")[:1],
                        device_id,
                        dev.get("aircraft_model", "")[:128],
                        dev.get("registration", "")[:32],
                        dev.get("cn", "")[:8],
                        dev.get("tracked", "Y") == "Y",
                        dev.get("identified", "Y") == "Y",
                    )

                    if "INSERT" in result:
                        stats["inserted"] += 1
                    else:
                        stats["updated"] += 1

                except Exception:
                    stats["errors"] += 1

        log.info("ddb_sync_complete", **stats)

    except httpx.HTTPError as e:
        log.error("ddb_sync_http_error", error=str(e))
        stats["errors"] = -1
    except Exception:
        log.exception("ddb_sync_failed")
        stats["errors"] = -1

    return stats


async def sync_flarmnet() -> dict:
    """Download and import FlarmNet database.

    FLN format: 172 hex chars per line = 86 bytes binary per record.
    Fields: ID(6), Pilot(21), Airfield(21), AircraftType(21),
            Registration(7), CN(3), Frequency(7)

    Returns:
        Dict with counts: inserted, updated, errors.
    """
    log.info("flarmnet_sync_start")
    stats = {"inserted": 0, "updated": 0, "errors": 0, "total": 0}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(FLARMNET_URL)
            resp.raise_for_status()

        content = resp.text.strip()
        lines = content.split("\n")

        # First line is version header
        if len(lines) < 2:
            log.warning("flarmnet_empty")
            return stats

        db = get_db()
        async with db.acquire() as conn:
            for line in lines[1:]:
                line = line.strip()
                if len(line) < 172:
                    continue

                stats["total"] += 1
                try:
                    # Decode FLN hex format
                    raw = bytes.fromhex(line)
                    device_id = raw[0:6].decode("ascii", errors="replace").strip().upper()
                    aircraft_model = raw[27:48].decode("ascii", errors="replace").strip()
                    registration = raw[48:55].decode("ascii", errors="replace").strip()
                    cn = raw[55:58].decode("ascii", errors="replace").strip()

                    if not device_id or len(device_id) < 6:
                        continue

                    result = await conn.execute(
                        """
                        INSERT INTO aircraft_registry
                            (device_type, device_id, aircraft_model, registration,
                             competition_sign, tracked, identified, source, updated_at)
                        VALUES ('F', $1, $2, $3, $4, TRUE, TRUE, 'flarmnet', NOW())
                        ON CONFLICT (device_id) DO UPDATE SET
                            aircraft_model = CASE
                                WHEN aircraft_registry.source IN ('tenant', 'ogn_ddb')
                                THEN aircraft_registry.aircraft_model
                                ELSE EXCLUDED.aircraft_model END,
                            registration = CASE
                                WHEN aircraft_registry.source IN ('tenant', 'ogn_ddb')
                                THEN aircraft_registry.registration
                                ELSE EXCLUDED.registration END,
                            competition_sign = CASE
                                WHEN aircraft_registry.source IN ('tenant', 'ogn_ddb')
                                THEN aircraft_registry.competition_sign
                                ELSE EXCLUDED.competition_sign END,
                            updated_at = NOW()
                        """,
                        device_id,
                        aircraft_model[:128],
                        registration[:32],
                        cn[:8],
                    )

                    if "INSERT" in result:
                        stats["inserted"] += 1
                    else:
                        stats["updated"] += 1

                except Exception:
                    stats["errors"] += 1

        log.info("flarmnet_sync_complete", **stats)

    except httpx.HTTPError as e:
        log.error("flarmnet_sync_http_error", error=str(e))
        stats["errors"] = -1
    except Exception:
        log.exception("flarmnet_sync_failed")
        stats["errors"] = -1

    return stats


async def scheduled_sync(shutdown_event: asyncio.Event) -> None:
    """Run DDB sync daily at 04:00 UTC, FlarmNet weekly on Sunday.

    This is a long-running background task for the worker.
    """
    while not shutdown_event.is_set():
        now = datetime.now(timezone.utc)

        # Calculate seconds until next 04:00 UTC
        next_run = now.replace(hour=4, minute=0, second=0, microsecond=0)
        if now.hour >= 4:
            next_run = next_run.replace(day=now.day + 1)
        wait_seconds = (next_run - now).total_seconds()

        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=wait_seconds,
            )
            break  # Shutdown requested
        except asyncio.TimeoutError:
            pass

        # Run DDB sync (daily)
        try:
            await sync_ddb()
        except Exception:
            log.exception("scheduled_ddb_sync_failed")

        # Run FlarmNet sync (weekly on Sunday)
        now = datetime.now(timezone.utc)
        if now.weekday() == 6:  # Sunday
            try:
                await sync_flarmnet()
            except Exception:
                log.exception("scheduled_flarmnet_sync_failed")
