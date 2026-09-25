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
    # reload: every raster subtile within this radius is fetched with one
    # query (10 km ~ 20-40 subtiles of 64x64 px). The query has its own
    # timeout; a timeout/error aborts the current warm-up (retried at the
    # next config reload) but never pauses the per-beacon lookups.
    terrain_warm_radius_km: int = 10
    terrain_warm_timeout_s: float = 20
    # Per-beacon lookup (cache miss = fetch of the subtiles around the
    # position) timeout; on timeout the beacon uses the airfield elevation
    # and lookups pause for terrain_db_backoff_s.
    terrain_lookup_timeout_s: float = 1.5
    # Upper bound of the in-memory elevation cache in raster subtiles
    # (64x64 px float32 = 16 KB each; a subtile covers ~6 km x 4 km at
    # 47N). When full, the oldest half is dropped. 4000 tiles ~ 65 MB
    # worst case and cover ~95,000 km^2, i.e. far more than the warm-up
    # areas (10 km radius ~ 30 tiles per airfield) plus the tracks of the
    # day; raise it only for many airfields with long cross-country flights.
    terrain_cache_max_tiles: int = 4000
    # After a DB error the lookup falls back to the airfield elevation for
    # this long before trying the database again (no error storm per beacon).
    terrain_db_backoff_s: int = 60
    # Import tool: tile source and default radius around each airfield
    terrain_dem_base_url: str = "https://copernicus-dem-90m.s3.amazonaws.com"
    terrain_import_radius_km: int = 60

    # Foreign airfields / visitors (table airports, migration 011; import:
    # python -m app.tools.import_airports). See
    # docs/dev-guides/implement-flight-logic.md "Fremde Flugplaetze / Besucher".
    # Radius around every active airfield whose airports the worker keeps in
    # memory (AirportIndex); also the import tool's default radius.
    airports_index_radius_km: int = 300
    airports_csv_url: str = "https://davidmegginson.github.io/ourairports-data/airports.csv"
    # An aircraft slow and low within this distance of a known airport is
    # "at that airport": landing there is a normal landing (landing_type
    # 'foreign'), not an outlanding; ground contact there is remembered so
    # a later takeoff yields takeoff_airfield / takeoff_time for visitors.
    foreign_airfield_radius_m: int = 2000
    # Aircraft airborne within this distance of home that did not start
    # there are tracked as visitors (is_visitor) so their landing at home
    # is visible; visitors leaving the zone again are dropped silently.
    visitor_zone_km: int = 15
    # Visitors are only picked up below this height above the airfield
    # (cross-country traffic passing high overhead is not a visitor).
    visitor_max_agl_m: int = 1500
    # Upper bound of the per-airfield foreign ground / departure /
    # visitor-candidate dicts (oldest entries are evicted).
    foreign_ground_max_entries: int = 5000

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
