"""Add optional product image key.

Revision ID: c4e8a1d7b2f6
Revises: f8c1d2e3a4b5
"""

from alembic import op
import sqlalchemy as sa


revision = "c4e8a1d7b2f6"
down_revision = "f8c1d2e3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("products", sa.Column("image_key", sa.String(length=100), nullable=True))


def downgrade():
    with op.batch_alter_table("products") as batch_op:
        batch_op.drop_column("image_key")
