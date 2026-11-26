"""Helpers for the mock payer's tests."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn

from coverage_check.mock_payer.app import create_app
from coverage_check.mock_payer.profile import Profile
from coverage_check.seed import sample_batch


def profile(**settings: Any) -> Profile:
    """Instant and faultless, with limits out of the way, unless a test says otherwise."""
    return Profile.model_validate(
        {
            "seed": 1,
            "rate_limit_per_second": 10_000,
            "max_in_flight": 10_000,
            "retry_after_seconds": 2,
            "latency_min_seconds": 0,
            "latency_max_seconds": 0,
            **settings,
        }
    )


def member_body(position: int = 0) -> dict[str, Any]:
    return sample_batch().checks[position].model_dump(mode="json")


@contextmanager
def serving(settings: Profile, db: Path) -> Iterator[str]:
    """The mock payer on a real server and port. Yields its URL."""
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
