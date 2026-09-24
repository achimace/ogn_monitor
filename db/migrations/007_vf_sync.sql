-- Migration 007: VF-Sync (Vereinsflieger integration), Konzept Kap. 4.1
--   vf_sync_config    per-airfield configuration, credentials encrypted (Fernet)
--   vf_sync_sessions  one aggregate per detected flight (idempotency anchor)
--   vf_sync_audit     append-only log of every API read/write attempt
--   vf_sync_budget    API requests used per airfield and day
-- Idempotent.

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
