-- Migration 009: terrain model (Copernicus GLO-90 DEM tiles) for AGL.
--
-- The worker computes the height above ground from these raster tiles
-- (app/app/tracking/elevation.py) instead of the airfield elevation. Tiles
-- are imported by the operator with
--     python -m app.tools.import_elevation --all
-- (see DEPLOYMENT.md "Geländemodell"). Without tiles the worker silently
-- falls back to the airfield elevation.
--
-- Idempotent: safe to run on every deploy.

CREATE EXTENSION IF NOT EXISTS postgis_raster;

CREATE TABLE IF NOT EXISTS elevation_tiles (
    id           BIGSERIAL PRIMARY KEY,
    -- Copernicus tile name, e.g. Copernicus_DSM_COG_30_N47_00_E011_00_DEM
    source_tile  VARCHAR(64) NOT NULL,
    -- 64x64 px sub-tile of the 1x1 degree source raster (EPSG:4326)
    rast         raster NOT NULL,
    imported_at  TIMESTAMPTZ DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_elevation_tiles_rast'
    ) THEN
        CREATE INDEX idx_elevation_tiles_rast
            ON elevation_tiles USING gist (ST_ConvexHull(rast));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_elevation_tiles_source
    ON elevation_tiles (source_tile);
