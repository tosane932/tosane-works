"""Add Dataset-scoped daily forecast snapshots.

Revision ID: b7f2a1c9d603
Revises: d9e4b1c7a630
"""
from alembic import op
import sqlalchemy as sa

revision = "b7f2a1c9d603"
down_revision = "d9e4b1c7a630"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "daily_forecasts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("weather_code", sa.Integer(), nullable=False),
        sa.Column("forecast_valid_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("location_name", sa.String(80), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.UniqueConstraint("dataset_id", "business_date", name="uq_daily_forecasts_dataset_date"),
        sa.CheckConstraint("weather_code BETWEEN 0 AND 99", name="ck_daily_forecasts_code"),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_daily_forecasts_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_daily_forecasts_longitude"),
    )


def downgrade():
    op.drop_table("daily_forecasts")
