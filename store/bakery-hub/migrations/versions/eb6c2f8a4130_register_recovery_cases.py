"""Durable register recovery cases and append-only review evidence.

Revision ID: eb6c2f8a4130
Revises: da5c1e7f9042
"""
from alembic import op
import sqlalchemy as sa

revision = "eb6c2f8a4130"
down_revision = "da5c1e7f9042"
branch_labels = None
depends_on = None

STATUSES = "'unreviewed','investigating','held','saved','cancelled','additional'"


def upgrade():
    op.add_column("register_cutover_approvals", sa.Column(
        "review_decisions", sa.Text(), nullable=False, server_default="{}"))
    op.create_table(
        "register_recovery_cases",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("submission_key", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("source_digest", sa.String(64), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("client_business_date", sa.Date(), nullable=False),
        sa.Column("total_yen", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "dataset_id", name="uq_register_recovery_id_dataset"),
        sa.UniqueConstraint("dataset_id", "submission_key", name="uq_register_recovery_submission"),
        sa.CheckConstraint("length(source_digest) = 64", name="ck_register_recovery_digest"),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="ck_register_recovery_status"),
        sa.CheckConstraint("kind IN ('sale','refund')", name="ck_register_recovery_kind"),
        sa.CheckConstraint("total_yen BETWEEN -1000000000 AND 1000000000", name="ck_register_recovery_total"),
    )
    op.create_table(
        "register_recovery_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("register_product_id", sa.Integer(), nullable=False),
        sa.Column("product_name_snapshot", sa.String(100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_yen", sa.Integer(), nullable=False),
        sa.Column("line_total_yen", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["case_id", "dataset_id"],
                                ["register_recovery_cases.id", "register_recovery_cases.dataset_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["register_product_id", "dataset_id"],
                                ["register_products.id", "register_products.dataset_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("case_id", "register_product_id", name="uq_register_recovery_line_product"),
        sa.CheckConstraint("quantity BETWEEN -50 AND 50 AND quantity <> 0", name="ck_register_recovery_line_qty"),
        sa.CheckConstraint("unit_price_yen BETWEEN 0 AND 500", name="ck_register_recovery_line_price"),
        sa.CheckConstraint("line_total_yen = quantity * unit_price_yen", name="ck_register_recovery_line_total"),
    )
    op.create_table(
        "register_recovery_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("server_result", sa.String(24), nullable=False),
        sa.Column("price_conflict", sa.Boolean(), nullable=False),
        sa.Column("day_conflict", sa.Boolean(), nullable=False),
        sa.Column("cash_result", sa.String(24), nullable=False),
        sa.Column("evidence_kind", sa.String(100), nullable=False),
        sa.Column("evidence_reference", sa.String(500), nullable=False),
        sa.ForeignKeyConstraint(["case_id", "dataset_id"],
                                ["register_recovery_cases.id", "register_recovery_cases.dataset_id"], ondelete="RESTRICT"),
        sa.CheckConstraint(f"to_status IN ({STATUSES})", name="ck_register_recovery_event_status"),
    )
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        for table in ("register_recovery_lines", "register_recovery_events"):
            op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION prevent_register_audit_change()")
        op.execute("CREATE TRIGGER register_recovery_cases_no_delete BEFORE DELETE ON register_recovery_cases "
                   "FOR EACH ROW EXECUTE FUNCTION prevent_register_audit_change()")
        op.execute("CREATE FUNCTION prevent_register_recovery_snapshot_change() RETURNS trigger "
                   "LANGUAGE plpgsql AS $$ BEGIN "
                   "IF ROW(NEW.id, NEW.dataset_id, NEW.submission_key, NEW.source_digest, NEW.snapshot_json, "
                   "NEW.kind, NEW.client_business_date, NEW.total_yen, NEW.received_at) IS DISTINCT FROM "
                   "ROW(OLD.id, OLD.dataset_id, OLD.submission_key, OLD.source_digest, OLD.snapshot_json, "
                   "OLD.kind, OLD.client_business_date, OLD.total_yen, OLD.received_at) THEN "
                   "RAISE EXCEPTION 'recovery snapshot is immutable'; END IF; RETURN NEW; END; $$")
        op.execute("CREATE TRIGGER register_recovery_cases_snapshot BEFORE UPDATE ON register_recovery_cases "
                   "FOR EACH ROW EXECUTE FUNCTION prevent_register_recovery_snapshot_change()")
    elif connection.dialect.name == "sqlite":
        for table in ("register_recovery_lines", "register_recovery_events"):
            for action in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                           "BEGIN SELECT RAISE(ABORT, 'register audit is append only'); END")
        op.execute("CREATE TRIGGER register_recovery_cases_no_delete BEFORE DELETE ON register_recovery_cases "
                   "BEGIN SELECT RAISE(ABORT, 'recovery case is permanent'); END")
        columns = ("id", "dataset_id", "submission_key", "source_digest", "snapshot_json",
                   "kind", "client_business_date", "total_yen", "received_at")
        condition = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in columns)
        op.execute(f"CREATE TRIGGER register_recovery_cases_snapshot BEFORE UPDATE ON register_recovery_cases "
                   f"WHEN {condition} BEGIN SELECT RAISE(ABORT, 'recovery snapshot is immutable'); END")


def downgrade():
    connection = op.get_bind()
    # SQLiteのDDLは部分的に確定し得るため、下位revisionの拒否条件も最初に検査。
    for table in ("register_recovery_events", "register_recovery_lines", "register_recovery_cases",
                  "register_price_history", "register_prep_links", "register_transaction_lines",
                  "register_transactions", "register_cutovers", "register_products"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"Refusing to downgrade: {table} contains recovery evidence")
    if connection.execute(sa.text("SELECT 1 FROM register_cutover_approvals LIMIT 1")).first():
        raise RuntimeError("Refusing to downgrade: cutover approvals exist")
    if connection.dialect.name == "postgresql":
        for table in ("register_recovery_lines", "register_recovery_events"):
            op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
        op.execute("DROP TRIGGER register_recovery_cases_no_delete ON register_recovery_cases")
        op.execute("DROP TRIGGER register_recovery_cases_snapshot ON register_recovery_cases")
        op.execute("DROP FUNCTION prevent_register_recovery_snapshot_change()")
    op.drop_table("register_recovery_events")
    op.drop_table("register_recovery_lines")
    op.drop_table("register_recovery_cases")
    with op.batch_alter_table("register_cutover_approvals") as batch:
        batch.drop_column("review_decisions")
    if connection.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER register_cutover_approvals_no_{action.lower()} "
                       f"BEFORE {action} ON register_cutover_approvals "
                       "BEGIN SELECT RAISE(ABORT, 'register audit is append only'); END")
