"""Add explicit one-to-one Admin register product to prep recipe links.

Revision ID: c9d3e7a1b5f2
Revises: a4e8c2f6d901
"""

from alembic import op
import sqlalchemy as sa


revision = "c9d3e7a1b5f2"
down_revision = "a4e8c2f6d901"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "register_prep_links",
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recipe_id", sa.Integer(), nullable=False),
        sa.Column("register_product_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("dataset_id", "recipe_id"),
        sa.UniqueConstraint("dataset_id", "register_product_id",
                            name="uq_register_prep_links_dataset_product"),
        sa.ForeignKeyConstraint(["register_product_id", "dataset_id"],
                                ["register_products.id", "register_products.dataset_id"],
                                name="fk_register_prep_links_product_dataset", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recipe_id", "dataset_id"],
                                ["prep_recipes.id", "prep_recipes.dataset_id"],
                                name="fk_register_prep_links_recipe_dataset", ondelete="RESTRICT"),
    )


def downgrade():
    # SQLiteのDDLはトランザクションで巻き戻せないため、後続の既存migrationが
    # 中断する前にすべてのレジデータを検査する。
    for table in ("register_prep_links", "register_transaction_lines",
                  "register_transactions", "register_cutovers", "register_products"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"Refusing to downgrade: {table} contains data")
    op.drop_table("register_prep_links")
