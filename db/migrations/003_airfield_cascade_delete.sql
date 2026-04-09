-- Migration 003: Add ON DELETE CASCADE to all airfield_id foreign keys
-- so deleting an airfield also removes its flight history.
-- Idempotent — safe to run multiple times.

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT conname, conrelid::regclass::text AS tbl
        FROM pg_constraint
        WHERE contype = 'f'
          AND confrelid = 'airfields'::regclass
          AND conrelid::regclass::text IN (
              'flight_status', 'flight_log', 'flight_profile_snapshot'
          )
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
    END LOOP;
END $$;

ALTER TABLE flight_status
    ADD CONSTRAINT flight_status_airfield_id_fkey
    FOREIGN KEY (airfield_id) REFERENCES airfields(id) ON DELETE CASCADE;

ALTER TABLE flight_log
    ADD CONSTRAINT flight_log_airfield_id_fkey
    FOREIGN KEY (airfield_id) REFERENCES airfields(id) ON DELETE CASCADE;

ALTER TABLE flight_profile_snapshot
    ADD CONSTRAINT flight_profile_snapshot_airfield_id_fkey
    FOREIGN KEY (airfield_id) REFERENCES airfields(id) ON DELETE CASCADE;
