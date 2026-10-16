"""Record prices observed after deployment; do not backfill historical prices.

Revision ID: da5c1e7f9042
Revises: c9d3e7a1b5f2
"""

from alembic import op
import sqlalchemy as sa

revision = "da5c1e7f9042"
down_revision = "c9d3e7a1b5f2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "register_price_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("register_product_id", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(16), nullable=False),
        sa.Column("old_price_yen", sa.Integer(), nullable=True),
        sa.Column("new_price_yen", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["register_product_id", "dataset_id"],
                                ["register_products.id", "register_products.dataset_id"],
                                name="fk_register_price_history_product_dataset", ondelete="RESTRICT"),
        sa.CheckConstraint("event IN ('created', 'changed')", name="ck_register_price_history_event"),
        sa.CheckConstraint("old_price_yen IS NULL OR old_price_yen BETWEEN 0 AND 500", name="ck_register_price_history_old_price"),
        sa.CheckConstraint("new_price_yen BETWEEN 0 AND 500", name="ck_register_price_history_new_price"),
        sa.CheckConstraint("(event = 'created' AND old_price_yen IS NULL) OR "
                           "(event = 'changed' AND old_price_yen IS NOT NULL AND old_price_yen <> new_price_yen)",
                           name="ck_register_price_history_transition"),
    )
    op.create_index("ix_register_price_history_dataset_product_time",
                    "register_price_history", ["dataset_id", "register_product_id", "recorded_at", "id"])
    connection = op.get_bind()
    op.create_table(
        "register_cutover_approvals",
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("first_register_date", sa.Date(), nullable=False),
        sa.Column("approved_by", sa.String(100), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("device_inventory", sa.String(1000), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["register_cutovers.dataset_id"],
                                ondelete="RESTRICT"),
    )
    if connection.dialect.name == "postgresql":
        op.execute("CREATE FUNCTION prevent_register_audit_change() RETURNS trigger "
                   "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'register audit is append only'; END; $$")
        for table in ("register_price_history", "register_cutover_approvals"):
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE "
                       f"ON {table} FOR EACH ROW EXECUTE FUNCTION prevent_register_audit_change()")
    elif connection.dialect.name == "sqlite":
        for table in ("register_price_history", "register_cutover_approvals"):
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} "
                           f"ON {table} BEGIN SELECT RAISE(ABORT, 'register audit is append only'); END")


def downgrade():
    # SQLite DDLの部分適用を避けるため、後続revisionが拒否する既存レジ表も先に検査。
    for table in ("register_price_history", "register_cutover_approvals",
                  "register_prep_links", "register_transaction_lines",
                  "register_transactions", "register_cutovers", "register_products"):
        if op.get_bind().execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"Refusing to downgrade: {table} contains register data")
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        for table in ("register_price_history", "register_cutover_approvals"):
            op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.execute("DROP FUNCTION prevent_register_audit_change()")
    elif dialect == "sqlite":
        for table in ("register_price_history", "register_cutover_approvals"):
            for action in ("UPDATE", "DELETE"):
                op.execute(f"DROP TRIGGER {table}_no_{action.lower()}")
    op.drop_index("ix_register_price_history_dataset_product_time", table_name="register_price_history")
    op.drop_table("register_price_history")
    op.drop_table("register_cutover_approvals")
