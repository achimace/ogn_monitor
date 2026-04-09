-- Migration 005: Deduplicate flight_log and add a UNIQUE constraint
-- so the same flight (airfield + flarm + takeoff_time) can never be
-- inserted twice. Idempotent.

-- 1. Drop existing duplicates, keeping the row with the highest id
DELETE FROM flight_log fl
USING flight_log keep
WHERE fl.airfield_id = keep.airfield_id
  AND fl.flarm_id    = keep.flarm_id
  AND fl.takeoff_time = keep.takeoff_time
  AND fl.id < keep.id;

-- 2. Add the UNIQUE constraint if not already present
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'flight_log_unique_takeoff'
    ) THEN
        ALTER TABLE flight_log
            ADD CONSTRAINT flight_log_unique_takeoff
            UNIQUE (airfield_id, flarm_id, takeoff_time);
    END IF;
END $$;
