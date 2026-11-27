"""Where each check's trace began (ADR 0013).

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A W3C traceparent. Null when tracing was off at submit.
    op.execute("ALTER TABLE checks ADD COLUMN traceparent text")


def downgrade() -> None:
    op.execute("ALTER TABLE checks DROP COLUMN traceparent")
