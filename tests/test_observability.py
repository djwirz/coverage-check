"""The numbers and traces the cluster shows, against real Postgres and a real mock payer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracerProvider
from prometheus_client import CollectorRegistry, generate_latest
from starlette.testclient import TestClient

from coverage_check import batches, limiter, metrics, queue, tracing
from coverage_check.mock_payer.app import create_app as create_payer
from coverage_check.policy import Retry
from coverage_check.seed import sample_batch
from tests.payer import member_body, profile, serving
from tests.test_workers import payer_profile, settings, submit, work


def value(registry: CollectorRegistry, name: str, **labels: str) -> float:
    found = registry.get_sample_value(name, labels)
    assert found is not None, f"no {name} {labels}"
    return found


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use(provider)
    yield exporter
    tracing.use(NoOpTracerProvider())


def test_metrics_after_a_batch(database_url: str, tmp_path: Path) -> None:
    with serving(payer_profile(), tmp_path / "payer.sqlite") as payer_url:
        submit(database_url)
        work(settings(database_url, payer_url))
    registry = metrics.registry(database_url)
    total = len(sample_batch().checks)

    assert value(registry, "coverage_check_database_up") == 1
    assert value(registry, "coverage_check_checks", state="queued") == 0
    done = value(registry, "coverage_check_checks", state="done")
    exceptions = value(registry, "coverage_check_checks", state="exception")
    assert done + exceptions == total
    assert value(registry, "coverage_check_attempts_total", outcome="answered") == done
    assert (
        value(
            registry,
            "coverage_check_final_checks",
            result="exception",
            reason="member_not_found",
        )
        > 0
    )
    assert value(registry, "coverage_check_seconds_to_final_count") == total
    assert value(registry, "coverage_check_payer_slots_in_use", payer_api="mock-payer") == 0
    assert value(registry, "coverage_check_oldest_lease_age_seconds") == 0
    assert value(registry, "coverage_check_lease_expiries_total") == 0


def test_a_late_reply_never_lowers_a_counter(database_url: str) -> None:
    submit(database_url, 1)
    config = replace(settings(database_url), lease_seconds=7)
    registry = metrics.registry(database_url)

    async def claim_expire_claim() -> queue.Claim:
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
            await limiter.configure(conn, config)
            await conn.execute("UPDATE limiter_buckets SET tokens = burst")
            first = await queue.claim(conn, config)
            assert isinstance(first, queue.Claim)
            await conn.execute(
                "UPDATE checks SET lease_expires_at = clock_timestamp(),"
                " updated_at = clock_timestamp() - interval '200 seconds'"
            )
            assert value(registry, "coverage_check_oldest_lease_age_seconds") >= 200
            await conn.execute("UPDATE limiter_buckets SET tokens = burst")
            second = await queue.claim(conn, config)
            assert isinstance(second, queue.Claim)
            return first

    first = asyncio.run(claim_expire_claim())
    assert value(registry, "coverage_check_lease_expiries_total") == 1
    assert value(registry, "coverage_check_retries_total", reason="lease_expired") == 1

    async def reply_late() -> None:
        async with await psycopg.AsyncConnection.connect(database_url) as conn:
            held = await queue.finish(
                conn, first, Retry("server_error"), attempt_class="server_error"
            )
            assert not held

    asyncio.run(reply_late())
    assert value(registry, "coverage_check_lease_expiries_total") == 1
    assert value(registry, "coverage_check_late_replies_total") == 1
    assert value(registry, "coverage_check_retries_total", reason="lease_expired") == 1
    assert value(registry, "coverage_check_attempts_total", outcome="server_error") == 1


def test_a_database_that_is_away_is_said_not_raised() -> None:
    registry = metrics.registry("postgresql://nobody:nothing@127.0.0.1:9/nowhere")
    assert value(registry, "coverage_check_database_up") == 0
    assert registry.get_sample_value("coverage_check_checks", {"state": "queued"}) is None


def test_the_payer_counts_what_it_saw(tmp_path: Path) -> None:
    now = [1000.0]
    app = create_payer(
        profile(rate_limit_per_second=2), tmp_path / "payer.sqlite", clock=lambda: now[0]
    )
    registry = app.state.metrics.registry
    assert value(registry, "mock_payer_requests_total", behaviour="rate_limit") == 0
    with TestClient(app) as client:
        for number in range(3):
            client.post(
                "/eligibility",
                content=json.dumps(member_body()),
                headers={"Idempotency-Key": f"key-{number}"},
            )
    assert value(registry, "mock_payer_requests_total", behaviour="ok") == 2
    assert value(registry, "mock_payer_requests_total", behaviour="rate_limit") == 1
    assert value(registry, "mock_payer_peak_requests_in_one_second") == 3
    assert value(registry, "mock_payer_rate_limit_per_second") == 2
    now[0] += 60
    assert value(registry, "mock_payer_peak_requests_in_one_second") == 0
    assert b"mock_payer_in_flight 0.0" in generate_latest(registry)


def by_name(spans: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.name == name]


def test_one_trace_from_submit_through_a_retry(
    database_url: str, tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    # Every first request fails; the payer answers the same body the second time or later.
    with serving(payer_profile(server_error_rate=0.5), tmp_path / "payer.sqlite") as payer_url:
        submit(database_url)
        work(settings(database_url, payer_url, max_attempts=20))
    submitted = by_name(spans, "check.submitted")
    assert len(submitted) == len(sample_batch().checks)
    assert len({s.context.trace_id for s in submitted if s.context}) == len(submitted)

    attempts = by_name(spans, "check.attempt")
    retried = next(s for s in attempts if s.attributes and s.attributes["attempt.number"] == 2)
    assert retried.attributes is not None
    assert retried.context is not None
    trace_id = retried.context.trace_id
    mine = [s for s in spans.get_finished_spans() if s.context and s.context.trace_id == trace_id]
    names = [s.name for s in mine]
    assert names.count("check.submitted") == 1
    assert names.count("check.attempt") >= 2
    assert names.count("check.claim") == names.count("check.attempt") == names.count("payer.call")
    assert {
        s.attributes["check.id"] for s in mine if s.attributes and "check.id" in s.attributes
    } == {retried.attributes["check.id"]}


def test_spans_carry_no_member_details(
    database_url: str, tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    with serving(payer_profile(), tmp_path / "payer.sqlite") as payer_url:
        submit(database_url)
        work(settings(database_url, payer_url))
    member = sample_batch().checks[0]
    everything = " ".join(s.to_json() for s in spans.get_finished_spans())
    for secret in (member.member_id, member.family, member.birth_date.isoformat()):
        assert secret not in everything


def test_without_tracing_nothing_is_stored(database_url: str, db: psycopg.Connection[Any]) -> None:
    async def go() -> None:
        async with await psycopg.AsyncConnection.connect(database_url) as conn:
            await batches.submit_batch(conn, "untraced", sample_batch(2))

    asyncio.run(go())
    assert db.execute("SELECT DISTINCT traceparent FROM checks").fetchall() == [(None,)]


def test_a_collector_that_is_away_changes_nothing(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")
    tracing.configure("test")
    try:
        with serving(payer_profile(), tmp_path / "payer.sqlite") as payer_url:
            batch_id = submit(database_url)
            work(settings(database_url, payer_url))
    finally:
        tracing.shutdown()
        tracing.use(NoOpTracerProvider())
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM checks WHERE batch_id = %s AND state IN ('queued', 'leased')",
            (batch_id,),
        ).fetchone()
    assert row == (0,)
