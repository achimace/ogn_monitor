"""Import Copernicus GLO-90 DEM tiles into ``elevation_tiles`` (operator CLI).

    python -m app.tools.import_elevation --all [--radius-km 60] [--force]
    python -m app.tools.import_elevation --slug ohlstadt [--radius-km 60] [--force]
    python -m app.tools.import_elevation --check 47.6386 11.2394

For every active airfield the 1x1 degree tiles covering lat/lon +/- radius
are downloaded from the public Copernicus S3 bucket (no credentials,
~5 MB per tile) and ingested by PostGIS itself (``ST_FromGDALRaster`` ->
64x64 px sub-tiles). Tiles already present are skipped unless ``--force``
(which replaces them). Tiles over sea do not exist in the bucket (HTTP
404): logged and skipped.

Copernicus GLO-90 is a surface model (DSM): over forest/buildings the
value is the canopy/roof, at airfields the ground.

Migration 009 must have run (deploy.sh does that). The worker picks new
tiles up on its next cache miss / warm-up (restart to re-warm).
"""

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from math import cos, floor, radians

import httpx
import structlog

from app.config import settings
from app.db.connection import close_db, get_db, init_db

log = structlog.get_logger()

M_PER_DEG_LAT = 111_320.0
DOWNLOAD_TIMEOUT_S = 120.0
# Sub-tile size for ST_Tile (px). 64x64 keeps rows small and the GiST
# lookup precise.
SUBTILE_PX = 64

INSERT_SQL = (
    "INSERT INTO elevation_tiles (source_tile, rast) "
    "SELECT $1, ST_Tile(ST_FromGDALRaster($2::bytea, 4326), "
    f"{SUBTILE_PX}, {SUBTILE_PX})"
)


def tile_name(lat_floor: int, lon_floor: int) -> str:
    """Copernicus tile name of the 1x1 degree cell with this lower-left corner.

    ``Copernicus_DSM_COG_30_N47_00_E011_00_DEM`` covers 47..48N, 11..12E;
    ``S34`` covers -34..-33, ``W001`` covers -1..0.
    """
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"Copernicus_DSM_COG_30_{ns}{abs(lat_floor):02d}_00_{ew}{abs(lon_floor):03d}_00_DEM"


def tile_url(name: str, base_url: str | None = None) -> str:
    base = (base_url or settings.terrain_dem_base_url).rstrip("/")
    return f"{base}/{name}/{name}.tif"


def tiles_for_area(lat: float, lon: float, radius_km: float) -> list[str]:
    """Names of the 1-degree tiles covering lat/lon +/- radius_km.

    Longitude is wrapped to -180..180; latitude is clamped to the poles.
    Sorted, unique.
    """
    if radius_km < 0:
        radius_km = 0.0
    d_lat = radius_km * 1000.0 / M_PER_DEG_LAT
    m_per_deg_lon = M_PER_DEG_LAT * cos(radians(lat))
    d_lon = radius_km * 1000.0 / m_per_deg_lon if m_per_deg_lon > 1.0 else 180.0

    lat_min = max(-90.0, lat - d_lat)
    lat_max = min(89.999999, lat + d_lat)
    lon_min = lon - d_lon
    lon_max = lon + d_lon

    names: set[str] = set()
    for la in range(floor(lat_min), floor(lat_max) + 1):
        for lo in range(floor(lon_min), floor(lon_max) + 1):
            lo_wrapped = ((lo + 180) % 360) - 180
            names.add(tile_name(la, lo_wrapped))
    return sorted(names)


@dataclass
class ImportSummary:
    """Counters printed at the end of a run."""
    airfields: int = 0
    tiles_wanted: list[str] = field(default_factory=list)
    imported: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 404 (sea)
    failed: list[str] = field(default_factory=list)
    bytes_downloaded: int = 0

    def print(self) -> None:
        print(
            f"airfields={self.airfields} tiles_wanted={len(self.tiles_wanted)} "
            f"imported={len(self.imported)} replaced={len(self.replaced)} "
            f"skipped_existing={len(self.skipped_existing)} "
            f"missing_404={len(self.missing)} failed={len(self.failed)} "
            f"downloaded={self.bytes_downloaded / 1e6:.1f} MB"
        )
        for label, items in (("imported", self.imported), ("replaced", self.replaced),
                             ("skipped", self.skipped_existing),
                             ("missing", self.missing), ("failed", self.failed)):
            for name in items:
                print(f"  {label:8} {name}")


async def existing_tiles(db) -> set[str]:
    """Names of tiles already in elevation_tiles."""
    rows = await db.fetch("SELECT DISTINCT source_tile FROM elevation_tiles")
    return {r["source_tile"] for r in rows}


async def load_airfields(db, slug: str | None) -> list[dict]:
    """Active airfields (all, or the one with ``slug``)."""
    if slug:
        rows = await db.fetch(
            "SELECT slug, latitude, longitude FROM airfields WHERE slug = $1", slug
        )
        if not rows:
            sys.exit(f"airfield '{slug}' not found")
    else:
        rows = await db.fetch(
            "SELECT slug, latitude, longitude FROM airfields "
            "WHERE is_active = TRUE ORDER BY slug"
        )
    return [dict(r) for r in rows]


def plan_tiles(airfields: list[dict], radius_km: float, existing: set[str],
               force: bool) -> tuple[list[str], list[str]]:
    """Split the wanted tiles into (to_fetch, skipped_existing)."""
    wanted: set[str] = set()
    for af in airfields:
        wanted.update(tiles_for_area(af["latitude"], af["longitude"], radius_km))
    to_fetch, skipped = [], []
    for name in sorted(wanted):
        if name in existing and not force:
            skipped.append(name)
        else:
            to_fetch.append(name)
    return to_fetch, skipped


async def download_tile(client: httpx.AsyncClient, name: str) -> bytes | None:
    """GeoTIFF bytes, or None when the tile does not exist (HTTP 404)."""
    url = tile_url(name)
    resp = await client.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


async def store_tile(db, name: str, data: bytes, replace: bool) -> int:
    """Insert one source tile (one transaction). Returns rows inserted."""
    async with db.acquire() as conn:
        async with conn.transaction():
            # Session GUC, only inside this transaction (DB user is superuser)
            await conn.execute("SET LOCAL postgis.gdal_enabled_drivers = 'GTiff'")
            if replace:
                await conn.execute(
                    "DELETE FROM elevation_tiles WHERE source_tile = $1", name
                )
            status = await conn.execute(INSERT_SQL, name, data)
    # "INSERT 0 <n>"
    try:
        return int(status.rsplit(" ", 1)[-1])
    except (ValueError, AttributeError):
        return 0


async def run_import(slug: str | None, radius_km: float, force: bool) -> ImportSummary:
    db = get_db()
    summary = ImportSummary()
    airfields = await load_airfields(db, slug)
    summary.airfields = len(airfields)
    existing = await existing_tiles(db)
    to_fetch, skipped = plan_tiles(airfields, radius_km, existing, force)
    summary.tiles_wanted = sorted(set(to_fetch) | set(skipped))
    summary.skipped_existing = skipped
    for name in skipped:
        log.info("tile_skipped_existing", tile=name)

    async with httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_S,
                                 follow_redirects=True) as client:
        for name in to_fetch:
            t0 = time.monotonic()
            try:
                data = await download_tile(client, name)
            except httpx.HTTPError as exc:
                log.error("tile_download_failed", tile=name, error=str(exc))
                summary.failed.append(name)
                continue
            if data is None:
                log.info("tile_missing_404", tile=name, hint="probably sea")
                summary.missing.append(name)
                continue
            summary.bytes_downloaded += len(data)
            replace = name in existing
            try:
                rows = await store_tile(db, name, data, replace=replace)
            except Exception as exc:  # noqa: BLE001 - report and go on
                log.error("tile_import_failed", tile=name, error=str(exc)[:300])
                summary.failed.append(name)
                continue
            (summary.replaced if replace else summary.imported).append(name)
            log.info(
                "tile_imported", tile=name, mb=round(len(data) / 1e6, 1),
                subtiles=rows, replaced=replace,
                duration_s=round(time.monotonic() - t0, 1),
            )
    if summary.imported or summary.replaced:
        await analyze_tiles(db)
    return summary


async def analyze_tiles(db) -> None:
    """Refresh planner statistics after a bulk load.

    Without them the planner may prefer a sequential scan over the GiST
    index for the worker's tile lookups (``ANALYZE`` is cheap here).
    """
    t0 = time.monotonic()
    await db.execute("ANALYZE elevation_tiles")
    log.info("tiles_analyzed", duration_s=round(time.monotonic() - t0, 1))


async def run_check(lat: float, lon: float) -> float | None:
    from app.tracking.elevation import ElevationService
    svc = ElevationService(get_db(), enabled=True)
    return await svc.get(lat, lon)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.tools.import_elevation",
        description="Import Copernicus GLO-90 tiles around the airfields.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="all active airfields")
    g.add_argument("--slug", help="one airfield")
    g.add_argument("--check", nargs=2, type=float, metavar=("LAT", "LON"),
                   help="print the elevation at a point and exit")
    p.add_argument("--radius-km", type=float, default=settings.terrain_import_radius_km,
                   help=f"area around each airfield (default {settings.terrain_import_radius_km})")
    p.add_argument("--force", action="store_true",
                   help="re-download and replace tiles that already exist")
    return p


async def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    await init_db()
    try:
        if args.check:
            lat, lon = args.check
            elev = await run_check(lat, lon)
            if elev is None:
                print(f"{lat:.5f} {lon:.5f}: no elevation data (no tile imported here)")
                return 1
            print(f"{lat:.5f} {lon:.5f}: {elev:.0f} m")
            return 0
        summary = await run_import(
            None if args.all else args.slug, args.radius_km, args.force
        )
        summary.print()
        return 1 if summary.failed else 0
    finally:
        await close_db()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
