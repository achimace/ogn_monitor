-- Migration 006: Tracking hardening for VF-Sync (Konzept Kap. 6.1-6.5)
--   flight_status / flight_log:
--     launch_type          widened to 24 chars ('aerotow_ambiguous')
--     landing_count        touch & go counter (VF landingcount semantics)
--     landing_method       'observed' | 'silence'
--     landing_confidence   0..1 (silence landings carry reduced confidence)
--     pairing_confidence   0..1 aerotow pair confidence
--     release_method       'pair_separation' | 'towplane_max' | 'winch_vs_drop' | 'winch_profile'
--     release_altitude_agl / tow_duration_s: already in flight_log,
--                          added to flight_status
--   tenant_aircraft.role   towplane | glider | motorglider_sl | powered
--   airfields              touch_go_max_ground_s, silence_landing_s
-- Idempotent.

ALTER TABLE flight_status ALTER COLUMN launch_type TYPE VARCHAR(24);
ALTER TABLE flight_log ALTER COLUMN launch_type TYPE VARCHAR(24);

ALTER TABLE flight_status
    ADD COLUMN IF NOT EXISTS release_altitude_agl INT,
    ADD COLUMN IF NOT EXISTS tow_duration_s INT,
    ADD COLUMN IF NOT EXISTS release_method VARCHAR(24),
    ADD COLUMN IF NOT EXISTS pairing_confidence REAL,
    ADD COLUMN IF NOT EXISTS landing_count INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS landing_method VARCHAR(16),
    ADD COLUMN IF NOT EXISTS landing_confidence REAL;

ALTER TABLE flight_log
    ADD COLUMN IF NOT EXISTS release_method VARCHAR(24),
    ADD COLUMN IF NOT EXISTS pairing_confidence REAL,
    ADD COLUMN IF NOT EXISTS landing_count INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS landing_method VARCHAR(16),
    ADD COLUMN IF NOT EXISTS landing_confidence REAL;

ALTER TABLE tenant_aircraft
    ADD COLUMN IF NOT EXISTS role VARCHAR(16);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'tenant_aircraft_role_check'
    ) THEN
        ALTER TABLE tenant_aircraft
            ADD CONSTRAINT tenant_aircraft_role_check
            CHECK (role IS NULL OR role IN ('towplane', 'glider', 'motorglider_sl', 'powered'));
    END IF;
END $$;

ALTER TABLE airfields
    ADD COLUMN IF NOT EXISTS touch_go_max_ground_s INT DEFAULT 90,
    ADD COLUMN IF NOT EXISTS silence_landing_s INT DEFAULT 180;
