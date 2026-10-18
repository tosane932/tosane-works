"""Unify product metadata and widen all register price constraints without rewriting sales.

Revision ID: b6d2e9f4a107
Revises: a3c9d7e5f102
"""
from alembic import op
import sqlalchemy as sa

revision = "b6d2e9f4a107"
down_revision = "a3c9d7e5f102"
branch_labels = None
depends_on = None

PRICE_CHECKS = {
    "register_products": [("ck_register_products_price", "price_yen", True, False)],
    "register_price_history": [
        ("ck_register_price_history_old_price", "old_price_yen", False, True),
        ("ck_register_price_history_new_price", "new_price_yen", False, False)],
    "register_transaction_lines": [("ck_register_lines_unit_price", "unit_price_yen", True, False)],
    "register_recovery_lines": [("ck_register_recovery_line_price", "unit_price_yen", False, False)],
}


def _prices(maximum):
    for table, checks in PRICE_CHECKS.items():
        with op.batch_alter_table(table) as batch:
            for name, column, integer, nullable in checks:
                batch.drop_constraint(name, type_="check")
                rule = f"{column} BETWEEN 0 AND {maximum}"
                if integer:
                    rule += f" AND {column} = CAST({column} AS INTEGER)"
                if nullable:
                    rule = f"{column} IS NULL OR ({rule})"
                batch.create_check_constraint(name, rule)
        # SQLite batch rebuild removes table triggers. Restore the append-only guards.
        if op.get_bind().dialect.name == "sqlite" and table in {
                "register_price_history", "register_recovery_lines"}:
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'register audit is append only'); END")


def upgrade():
    with op.batch_alter_table("register_products") as batch:
        batch.add_column(sa.Column("bread_kind", sa.String(16), nullable=False, server_default="other"))
        batch.add_column(sa.Column("product_class", sa.String(16), nullable=False, server_default="regular"))
        batch.add_column(sa.Column("photo_webp", sa.LargeBinary(), nullable=True))
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="0"))
        batch.create_check_constraint("ck_register_products_bread_kind",
            "bread_kind IN ('savory','sweet','loaf','hard','sandwich','fried','baked','other')")
        batch.create_check_constraint("ck_register_products_class", "product_class IN ('regular','seasonal','special')")
        batch.create_check_constraint("ck_register_products_photo",
            "photo_webp IS NULL OR (length(photo_webp) BETWEEN 1 AND 512000 AND image_key IS NULL)")
        batch.create_check_constraint("ck_register_products_revision", "revision >= 0")
    _prices(100000)


def downgrade():
    bind = op.get_bind()
    # Inspect everything before any DDL, including SQLite's non-transactional rebuilds.
    if bind.execute(sa.text("SELECT 1 FROM register_products WHERE bread_kind <> 'other' "
            "OR product_class <> 'regular' OR photo_webp IS NOT NULL OR revision <> 0 LIMIT 1")).first():
        raise RuntimeError("Refusing to discard unified product metadata/photo/revision")
    for table, checks in PRICE_CHECKS.items():
        for _, column, _, _ in checks:
            if bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {column} > 500 LIMIT 1")).first():
                raise RuntimeError(f"Refusing to narrow recorded prices: {table}.{column}")
    # Earlier revisions also refuse populated register tables. Check before any
    # DDL here so a multi-revision downgrade cannot partially remove this schema.
    for table in sa.inspect(bind).get_table_names():
        if table.startswith("register_") and bind.execute(sa.text(f'SELECT 1 FROM "{table}" LIMIT 1')).first():
            raise RuntimeError(f"Refusing to downgrade populated register evidence: {table}")
    _prices(500)
    with op.batch_alter_table("register_products") as batch:
        for name in ("bread_kind", "class", "photo", "revision"):
            batch.drop_constraint(f"ck_register_products_{name}", type_="check")
        for name in ("revision", "photo_webp", "product_class", "bread_kind"):
            batch.drop_column(name)
