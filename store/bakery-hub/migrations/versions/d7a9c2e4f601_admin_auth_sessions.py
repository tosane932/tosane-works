"""Add durable, per-login Admin revocation records without modifying business rows."""
from alembic import op
import sqlalchemy as sa

revision = "d7a9c2e4f601"
down_revision = "c8d3e7f1a204"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(token_hash) = 64", name="ck_admin_auth_session_hash"),
        sa.CheckConstraint("expires_at > issued_at", name="ck_admin_auth_session_expiry"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= issued_at",
                           name="ck_admin_auth_session_revocation"),
    )
    op.create_index("ix_admin_auth_sessions_expiry", "admin_auth_sessions", ["expires_at"])


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM admin_auth_sessions LIMIT 1")).first():
        raise RuntimeError("Refusing to discard Admin session revocation records")
    op.drop_index("ix_admin_auth_sessions_expiry", table_name="admin_auth_sessions")
    op.drop_table("admin_auth_sessions")
