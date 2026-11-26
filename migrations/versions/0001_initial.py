"""Batches, checks, attempts and the limiter (ADRs 0004 and 0005).

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE batches (
            id uuid PRIMARY KEY,
            idempotency_key text NOT NULL UNIQUE,
            request_sha256 bytea NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    # The constraints hold part of invariants 2 and 3 whatever the code does: a finished check
    # says how it finished, and a leased check always has a lease that can expire.
    op.execute("""
        CREATE TABLE checks (
            id uuid PRIMARY KEY,
            batch_id uuid NOT NULL REFERENCES batches (id),
            position integer NOT NULL CHECK (position >= 0),
            member_id text NOT NULL,
            given text[] NOT NULL,
            family text NOT NULL,
            birth_date date NOT NULL,
            payer text NOT NULL,
            service_date date NOT NULL,
            state text NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued', 'leased', 'done', 'exception')),
            outcome text CHECK (outcome IN ('covered', 'not_covered')),
            exception_reason text,
            result jsonb,
            not_before timestamptz NOT NULL DEFAULT now(),
            lease_expires_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (batch_id, position),
            CONSTRAINT outcome_iff_done CHECK ((state = 'done') = (outcome IS NOT NULL)),
            CONSTRAINT done_has_result CHECK (state <> 'done' OR result IS NOT NULL),
            CONSTRAINT result_only_when_final
                CHECK (result IS NULL OR state IN ('done', 'exception')),
            CONSTRAINT reason_iff_exception
                CHECK ((state = 'exception') = (exception_reason IS NOT NULL)),
            CONSTRAINT lease_iff_leased
                CHECK ((state = 'leased') = (lease_expires_at IS NOT NULL))
        )
    """)
    op.execute("CREATE INDEX checks_queued ON checks (not_before) WHERE state = 'queued'")
    op.execute("CREATE INDEX checks_leased ON checks (lease_expires_at) WHERE state = 'leased'")
    op.execute("CREATE INDEX checks_exception ON checks (updated_at) WHERE state = 'exception'")
    op.execute("""
        CREATE TABLE attempts (
            id uuid PRIMARY KEY,
            check_id uuid NOT NULL REFERENCES checks (id),
            number integer NOT NULL CHECK (number > 0),
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            outcome_class text,
            http_status integer,
            response_body text,
            UNIQUE (check_id, number)
        )
    """)
    # Keyed by the payer API (the account whose limit it is), not by a member's insurer.
    op.execute("""
        CREATE TABLE limiter_buckets (
            payer_api text PRIMARY KEY,
            rate_per_second numeric NOT NULL CHECK (rate_per_second > 0),
            burst integer NOT NULL CHECK (burst > 0),
            tokens numeric NOT NULL CHECK (tokens >= 0),
            refilled_at timestamptz NOT NULL DEFAULT now(),
            max_in_flight integer NOT NULL CHECK (max_in_flight > 0)
        )
    """)
    op.execute("""
        CREATE TABLE limiter_slots (
            id uuid PRIMARY KEY,
            payer_api text NOT NULL REFERENCES limiter_buckets (payer_api),
            expires_at timestamptz NOT NULL
        )
    """)
    op.execute("CREATE INDEX limiter_slots_expiry ON limiter_slots (payer_api, expires_at)")


def downgrade() -> None:
    op.execute("DROP TABLE limiter_slots, limiter_buckets, attempts, checks, batches")
