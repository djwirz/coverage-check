"""Helpers for the mock payer's tests."""

from __future__ import annotations

from typing import Any

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
