"""Import OurAirports into the ``airports`` table (operator CLI).

    python -m app.tools.import_airports [--radius-km 300] [--all-world]
    python -m app.tools.import_airports --check 47.7297 12.4382

Downloads the public-domain airports.csv from OurAirports
(``settings.airports_csv_url``), keeps the rows of type small_airport /
medium_airport / large_airport (closed airports, heliports, seaplane bases
and balloonports are skipped) that lie within ``--radius-km`` of any active
airfield (``--all-world`` keeps every airport) and upserts them by
``ident``. Nothing is deleted: airports that vanished from the CSV stay.
Elevation is converted from feet to metres.

Migration 011 must have run (deploy.sh does that). The worker loads the
airports around its airfields at start and on every config reload (every
5 min), so no restart is needed after the import.

``--check LAT LON`` prints the nearest airport and its distance (uses the
GiST index, same query family as the worker's loader).
"""

import argparse
import asyncio
import csv
import io
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable

import httpx
import structlog

from app.config import settings
from app.db.connection import close_db, get_db, init_db
from app.tracking.geo_calc import haversine

log = structlog.get_logger()

DOWNLOAD_TIMEOUT_S = 120.0
FT_TO_M = 0.3048

# OurAirports types that are real airfields for our purposes
KEEP_TYPES = frozenset({"small_airport", "medium_airport", "large_airport"})

UPSERT_SQL = """
    INSERT INTO airports (
        ident, icao_code, name, type, latitude, longitude, elevation_m,
        iso_country, municipality, location, updated_at
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9,
        ST_SetSRID(ST_MakePoint($6, $5), 4326)::geography, now()
    )
    ON CONFLICT (ident) DO UPDATE SET
        icao_code    = EXCLUDED.icao_code,
        name         = EXCLUDED.name,
        type         = EXCLUDED.type,
        latitude     = EXCLUDED.latitude,
        longitude    = EXCLUDED.longitude,
        elevation_m  = EXCLUDED.elevation_m,
        iso_country  = EXCLUDED.iso_country,
        municipality = EXCLUDED.municipality,
        location     = EXCLUDED.location,
        updated_at   = now()
"""

CHECK_SQL = """
    SELECT ident, icao_code, name, type, elevation_m, municipality,
           ST_Distance(location, ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography) AS dist_m
    FROM airports
    ORDER BY location <-> ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
    LIMIT 1
"""


@dataclass
class AirportRecord:
    """One row ready for the upsert (parameter order = UPSERT_SQL)."""
    ident: str
    icao_code: str | None
    name: str
    type: str
    latitude: float
    longitude: float
    elevation_m: float | None
    iso_country: str | None
    municipality: str | None

    def params(self) -> tuple:
        return (
            self.ident, self.icao_code, self.name, self.type,
            self.latitude, self.longitude, self.elevation_m,
            self.iso_country, self.municipality,
        )


@dataclass
class ImportSummary:
    """Counters printed at the end of a run."""
    rows_total: int = 0
    rows_type_kept: int = 0
    rows_kept: int = 0
    rows_invalid: int = 0
    upserted: int = 0
    airfields: int = 0
    radius_km: float | None = None
    by_country: dict[str, int] = field(default_factory=dict)

    def print(self) -> None:
        area = "all-world" if self.radius_km is None else f"radius_km={self.radius_km:g}"
        print(
            f"rows_total={self.rows_total} rows_type_kept={self.rows_type_kept} "
            f"rows_invalid={self.rows_invalid} rows_kept={self.rows_kept} "
            f"upserted={self.upserted} airfields={self.airfields} {area}"
        )
        for country, n in sorted(self.by_country.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {country or '??':2} {n}")


def _float_or_none(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def elevation_ft_to_m(value: str | None) -> float | None:
    """OurAirports ``elevation_ft`` -> metres (1 decimal), None when empty."""
    ft = _float_or_none(value)
    if ft is None:
        return None
    return round(ft * FT_TO_M, 1)


def icao_code_of(row: dict) -> str | None:
    """ICAO code of a CSV row: ``icao_code`` column, else a 4-letter gps_code/ident."""
    for key in ("icao_code", "gps_code", "ident"):
        code = (row.get(key) or "").strip().upper()
        if len(code) == 4 and code.isalpha():
            return code
    return None


def parse_row(row: dict) -> AirportRecord | None:
    """CSV row -> AirportRecord, or None when the row is unusable."""
    ident = (row.get("ident") or "").strip()
    name = (row.get("name") or "").strip()
    lat = _float_or_none(row.get("latitude_deg"))
    lon = _float_or_none(row.get("longitude_deg"))
    if not ident or not name or lat is None or lon is None:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    country = (row.get("iso_country") or "").strip().upper() or None
    municipality = (row.get("municipality") or "").strip() or None
    return AirportRecord(
        ident=ident[:16],
        icao_code=icao_code_of(row),
        name=name[:120],
        type=(row.get("type") or "").strip()[:24],
        latitude=lat,
        longitude=lon,
        elevation_m=elevation_ft_to_m(row.get("elevation_ft")),
        iso_country=country[:2] if country else None,
        municipality=municipality[:120] if municipality else None,
    )


def within_radius(rec: AirportRecord, centres: Iterable[tuple[float, float]],
                  radius_km: float) -> bool:
    """Whether an airport lies within ``radius_km`` of any (lat, lon) centre."""
    radius_m = radius_km * 1000.0
    return any(
        haversine(rec.latitude, rec.longitude, lat, lon) <= radius_m
        for lat, lon in centres
    )


def filter_rows(rows: Iterable[dict], centres: list[tuple[float, float]],
                radius_km: float | None, summary: ImportSummary) -> list[AirportRecord]:
    """Apply the type and radius filters; counts go into ``summary``.

    ``radius_km`` None = keep everything (``--all-world``).
    """
    kept: list[AirportRecord] = []
    for row in rows:
        summary.rows_total += 1
        if (row.get("type") or "").strip() not in KEEP_TYPES:
            continue
        summary.rows_type_kept += 1
        rec = parse_row(row)
        if rec is None:
            summary.rows_invalid += 1
            continue
        if radius_km is not None and not within_radius(rec, centres, radius_km):
            continue
        kept.append(rec)
    summary.rows_kept = len(kept)
    for rec in kept:
        summary.by_country[rec.iso_country or ""] = summary.by_country.get(rec.iso_country or "", 0) + 1
    return kept


def parse_csv(text: str) -> Iterable[dict]:
    """Rows of the OurAirports CSV (header row = keys)."""
    return csv.DictReader(io.StringIO(text))


async def download_csv(url: str | None = None) -> str:
    """The airports.csv text (UTF-8)."""
    url = url or settings.airports_csv_url
    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


async def load_airfield_centres(db) -> list[tuple[float, float]]:
    """(lat, lon) of every active airfield."""
    rows = await db.fetch(
        "SELECT latitude, longitude FROM airfields WHERE is_active = TRUE"
    )
    return [(float(r["latitude"]), float(r["longitude"])) for r in rows]


async def upsert_airports(db, records: list[AirportRecord]) -> int:
    """Upsert all records (one transaction, executemany). Returns the count."""
    if not records:
        return 0
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(UPSERT_SQL, [r.params() for r in records])
    return len(records)


async def analyze_airports(db) -> None:
    """Refresh planner statistics after the bulk load (GiST lookups)."""
    await db.execute("ANALYZE airports")


async def run_import(radius_km: float | None, csv_text: str | None = None) -> ImportSummary:
    """Download (unless ``csv_text`` is given), filter, upsert, analyze."""
    db = get_db()
    summary = ImportSummary(radius_km=radius_km)
    centres = await load_airfield_centres(db)
    summary.airfields = len(centres)
    if radius_km is not None and not centres:
        log.warning("no_active_airfields", hint="nothing to import within a radius; use --all-world")
        return summary

    t0 = time.monotonic()
    if csv_text is None:
        csv_text = await download_csv()
        log.info("airports_csv_downloaded", mb=round(len(csv_text) / 1e6, 1),
                 duration_s=round(time.monotonic() - t0, 1))

    records = filter_rows(parse_csv(csv_text), centres, radius_km, summary)
    summary.upserted = await upsert_airports(db, records)
    if summary.upserted:
        await analyze_airports(db)
    log.info(
        "airports_imported",
        upserted=summary.upserted,
        rows_total=summary.rows_total,
        radius_km=radius_km,
        duration_s=round(time.monotonic() - t0, 1),
    )
    return summary


async def run_check(lat: float, lon: float) -> dict | None:
    """Nearest airport to a point as a dict (``dist_m`` included), or None."""
    db = get_db()
    row = await db.fetchrow(CHECK_SQL, lat, lon)
    return dict(row) if row else None


def format_check(lat: float, lon: float, row: dict | None) -> str:
    if row is None:
        return f"{lat:.5f} {lon:.5f}: no airports imported"
    code = f" ({row['icao_code']})" if row.get("icao_code") else ""
    elev = f", {row['elevation_m']:.0f} m" if row.get("elevation_m") is not None else ""
    return (
        f"{lat:.5f} {lon:.5f}: {row['name']}{code} [{row['ident']}, {row['type']}"
        f"{elev}] {row['dist_m']:.0f} m"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.tools.import_airports",
        description="Import OurAirports airports around the active airfields.",
    )
    p.add_argument("--radius-km", type=float, default=settings.airports_index_radius_km,
                   help=f"keep airports within this radius of any active airfield "
                        f"(default {settings.airports_index_radius_km})")
    p.add_argument("--all-world", action="store_true",
                   help="keep every airport (ignores --radius-km)")
    p.add_argument("--check", nargs=2, type=float, metavar=("LAT", "LON"),
                   help="print the nearest airport to a point and exit")
    return p


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    await init_db()
    try:
        if args.check:
            lat, lon = args.check
            row = await run_check(lat, lon)
            print(format_check(lat, lon, row))
            return 0 if row else 1
        summary = await run_import(None if args.all_world else args.radius_km)
        summary.print()
        # Nothing imported (empty / unusable CSV, no active airfield, no
        # airport in range) is a failure for the operator, not a success.
        return 0 if summary.upserted else 1
    finally:
        await close_db()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
