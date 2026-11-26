"""The mock payer's failure modes, and that they are repeatable."""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
import uvicorn
from pydantic import ValidationError
from starlette.testclient import TestClient

from coverage_check.mock_payer.app import create_app
from coverage_check.mock_payer.profile import Profile, load_profile
from tests.payer import member_body, profile

CONFIG = Path(__file__).parent.parent / "config" / "mock-payer"
HEADERS = {"Idempotency-Key": "attempt-1"}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "payer.sqlite"


def send(settings: Profile, db: Path) -> tuple[Any, list[dict[str, Any]]]:
    with TestClient(create_app(settings, db)) as client:
        response = client.post("/eligibility", json=member_body(), headers=HEADERS)
        return response, client.get("/requests").json()["requests"]


@pytest.mark.parametrize(
    ("fault", "statuses", "error"),
    [
        ("throttle", {429}, "rate_limited"),
        ("server_error", {500, 502, 503}, "server_error"),
        ("timeout", {504}, "timed_out"),
    ],
)
def test_a_failed_request_answers_nothing(
    db: Path, fault: str, statuses: set[int], error: str
) -> None:
    response, (entry,) = send(profile(**{f"{fault}_rate": 1}), db)
    assert response.status_code in statuses
    assert response.json() == {"error": error}
    assert ("Retry-After" in response.headers) == (fault == "throttle")
    assert (entry["behaviour"], entry["billable"]) == (fault, False)
    # Nothing was stored under the key, so asking again is a new request.
    healthy, entries = send(profile(), db)
    assert healthy.status_code == 200
    assert [e["replay"] for e in entries] == [False, False]


def test_a_malformed_reply_is_a_200_that_does_not_parse(db: Path) -> None:
    response, (entry,) = send(profile(malformed_rate=1), db)
    assert response.status_code == 200
    with pytest.raises(ValueError, match="Unterminated"):
        response.json()
    assert (entry["behaviour"], entry["billable"]) == ("malformed", False)


@pytest.mark.parametrize("fault", ["slow", "late", "timeout"])
def test_slow_late_and_timeout_take_their_time(db: Path, fault: str) -> None:
    _, (entry,) = send(profile(**{f"{fault}_rate": 1, f"{fault}_seconds": 0.2}), db)
    assert entry["replied_at"] - entry["received_at"] >= 0.19
    assert entry["behaviour"] == fault
    assert entry["billable"] == (fault != "timeout")


@contextmanager
def serving(settings: Profile, db: Path) -> Iterator[str]:
    """A real server, because only a real connection can be hung up on."""
    server = uvicorn.Server(uvicorn.Config(create_app(settings, db), port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_a_caller_that_gives_up_does_not_stop_the_payer(db: Path) -> None:
    """The late reply: answered, billed and still in flight after the caller has gone."""
    settings = profile(max_in_flight=1, late_rate=1, late_seconds=1)
    with serving(settings, db) as url, httpx2.Client(base_url=url) as client:
        with pytest.raises(httpx2.ReadTimeout):
            client.post("/eligibility", json=member_body(), headers=HEADERS, timeout=0.2)

        # It still holds the only place.
        other = client.post("/eligibility", json=member_body(), headers={"Idempotency-Key": "b"})
        assert other.status_code == 429

        deadline = time.monotonic() + 10
        while client.get("/requests").json()["requests"][0]["replied_at"] is None:
            assert time.monotonic() < deadline, "the late reply was never finished"
            time.sleep(0.05)
        first = client.get("/requests").json()["requests"][0]
        assert (first["behaviour"], first["billable"]) == ("late", True)
        assert first["replied_at"] - first["received_at"] >= 0.99

        # Asking again with the same key hears the answer that was missed, billed once.
        again = client.post("/eligibility", json=member_body(), headers=HEADERS)
        assert again.status_code == 200
        entries = client.get("/requests").json()["requests"]
        assert sum(e["billable"] for e in entries) == 1


def faults(settings: Profile, db: Path, keys: str, order: list[int]) -> Counter[tuple[str, str]]:
    with TestClient(create_app(settings, db)) as client:
        for i, position in enumerate(order):
            client.post(
                "/eligibility",
                json=member_body(position),
                headers={"Idempotency-Key": f"{keys}-{i}"},
            )
        entries = client.get("/requests").json()["requests"]
    return Counter((e["body_sha256"], e["behaviour"]) for e in entries)


def test_a_run_gets_the_same_faults_in_any_order(tmp_path: Path) -> None:
    settings = profile(throttle_rate=0.2, server_error_rate=0.2, malformed_rate=0.2)
    order = [p for p in range(35) for _ in range(3)]
    first = faults(settings, tmp_path / "a.sqlite", "one", order)
    second = faults(settings, tmp_path / "b.sqlite", "two", list(reversed(order)))
    assert first == second
    assert {behaviour for _, behaviour in first} == {"ok", "throttle", "server_error", "malformed"}
    other_seed = settings.model_copy(update={"seed": 2})
    assert faults(other_seed, tmp_path / "c.sqlite", "one", order) != first


def test_the_default_profile_produces_every_fault_about_as_often_as_it_says() -> None:
    settings = load_profile(CONFIG / "default.toml")
    n = 20_000
    seen = Counter(settings.draw(f"body-{i}", 0).fault for i in range(n))
    for fault in ("throttle", "server_error", "malformed", "slow", "timeout", "late"):
        rate = getattr(settings, f"{fault}_rate")
        assert 0.7 * rate < seen[fault] / n < 1.3 * rate, fault


def test_the_calm_profile_has_no_faults() -> None:
    settings = load_profile(CONFIG / "calm.toml")
    assert {settings.draw(f"body-{i}", 0).fault for i in range(2000)} == {"ok"}


@pytest.mark.parametrize(
    "settings",
    [
        {"thottle_rate": 0.1},
        {"throttle_rate": 0.6, "late_rate": 0.6},
        {"latency_min_seconds": 2, "latency_max_seconds": 1},
    ],
    ids=["misspelled", "rates-over-one", "latency-reversed"],
)
def test_a_bad_profile_is_refused(settings: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        profile(**settings)
