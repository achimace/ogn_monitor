-- Migration 010: tower alarm workflow (acknowledge / annotate with history).
--   flight_alarm_actions  one row per Flugleiter action on an alarm; the
--                         newest row per (airfield, flarm_id) is mirrored
--                         into the Redis hot state (alarm_* fields), the
--                         table keeps the full history after the flight is
--                         archived.
-- Idempotent.

CREATE TABLE IF NOT EXISTS flight_alarm_actions (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    airfield_id        UUID NOT NULL REFERENCES airfields(id) ON DELETE CASCADE,
    flarm_id           VARCHAR(16) NOT NULL,
    flight_takeoff_ts  TIMESTAMPTZ,
    alarm_kind         VARCHAR(24) NOT NULL,   -- alarm | emergency | outlanding | signal_lost | other
    state              VARCHAR(24) NOT NULL,   -- acknowledged | retrieval_underway | resolved | false_alarm
    comment            TEXT,
    set_by             VARCHAR(255) NOT NULL,  -- user email
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_flight_alarm_actions_lookup
    ON flight_alarm_actions (airfield_id, flarm_id, created_at DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'flight_alarm_actions_kind_check'
    ) THEN
        ALTER TABLE flight_alarm_actions
            ADD CONSTRAINT flight_alarm_actions_kind_check
            CHECK (alarm_kind IN ('alarm', 'emergency', 'outlanding', 'signal_lost', 'other'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'flight_alarm_actions_state_check'
    ) THEN
        ALTER TABLE flight_alarm_actions
            ADD CONSTRAINT flight_alarm_actions_state_check
            CHECK (state IN ('acknowledged', 'retrieval_underway', 'resolved', 'false_alarm'));
    END IF;
END $$;
