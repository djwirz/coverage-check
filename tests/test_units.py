"""The retry policy, the bucket's arithmetic, worker settings, FHIR and the report's counting."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from coverage_check import fhir
from coverage_check.config import WorkerSettings, worker_settings_from_env
from coverage_check.limiter import refill
from coverage_check.mock_payer.members import Directory
from coverage_check.policy import (
    Answered,
    Done,
    Failed,
    GiveUp,
    Retry,
    backoff_seconds,
    classify,
    retry_after_seconds,
)
from coverage_check.report import peak_in_flight, peak_per_second
from coverage_check.seed import sample_batch

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("active", "covered"),
        ("inactive", "not_covered"),
    ],
)
def test_an_answer_about_coverage_is_done(result: str, expected: str) -> None:
    body = (
        f'{{"result": "{result}", "plan": {{"name": "P"}},'
        ' "coverage_period": {"start": "2026-01-01", "end": "2027-01-01"}}'
    )
    decision = classify(Answered(200, body), 0)
    assert isinstance(decision, Done)
    assert decision.outcome == expected


@pytest.mark.parametrize(
    ("seen", "reason"),
    [
        (Answered(200, '{"result": "member_not_found"}'), "member_not_found"),
        (Answered(200, '{"result": "multiple_matches"}'), "multiple_matches"),
        (Answered(400, '{"error": "invalid_request"}'), "invalid_request"),
        (Answered(422, '{"error": "key_reused"}'), "key_reused"),
        (Answered(401, ""), "unexpected_status"),
        (Answered(302, ""), "unexpected_status"),
    ],
)
def test_permanent_replies_are_not_retried(seen: Answered, reason: str) -> None:
    assert classify(seen, 0) == GiveUp(reason)


@pytest.mark.parametrize(
    ("seen", "reason", "delay"),
    [
        (Answered(409, ""), "in_progress", 0),
        (Answered(429, "", "7"), "rate_limited", 7),
        (Answered(500, ""), "server_error", 0),
        (Answered(503, "", "3"), "server_error", 3),
        (Answered(504, ""), "server_error", 0),
        (Failed("timeout", sent=True), "timeout", 0),
        (Failed("connection", sent=False), "connection", 0),
    ],
)
def test_transient_failures_are_retried(seen: Any, reason: str, delay: float) -> None:
    assert classify(seen, 0) == Retry(reason, delay)


@pytest.mark.parametrize(
    "body", ['{"result": "act', "{}", '{"result": "maybe"}', "", '{"result": "active"}']
)
def test_a_malformed_reply_is_retried_once(body: str) -> None:
    assert classify(Answered(200, body), 0) == Retry("malformed")
    assert classify(Answered(200, body), 1) == GiveUp("malformed_response")


def test_retry_after_as_seconds_or_a_date() -> None:
    assert retry_after_seconds("12") == 12
    assert retry_after_seconds("Sun, 01 Nov 2026 12:00:30 GMT", NOW) == 30
    assert retry_after_seconds("Sun, 01 Nov 2026 11:00:00 GMT", NOW) == 0
    assert retry_after_seconds("soon") == 0
    assert retry_after_seconds(None) == 0


def test_backoff_doubles_up_to_the_cap_with_full_jitter() -> None:
    assert [backoff_seconds(n, 1, 30, lambda: 1.0) for n in range(1, 8)] == [
        1,
        2,
        4,
        8,
        16,
        30,
        30,
    ]
    assert backoff_seconds(4, 1, 30, lambda: 0.0) == 0
    assert backoff_seconds(10_000, 1, 30, lambda: 0.5) == 15


def test_the_bucket_refills_with_time_up_to_the_burst() -> None:
    assert refill(0, 1, 17, 0.5 / 17) == pytest.approx(0.5)
    assert refill(0, 1, 17, 60) == 1
    assert refill(0.4, 3, 10, 0.1) == pytest.approx(1.4)
    assert refill(0.4, 3, 10, -5) == pytest.approx(0.4)


def test_default_settings_leave_room_under_the_payers_limits() -> None:
    settings = WorkerSettings(database_url="postgresql://x")
    assert settings.burst + settings.rate_per_second <= settings.payer_rate_limit - 2
    assert settings.max_in_flight < settings.payer_max_in_flight


@pytest.mark.parametrize(
    "change",
    [
        {"rate_per_second": 17},
        {"burst": 20},
        {"max_in_flight": 11},
        {"attempt_timeout_seconds": 40},
        {"lease_seconds": 39},
        {"max_attempts": 0},
    ],
)
def test_unsafe_settings_are_refused(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match=r"."):
        WorkerSettings(database_url="postgresql://x", **change)


def test_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("WORKER_CONCURRENCY", "2")
    monkeypatch.setenv("WORKER_RATE_PER_SECOND", "5.5")
    monkeypatch.setenv("WORKER_PAYER_URL", "http://payer:8000")
    settings = worker_settings_from_env()
    assert (settings.concurrency, settings.rate_per_second, settings.payer_url) == (
        2,
        5.5,
        "http://payer:8000",
    )


def test_peaks_are_counted_over_any_window() -> None:
    assert peak_per_second([]) == 0
    assert peak_per_second([0.0, 0.5, 0.999, 1.0, 1.001]) == 4
    assert peak_per_second([0, 1, 2, 3]) == 1
    assert peak_in_flight([(0, 2), (1, 3), (2, 4)]) == 2
    assert peak_in_flight([(0, None), (5, 6)]) == 2


def results() -> list[dict[str, Any]]:
    """A result for every member, as the payer would answer, plus an exception."""
    directory = Directory()
    built = []
    for check in sample_batch().checks:
        reply = directory.answer(check)
        if reply.result in ("active", "inactive"):
            built.append(fhir.answered(uuid4(), check, reply, NOW))
        else:
            built.append(fhir.failed(uuid4(), check, reply.result, NOW))
    return built


def test_every_result_is_a_coverage_eligibility_response() -> None:
    built = results()
    for result in built:
        fhir.CoverageEligibilityResponse.model_validate(result)
        assert result["resourceType"] == "CoverageEligibilityResponse"
        assert {"status", "purpose", "patient", "created", "request", "outcome", "insurer"} <= set(
            result
        )
        assert result["servicedDate"] == date(2026, 11, 1).isoformat()
    inforce = [r["insurance"][0]["inforce"] for r in built if r["outcome"] == "complete"]
    assert (inforce.count(True), inforce.count(False)) == (23, 4)
    assert sum(r["outcome"] == "error" for r in built) == 8
    active = next(r for r in built if r.get("insurance", [{}])[0].get("inforce"))
    assert active["insurance"][0]["coverage"] == {"reference": "#coverage"}
    assert active["contained"][1]["class"][0]["value"].endswith("synthetic plan")
    assert all(set(item) == {"category", "benefit"} for item in active["insurance"][0]["item"])


@pytest.mark.parametrize(
    "break_it",
    [
        lambda r: r.pop("insurer"),
        lambda r: r.update(outcome="done"),
        lambda r: r.update(purpose=[]),
        lambda r: r["contained"].pop(),
        lambda r: r.pop("insurance"),
        lambda r: r["insurance"][0]["item"][0].pop("category"),
        lambda r: r["insurance"][0]["item"][0]["benefit"][0]["allowedMoney"].update(currency="XXQ"),
    ],
)
def test_the_model_refuses_a_broken_result(break_it: Any) -> None:
    result = next(r for r in results() if r.get("insurance", [{}])[0].get("inforce"))
    break_it(result)
    with pytest.raises(ValidationError):
        fhir.CoverageEligibilityResponse.model_validate(result)
