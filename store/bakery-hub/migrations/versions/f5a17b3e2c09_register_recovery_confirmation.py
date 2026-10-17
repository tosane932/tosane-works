"""Audit the exceptional recovery path without altering existing sales.

Revision ID: f5a17b3e2c09
Revises: eb6c2f8a4130
"""
from alembic import op
import sqlalchemy as sa

revision = "f5a17b3e2c09"
down_revision = "eb6c2f8a4130"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("register_transactions") as batch:
        batch.add_column(sa.Column("save_method", sa.String(16), nullable=False, server_default="normal"))
        batch.add_column(sa.Column("recovery_case_id", sa.Uuid(as_uuid=True), nullable=True))
        batch.add_column(sa.Column("recovery_evidence_digest", sa.String(64), nullable=True))
        batch.add_column(sa.Column("client_completed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_unique_constraint("uq_register_transactions_recovery_case", ["recovery_case_id"])
        batch.create_unique_constraint("uq_register_transactions_recovery_evidence", ["dataset_id", "recovery_evidence_digest"])
        batch.create_foreign_key("fk_register_transactions_recovery_dataset", "register_recovery_cases",
                                 ["recovery_case_id", "dataset_id"], ["id", "dataset_id"], ondelete="RESTRICT")
        batch.create_check_constraint("ck_register_transactions_save_method", "save_method IN ('normal','recovery')")
        batch.create_check_constraint("ck_register_transactions_recovery_fields",
            "(save_method = 'normal' AND recovery_case_id IS NULL AND client_completed_at IS NULL "
            "AND recovery_evidence_digest IS NULL) OR "
            "(save_method = 'recovery' AND recovery_case_id IS NOT NULL AND client_completed_at IS NOT NULL "
            "AND recovery_evidence_digest IS NOT NULL AND length(recovery_evidence_digest) = 64)")
    with op.batch_alter_table("register_recovery_events") as batch:
        batch.add_column(sa.Column("transaction_id", sa.Uuid(as_uuid=True), nullable=True))
        batch.add_column(sa.Column("adopted_business_date", sa.Date(), nullable=True))
        batch.create_foreign_key("fk_register_recovery_event_transaction_dataset", "register_transactions",
                                 ["transaction_id", "dataset_id"], ["id", "dataset_id"], ondelete="RESTRICT")
    if op.get_bind().dialect.name == "sqlite":
        # SQLite's batch DDL rebuilds the table, including its triggers.
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER register_recovery_events_no_{action.lower()} BEFORE {action} "
                       "ON register_recovery_events BEGIN SELECT RAISE(ABORT, 'register audit is append only'); END")


def downgrade():
    bind = op.get_bind()
    # SQLite DDL may commit each rebuild: inspect every downstream guard first.
    for table in ("register_recovery_events", "register_recovery_lines", "register_recovery_cases",
                  "register_price_history", "register_prep_links", "register_transaction_lines",
                  "register_transactions", "register_cutovers", "register_products",
                  "register_cutover_approvals"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"Refusing to discard register evidence: {table}")
    if bind.execute(sa.text("SELECT 1 FROM register_transactions WHERE save_method='recovery' LIMIT 1")).first():
        raise RuntimeError("Refusing to discard recovery transaction evidence")
    if bind.execute(sa.text("SELECT 1 FROM register_recovery_events WHERE transaction_id IS NOT NULL LIMIT 1")).first():
        raise RuntimeError("Refusing to discard recovery approval evidence")
    # The previous revision itself refuses downgrade when register data exists.
    with op.batch_alter_table("register_recovery_events") as batch:
        batch.drop_constraint("fk_register_recovery_event_transaction_dataset", type_="foreignkey")
        batch.drop_column("adopted_business_date")
        batch.drop_column("transaction_id")
    with op.batch_alter_table("register_transactions") as batch:
        batch.drop_constraint("fk_register_transactions_recovery_dataset", type_="foreignkey")
        batch.drop_constraint("uq_register_transactions_recovery_case", type_="unique")
        batch.drop_constraint("uq_register_transactions_recovery_evidence", type_="unique")
        batch.drop_constraint("ck_register_transactions_save_method", type_="check")
        batch.drop_constraint("ck_register_transactions_recovery_fields", type_="check")
        batch.drop_column("client_completed_at")
        batch.drop_column("recovery_case_id")
        batch.drop_column("recovery_evidence_digest")
        batch.drop_column("save_method")
    if bind.dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER register_recovery_events_no_{action.lower()} BEFORE {action} "
                       "ON register_recovery_events BEGIN SELECT RAISE(ABORT, 'register audit is append only'); END")
