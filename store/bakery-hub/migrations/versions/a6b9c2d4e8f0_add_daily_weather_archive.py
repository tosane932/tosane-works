"""add daily weather archive

Revision ID: a6b9c2d4e8f0
Revises: c4e8a1d7b2f6
"""
from alembic import op
import sqlalchemy as sa

revision = "a6b9c2d4e8f0"
down_revision = "c4e8a1d7b2f6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "daily_weather",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("weather_code", sa.Integer(), nullable=False),
        sa.Column("precipitation_mm", sa.Float(), nullable=True),
        sa.Column("max_temperature_c", sa.Float(), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("dataset_id", "date", name="uq_daily_weather_dataset_date"),
        sa.CheckConstraint("weather_code BETWEEN 0 AND 99", name="ck_daily_weather_code"),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="ck_daily_weather_latitude"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="ck_daily_weather_longitude"),
        sa.CheckConstraint("precipitation_mm IS NULL OR precipitation_mm >= 0", name="ck_daily_weather_precipitation"),
    )
    op.create_index("ix_daily_weather_dataset_id", "daily_weather", ["dataset_id"])


def downgrade():
    op.drop_index("ix_daily_weather_dataset_id", table_name="daily_weather")
    op.drop_table("daily_weather")
