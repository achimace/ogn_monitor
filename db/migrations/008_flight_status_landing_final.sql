-- Migration 008: persist whether a landing is past the touch & go window.
-- VF-Sync recovery may only take a landing_time from flight_status when it
-- is final (flight_log rows are written on landing_final anyway).
-- Idempotent.

ALTER TABLE flight_status
    ADD COLUMN IF NOT EXISTS landing_final BOOLEAN NOT NULL DEFAULT FALSE;
