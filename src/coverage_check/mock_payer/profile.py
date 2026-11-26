"""The failure profile: the payer's limits and how often it misbehaves."""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Fault = Literal["ok", "throttle", "server_error", "malformed", "slow", "timeout", "late"]

Rate = Annotated[float, Field(ge=0, le=1)]
Seconds = Annotated[float, Field(ge=0)]


@dataclass(frozen=True)
class Draw:
    fault: Fault
    latency_seconds: float
    server_status: int


class Profile(BaseModel):
    # A misspelled setting must fail loudly, not leave a fault silently off.
    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int
    rate_limit_per_second: Annotated[int, Field(ge=1)]
    max_in_flight: Annotated[int, Field(ge=1)]
    latency_min_seconds: Seconds
    latency_max_seconds: Seconds
    retry_after_seconds: Annotated[int, Field(ge=0)]
    throttle_rate: Rate = 0
    server_error_rate: Rate = 0
    malformed_rate: Rate = 0
    slow_rate: Rate = 0
    timeout_rate: Rate = 0
    late_rate: Rate = 0
    slow_seconds: Seconds = 0
    timeout_seconds: Seconds = 0
    late_seconds: Seconds = 0

    def _bands(self) -> list[tuple[Fault, float]]:
        return [
            ("throttle", self.throttle_rate),
            ("server_error", self.server_error_rate),
            ("malformed", self.malformed_rate),
            ("slow", self.slow_rate),
            ("timeout", self.timeout_rate),
            ("late", self.late_rate),
        ]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if sum(rate for _, rate in self._bands()) > 1:
            raise ValueError("the fault rates add up to more than 1")
        if self.latency_min_seconds > self.latency_max_seconds:
            raise ValueError("latency_min_seconds is above latency_max_seconds")
        return self

    def draw(self, body_sha256: str, occurrence: int) -> Draw:
        """What happens to the nth time this body gets this far.

        A hash, not a random generator: the same run gets the same faults whatever order
        requests arrive in, and whatever random ids they carry.
        """
        digest = hashlib.sha256(f"{self.seed}:{body_sha256}:{occurrence}".encode()).digest()
        pick, latency = (int.from_bytes(digest[i : i + 8]) / 2**64 for i in (0, 8))
        fault: Fault = "ok"
        upper = 0.0
        for name, rate in self._bands():
            upper += rate
            if pick < upper:
                fault = name
                break
        span = self.latency_max_seconds - self.latency_min_seconds
        return Draw(
            fault=fault,
            latency_seconds=self.latency_min_seconds + latency * span,
            server_status=(500, 502, 503)[digest[16] % 3],
        )


def load_profile(path: Path) -> Profile:
    return Profile.model_validate(tomllib.loads(path.read_text()))
