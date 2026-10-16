"""Add optional creation keys for idempotent register product form submissions.

Revision ID: a4e8c2f6d901
Revises: f3b7e5a9c102
"""

from alembic import op
import sqlalchemy as sa


revision = "a4e8c2f6d901"
down_revision = "f3b7e5a9c102"
branch_labels = None
depends_on = None


def upgrade():
    # Nullable: existing products and the standard 18 require no backfill.
    op.add_column("register_products", sa.Column("creation_key", sa.Uuid(as_uuid=True)))
    op.add_column("register_products", sa.Column("creation_digest", sa.String(64)))
    op.create_index("ux_register_products_dataset_creation_key", "register_products",
                    ["dataset_id", "creation_key"], unique=True)


def downgrade():
    connection = op.get_bind()
    # SQLite DDL is not fully transactional. Refuse *before* dropping columns
    # when a downgrade through the preceding register revision would discard data.
    for table in ("register_products", "register_cutovers", "register_transactions",
                  "register_transaction_lines"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"Refusing to downgrade: {table} contains register data")
    op.drop_index("ux_register_products_dataset_creation_key", table_name="register_products")
    op.drop_column("register_products", "creation_digest")
    op.drop_column("register_products", "creation_key")
