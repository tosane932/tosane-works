"""Bounded offline device grants and the distinct offline sale method.

Revision ID: a3c9d7e5f102
Revises: f5a17b3e2c09
"""
from alembic import op
import sqlalchemy as sa

revision = "a3c9d7e5f102"
down_revision = "f5a17b3e2c09"
branch_labels = None
depends_on = None

FIELDS = (
    "(save_method = 'normal' AND recovery_case_id IS NULL AND client_completed_at IS NULL "
    "AND recovery_evidence_digest IS NULL AND offline_permit_id IS NULL) OR "
    "(save_method = 'recovery' AND recovery_case_id IS NOT NULL AND client_completed_at IS NOT NULL "
    "AND recovery_evidence_digest IS NOT NULL AND length(recovery_evidence_digest) = 64 "
    "AND offline_permit_id IS NULL) OR "
    "(save_method = 'offline' AND recovery_case_id IS NULL AND client_completed_at IS NOT NULL "
    "AND recovery_evidence_digest IS NULL AND offline_permit_id IS NOT NULL)"
)


def upgrade():
    op.create_table(
        "register_offline_permits",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("approved_by", sa.String(100), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("next_business_date", sa.Date(), nullable=True),
        sa.Column("catalog_digest", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("id", "dataset_id", name="uq_register_offline_permit_id_dataset"),
        sa.CheckConstraint("length(device_id) = 36", name="ck_register_offline_device"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_register_offline_window"),
    )
    op.create_index("ix_register_offline_permit_device", "register_offline_permits",
                    ["dataset_id", "device_id"])
    with op.batch_alter_table("register_transactions") as batch:
        batch.add_column(sa.Column("offline_permit_id", sa.Uuid(as_uuid=True), nullable=True))
        batch.create_foreign_key("fk_register_transactions_offline_permit_dataset",
                                 "register_offline_permits", ["offline_permit_id", "dataset_id"],
                                 ["id", "dataset_id"], ondelete="RESTRICT")
        batch.drop_constraint("ck_register_transactions_save_method", type_="check")
        batch.drop_constraint("ck_register_transactions_recovery_fields", type_="check")
        batch.create_check_constraint("ck_register_transactions_save_method",
                                      "save_method IN ('normal','recovery','offline')")
        batch.create_check_constraint("ck_register_transactions_recovery_fields", FIELDS)


def downgrade():
    bind = op.get_bind()
    # SQLite batch DDL rebuilds tables outside one reliable DDL transaction.
    # Preflight downstream downgrade guards before changing any schema.
    for table in ("register_offline_permits", "register_recovery_events",
                  "register_recovery_lines", "register_recovery_cases", "register_price_history",
                  "register_prep_links", "register_transaction_lines", "register_transactions",
                  "register_cutovers", "register_products", "register_cutover_approvals"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"Refusing to discard register evidence: {table}")
    with op.batch_alter_table("register_transactions") as batch:
        batch.drop_constraint("fk_register_transactions_offline_permit_dataset", type_="foreignkey")
        batch.drop_constraint("ck_register_transactions_save_method", type_="check")
        batch.drop_constraint("ck_register_transactions_recovery_fields", type_="check")
        batch.create_check_constraint("ck_register_transactions_save_method",
                                      "save_method IN ('normal','recovery')")
        batch.create_check_constraint("ck_register_transactions_recovery_fields",
            "(save_method = 'normal' AND recovery_case_id IS NULL AND client_completed_at IS NULL "
            "AND recovery_evidence_digest IS NULL) OR "
            "(save_method = 'recovery' AND recovery_case_id IS NOT NULL AND client_completed_at IS NOT NULL "
            "AND recovery_evidence_digest IS NOT NULL AND length(recovery_evidence_digest) = 64)")
        batch.drop_column("offline_permit_id")
    op.drop_index("ix_register_offline_permit_device", table_name="register_offline_permits")
    op.drop_table("register_offline_permits")
