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
    launch_type             VARCHAR(16),  -- 'winch', 'aerotow', 'self', 'unknown'
    tow_plane_flarm_id      VARCHAR(16),
    tow_plane_registration  VARCHAR(32),
    release_altitude_m      INT,
    release_time            TIMESTAMPTZ,

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
    launch_type             VARCHAR(16),
    tow_plane_flarm_id      VARCHAR(16),
    tow_plane_registration  VARCHAR(32),
    release_altitude_m      INT,
    release_altitude_agl    INT,
    release_time            TIMESTAMPTZ,
    tow_duration_s          INT,

    -- Landing info
    landing_type            VARCHAR(16),  -- 'home', 'outlanding', 'diverted'
    landing_airfield        VARCHAR(100),
    landing_latitude        DOUBLE PRECISION,
    landing_longitude       DOUBLE PRECISION,

    -- Signal loss (if applicable)
    signal_loss_scenario    VARCHAR(16),
    signal_loss_severity    VARCHAR(8),

    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_flight_log_airfield ON flight_log(airfield_id);
CREATE INDEX idx_flight_log_date ON flight_log(takeoff_time);
CREATE INDEX idx_flight_log_flarm ON flight_log(flarm_id);
CREATE INDEX idx_flight_log_registration ON flight_log(registration);

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
