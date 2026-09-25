-- Migration 012: per-airfield aircraft ignore list + registry aircraft_type.
--
--   airfield_ignored_aircraft  FLARM-IDs (uppercase hex) that are never
--                              tracked at ONE airfield (rescue helicopter of
--                              the clinic next door, ...). Managed by the
--                              tenant via /api/airfields/{id}/ignored-aircraft;
--                              the worker loads it with the airfield configs
--                              (AirfieldConfig.ignored_flarm_ids) and reloads
--                              on the Redis channel tracker:config. Other
--                              airfields are unaffected; flight_log history
--                              is kept.
--
--   aircraft_registry.aircraft_type  optional category string (see
--                              app/tracking/aircraft_category.py); present in
--                              init.sql from the start but ensured here for
--                              databases created before it existed. The DDB
--                              import leaves it NULL (the DDB has no category).
--
-- Idempotent: safe to run on every deploy.

CREATE TABLE IF NOT EXISTS airfield_ignored_aircraft (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    airfield_id  UUID NOT NULL REFERENCES airfields(id) ON DELETE CASCADE,
    flarm_id     VARCHAR(16) NOT NULL,
    note         VARCHAR(120),
    created_at   TIMESTAMPTZ DEFAULT now(),
    UNIQUE (airfield_id, flarm_id)
);

CREATE INDEX IF NOT EXISTS idx_airfield_ignored_aircraft_airfield
    ON airfield_ignored_aircraft (airfield_id);

ALTER TABLE aircraft_registry ADD COLUMN IF NOT EXISTS aircraft_type VARCHAR(32);
