-- OGN FlightMonitor - Database Schema
-- PostgreSQL 16 + PostGIS
-- Executed on first docker-compose up

-- Enable PostGIS
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- =============================================
-- TENANTS (Mandanten)
-- =============================================
CREATE TABLE tenants (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                    VARCHAR(100) NOT NULL,
    slug                    VARCHAR(50) NOT NULL UNIQUE,
    email                   VARCHAR(255) NOT NULL UNIQUE,
    password_hash           VARCHAR(255) NOT NULL,
    email_verified          BOOLEAN DEFAULT FALSE,
    email_verification_token VARCHAR(255),
    disclaimer_accepted_at  TIMESTAMPTZ,
    disclaimer_version      INT DEFAULT 1,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_tenants_slug ON tenants(slug);
CREATE INDEX idx_tenants_email ON tenants(email);

-- =============================================
-- AIRFIELDS (Flugplaetze)
-- =============================================
CREATE TABLE airfields (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name                    VARCHAR(100) NOT NULL,
    slug                    VARCHAR(50) NOT NULL,
    icao_code               VARCHAR(4),
    latitude                DOUBLE PRECISION NOT NULL,
    longitude               DOUBLE PRECISION NOT NULL,
    elevation_m             INT NOT NULL,
    home_radius_m           INT DEFAULT 800,
    ogn_filter_radius_km    INT DEFAULT 500,
    alarm_timeout_s         INT DEFAULT 600,
    signal_loss_timeout_s   INT DEFAULT 300,
    takeoff_speed_kmh       INT DEFAULT 40,
    takeoff_alt_offset_m    INT DEFAULT 50,
    tow_plane_flarm_ids     TEXT[] DEFAULT '{}',
    winch_vs_threshold_ms   DOUBLE PRECISION DEFAULT 8.0,
    landed_visible_minutes  INT DEFAULT 1440,           -- 24h sticky-landed
    touch_go_max_ground_s   INT DEFAULT 90,             -- re-takeoff within -> touch & go
    silence_landing_s       INT DEFAULT 180,            -- final approach + silence -> landing
    monitor_strip_fields    TEXT[] DEFAULT ARRAY[
        'competition_sign','aircraft_model','takeoff_time','landing_time',
        'duration','launch_type','qdr','distance','altitude','agl',
        'speed','vs','track'
    ],
    is_active               BOOLEAN DEFAULT TRUE,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, slug)
);

CREATE INDEX idx_airfields_tenant ON airfields(tenant_id);
CREATE INDEX idx_airfields_active ON airfields(is_active) WHERE is_active = TRUE;

-- Spatial index for airfield positions
SELECT AddGeometryColumn('airfields', 'geom', 4326, 'POINT', 2);
CREATE INDEX idx_airfields_geom ON airfields USING GIST(geom);

-- Optional polygon defining the "home area" where takeoffs/landings are
-- detected. If NULL, the circular fallback (home_radius_m) is used.
SELECT AddGeometryColumn('airfields', 'home_polygon', 4326, 'POLYGON', 2);
CREATE INDEX idx_airfields_home_polygon ON airfields USING GIST(home_polygon);

-- Trigger to auto-update geom from lat/lon
CREATE OR REPLACE FUNCTION update_airfield_geom()
RETURNS TRIGGER AS $$
BEGIN
    NEW.geom = ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_airfield_geom
    BEFORE INSERT OR UPDATE OF latitude, longitude ON airfields
    FOR EACH ROW EXECUTE FUNCTION update_airfield_geom();

-- =============================================
-- ELEVATION TILES (Geländemodell, Migration 009)
-- =============================================
-- Copernicus GLO-90 DEM tiles, imported by the operator with
--   python -m app.tools.import_elevation --all
-- The worker computes AGL from these (app/tracking/elevation.py) and
-- falls back to the airfield elevation while no tile covers a position.
CREATE EXTENSION IF NOT EXISTS postgis_raster;

CREATE TABLE IF NOT EXISTS elevation_tiles (
    id           BIGSERIAL PRIMARY KEY,
    source_tile  VARCHAR(64) NOT NULL,        -- e.g. Copernicus_DSM_COG_30_N47_00_E011_00_DEM
    rast         raster NOT NULL,             -- 64x64 px sub-tile, EPSG:4326
    imported_at  TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_elevation_tiles_rast
    ON elevation_tiles USING gist (ST_ConvexHull(rast));
CREATE INDEX IF NOT EXISTS idx_elevation_tiles_source
    ON elevation_tiles (source_tile);

-- =============================================
-- AIRPORTS (bekannte Flugplaetze, OurAirports, Migration 011)
-- =============================================
-- Filled by the operator with  python -m app.tools.import_airports
-- (DEPLOYMENT.md 9b). The worker keeps the airports around the active
-- airfields in memory (app/tracking/airports.py): a landing within
-- foreign_airfield_radius_m of one is a landing there (landing_type
-- 'foreign'), not an outlanding; takeoffs there give visitors their
-- takeoff airfield / time. Kept in sync with db/migrations/011_airports.sql
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

CREATE INDEX IF NOT EXISTS idx_airports_location ON airports USING gist (location);
CREATE INDEX IF NOT EXISTS idx_airports_country ON airports (iso_country);

-- =============================================
-- TENANT AIRCRAFT (Vereinsflugzeuge)
-- =============================================
CREATE TABLE tenant_aircraft (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    airfield_id             UUID NOT NULL REFERENCES airfields(id) ON DELETE CASCADE,
    flarm_id                VARCHAR(16) NOT NULL,
    registration            VARCHAR(16) NOT NULL,
    competition_sign        VARCHAR(4),
    aircraft_model          VARCHAR(64),
    aircraft_type           VARCHAR(32),  -- 'glider', 'tow_plane', 'motor_glider', 'tmg'
    -- Launch-detection role (overrides aircraft_type mapping when set)
    role                    VARCHAR(16)
        CONSTRAINT tenant_aircraft_role_check
        CHECK (role IS NULL OR role IN ('towplane', 'glider', 'motorglider_sl', 'powered')),
    is_active               BOOLEAN DEFAULT TRUE,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(airfield_id, flarm_id)
);

CREATE INDEX idx_tenant_aircraft_airfield ON tenant_aircraft(airfield_id);
CREATE INDEX idx_tenant_aircraft_flarm ON tenant_aircraft(flarm_id);

-- =============================================
-- AIRCRAFT REGISTRY (OGN DDB + FlarmNet)
-- =============================================
CREATE TABLE aircraft_registry (
    device_id               VARCHAR(16) PRIMARY KEY,
    device_type             VARCHAR(1),   -- 'F' = FLARM, 'I' = ICAO, 'O' = OGN
    registration            VARCHAR(16),
    competition_sign        VARCHAR(4),
    aircraft_model          VARCHAR(64),
    aircraft_type           VARCHAR(32),
    tracked                 BOOLEAN DEFAULT TRUE,
    identified              BOOLEAN DEFAULT TRUE,
    source                  VARCHAR(16) DEFAULT 'ogn_ddb',  -- 'ogn_ddb', 'flarmnet'
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_registry_registration ON aircraft_registry(registration);

-- =============================================
-- FLIGHT STATUS (aktuell aktive Fluege - Sync von Redis)
-- =============================================
CREATE TABLE flight_status (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    airfield_id             UUID NOT NULL REFERENCES airfields(id) ON DELETE CASCADE,
    flarm_id                VARCHAR(16) NOT NULL,
    registration            VARCHAR(16),
    competition_sign        VARCHAR(4),
    aircraft_model          VARCHAR(64),
    status                  SMALLINT NOT NULL DEFAULT 0,
    -- 0=GROUND, 1=TAKEOFF, 2=FLYING, 3=LANDING, 4=OUTLANDING,
    -- 5=ALARM, 6=TOWING, 7=OUTLANDING_PENDING, 8=EMERGENCY,
    -- 9=DIVERTED, 10=SIGNAL_LOST

    -- Position
    latitude                DOUBLE PRECISION,
    longitude               DOUBLE PRECISION,
    altitude_m              INT,
    altitude_agl            INT,
    speed_kmh               INT,
    vertical_speed_ms       DOUBLE PRECISION,
    track_deg               INT,

    -- QDR from home airfield
    distance_m              INT,
    qdr_deg                 INT,
    bearing_text            VARCHAR(4),

    -- Times
    takeoff_time            TIMESTAMPTZ,
    landing_time            TIMESTAMPTZ,
    last_seen               TIMESTAMPTZ,

    -- Statistics
    max_altitude_m          INT,
    max_distance_m          INT,

    -- Launch type
    launch_type             VARCHAR(24),  -- winch/aerotow/aerotow_ambiguous/self/powered/unknown
    tow_plane_flarm_id      VARCHAR(16),
    tow_plane_registration  VARCHAR(32),
    release_altitude_m      INT,
    release_altitude_agl    INT,
    release_time            TIMESTAMPTZ,
    release_method          VARCHAR(24),  -- pair_separation/towplane_max/winch_vs_drop/winch_profile
    tow_duration_s          INT,
    pairing_confidence      REAL,

    -- Landing bookkeeping (touch & go, silence landing)
    landing_count           INT NOT NULL DEFAULT 1,
    landing_method          VARCHAR(16),  -- observed/silence
    landing_confidence      REAL,
    landing_final           BOOLEAN NOT NULL DEFAULT FALSE,  -- past the T&G window

    -- Foreign airfields / visitors (Migration 011)
    takeoff_airfield        VARCHAR(100), -- home name / known airport / 'Feld' / 'unbekannt'
    landing_airfield        VARCHAR(100), -- home name / known airport, '' for outlandings
    landing_type            VARCHAR(16),  -- 'home', 'foreign', 'outlanding', 'diverted'
    is_visitor              BOOLEAN NOT NULL DEFAULT FALSE,  -- did not start here

    -- Signal loss analysis
    signal_loss_scenario    VARCHAR(16),  -- DIVERTED/OUTLANDED/EMERGENCY/SIGNAL_LOST
    signal_loss_severity    VARCHAR(8),   -- CRITICAL/HIGH/MEDIUM/LOW
    signal_loss_flags       TEXT[],

    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(airfield_id, flarm_id)
);

CREATE INDEX idx_flight_status_airfield ON flight_status(airfield_id);
CREATE INDEX idx_flight_status_flarm ON flight_status(flarm_id);
CREATE INDEX idx_flight_status_active ON flight_status(status) WHERE status BETWEEN 1 AND 10;

-- =============================================
-- FLIGHT LOG (archivierte Fluege)
-- =============================================
CREATE TABLE flight_log (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    airfield_id             UUID NOT NULL REFERENCES airfields(id) ON DELETE CASCADE,
    flarm_id                VARCHAR(16) NOT NULL,
    registration            VARCHAR(16),
    competition_sign        VARCHAR(4),
    aircraft_model          VARCHAR(64),

    -- Flight data
    takeoff_time            TIMESTAMPTZ NOT NULL,
    landing_time            TIMESTAMPTZ,
    flight_duration_s       INT,
    max_altitude_m          INT,
    max_altitude_agl        INT,
    max_distance_m          INT,

    -- Launch type + F-Schlepp billing
    launch_type             VARCHAR(24),
    tow_plane_flarm_id      VARCHAR(16),
    tow_plane_registration  VARCHAR(32),
    release_altitude_m      INT,
    release_altitude_agl    INT,
    release_time            TIMESTAMPTZ,
    release_method          VARCHAR(24),
    tow_duration_s          INT,
    pairing_confidence      REAL,

    -- Landing bookkeeping (touch & go, silence landing)
    landing_count           INT NOT NULL DEFAULT 1,
    landing_method          VARCHAR(16),
    landing_confidence      REAL,

    -- Landing info (Migration 011: 'foreign' = landing at a known airport)
    landing_type            VARCHAR(16),  -- 'home', 'foreign', 'outlanding', 'diverted'
    landing_airfield        VARCHAR(100), -- home name / known airport, NULL for outlandings
    landing_latitude        DOUBLE PRECISION,
    landing_longitude       DOUBLE PRECISION,
    takeoff_airfield        VARCHAR(100), -- home name / known airport / 'Feld' / 'unbekannt'
    is_visitor              BOOLEAN NOT NULL DEFAULT FALSE,  -- did not start here

    -- Signal loss (if applicable)
    signal_loss_scenario    VARCHAR(16),
    signal_loss_severity    VARCHAR(8),

    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_flight_log_airfield ON flight_log(airfield_id);
CREATE INDEX idx_flight_log_date ON flight_log(takeoff_time);
CREATE INDEX idx_flight_log_flarm ON flight_log(flarm_id);
CREATE INDEX idx_flight_log_registration ON flight_log(registration);
ALTER TABLE flight_log
    ADD CONSTRAINT flight_log_unique_takeoff
    UNIQUE (airfield_id, flarm_id, takeoff_time);

-- =============================================
-- FLIGHT ALARM ACTIONS (Flugleiter-Workflow: quittieren / kommentieren)
-- Kept in sync with db/migrations/010_flight_alarm_actions.sql
-- =============================================
CREATE TABLE IF NOT EXISTS flight_alarm_actions (
    id                 UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    airfield_id        UUID NOT NULL REFERENCES airfields(id) ON DELETE CASCADE,
    flarm_id           VARCHAR(16) NOT NULL,
    flight_takeoff_ts  TIMESTAMPTZ,
    alarm_kind         VARCHAR(24) NOT NULL,   -- alarm | emergency | outlanding | signal_lost | other
    state              VARCHAR(24) NOT NULL,   -- acknowledged | retrieval_underway | resolved | false_alarm
    comment            TEXT,
    set_by             VARCHAR(255) NOT NULL,  -- user email
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT flight_alarm_actions_kind_check
        CHECK (alarm_kind IN ('alarm', 'emergency', 'outlanding', 'signal_lost', 'other')),
    CONSTRAINT flight_alarm_actions_state_check
        CHECK (state IN ('acknowledged', 'retrieval_underway', 'resolved', 'false_alarm'))
);

CREATE INDEX IF NOT EXISTS idx_flight_alarm_actions_lookup
    ON flight_alarm_actions (airfield_id, flarm_id, created_at DESC);

-- =============================================
-- FLIGHT PROFILE SNAPSHOT (bei Alarm gespeichert)
-- =============================================
CREATE TABLE flight_profile_snapshot (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    flight_status_id        UUID REFERENCES flight_status(id),
    flarm_id                VARCHAR(16) NOT NULL,
    airfield_id             UUID NOT NULL REFERENCES airfields(id) ON DELETE CASCADE,
    scenario                VARCHAR(16) NOT NULL,
    severity                VARCHAR(8) NOT NULL,
    abnormal_flags          TEXT[],
    profile_data            JSONB NOT NULL,  -- Last 10 min of beacons as JSON
    analysis_result         JSONB,           -- Analysis details
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_profile_snapshot_airfield ON flight_profile_snapshot(airfield_id);
CREATE INDEX idx_profile_snapshot_date ON flight_profile_snapshot(created_at);

-- =============================================
-- VF-SYNC (Vereinsflieger-Integration, Konzept Kap. 4.1)
-- Kept in sync with db/migrations/007_vf_sync.sql
-- =============================================
CREATE TABLE IF NOT EXISTS vf_sync_config (
    airfield_id      UUID PRIMARY KEY REFERENCES airfields(id) ON DELETE CASCADE,
    enabled          BOOLEAN NOT NULL DEFAULT FALSE,
    dry_run          BOOLEAN NOT NULL DEFAULT TRUE,
    vf_base_url      TEXT NOT NULL DEFAULT 'https://www.vereinsflieger.de',
    vf_cid           INTEGER,
    vf_username      TEXT,
    vf_password_enc  BYTEA,          -- Fernet(md5(password)), never plaintext
    vf_appkey_enc    BYTEA,          -- Fernet(appkey)
    flags            JSONB NOT NULL DEFAULT '{}'::jsonb,
    daily_budget     INTEGER NOT NULL DEFAULT 450,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vf_sync_sessions (
    session_id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    airfield_id          UUID NOT NULL REFERENCES airfields(id) ON DELETE CASCADE,
    flarm_id             VARCHAR(16) NOT NULL,
    registration         VARCHAR(16),
    takeoff_ts           TIMESTAMPTZ,
    landing_ts           TIMESTAMPTZ,
    landing_method       VARCHAR(16),
    start_type_detected  VARCHAR(24),
    tow_registration     VARCHAR(16),
    release_ts           TIMESTAMPTZ,
    release_alt_agl_m    INTEGER,
    release_method       VARCHAR(24),
    tow_time_min         INTEGER,
    landing_count        INTEGER NOT NULL DEFAULT 1,
    conf_pairing         REAL,
    conf_landing         REAL,
    conf_touchgo         REAL,
    matched_flid         BIGINT,
    state                VARCHAR(24) NOT NULL DEFAULT 'tracking',
    review_reason        TEXT,
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_attempt         TIMESTAMPTZ,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (airfield_id, flarm_id, takeoff_ts)
);

CREATE INDEX IF NOT EXISTS idx_vfsync_open
    ON vf_sync_sessions(airfield_id, state)
    WHERE state NOT IN ('completed', 'expired');

CREATE INDEX IF NOT EXISTS idx_vfsync_sessions_takeoff
    ON vf_sync_sessions(airfield_id, takeoff_ts);

CREATE TABLE IF NOT EXISTS vf_sync_audit (
    id            BIGSERIAL PRIMARY KEY,
    session_id    UUID REFERENCES vf_sync_sessions(session_id) ON DELETE SET NULL,
    airfield_id   UUID NOT NULL,
    ts            TIMESTAMPTZ DEFAULT NOW(),
    action        VARCHAR(24) NOT NULL,
    flid          BIGINT,
    fields_sent   JSONB,
    pre_state     JSONB,
    http_status   INTEGER,
    detail        TEXT
);

CREATE INDEX IF NOT EXISTS idx_vfsync_audit_airfield_ts
    ON vf_sync_audit(airfield_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_vfsync_audit_session
    ON vf_sync_audit(session_id);

CREATE TABLE IF NOT EXISTS vf_sync_budget (
    airfield_id  UUID NOT NULL,
    day          DATE NOT NULL,
    used         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (airfield_id, day)
);
