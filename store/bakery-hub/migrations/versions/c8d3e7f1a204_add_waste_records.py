"""Record actual production and disposal separately from prep plans and sales.

Revision ID: c8d3e7f1a204
Revises: b6d2e9f4a107
"""

from alembic import op
import sqlalchemy as sa


revision = "c8d3e7f1a204"
down_revision = "b6d2e9f4a107"
branch_labels = None
depends_on = None


def upgrade():
    # The legacy Product PK is global, but the composite FK below also guards
    # the Dataset boundary at database level. Batch mode supports SQLite tests.
    with op.batch_alter_table("products") as batch:
        batch.create_unique_constraint("uq_products_id_dataset_waste", ["id", "dataset_id"])
    op.create_table(
        "waste_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("register_product_id", sa.Integer(), nullable=True),
        sa.Column("legacy_product_id", sa.Integer(), nullable=True),
        sa.Column("product_label", sa.String(160), nullable=False),
        sa.Column("made_quantity", sa.Integer(), nullable=True),
        sa.Column("discarded_quantity", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(500), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["register_product_id", "dataset_id"],
                                ["register_products.id", "register_products.dataset_id"],
                                name="fk_waste_register_product_dataset", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["legacy_product_id", "dataset_id"],
                                ["products.id", "products.dataset_id"],
                                name="fk_waste_legacy_product_dataset", ondelete="RESTRICT"),
        sa.UniqueConstraint("dataset_id", "business_date", "register_product_id",
                            name="uq_waste_register_day"),
        sa.UniqueConstraint("dataset_id", "business_date", "legacy_product_id",
                            name="uq_waste_legacy_day"),
        sa.CheckConstraint("(register_product_id IS NULL) <> (legacy_product_id IS NULL)",
                           name="ck_waste_one_product"),
        sa.CheckConstraint("made_quantity IS NOT NULL OR discarded_quantity IS NOT NULL",
                           name="ck_waste_has_quantity"),
        sa.CheckConstraint("made_quantity IS NULL OR made_quantity BETWEEN 0 AND 100000",
                           name="ck_waste_made_quantity"),
        sa.CheckConstraint("discarded_quantity IS NULL OR discarded_quantity BETWEEN 0 AND 100000",
                           name="ck_waste_discarded_quantity"),
        sa.CheckConstraint("length(note) <= 500", name="ck_waste_note_length"),
    )
    op.create_index("ix_waste_dataset_day", "waste_records", ["dataset_id", "business_date"])


def downgrade():
    # Deliberately removes saved waste history. Never use as a production rollback.
    op.drop_index("ix_waste_dataset_day", table_name="waste_records")
    op.drop_table("waste_records")
    with op.batch_alter_table("products") as batch:
        batch.drop_constraint("uq_products_id_dataset_waste", type_="unique")
