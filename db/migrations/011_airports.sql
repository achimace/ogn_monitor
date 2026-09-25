-- Migration 011: known airports (OurAirports, public domain) + foreign
-- airfields / visitors on flight_status and flight_log.
--
--   airports   one row per airport (small/medium/large), filled by the
--              operator with  python -m app.tools.import_airports
--              (see DEPLOYMENT.md 9b). The worker keeps the airports around
--              the active airfields in memory (app/tracking/airports.py):
--              a landing within foreign_airfield_radius_m of one of them is
--              a normal landing there (landing_type 'foreign'), not an
--              outlanding; ground contact / takeoffs there give visitors
--              their takeoff airfield and time.
--
--   flight_status / flight_log: takeoff_airfield, landing_airfield,
--              landing_type (flight_status), is_visitor.
--              landing_type values: 'home' | 'foreign' | 'outlanding' | 'diverted'
--
-- Idempotent: safe to run on every deploy.

CREATE TABLE IF NOT EXISTS airports (
    id            SERIAL PRIMARY KEY,
    ident         VARCHAR(16) UNIQUE NOT NULL,   -- OurAirports ident (ICAO or e.g. DE-0123)
    icao_code     VARCHAR(8),
    name          VARCHAR(120) NOT NULL,
    type          VARCHAR(24) NOT NULL,          -- small_airport / medium_airport / large_airport
    latitude      DOUBLE PRECISION NOT NULL,
    longitude     DOUBLE PRECISION NOT NULL,
    elevation_m   REAL,
    iso_country   CHAR(2),
    municipality  VARCHAR(120),
    location      GEOGRAPHY(POINT, 4326) NOT NULL,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
        WHERE schemaname = 'public' AND indexname = 'idx_airports_location'
    ) THEN
        CREATE INDEX idx_airports_location ON airports USING gist (location);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_airports_country ON airports (iso_country);

-- flight_status: where the flight started / landed, visitor flag
ALTER TABLE flight_status ADD COLUMN IF NOT EXISTS takeoff_airfield VARCHAR(100);
ALTER TABLE flight_status ADD COLUMN IF NOT EXISTS landing_airfield VARCHAR(100);
ALTER TABLE flight_status ADD COLUMN IF NOT EXISTS landing_type     VARCHAR(16);
ALTER TABLE flight_status ADD COLUMN IF NOT EXISTS is_visitor       BOOLEAN NOT NULL DEFAULT FALSE;

-- flight_log: landing_airfield / landing_type exist since init.sql;
-- landing_type gains the value 'foreign' (landing at a known airport).
ALTER TABLE flight_log ADD COLUMN IF NOT EXISTS takeoff_airfield VARCHAR(100);
ALTER TABLE flight_log ADD COLUMN IF NOT EXISTS landing_airfield VARCHAR(100);
ALTER TABLE flight_log ADD COLUMN IF NOT EXISTS is_visitor       BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN flight_log.landing_type IS
    'home | foreign (known airport, see landing_airfield) | outlanding | diverted';
COMMENT ON COLUMN flight_status.landing_type IS
    'home | foreign (known airport, see landing_airfield) | outlanding | diverted';
