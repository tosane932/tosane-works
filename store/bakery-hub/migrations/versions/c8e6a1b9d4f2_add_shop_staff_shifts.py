"""Add Admin staff directory and planned shifts without changing existing data.

Revision ID: c8e6a1b9d4f2
Revises: b7f2a1c9d603
"""
from alembic import op
import sqlalchemy as sa

revision = "c8e6a1b9d4f2"
down_revision = "b7f2a1c9d603"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shop_staff",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("datasets.id", name="fk_shop_staff_dataset", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.String(80), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("hired_on", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("submission_key", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("id", "dataset_id", name="uq_shop_staff_id_dataset"),
        sa.UniqueConstraint("dataset_id", "submission_key", name="uq_shop_staff_submission"),
        sa.CheckConstraint("length(trim(display_name)) BETWEEN 1 AND 80", name="ck_shop_staff_name"),
        sa.CheckConstraint("role IN ('production', 'sales', 'both', 'other')", name="ck_shop_staff_role"),
    )
    op.create_index("ix_shop_staff_dataset_active_id", "shop_staff", ["dataset_id", "is_active", "id"])
    op.create_table(
        "shop_shifts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), sa.ForeignKey("datasets.id", name="fk_shop_shifts_dataset", ondelete="CASCADE"), nullable=False),
        sa.Column("staff_id", sa.Integer(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["staff_id", "dataset_id"], ["shop_staff.id", "shop_staff.dataset_id"], name="fk_shop_shifts_staff_dataset", ondelete="RESTRICT"),
        sa.UniqueConstraint("dataset_id", "staff_id", "business_date", name="uq_shop_shifts_staff_day"),
        sa.CheckConstraint("status IN ('work', 'off')", name="ck_shop_shifts_status"),
        sa.CheckConstraint(
            "(status = 'work' AND starts_at IS NOT NULL AND ends_at IS NOT NULL AND ends_at > starts_at) "
            "OR (status = 'off' AND starts_at IS NULL AND ends_at IS NULL)",
            name="ck_shop_shifts_times_by_status",
        ),
        sa.CheckConstraint("note IS NULL OR length(note) <= 500", name="ck_shop_shifts_note"),
    )
    op.create_index("ix_shop_shifts_dataset_day_id", "shop_shifts", ["dataset_id", "business_date", "id"])


def downgrade():
    op.drop_index("ix_shop_shifts_dataset_day_id", table_name="shop_shifts")
    op.drop_table("shop_shifts")
    op.drop_index("ix_shop_staff_dataset_active_id", table_name="shop_staff")
    op.drop_table("shop_staff")
