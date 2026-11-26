"""Workers against real Postgres and a real mock payer."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx2
import psycopg
import pytest
import synthetic_cohort
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from coverage_check import limiter, queue, report, worker
from coverage_check.batches import submit_batch
from coverage_check.config import WorkerSettings
from coverage_check.fhir import CoverageEligibilityResponse
from coverage_check.mock_payer.profile import Profile
from coverage_check.payer_client import PayerClient
from coverage_check.policy import GiveUp
from coverage_check.seed import sample_batch
from tests.payer import profile, serving

NOWHERE = "http://127.0.0.1:9"  # nothing listens here


def settings(database_url: str, payer_url: str = NOWHERE, **change: Any) -> WorkerSettings:
    """Fast: a high limit, short waits and short leases."""
    values: dict[str, Any] = {
        "database_url": database_url,
        "payer_url": payer_url,
        "payer_rate_limit": 500,
        "payer_max_in_flight": 100,
        "rate_per_second": 200,
        "grant_max_age_seconds": 5,
        "attempt_timeout_seconds": 5,
        "slot_seconds": 6,
        "lease_seconds": 7,
        "backoff_base_seconds": 0.02,
        "backoff_cap_seconds": 0.2,
        "idle_seconds": 0.05,
    }
    return WorkerSettings(**{**values, **change})


def payer_profile(**change: Any) -> Profile:
    return profile(
        **{
            "rate_limit_per_second": 500,
            "max_in_flight": 100,
            "retry_after_seconds": 0,
            **change,
        }
    )


@pytest.fixture
def payer_db(tmp_path: Path) -> Path:
    return tmp_path / "payer.sqlite"


@pytest.fixture
def rows(database_url: str) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        yield conn


def submit(database_url: str, count: int | None = None) -> UUID:
    async def go() -> UUID:
        async with await psycopg.AsyncConnection.connect(database_url) as conn:
            batch_id, _ = await submit_batch(conn, "key", sample_batch(count))
            return batch_id

    return asyncio.run(go())


def work(config: WorkerSettings, workers: int = 1, timeout: float = 40) -> None:
    """Run workers until no check is queued or leased."""

    async def go() -> None:
        stop = asyncio.Event()
        running = [asyncio.create_task(worker.run(config, stop)) for _ in range(workers)]
        async with await psycopg.AsyncConnection.connect(
            config.database_url, autocommit=True
        ) as conn:
            async with asyncio.timeout(timeout):
                while True:
                    cursor = await conn.execute(
                        "SELECT count(*) FROM checks WHERE state IN ('queued', 'leased')"
                    )
                    row = await cursor.fetchone()
                    if row == (0,):
                        break
                    await asyncio.sleep(0.05)
        stop.set()
        await asyncio.gather(*running)

    asyncio.run(go())


def payer_requests(url: str) -> list[dict[str, Any]]:
    with httpx2.Client(base_url=url) as client:
        return report.payer_log(client)


def passed(config: WorkerSettings, batch_id: UUID) -> dict[int, bool]:
    findings = report.build(config.database_url, config.payer_url, batch_id, config.max_attempts)
    print(report.render(findings))
    return {f.invariant: f.passed for f in findings if f.invariant}


def test_a_batch_ends_with_the_answer_the_cohort_expects(
    database_url: str, payer_db: Path, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    batch_id = submit(database_url)
    with serving(payer_profile(), payer_db) as url:
        config = settings(database_url, url)
        work(config)
        assert passed(config, batch_id) == {n: True for n in range(1, 7)}
    expected = {
        "covered": ("done", "covered", None),
        "inactive": ("done", "not_covered", None),
        "missing": ("exception", None, "member_not_found"),
        "ambiguous": ("exception", None, "multiple_matches"),
    }
    by_member = {m["member_id"]: m["coverage_state"] for m in synthetic_cohort.members()}
    checks = rows.execute("SELECT * FROM checks").fetchall()
    for check in checks:
        got = (check["state"], check["outcome"], check["exception_reason"])
        assert got == expected[by_member[check["member_id"]]]
        CoverageEligibilityResponse.model_validate(check["result"])
    assert len(checks) == 35
    assert rows.execute("SELECT count(*) AS n FROM limiter_slots").fetchone() == {"n": 0}


def test_two_workers_on_a_bad_day_keep_the_invariants(
    database_url: str, payer_db: Path, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    batch_id = submit(database_url, 150)
    bad = payer_profile(
        throttle_rate=0.1,
        server_error_rate=0.1,
        malformed_rate=0.05,
        late_rate=0.03,
        late_seconds=0.3,
    )
    with serving(bad, payer_db) as url:
        config = settings(database_url, url)
        work(config, workers=2)
        assert passed(config, batch_id) == {n: True for n in range(1, 7)}
        seen = Counter(e["behaviour"] for e in payer_requests(url))
    assert {"throttle", "server_error", "malformed", "late"} <= set(seen)
    assert rows.execute("SELECT count(*) AS n FROM checks WHERE state = 'done'").fetchone() != {
        "n": 0
    }


@pytest.mark.parametrize(
    ("change", "reason", "calls"),
    [
        ({"server_error_rate": 1}, "payer_unavailable", 3),
        ({"throttle_rate": 1}, "payer_unavailable", 3),
        ({"malformed_rate": 1}, "malformed_response", 2),
    ],
)
def test_retries_stop(
    database_url: str,
    payer_db: Path,
    rows: psycopg.Connection[dict[str, Any]],
    change: dict[str, float],
    reason: str,
    calls: int,
) -> None:
    batch_id = submit(database_url, 1)
    with serving(payer_profile(**change), payer_db) as url:
        config = settings(database_url, url, max_attempts=3)
        work(config)
        assert passed(config, batch_id)[5]
        log = payer_requests(url)
    assert len(log) == calls
    assert len({e["idempotency_key"] for e in log}) == 1
    check = rows.execute("SELECT * FROM checks").fetchone()
    assert check is not None
    assert (check["state"], check["exception_reason"]) == ("exception", reason)
    assert check["result"]["error"][0]["code"]["coding"][0]["code"] == reason
    bodies = [
        r["response_body"]
        for r in rows.execute("SELECT response_body FROM attempts ORDER BY number").fetchall()
    ]
    assert all((b is not None) == (reason == "malformed_response") for b in bodies)


def test_a_retry_waits_at_least_retry_after(database_url: str, payer_db: Path) -> None:
    submit(database_url, 1)
    with serving(payer_profile(throttle_rate=1, retry_after_seconds=1), payer_db) as url:
        work(settings(database_url, url, max_attempts=2))
        first, second = payer_requests(url)
    assert second["received_at"] - first["replied_at"] >= 0.99


def test_a_payer_that_is_down_ends_in_exceptions(
    database_url: str, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    submit(database_url, 3)
    work(settings(database_url, max_attempts=2))
    assert rows.execute(
        "SELECT state, exception_reason, count(*) AS n FROM checks GROUP BY 1, 2"
    ).fetchall() == [{"state": "exception", "exception_reason": "payer_unavailable", "n": 3}]
    assert rows.execute("SELECT count(*) AS n FROM attempts").fetchone() == {"n": 6}
    # Nothing was sent, so nothing may still be in flight.
    assert rows.execute("SELECT count(*) AS n FROM limiter_slots").fetchone() == {"n": 0}


def test_a_retry_after_a_timeout_is_not_billed_again(
    database_url: str, payer_db: Path, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    submit(database_url, 1)
    with serving(payer_profile(late_rate=1, late_seconds=1), payer_db) as url:
        work(
            settings(
                database_url,
                url,
                attempt_timeout_seconds=0.4,
                max_attempts=20,
                backoff_base_seconds=0.2,
                backoff_cap_seconds=0.4,
            )
        )
        log = payer_requests(url)
    assert log[0]["behaviour"] == "late"
    assert log[-1]["behaviour"] == "replay"
    assert {e["behaviour"] for e in log[1:-1]} <= {"in_progress"}
    assert sum(e["billable"] for e in log) == 1
    assert rows.execute("SELECT state, outcome FROM checks").fetchone() == {
        "state": "done",
        "outcome": "covered",
    }
    outcomes = rows.execute("SELECT outcome_class FROM attempts ORDER BY number").fetchall()
    assert outcomes[0] == {"outcome_class": "timeout"}


def test_a_timed_out_call_keeps_its_slot(
    database_url: str, payer_db: Path, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    submit(database_url, 1)
    with serving(payer_profile(late_rate=1, late_seconds=1), payer_db) as url:
        config = settings(database_url, url, attempt_timeout_seconds=0.2)

        async def one_attempt() -> None:
            payer = PayerClient(url, config.attempt_timeout_seconds)
            async with AsyncConnectionPool(database_url, open=False) as pool:
                async with pool.connection() as conn:
                    await limiter.configure(conn, config)
                await asyncio.sleep(0.05)
                async with pool.connection() as conn:
                    claimed = await queue.claim(conn, config)
                assert isinstance(claimed, queue.Claim)
                await worker.handle(pool, payer, config, claimed)
            await payer.close()

        asyncio.run(one_attempt())
    assert rows.execute("SELECT count(*) AS n FROM limiter_slots").fetchone() == {"n": 1}
    assert rows.execute("SELECT state FROM checks").fetchone() == {"state": "queued"}


def test_a_stale_grant_is_not_sent(
    database_url: str, payer_db: Path, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    submit(database_url, 1)
    with serving(payer_profile(), payer_db) as url:
        config = settings(database_url, url, max_attempts=1)

        async def one_attempt() -> None:
            payer = PayerClient(url, config.attempt_timeout_seconds)
            async with AsyncConnectionPool(database_url, open=False) as pool:
                async with pool.connection() as conn:
                    await limiter.configure(conn, config)
                await asyncio.sleep(0.05)
                async with pool.connection() as conn:
                    claimed = await queue.claim(conn, config)
                assert isinstance(claimed, queue.Claim)
                stale = replace(claimed, granted_at=claimed.granted_at - 60)
                await worker.handle(pool, payer, config, stale)
            await payer.close()

        asyncio.run(one_attempt())
        assert payer_requests(url) == []
        # It didn't use up the only attempt.
        work(config)
    assert rows.execute("SELECT state FROM checks").fetchone() == {"state": "done"}
    assert [
        r["outcome_class"]
        for r in rows.execute("SELECT outcome_class FROM attempts ORDER BY number").fetchall()
    ] == ["not_sent", "answered"]


async def claim_when_ready(
    conn: psycopg.AsyncConnection[Any], config: WorkerSettings
) -> queue.Claim | queue.Exhausted:
    async with asyncio.timeout(10):
        while True:
            claimed = await queue.claim(conn, config)
            if isinstance(claimed, queue.Claim | queue.Exhausted):
                return claimed
            await asyncio.sleep(0.01)


def expire(rows: psycopg.Connection[dict[str, Any]]) -> None:
    """As if the lease and the slot had run out."""
    rows.execute(
        "UPDATE checks SET lease_expires_at = now() - interval '1 second' WHERE state = 'leased'"
    )
    rows.execute("UPDATE limiter_slots SET expires_at = now() - interval '1 second'")


def test_a_dead_workers_check_is_claimed_again_and_its_late_reply_changes_nothing(
    database_url: str, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    submit(database_url, 1)
    config = settings(database_url)

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(database_url) as conn:
            await limiter.configure(conn, config)
            first = await claim_when_ready(conn, config)
            assert isinstance(first, queue.Claim)
            assert isinstance(await queue.claim(conn, config), queue.Idle | queue.Wait)
            expire(rows)
            second = await claim_when_ready(conn, config)
            assert isinstance(second, queue.Claim)
            assert second.check_id == first.check_id
            assert second.key == first.key
            assert second.attempt_id != first.attempt_id
            assert second.attempts == 2

            # The first worker comes back to life while the second holds the lease.
            late = await queue.finish(
                conn, first, GiveUp("member_not_found"), attempt_class="member_not_found"
            )
            assert late is False
            held = await queue.finish(
                conn, second, GiveUp("multiple_matches"), attempt_class="multiple_matches"
            )
            assert held is True

    asyncio.run(go())
    assert rows.execute("SELECT state, exception_reason FROM checks").fetchone() == {
        "state": "exception",
        "exception_reason": "multiple_matches",
    }
    assert [
        r["outcome_class"]
        for r in rows.execute("SELECT outcome_class FROM attempts ORDER BY number").fetchall()
    ] == ["late:member_not_found", "multiple_matches"]


def test_a_check_that_keeps_killing_its_worker_becomes_an_exception(
    database_url: str, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    submit(database_url, 1)
    config = settings(database_url, max_attempts=3)

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(database_url) as conn:
            await limiter.configure(conn, config)
            for _ in range(3):
                assert isinstance(await claim_when_ready(conn, config), queue.Claim)
                expire(rows)
            assert isinstance(await claim_when_ready(conn, config), queue.Exhausted)

    asyncio.run(go())
    check = rows.execute("SELECT * FROM checks").fetchone()
    assert check is not None
    assert (check["state"], check["exception_reason"]) == ("exception", "payer_unavailable")
    CoverageEligibilityResponse.model_validate(check["result"])
    assert rows.execute("SELECT count(*) AS n FROM attempts").fetchone() == {"n": 3}


def test_many_claimers_at_once_stay_under_the_cap_and_never_share_a_check(
    database_url: str,
) -> None:
    submit(database_url, 100)
    config = settings(database_url, max_in_flight=5)

    async def one() -> list[queue.Claim]:
        got = []
        async with await psycopg.AsyncConnection.connect(database_url) as conn:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                claimed = await queue.claim(conn, config)
                if isinstance(claimed, queue.Claim):
                    got.append(claimed)
                await asyncio.sleep(0.005)
        return got

    async def go() -> list[queue.Claim]:
        async with await psycopg.AsyncConnection.connect(database_url) as conn:
            await limiter.configure(conn, config)
        return [c for got in await asyncio.gather(*(one() for _ in range(20))) for c in got]

    claims = asyncio.run(go())
    # Nothing is finished, so the cap is all that was ever handed out.
    assert len(claims) == 5
    assert len({c.check_id for c in claims}) == 5


def test_many_claimers_stay_under_the_rate(
    database_url: str, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    submit(database_url, 200)
    config = settings(database_url, rate_per_second=20, burst=1, max_in_flight=100)

    async def one(deadline: float) -> None:
        async with await psycopg.AsyncConnection.connect(database_url) as conn:
            while time.monotonic() < deadline:
                await queue.claim(conn, config)
                await asyncio.sleep(0.002)

    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(database_url) as conn:
            await limiter.configure(conn, config)
        deadline = time.monotonic() + 2.5
        await asyncio.gather(*(one(deadline) for _ in range(20)))

    asyncio.run(go())
    granted = [
        r["started_at"].timestamp()
        for r in rows.execute("SELECT started_at FROM attempts").fetchall()
    ]
    assert report.peak_per_second(granted) <= 21  # burst + rate
    assert len(granted) >= 35  # and it does hand them out: about 50 in 2.5 s


def test_the_report_fails_when_the_limit_was_broken(database_url: str, payer_db: Path) -> None:
    """An engine told the wrong limit: the payer's own log has to catch it."""
    batch_id = submit(database_url, 30)
    with serving(payer_profile(rate_limit_per_second=5), payer_db) as url:
        config = settings(database_url, url, max_attempts=50)
        work(config)
        findings = passed(config, batch_id)
    assert findings[1] is False
    assert findings[2] is True
