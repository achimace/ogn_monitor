"""Initial schema - matches db/init.sql.

Revision ID: 001
Revises: None
Create Date: 2026-03-11
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute('CREATE EXTENSION IF NOT EXISTS postgis')
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')

    # Tenants
    op.create_table(
        "tenants",
        sa.Column("id", sa.dialects.postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(50), nullable=False, unique=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("email_verified", sa.Boolean(), server_default=sa.text("FALSE")),
        sa.Column("email_verification_token", sa.String(255)),
        sa.Column("disclaimer_accepted_at", sa.DateTime(timezone=True)),
        sa.Column("disclaimer_version", sa.Integer(), server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_tenants_slug", "tenants", ["slug"])
    op.create_index("idx_tenants_email", "tenants", ["email"])

    # Airfields
    op.create_table(
        "airfields",
        sa.Column("id", sa.dialects.postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), primary_key=True),
        sa.Column("tenant_id", sa.dialects.postgresql.UUID(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(50), nullable=False),
        sa.Column("icao_code", sa.String(4)),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("elevation_m", sa.Integer(), nullable=False),
        sa.Column("home_radius_m", sa.Integer(), server_default=sa.text("800")),
        sa.Column("ogn_filter_radius_km", sa.Integer(), server_default=sa.text("500")),
        sa.Column("alarm_timeout_s", sa.Integer(), server_default=sa.text("600")),
        sa.Column("signal_loss_timeout_s", sa.Integer(), server_default=sa.text("300")),
        sa.Column("takeoff_speed_kmh", sa.Integer(), server_default=sa.text("40")),
        sa.Column("takeoff_alt_offset_m", sa.Integer(), server_default=sa.text("50")),
        sa.Column("tow_plane_flarm_ids", sa.dialects.postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]")),
        sa.Column("winch_vs_threshold_ms", sa.Float(), server_default=sa.text("8.0")),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("TRUE")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("tenant_id", "slug"),
    )
    op.create_index("idx_airfields_tenant", "airfields", ["tenant_id"])
    op.execute("CREATE INDEX idx_airfields_active ON airfields(is_active) WHERE is_active = TRUE")
    op.execute("SELECT AddGeometryColumn('airfields', 'geom', 4326, 'POINT', 2)")
    op.execute("CREATE INDEX idx_airfields_geom ON airfields USING GIST(geom)")
    op.execute("""
        CREATE OR REPLACE FUNCTION update_airfield_geom()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.geom = ST_SetSRID(ST_MakePoint(NEW.longitude, NEW.latitude), 4326);
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_airfield_geom
            BEFORE INSERT OR UPDATE OF latitude, longitude ON airfields
            FOR EACH ROW EXECUTE FUNCTION update_airfield_geom();
    """)

    # Tenant Aircraft
    op.create_table(
        "tenant_aircraft",
        sa.Column("id", sa.dialects.postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), primary_key=True),
        sa.Column("airfield_id", sa.dialects.postgresql.UUID(), sa.ForeignKey("airfields.id", ondelete="CASCADE"), nullable=False),
        sa.Column("flarm_id", sa.String(16), nullable=False),
        sa.Column("registration", sa.String(16), nullable=False),
        sa.Column("competition_sign", sa.String(4)),
        sa.Column("aircraft_model", sa.String(64)),
        sa.Column("aircraft_type", sa.String(32)),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("TRUE")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("airfield_id", "flarm_id"),
    )
    op.create_index("idx_tenant_aircraft_airfield", "tenant_aircraft", ["airfield_id"])
    op.create_index("idx_tenant_aircraft_flarm", "tenant_aircraft", ["flarm_id"])

    # Aircraft Registry
    op.create_table(
        "aircraft_registry",
        sa.Column("device_id", sa.String(16), primary_key=True),
        sa.Column("device_type", sa.String(1)),
        sa.Column("registration", sa.String(16)),
        sa.Column("competition_sign", sa.String(4)),
        sa.Column("aircraft_model", sa.String(64)),
        sa.Column("aircraft_type", sa.String(32)),
        sa.Column("tracked", sa.Boolean(), server_default=sa.text("TRUE")),
        sa.Column("identified", sa.Boolean(), server_default=sa.text("TRUE")),
        sa.Column("source", sa.String(16), server_default=sa.text("'ogn_ddb'")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_registry_registration", "aircraft_registry", ["registration"])

    # Flight Status
    op.create_table(
        "flight_status",
        sa.Column("id", sa.dialects.postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), primary_key=True),
        sa.Column("airfield_id", sa.dialects.postgresql.UUID(), sa.ForeignKey("airfields.id"), nullable=False),
        sa.Column("flarm_id", sa.String(16), nullable=False),
        sa.Column("registration", sa.String(16)),
        sa.Column("competition_sign", sa.String(4)),
        sa.Column("aircraft_model", sa.String(64)),
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("latitude", sa.Float()),
        sa.Column("longitude", sa.Float()),
        sa.Column("altitude_m", sa.Integer()),
        sa.Column("altitude_agl", sa.Integer()),
        sa.Column("speed_kmh", sa.Integer()),
        sa.Column("vertical_speed_ms", sa.Float()),
        sa.Column("track_deg", sa.Integer()),
        sa.Column("distance_m", sa.Integer()),
        sa.Column("qdr_deg", sa.Integer()),
        sa.Column("bearing_text", sa.String(4)),
        sa.Column("takeoff_time", sa.DateTime(timezone=True)),
        sa.Column("landing_time", sa.DateTime(timezone=True)),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("max_altitude_m", sa.Integer()),
        sa.Column("max_distance_m", sa.Integer()),
        sa.Column("launch_type", sa.String(16)),
        sa.Column("tow_plane_flarm_id", sa.String(16)),
        sa.Column("tow_plane_registration", sa.String(32)),
        sa.Column("release_altitude_m", sa.Integer()),
        sa.Column("release_time", sa.DateTime(timezone=True)),
        sa.Column("signal_loss_scenario", sa.String(16)),
        sa.Column("signal_loss_severity", sa.String(8)),
        sa.Column("signal_loss_flags", sa.dialects.postgresql.ARRAY(sa.Text())),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.UniqueConstraint("airfield_id", "flarm_id"),
    )
    op.create_index("idx_flight_status_airfield", "flight_status", ["airfield_id"])
    op.create_index("idx_flight_status_flarm", "flight_status", ["flarm_id"])
    op.execute("CREATE INDEX idx_flight_status_active ON flight_status(status) WHERE status BETWEEN 1 AND 10")

    # Flight Log
    op.create_table(
        "flight_log",
        sa.Column("id", sa.dialects.postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), primary_key=True),
        sa.Column("airfield_id", sa.dialects.postgresql.UUID(), sa.ForeignKey("airfields.id"), nullable=False),
        sa.Column("flarm_id", sa.String(16), nullable=False),
        sa.Column("registration", sa.String(16)),
        sa.Column("competition_sign", sa.String(4)),
        sa.Column("aircraft_model", sa.String(64)),
        sa.Column("takeoff_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("landing_time", sa.DateTime(timezone=True)),
        sa.Column("flight_duration_s", sa.Integer()),
        sa.Column("max_altitude_m", sa.Integer()),
        sa.Column("max_altitude_agl", sa.Integer()),
        sa.Column("max_distance_m", sa.Integer()),
        sa.Column("launch_type", sa.String(16)),
        sa.Column("tow_plane_flarm_id", sa.String(16)),
        sa.Column("tow_plane_registration", sa.String(32)),
        sa.Column("release_altitude_m", sa.Integer()),
        sa.Column("release_altitude_agl", sa.Integer()),
        sa.Column("release_time", sa.DateTime(timezone=True)),
        sa.Column("tow_duration_s", sa.Integer()),
        sa.Column("landing_type", sa.String(16)),
        sa.Column("landing_airfield", sa.String(100)),
        sa.Column("landing_latitude", sa.Float()),
        sa.Column("landing_longitude", sa.Float()),
        sa.Column("signal_loss_scenario", sa.String(16)),
        sa.Column("signal_loss_severity", sa.String(8)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_flight_log_airfield", "flight_log", ["airfield_id"])
    op.create_index("idx_flight_log_date", "flight_log", ["takeoff_time"])
    op.create_index("idx_flight_log_flarm", "flight_log", ["flarm_id"])
    op.create_index("idx_flight_log_registration", "flight_log", ["registration"])

    # Flight Profile Snapshot
    op.create_table(
        "flight_profile_snapshot",
        sa.Column("id", sa.dialects.postgresql.UUID(), server_default=sa.text("uuid_generate_v4()"), primary_key=True),
        sa.Column("flight_status_id", sa.dialects.postgresql.UUID(), sa.ForeignKey("flight_status.id")),
        sa.Column("flarm_id", sa.String(16), nullable=False),
        sa.Column("airfield_id", sa.dialects.postgresql.UUID(), sa.ForeignKey("airfields.id"), nullable=False),
        sa.Column("scenario", sa.String(16), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("abnormal_flags", sa.dialects.postgresql.ARRAY(sa.Text())),
        sa.Column("profile_data", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("analysis_result", sa.dialects.postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )
    op.create_index("idx_profile_snapshot_airfield", "flight_profile_snapshot", ["airfield_id"])
    op.create_index("idx_profile_snapshot_date", "flight_profile_snapshot", ["created_at"])


def downgrade() -> None:
    op.drop_table("flight_profile_snapshot")
    op.drop_table("flight_log")
    op.drop_table("flight_status")
    op.drop_table("aircraft_registry")
    op.drop_table("tenant_aircraft")
    op.execute("DROP TRIGGER IF EXISTS trg_airfield_geom ON airfields")
    op.execute("DROP FUNCTION IF EXISTS update_airfield_geom()")
    op.drop_table("airfields")
    op.drop_table("tenants")
