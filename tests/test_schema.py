"""The migration, and what the database refuses whatever the code does."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from tests.conftest import migrate


def test_migration_goes_up_down_and_up(postgres_url: str) -> None:
    name = "migration_check"
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS migration_check")
        conn.execute("CREATE DATABASE migration_check")
    url = postgres_url.rsplit("/", 1)[0] + f"/{name}"

    def tables() -> set[str]:
        with psycopg.connect(url) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            ).fetchall()
        return {r[0] for r in rows} - {"alembic_version"}

    expected = {"batches", "checks", "attempts", "limiter_buckets", "limiter_slots"}
    migrate(url)
    assert tables() == expected
    migrate(url, "base")
    assert tables() == set()
    migrate(url)
    assert tables() == expected


@pytest.fixture
def check_id(db: psycopg.Connection[Any]) -> str:
    batch_id, check_id = uuid4(), uuid4()
    db.execute(
        "INSERT INTO batches (id, idempotency_key, request_sha256) VALUES (%s, 'k', '')",
        (batch_id,),
    )
    db.execute(
        "INSERT INTO checks (id, batch_id, position, member_id, given, family, birth_date,"
        " payer, service_date) VALUES (%s, %s, 0, 'm', '{A}', 'B', '1990-01-01', 'P',"
        " '2026-11-01')",
        (check_id, batch_id),
    )
    return str(check_id)


@pytest.mark.parametrize(
    ("assignment", "constraint"),
    [
        ("state = 'done', result = '{}'", "outcome_iff_done"),
        ("state = 'done', outcome = 'covered'", "done_has_result"),
        ("outcome = 'covered'", "outcome_iff_done"),
        ("result = '{}'", "result_only_when_final"),
        ("state = 'exception'", "reason_iff_exception"),
        ("exception_reason = 'x'", "reason_iff_exception"),
        ("state = 'leased', lease_id = gen_random_uuid()", "lease_iff_leased"),
        ("lease_expires_at = now()", "lease_iff_leased"),
        ("state = 'leased', lease_expires_at = now()", "lease_id_iff_leased"),
        ("lease_id = gen_random_uuid()", "lease_id_iff_leased"),
        ("state = 'finished'", "checks_state_check"),
    ],
)
def test_a_check_cannot_be_half_finished(
    db: psycopg.Connection[Any], check_id: str, assignment: str, constraint: str
) -> None:
    query = psycopg.sql.SQL("UPDATE checks SET {} WHERE id = %s").format(
        psycopg.sql.SQL(assignment)
    )
    with pytest.raises(psycopg.errors.CheckViolation) as error:
        db.execute(query, (check_id,))
    assert error.value.diag.constraint_name == constraint


def test_attempt_numbers_do_not_repeat(db: psycopg.Connection[Any], check_id: str) -> None:
    insert = "INSERT INTO attempts (id, check_id, number) VALUES (%s, %s, 1)"
    db.execute(insert, (uuid4(), check_id))
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(insert, (uuid4(), check_id))
