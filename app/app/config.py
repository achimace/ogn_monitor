"""Application configuration via environment variables."""

from pydantic_settings import BaseSettings


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

    # Per-aircraft track stream (track:{slug}:{flarm_id}) for the monitor map
    track_min_interval_s: int = 5  # Thinning: min. beacon-time gap between stored points
    track_retention_s: int = 86400  # 24h sliding TTL of the track stream

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
