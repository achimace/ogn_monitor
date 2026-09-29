-- Migration 013: per-airfield switch for passing visitors on the monitor.
--
--   show_passing_visitors  TRUE (default): aircraft that did not start here
--                          and are airborne inside the visitor zone
--                          (is_visitor, see visitor_zone_km) are listed on
--                          the tower monitor. FALSE: the monitor list hides
--                          them while they are airborne; once a visitor has
--                          landed here it is shown like any other landing.
--                          Display only - the worker keeps tracking visitors
--                          so their takeoff airfield / landing stay known.
--
-- Idempotent: safe to run on every deploy.

ALTER TABLE airfields
    ADD COLUMN IF NOT EXISTS show_passing_visitors BOOLEAN NOT NULL DEFAULT TRUE;
