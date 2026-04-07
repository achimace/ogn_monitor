-- Migration 002: Add optional home polygon to airfields.
-- Idempotent - safe to run multiple times.
-- Matches Alembic revision 002_airfield_home_polygon.py.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'airfields' AND column_name = 'home_polygon'
    ) THEN
        PERFORM AddGeometryColumn('airfields', 'home_polygon', 4326, 'POLYGON', 2);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_airfields_home_polygon
    ON airfields USING GIST(home_polygon);
