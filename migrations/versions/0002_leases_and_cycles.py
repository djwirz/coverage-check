"""Who holds a lease, and which cycle a check is on (ADR 0010).

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # lease_id fences writes: only the claim that holds it may finish the check.
    op.execute("""
        ALTER TABLE checks
            ADD COLUMN lease_id uuid,
            ADD COLUMN cycle integer NOT NULL DEFAULT 0,
            ADD CONSTRAINT lease_id_iff_leased
                CHECK ((state = 'leased') = (lease_id IS NOT NULL))
    """)
    op.execute("ALTER TABLE attempts ADD COLUMN cycle integer NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE attempts DROP COLUMN cycle")
    op.execute("""
        ALTER TABLE checks
            DROP CONSTRAINT lease_id_iff_leased,
            DROP COLUMN lease_id,
            DROP COLUMN cycle
    """)
