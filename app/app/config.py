"""Application configuration via environment variables."""

from pydantic import field_validator
from pydantic_settings import BaseSettings

# OGN data usage policy (https://www.glidernet.org/ogn-data-usage/):
# "You do not re-distribute OGN data older than 24 hours". The per-aircraft
# track stream is served to browsers via the API, so its retention may
# never exceed this bound.
OGN_MAX_REDISTRIBUTION_AGE_S = 24 * 3600


class Settings(BaseSettings):
    """Central configuration - all values from environment variables / .env file."""

    # Database
    database_url: str = "postgresql+asyncpg://ogn_monitor:ogn_monitor@localhost:5432/ogn_monitor"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # JWT Auth
    jwt_secret: str = "CHANGE-ME-IN-PRODUCTION"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24  # 24 hours

    # OGN / APRS
    ogn_callsign: str = "OGNMON"
    ogn_server: str = "aprs.glidernet.org"
    ogn_port: int = 14580
    ogn_default_radius_km: int = 500

    # SMTP (optional, for email verification)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""

    # Application
    base_url: str = "http://localhost"
    log_level: str = "info"

    # Redis Hot State
    redis_hot_state_ttl: int = 86400  # 24h TTL for flight keys
    redis_sync_interval: int = 30  # Seconds between Redis -> PG sync

    # Flight detection thresholds (defaults, overridable per airfield)
    home_radius_m: int = 800
    takeoff_speed_threshold_kmh: int = 40
    takeoff_altitude_offset_m: int = 50
    alarm_timeout_s: int = 600  # 10 minutes without beacon -> ALARM
    signal_loss_timeout_s: int = 300  # 5 minutes -> start concern

    # Takeoff confirmation: the raw ground speed must be at/above
    # takeoff_speed_kmh on at least this many consecutive beacons (the
    # declaring beacon included) before a takeoff / restart / touch & go is
    # declared. Defeats single GPS glitch beacons. The takeoff time stays
    # the first fast beacon; only the declaration waits. 1 = declare on the
    # first fast + high beacon (legacy behaviour).
    takeoff_min_fast_beacons: int = 2

    # Per-aircraft track stream (track:{slug}:{flarm_id}) for the monitor map
    track_min_interval_s: int = 5  # Thinning: min. beacon-time gap between stored points
    # 24h sliding TTL of the track stream. Hard upper bound: the OGN data
    # usage policy (https://www.glidernet.org/ogn-data-usage/) forbids
    # re-distributing OGN data older than 24 hours, and the API serves this
    # stream to browsers. See OGN_MAX_REDISTRIBUTION_AGE_S / the validator.
    track_retention_s: int = 86400

    @field_validator("track_retention_s")
    @classmethod
    def _track_retention_within_ogn_policy(cls, value: int) -> int:
        """Reject retention windows beyond the OGN 24 h redistribution limit."""
        if value > OGN_MAX_REDISTRIBUTION_AGE_S:
            raise ValueError(
                f"track_retention_s={value} exceeds {OGN_MAX_REDISTRIBUTION_AGE_S} s: "
                "the OGN data usage policy (glidernet.org/ogn-data-usage) forbids "
                "re-distributing OGN data older than 24 hours"
            )
        return value

    # Terrain model (Copernicus GLO-90 DEM in elevation_tiles, migration 009).
    # Height above ground of a flight (altitude_agl, outlanding detection)
    # is computed against the terrain under the aircraft instead of the
    # airfield elevation. Kill switch: False = airfield elevation everywhere
    # (legacy behaviour). Decisions tied to the home runway (takeoff,
    # landing band, touch & go) always use the airfield elevation.
    terrain_agl_enabled: bool = True
    # Cache warm-up around every active airfield at worker start / config
    # reload: radius and grid step (metres). The step should not exceed the
    # ~100 m cache cell, otherwise every second cell still misses.
    # 10 km @ 100 m ~ 31k cells per airfield.
    terrain_warm_radius_km: int = 10
    terrain_warm_step_m: int = 100
    # Warm-up query batch size (points per SQL statement) and the timeout
    # per batch. A timeout/error aborts the current warm-up (retried at the
    # next config reload) but never pauses the per-beacon lookups.
    terrain_warm_batch: int = 1000
    terrain_warm_timeout_s: float = 20
    # Per-beacon lookup (cache miss) timeout; on timeout the beacon uses the
    # airfield elevation and lookups pause for terrain_db_backoff_s.
    terrain_lookup_timeout_s: float = 1.5
    # Upper bound of the in-memory elevation cache (~100 m cells); when
    # full, the oldest half is dropped. Sizing: one airfield warm-up needs
    # about pi * (radius_m / step_m)^2 cells, i.e. 10 km @ 100 m ~ 31k grid
    # points ~ 28k distinct cache cells (100 m steps partly collapse into
    # the 111 m latitude cells). 250k thus holds ~8 airfields plus the
    # per-beacon misses; raise it when more airfields are active or
    # terrain_warm_radius_km grows, otherwise the warm-up of the last
    # airfields evicts the first ones.
    terrain_cache_max_entries: int = 250_000
    # After a DB error the lookup falls back to the airfield elevation for
    # this long before trying the database again (no error storm per beacon).
    terrain_db_backoff_s: int = 60
    # Import tool: tile source and default radius around each airfield
    terrain_dem_base_url: str = "https://copernicus-dem-90m.s3.amazonaws.com"
    terrain_import_radius_km: int = 60

    # VF-Sync worker (python -m app.vfsync), see docs/konzept-vf-sync.md Kap. 7
    vfsync_enabled: bool = False
    vfsync_cred_key: str = ""            # Fernet key (base64) for vf_sync_config credentials
    vfsync_health_port: int = 8090
    vfsync_alert_ntfy_url: str = ""
    vfsync_alert_email_to: str = ""      # uses the SMTP settings above
    vfsync_timezone: str = "Europe/Berlin"
    vfsync_config_reload_s: int = 300
    vfsync_list_cache_s: int = 300       # flight/list/today cache per tenant
    vfsync_health_interval_s: int = 30   # vfsync:health Redis hash refresh
    vfsync_pubsub_reconnect_s: int = 5   # delay before re-subscribing after a Redis error
    # vf_base_url policy (Kap. 8.4): https only, hosts from this list.
    # Insecure http:// is only for the mock in test profiles.
    vfsync_allowed_hosts: str = "www.vereinsflieger.de"
    vfsync_allow_insecure_base_url: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


settings = Settings()
