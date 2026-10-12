"""Add Dataset-scoped store schedules.

Revision ID: d9e4b1c7a630
Revises: a6b9c2d4e8f0
"""
from alembic import op
import sqlalchemy as sa

revision = "d9e4b1c7a630"
down_revision = "a6b9c2d4e8f0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shop_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("datasets.id", name="fk_shop_schedules_dataset_id_datasets", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(trim(title)) >= 1", name="ck_shop_schedules_title_nonblank"),
        sa.CheckConstraint("length(title) <= 100", name="ck_shop_schedules_title_max_length"),
        sa.CheckConstraint("note IS NULL OR length(note) <= 1000", name="ck_shop_schedules_note_max_length"),
        sa.CheckConstraint("ends_at IS NULL OR ends_at > starts_at", name="ck_shop_schedules_end_after_start"),
    )
    op.create_index("ix_shop_schedules_dataset_starts_id", "shop_schedules", ["dataset_id", "starts_at", "id"])


def downgrade():
    op.drop_index("ix_shop_schedules_dataset_starts_id", table_name="shop_schedules")
    op.drop_table("shop_schedules")
