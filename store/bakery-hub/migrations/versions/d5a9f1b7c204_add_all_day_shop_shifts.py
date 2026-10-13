"""Distinguish untimed all-day plans from timed work and days off.

Revision ID: d5a9f1b7c204
Revises: c8e6a1b9d4f2
"""
from alembic import op
import sqlalchemy as sa

revision = "d5a9f1b7c204"
down_revision = "c8e6a1b9d4f2"
branch_labels = None
depends_on = None

OLD_TIMES = (
    "(status = 'work' AND starts_at IS NOT NULL AND ends_at IS NOT NULL AND ends_at > starts_at) "
    "OR (status = 'off' AND starts_at IS NULL AND ends_at IS NULL)"
)
NEW_TIMES = (
    "(status = 'work' AND starts_at IS NOT NULL AND ends_at IS NOT NULL AND ends_at > starts_at) "
    "OR (status IN ('all_day', 'off') AND starts_at IS NULL AND ends_at IS NULL)"
)


def _replace_constraints(status_check, time_check):
    # SQLite must copy the table to alter CHECKs; PostgreSQL alters constraints in place.
    with op.batch_alter_table("shop_shifts", recreate="always" if op.get_bind().dialect.name == "sqlite" else "auto") as batch:
        batch.drop_constraint("ck_shop_shifts_status", type_="check")
        batch.drop_constraint("ck_shop_shifts_times_by_status", type_="check")
        batch.create_check_constraint("ck_shop_shifts_status", status_check)
        batch.create_check_constraint("ck_shop_shifts_times_by_status", time_check)


def upgrade():
    _replace_constraints("status IN ('work', 'all_day', 'off')", NEW_TIMES)


def downgrade():
    count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM shop_shifts WHERE status = 'all_day'")
    ).scalar_one()
    if count:
        raise RuntimeError("終日予定が残っています。手動で扱いを決めてからdowngradeしてください。")
    _replace_constraints("status IN ('work', 'off')", OLD_TIMES)
