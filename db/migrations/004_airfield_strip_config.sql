-- Migration 004: Per-airfield monitor configuration
--   landed_visible_minutes: how long landed flights stay visible on the
--                           tower monitor before being archived
--   monitor_strip_fields:   list of optional flight strip field IDs to show
-- Idempotent.

ALTER TABLE airfields
    ADD COLUMN IF NOT EXISTS landed_visible_minutes INT DEFAULT 1440;

ALTER TABLE airfields
    ADD COLUMN IF NOT EXISTS monitor_strip_fields TEXT[] DEFAULT ARRAY[
        'competition_sign','aircraft_model','takeoff_time','landing_time',
        'duration','launch_type','qdr','distance','altitude','agl',
        'speed','vs','track'
    ];
