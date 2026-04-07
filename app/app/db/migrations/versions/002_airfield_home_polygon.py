"""Add optional home polygon to airfields.

Allows defining the takeoff/landing area as an arbitrary polygon instead of
just a circle around the airfield centre. The circle (home_radius_m) remains
as fallback when home_polygon IS NULL.

Revision ID: 002
Revises: 001
Create Date: 2026-04-07
"""
from typing import Sequence, Union

from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "SELECT AddGeometryColumn('airfields', 'home_polygon', 4326, 'POLYGON', 2)"
    )
    op.execute(
        "CREATE INDEX idx_airfields_home_polygon "
        "ON airfields USING GIST(home_polygon)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_airfields_home_polygon")
    op.execute("ALTER TABLE airfields DROP COLUMN IF EXISTS home_polygon")
