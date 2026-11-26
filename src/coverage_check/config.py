"""Settings from the environment. Secrets are never defaulted."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    api_token: str = field(repr=False)


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return value


# How far under the payer's per-second limit the bucket must stay, for network jitter.
RATE_MARGIN = 2


@dataclass(frozen=True)
class WorkerSettings:
    """Defaults fit config/mock-payer/default.toml. Every field can be set from the environment."""

    database_url: str = field(repr=False)
    payer_url: str = "http://127.0.0.1:8001"
    payer_api: str = "mock-payer"  # the account whose limits the bucket stands for
    # The payer's limits, and what the engine allows itself.
    payer_rate_limit: int = 20
    payer_max_in_flight: int = 10
    # A bucket holding two means a worker that wakes a little late doesn't waste the token.
    rate_per_second: float = 16
    burst: int = 2
    max_in_flight: int = 8
    # A grant older than this is not sent: a stalled process would bunch requests up.
    grant_max_age_seconds: float = 0.1
    # One timeout, longer than the payer's longest call, then the slot, then the lease.
    attempt_timeout_seconds: float = 35
    slot_seconds: float = 40
    lease_seconds: float = 45
    max_attempts: int = 6
    backoff_base_seconds: float = 1
    backoff_cap_seconds: float = 30
    concurrency: int = 4
    idle_seconds: float = 0.5

    def __post_init__(self) -> None:
        # In any one second a token bucket can let through burst + rate requests.
        if self.burst + self.rate_per_second > self.payer_rate_limit - RATE_MARGIN:
            raise ValueError(
                f"burst + rate_per_second must be at most {self.payer_rate_limit - RATE_MARGIN}"
                " to stay under the payer's limit"
            )
        if not (self.burst >= 1 and self.rate_per_second > 0):
            raise ValueError("burst and rate_per_second must be positive")
        if not 1 <= self.max_in_flight <= self.payer_max_in_flight:
            raise ValueError("max_in_flight must be from 1 to the payer's cap")
        if not self.attempt_timeout_seconds < self.slot_seconds < self.lease_seconds:
            raise ValueError("attempt timeout < slot_seconds < lease_seconds must hold")
        if self.max_attempts < 1 or self.concurrency < 1:
            raise ValueError("max_attempts and concurrency must be at least 1")


def worker_settings_from_env() -> WorkerSettings:
    given: dict[str, Any] = {}
    for item in fields(WorkerSettings):
        name = "DATABASE_URL" if item.name == "database_url" else item.name.upper()
        value = os.environ.get(name if name == "DATABASE_URL" else f"WORKER_{name}")
        if value:
            kind = {"int": int, "float": float, "str": str}[str(item.type)]
            given[item.name] = kind(value)
    given["database_url"] = require_env("DATABASE_URL")
    return WorkerSettings(**given)


def settings_from_env() -> Settings:
    return Settings(
        database_url=require_env("DATABASE_URL"),
        api_token=require_env("COVERAGE_CHECK_API_TOKEN"),
    )
