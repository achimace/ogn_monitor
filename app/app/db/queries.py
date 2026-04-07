"""Parametrized SQL queries for all database operations.

All queries use $1, $2, ... parameter placeholders (asyncpg style).
NEVER use string concatenation for query building.
"""

# =============================================
# TENANTS
# =============================================

TENANT_INSERT = """
    INSERT INTO tenants (name, slug, email, password_hash, email_verification_token)
    VALUES ($1, $2, $3, $4, $5)
    RETURNING id, name, slug, email, email_verified, disclaimer_accepted_at, created_at
"""

TENANT_BY_EMAIL = """
    SELECT id, name, slug, email, password_hash, email_verified,
           email_verification_token, disclaimer_accepted_at, disclaimer_version,
           created_at, updated_at
    FROM tenants WHERE email = $1
"""

TENANT_BY_ID = """
    SELECT id, name, slug, email, email_verified,
           disclaimer_accepted_at, disclaimer_version, created_at, updated_at
    FROM tenants WHERE id = $1
"""

TENANT_BY_SLUG = """
    SELECT id, name, slug, email, email_verified,
           disclaimer_accepted_at, disclaimer_version, created_at, updated_at
    FROM tenants WHERE slug = $1
"""

TENANT_UPDATE = """
    UPDATE tenants SET name = $2, slug = $3, updated_at = NOW()
    WHERE id = $1
    RETURNING id, name, slug, email, email_verified, disclaimer_accepted_at, updated_at
"""

TENANT_DELETE = """
    DELETE FROM tenants WHERE id = $1
"""

TENANT_VERIFY_EMAIL = """
    UPDATE tenants SET email_verified = TRUE, email_verification_token = NULL, updated_at = NOW()
    WHERE email_verification_token = $1
    RETURNING id, email
"""

TENANT_ACCEPT_DISCLAIMER = """
    UPDATE tenants SET disclaimer_accepted_at = NOW(), disclaimer_version = $2, updated_at = NOW()
    WHERE id = $1
    RETURNING id, disclaimer_accepted_at, disclaimer_version
"""

TENANT_UPDATE_PASSWORD = """
    UPDATE tenants SET password_hash = $2, updated_at = NOW()
    WHERE id = $1
"""

# =============================================
# AIRFIELDS
# =============================================

_AIRFIELD_COLS = (
    "id, tenant_id, name, slug, icao_code, latitude, longitude, elevation_m, "
    "home_radius_m, ogn_filter_radius_km, alarm_timeout_s, signal_loss_timeout_s, "
    "takeoff_speed_kmh, takeoff_alt_offset_m, tow_plane_flarm_ids, "
    "winch_vs_threshold_ms, is_active, created_at, updated_at, "
    "ST_AsGeoJSON(home_polygon) AS home_polygon"
)

AIRFIELD_INSERT = f"""
    INSERT INTO airfields (
        tenant_id, name, slug, icao_code, latitude, longitude, elevation_m,
        home_radius_m, ogn_filter_radius_km, alarm_timeout_s, signal_loss_timeout_s,
        takeoff_speed_kmh, takeoff_alt_offset_m, tow_plane_flarm_ids, winch_vs_threshold_ms,
        home_polygon
    ) VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
        CASE WHEN $16::text IS NULL THEN NULL
             ELSE ST_SetSRID(ST_GeomFromGeoJSON($16), 4326) END
    )
    RETURNING {_AIRFIELD_COLS}
"""

AIRFIELD_LIST_BY_TENANT = f"""
    SELECT {_AIRFIELD_COLS}
    FROM airfields WHERE tenant_id = $1 ORDER BY name
"""

AIRFIELD_BY_ID = f"""
    SELECT {_AIRFIELD_COLS}
    FROM airfields WHERE id = $1
"""

AIRFIELD_BY_SLUG = """
    SELECT id, tenant_id, name, slug, icao_code, latitude, longitude, elevation_m,
           home_radius_m, ogn_filter_radius_km
    FROM airfields WHERE slug = $1 AND is_active = TRUE
"""

AIRFIELD_UPDATE = f"""
    UPDATE airfields SET
        name = $2, slug = $3, icao_code = $4, latitude = $5, longitude = $6,
        elevation_m = $7, home_radius_m = $8, ogn_filter_radius_km = $9,
        alarm_timeout_s = $10, signal_loss_timeout_s = $11,
        takeoff_speed_kmh = $12, takeoff_alt_offset_m = $13,
        tow_plane_flarm_ids = $14, winch_vs_threshold_ms = $15,
        is_active = $16,
        home_polygon = CASE WHEN $17::text IS NULL THEN NULL
                            ELSE ST_SetSRID(ST_GeomFromGeoJSON($17), 4326) END,
        updated_at = NOW()
    WHERE id = $1
    RETURNING {_AIRFIELD_COLS}
"""

AIRFIELD_DELETE = """
    DELETE FROM airfields WHERE id = $1 AND tenant_id = $2
"""

# =============================================
# TENANT AIRCRAFT
# =============================================

AIRCRAFT_INSERT = """
    INSERT INTO tenant_aircraft (airfield_id, flarm_id, registration, competition_sign, aircraft_model, aircraft_type)
    VALUES ($1, $2, $3, $4, $5, $6)
    RETURNING id, airfield_id, flarm_id, registration, competition_sign, aircraft_model, aircraft_type, is_active, created_at
"""

AIRCRAFT_LIST_BY_AIRFIELD = """
    SELECT id, airfield_id, flarm_id, registration, competition_sign,
           aircraft_model, aircraft_type, is_active, created_at
    FROM tenant_aircraft WHERE airfield_id = $1 ORDER BY registration
"""

AIRCRAFT_BY_FLARM_ID = """
    SELECT id, airfield_id, flarm_id, registration, competition_sign,
           aircraft_model, aircraft_type, is_active, created_at
    FROM tenant_aircraft WHERE airfield_id = $1 AND flarm_id = $2
"""

AIRCRAFT_UPDATE = """
    UPDATE tenant_aircraft SET
        registration = $3, competition_sign = $4, aircraft_model = $5,
        aircraft_type = $6
    WHERE airfield_id = $1 AND flarm_id = $2
    RETURNING id, airfield_id, flarm_id, registration, competition_sign, aircraft_model, aircraft_type, is_active, created_at
"""

AIRCRAFT_DELETE = """
    DELETE FROM tenant_aircraft WHERE airfield_id = $1 AND flarm_id = $2
"""

AIRCRAFT_UPSERT = """
    INSERT INTO tenant_aircraft (airfield_id, flarm_id, registration, competition_sign, aircraft_model, aircraft_type)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (airfield_id, flarm_id) DO UPDATE SET
        registration = EXCLUDED.registration,
        competition_sign = EXCLUDED.competition_sign,
        aircraft_model = EXCLUDED.aircraft_model,
        aircraft_type = EXCLUDED.aircraft_type
    RETURNING id, flarm_id, registration
"""
