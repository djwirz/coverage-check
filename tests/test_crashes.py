"""Real worker processes, killed and stopped, ten at once, and Postgres restarted under them."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest
from docker.errors import APIError  # type: ignore[import-untyped]
from psycopg.rows import dict_row
from starlette.testclient import TestClient
from testcontainers.community.postgres import PostgresContainer

from coverage_check import report
from coverage_check.api import create_app
from coverage_check.config import Settings
from tests.conftest import TOKEN, migrate
from tests.payer import serving
from tests.test_workers import passed, payer_profile, payer_requests, settings, submit

COMMAND = str(Path(sys.executable).parent / "coverage-check")
ALL_PASS = {n: True for n in range(1, 7)}

# Short enough that a lease runs out within a test.
QUICK = {
    "payer_rate_limit": 500,
    "payer_max_in_flight": 100,
    "rate_per_second": 200,
    "grant_max_age_seconds": 5,
    "attempt_timeout_seconds": 3,
    "slot_seconds": 3.5,
    "lease_seconds": 4,
    "max_attempts": 20,
    "backoff_base_seconds": 0.1,
    "backoff_cap_seconds": 0.5,
    "idle_seconds": 0.05,
}


@pytest.fixture
def payer_db(tmp_path: Path) -> Path:
    return tmp_path / "payer.sqlite"


@pytest.fixture
def rows(database_url: str) -> Iterator[psycopg.Connection[dict[str, Any]]]:
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        yield conn


class Workers:
    def __init__(self, database_url: str, payer_url: str, logs: Path) -> None:
        self.env = {"DATABASE_URL": database_url, "WORKER_PAYER_URL": payer_url}
        self.logs = logs
        self.processes: list[subprocess.Popen[bytes]] = []

    def start(self, **change: Any) -> subprocess.Popen[bytes]:
        env = {f"WORKER_{name.upper()}": str(value) for name, value in change.items()}
        with (self.logs / f"worker-{len(self.processes)}.log").open("wb") as log:
            process = subprocess.Popen(  # noqa: S603 - our own command
                [COMMAND, "worker"],
                env={**os.environ, **self.env, **env},
                stdout=log,
                stderr=log,
            )
        self.processes.append(process)
        return process

    def output(self) -> str:
        return "".join(p.read_text() for p in sorted(self.logs.glob("worker-*.log")))


@contextmanager
def workers_for(database_url: str, payer_url: str, logs: Path) -> Iterator[Workers]:
    workers = Workers(database_url, payer_url, logs)
    try:
        yield workers
    finally:
        for process in workers.processes:
            process.kill()
            process.wait()
        print(workers.output()[-5000:])


def wait_for(true: Callable[[], bool], what: str, seconds: float = 40) -> None:
    deadline = time.monotonic() + seconds
    while not true():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def count(database_url: str, where: str) -> int:
    with psycopg.connect(database_url) as conn:
        query = psycopg.sql.SQL("SELECT count(*) FROM checks WHERE {}").format(
            psycopg.sql.SQL(where)
        )
        row = conn.execute(query).fetchone()
    assert row is not None
    return int(row[0])


def finished(database_url: str) -> bool:
    return count(database_url, "state IN ('queued', 'leased')") == 0


def attempt_classes(rows: psycopg.Connection[dict[str, Any]]) -> list[str | None]:
    found = rows.execute("SELECT outcome_class FROM attempts ORDER BY number").fetchall()
    return [r["outcome_class"] for r in found]


def check_report(database_url: str, payer_url: str, batch_id: UUID) -> None:
    config = settings(database_url, payer_url, max_attempts=20)
    assert passed(config, batch_id) == ALL_PASS


def test_a_killed_workers_check_is_picked_up_and_billed_once(
    database_url: str, payer_db: Path, tmp_path: Path, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    batch_id = submit(database_url, 1)
    with (
        serving(payer_profile(late_rate=1, late_seconds=1), payer_db) as url,
        workers_for(database_url, url, tmp_path) as workers,
    ):
        first = workers.start(**QUICK)
        wait_for(lambda: len(payer_requests(url)) == 1, "the call")
        first.send_signal(signal.SIGKILL)
        first.wait()
        assert count(database_url, "state = 'leased'") == 1

        workers.start(**QUICK)
        wait_for(lambda: finished(database_url), "the second worker")
        check_report(database_url, url, batch_id)
        log = payer_requests(url)
    assert rows.execute("SELECT outcome FROM checks").fetchone() == {"outcome": "covered"}
    assert attempt_classes(rows) == ["lease_expired", "answered"]
    assert sum(e["billable"] for e in log) == 1


def test_sigterm_lets_the_call_in_flight_finish(
    database_url: str, payer_db: Path, tmp_path: Path, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    submit(database_url, 1)
    with (
        serving(payer_profile(late_rate=1, late_seconds=1), payer_db) as url,
        workers_for(database_url, url, tmp_path) as workers,
    ):
        process = workers.start(**QUICK)
        wait_for(lambda: len(payer_requests(url)) == 1, "the call")
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        assert len(payer_requests(url)) == 1
        assert "worker_stopped" in workers.output()
    assert rows.execute("SELECT state FROM checks").fetchone() == {"state": "done"}


def test_a_worker_out_of_grace_hands_its_check_back(
    database_url: str, payer_db: Path, tmp_path: Path, rows: psycopg.Connection[dict[str, Any]]
) -> None:
    batch_id = submit(database_url, 1)
    with (
        serving(payer_profile(late_rate=1, late_seconds=2), payer_db) as url,
        workers_for(database_url, url, tmp_path) as workers,
    ):
        process = workers.start(**QUICK, shutdown_grace_seconds=0.2)
        wait_for(lambda: len(payer_requests(url)) == 1, "the call")
        stopped_at = time.monotonic()
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        assert time.monotonic() - stopped_at < 1.5, "it waited for the call after all"
        assert rows.execute("SELECT state FROM checks").fetchone() == {"state": "queued"}
        assert attempt_classes(rows) == ["released"]
        # The payer is still working on it.
        assert rows.execute("SELECT count(*) AS n FROM limiter_slots").fetchone() == {"n": 1}

        workers.start(**QUICK)
        wait_for(lambda: finished(database_url), "the next worker")
        check_report(database_url, url, batch_id)
        assert sum(e["billable"] for e in payer_requests(url)) == 1


def test_ten_workers_share_one_limit(database_url: str, payer_db: Path, tmp_path: Path) -> None:
    """The default limits against the mock's own, with far more workers than they need."""
    batch_id = submit(database_url, 160)
    limited = payer_profile(
        rate_limit_per_second=20,
        max_in_flight=10,
        latency_min_seconds=0.05,
        latency_max_seconds=0.2,
    )
    with serving(limited, payer_db) as url, workers_for(database_url, url, tmp_path) as workers:
        for _ in range(10):
            workers.start()
        wait_for(lambda: finished(database_url), "ten workers", seconds=90)
        findings = report.build(database_url, url, batch_id, 6)
        print(report.render(findings))
        assert all(f.passed for f in findings)
        peak = report.peak_per_second([e["received_at"] for e in payer_requests(url)])
    assert 10 <= peak <= 20


def start_postgres_on_a_fixed_port() -> PostgresContainer:
    """So that it comes back where it was. The port can be taken meanwhile, so try a few."""
    for attempt in range(5):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        container = PostgresContainer("postgres:18", driver=None).with_bind_ports(5432, port)
        try:
            return container.start()
        except APIError:
            container.stop()
            if attempt == 4:
                raise
    raise AssertionError("unreachable")


@pytest.fixture
def restartable() -> Iterator[tuple[str, Callable[[], None], Callable[[], None]]]:
    """A Postgres of its own, to stop and start."""
    postgres = start_postgres_on_a_fixed_port()
    try:
        url = postgres.get_connection_url()
        migrate(url)
        wrapped = postgres.get_wrapped_container()
        yield url, lambda: wrapped.stop(timeout=5), wrapped.start
    finally:
        postgres.stop()


def test_postgres_restarting_loses_nothing(
    restartable: tuple[str, Callable[[], None], Callable[[], None]],
    payer_db: Path,
    tmp_path: Path,
) -> None:
    url_db, stop, start = restartable
    batch_id = submit(url_db, 120)
    app = create_app(Settings(database_url=url_db, api_token=TOKEN))
    slower = payer_profile(latency_min_seconds=0.1, latency_max_seconds=0.3)
    with (
        TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"}) as api,
        serving(slower, payer_db) as url,
        workers_for(url_db, url, tmp_path) as workers,
    ):
        for _ in range(2):
            workers.start(**{**QUICK, "rate_per_second": 40})
        wait_for(lambda: count(url_db, "state = 'done'") >= 10, "some checks")

        stop()
        response = api.get(f"/batches/{batch_id}")
        assert response.status_code == 503
        assert response.headers["content-type"] == "application/problem+json"
        # An outage longer than a worker will wait for a connection, so it has to say so.
        wait_for(lambda: "database_unavailable" in workers.output(), "workers to notice")
        assert all(p.poll() is None for p in workers.processes), "a worker died"
        start()

        def back() -> bool:
            try:
                return finished(url_db)
            except psycopg.OperationalError:
                return False

        wait_for(back, "the batch after the restart", seconds=60)
        assert api.get(f"/batches/{batch_id}").json()["done"] is True
        check_report(url_db, url, batch_id)
