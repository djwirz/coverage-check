"""The mock payer's contract: answers, idempotency, limits and the request log."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
import synthetic_cohort
from starlette.testclient import TestClient

from coverage_check.mock_payer.app import create_app
from coverage_check.mock_payer.profile import Profile
from coverage_check.protocol import EligibilityReply
from coverage_check.seed import sample_batch
from tests.payer import member_body, profile

EXPECTED = {
    "covered": "active",
    "inactive": "inactive",
    "missing": "member_not_found",
    "ambiguous": "multiple_matches",
}

Open = Callable[..., TestClient]


@pytest.fixture
def start(tmp_path: Path) -> Iterator[Open]:
    """Start a payer. Called twice, it is the same payer restarted: one database."""
    clients: list[TestClient] = []

    def open_(settings: Profile | None = None, **kwargs: Any) -> TestClient:
        app = create_app(settings or profile(), tmp_path / "payer.sqlite", **kwargs)
        clients.append(TestClient(app))
        return clients[-1]

    yield open_
    for client in clients:
        client.close()


def ask(client: TestClient, body: Any, key: str | None = "attempt-1") -> Any:
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post("/eligibility", json=body, headers=headers)


def log(client: TestClient) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = client.get("/requests").json()["requests"]
    return requests


def test_every_member_gets_the_expected_answer(start: Open) -> None:
    client = start()
    members = synthetic_cohort.members()
    for member, check in zip(members, sample_batch().checks, strict=True):
        response = ask(client, check.model_dump(mode="json"), key=member["member_id"])
        assert response.status_code == 200
        reply = EligibilityReply.model_validate(response.json())
        assert reply.result == EXPECTED[member["coverage_state"]], member["member_id"]
        assert (reply.plan is None) == (reply.result in ("member_not_found", "multiple_matches"))
        assert bool(reply.benefits) == (reply.result == "active")


@pytest.mark.parametrize(
    "change",
    [{"member_id": "nobody"}, {"birth_date": "1900-01-01"}, {"payer": "Another Payer"}],
)
def test_details_that_do_not_match_find_no_member(start: Open, change: dict[str, str]) -> None:
    response = ask(start(), {**member_body(), **change})
    assert response.json()["result"] == "member_not_found"


@pytest.mark.parametrize(
    ("day", "result"),
    [("start", "active"), ("end", "inactive"), ("before", "inactive")],
)
def test_coverage_runs_from_start_up_to_end(start: Open, day: str, result: str) -> None:
    member = synthetic_cohort.members()[0]
    assert member["coverage_state"] == "covered"
    date = {
        "start": member["coverage_start"],
        "end": member["coverage_end"],
        "before": "1999-01-01",
    }[day]
    response = ask(start(), {**member_body(), "service_date": date})
    assert response.json()["result"] == result


def test_a_repeated_key_gets_the_first_answer_and_is_billed_once(start: Open) -> None:
    client = start()
    first = ask(client, member_body())
    again = ask(client, member_body())
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.content == first.content
    assert [(r["behaviour"], r["replay"], r["billable"]) for r in log(client)] == [
        ("ok", False, True),
        ("replay", True, False),
    ]


def test_a_key_cannot_be_reused_for_another_question(start: Open) -> None:
    client = start()
    ask(client, member_body(0))
    response = ask(client, member_body(1))
    assert response.status_code == 422
    assert response.json() == {"error": "key_reused"}


def test_answers_and_log_survive_a_restart(start: Open) -> None:
    first = ask(start(), member_body())
    restarted = start()
    again = ask(restarted, member_body())
    assert again.content == first.content
    assert [r["behaviour"] for r in log(restarted)] == ["ok", "replay"]


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ({"member_id": "x"}, "k"),
        ({**member_body(), "coverage_state": "covered"}, "k"),
        (member_body(), None),
        (member_body(), "k" * 256),
    ],
    ids=["incomplete", "unknown-field", "no-key", "long-key"],
)
def test_invalid_requests_are_refused_and_logged(start: Open, body: Any, key: str | None) -> None:
    client = start()
    response = ask(client, body, key)
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    (entry,) = log(client)
    assert (entry["status"], entry["behaviour"], entry["billable"]) == (
        400,
        "invalid_request",
        False,
    )


def test_no_one_second_window_gets_more_than_the_limit(start: Open) -> None:
    now = [100.0]
    client = start(profile(rate_limit_per_second=3), clock=lambda: now[0])
    statuses = []
    for i, at in enumerate([100.0, 100.5, 100.9, 100.99, 101.0, 101.4, 102.5]):
        now[0] = at
        statuses.append(ask(client, member_body(), key=str(i)).status_code)
    # At 101.0 the first request has left the window but the refused one still counts.
    assert statuses == [200, 200, 200, 429, 429, 429, 200]
    refused = log(client)[3]
    assert refused["behaviour"] == "rate_limit"
    response = None
    for i in range(4):
        response = ask(client, member_body(), key=f"more-{i}")
    assert response is not None
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "2"


def at_once(settings: Profile, tmp_path: Path, keys: list[str]) -> tuple[list[int], list[Any]]:
    """Send one request per key, all at the same time. Returns statuses and the log."""
    app = create_app(settings, tmp_path / "payer.sqlite")

    async def run() -> tuple[list[int], list[Any]]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://payer") as client:
            responses = await asyncio.gather(
                *(
                    client.post("/eligibility", json=member_body(), headers={"Idempotency-Key": k})
                    for k in keys
                )
            )
            entries = (await client.get("/requests")).json()["requests"]
        return sorted(r.status_code for r in responses), entries

    return asyncio.run(run())


def test_more_than_the_cap_in_flight_is_refused(tmp_path: Path) -> None:
    settings = profile(max_in_flight=2, latency_min_seconds=0.2, latency_max_seconds=0.2)
    statuses, entries = at_once(settings, tmp_path, ["a", "b", "c", "d"])
    assert statuses == [200, 200, 429, 429]
    assert sorted(e["behaviour"] for e in entries) == [
        "in_flight_limit",
        "in_flight_limit",
        "ok",
        "ok",
    ]


def test_a_key_still_being_handled_is_a_conflict(tmp_path: Path) -> None:
    settings = profile(latency_min_seconds=0.2, latency_max_seconds=0.2)
    statuses, entries = at_once(settings, tmp_path, ["same", "same"])
    assert statuses == [200, 409]
    assert sum(e["billable"] for e in entries) == 1


def test_the_log_is_paged_and_holds_no_member_details(start: Open) -> None:
    client = start()
    for key in ("a", "b", "c"):
        ask(client, member_body(), key)
    page = client.get("/requests?after=1&limit=1").json()["requests"]
    assert [e["idempotency_key"] for e in page] == ["b"]
    assert page[0]["received_at"] <= page[0]["replied_at"]
    member = synthetic_cohort.members()[0]
    text = client.get("/requests").text
    for value in (member["member_id"], member["family"], member["birth_date"]):
        assert value not in text


def test_health_is_neither_limited_nor_logged(start: Open) -> None:
    client = start(profile(rate_limit_per_second=1))
    assert [client.get("/healthz").status_code for _ in range(5)] == [200] * 5
    assert log(client) == []
