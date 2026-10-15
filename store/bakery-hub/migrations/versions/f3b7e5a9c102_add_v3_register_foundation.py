"""Add the four independent v3 register tables without migrating legacy sales.

Revision ID: f3b7e5a9c102
Revises: e2a7c4d9b105
"""

from alembic import op
import sqlalchemy as sa


revision = "f3b7e5a9c102"
down_revision = "e2a7c4d9b105"
branch_labels = None
depends_on = None


REGISTER_TABLES = (
    "register_products",
    "register_cutovers",
    "register_transactions",
    "register_transaction_lines",
)


def upgrade():
    op.create_table(
        "register_products",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("template_key", sa.String(100), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("price_yen", sa.Integer(), nullable=False),
        sa.Column("image_key", sa.String(100), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("display_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], name="fk_register_products_dataset", ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "dataset_id", name="uq_register_products_id_dataset"),
        sa.UniqueConstraint("dataset_id", "template_key", name="uq_register_products_dataset_template"),
        sa.CheckConstraint("length(trim(name)) BETWEEN 1 AND 100", name="ck_register_products_name"),
        sa.CheckConstraint("template_key IS NULL OR length(trim(template_key)) BETWEEN 1 AND 100", name="ck_register_products_template_key"),
        sa.CheckConstraint("image_key IS NULL OR length(trim(image_key)) BETWEEN 1 AND 100", name="ck_register_products_image_key"),
        sa.CheckConstraint("price_yen BETWEEN 0 AND 500 AND price_yen = CAST(price_yen AS INTEGER)", name="ck_register_products_price"),
        sa.CheckConstraint("is_active IN (true, false)", name="ck_register_products_active"),
        sa.CheckConstraint("display_order >= 0 AND display_order = CAST(display_order AS INTEGER)", name="ck_register_products_display_order"),
    )
    op.create_index("ix_register_products_dataset_active_order", "register_products", ["dataset_id", "is_active", "display_order", "id"])

    op.create_table(
        "register_cutovers",
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("first_register_date", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], name="fk_register_cutovers_dataset", ondelete="RESTRICT"),
    )

    op.create_table(
        "register_transactions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("submission_key", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("total_yen", sa.BigInteger(), nullable=False),
        sa.Column("received_yen", sa.BigInteger(), nullable=True),
        sa.Column("change_yen", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], name="fk_register_transactions_dataset", ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "dataset_id", name="uq_register_transactions_id_dataset"),
        sa.UniqueConstraint("dataset_id", "submission_key", name="uq_register_transactions_submission"),
        sa.CheckConstraint("length(request_digest) = 64", name="ck_register_transactions_digest"),
        sa.CheckConstraint("kind IN ('sale', 'refund')", name="ck_register_transactions_kind"),
        sa.CheckConstraint("total_yen BETWEEN -1000000000 AND 1000000000 AND total_yen = CAST(total_yen AS INTEGER)", name="ck_register_transactions_total"),
        sa.CheckConstraint(
            "(kind = 'sale' AND total_yen >= 0 AND received_yen IS NOT NULL "
            "AND change_yen IS NOT NULL AND received_yen BETWEEN 0 AND 1000000000 "
            "AND change_yen BETWEEN 0 AND 1000000000 "
            "AND received_yen = CAST(received_yen AS INTEGER) "
            "AND change_yen = CAST(change_yen AS INTEGER) "
            "AND received_yen >= total_yen AND change_yen = received_yen - total_yen) "
            "OR (kind = 'refund' AND total_yen <= 0 "
            "AND received_yen IS NULL AND change_yen IS NULL)",
            name="ck_register_transactions_settlement",
        ),
    )
    op.create_index("ix_register_transactions_dataset_day_completed", "register_transactions", ["dataset_id", "business_date", "completed_at"])

    op.create_table(
        "register_transaction_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("transaction_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("register_product_id", sa.Integer(), nullable=False),
        sa.Column("product_name_snapshot", sa.String(100), nullable=False),
        sa.Column("unit_price_yen", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("line_total_yen", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["transaction_id", "dataset_id"], ["register_transactions.id", "register_transactions.dataset_id"], name="fk_register_lines_transaction_dataset", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["register_product_id", "dataset_id"], ["register_products.id", "register_products.dataset_id"], name="fk_register_lines_product_dataset", ondelete="RESTRICT"),
        sa.UniqueConstraint("transaction_id", "register_product_id", name="uq_register_lines_transaction_product"),
        sa.CheckConstraint("length(trim(product_name_snapshot)) BETWEEN 1 AND 100", name="ck_register_lines_name"),
        sa.CheckConstraint("quantity BETWEEN -50 AND 50 AND quantity <> 0 AND quantity = CAST(quantity AS INTEGER)", name="ck_register_lines_quantity"),
        sa.CheckConstraint("unit_price_yen BETWEEN 0 AND 500 AND unit_price_yen = CAST(unit_price_yen AS INTEGER)", name="ck_register_lines_unit_price"),
        sa.CheckConstraint("line_total_yen = unit_price_yen * quantity", name="ck_register_lines_total"),
    )
    op.create_index("ix_register_lines_dataset_product", "register_transaction_lines", ["dataset_id", "register_product_id"])


def downgrade():
    # Check *all* tables before dropping any, including products without sales.
    # A populated v3 schema must never be destroyed by a routine downgrade.
    connection = op.get_bind()
    for table_name in REGISTER_TABLES:
        if connection.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).first():
            raise RuntimeError(
                f"Refusing to downgrade: {table_name} contains register data"
            )

    op.drop_index("ix_register_lines_dataset_product", table_name="register_transaction_lines")
    op.drop_table("register_transaction_lines")
    op.drop_index("ix_register_transactions_dataset_day_completed", table_name="register_transactions")
    op.drop_table("register_transactions")
    op.drop_table("register_cutovers")
    op.drop_index("ix_register_products_dataset_active_order", table_name="register_products")
    op.drop_table("register_products")
