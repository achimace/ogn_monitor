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

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
    }


settings = Settings()
