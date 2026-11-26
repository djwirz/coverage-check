"""The payer protocol: simplified JSON (ADR 0009). The mock speaks it; the workers will read it.

POST /eligibility with an Idempotency-Key header (the attempt id) and a CheckRequest body.
A 200 is the payer's answer, whatever it says. Anything else carries an ErrorReply.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel

Result = Literal["active", "inactive", "member_not_found", "multiple_matches"]
Error = Literal[
    "invalid_request",  # 400
    "in_progress",  # 409: this key is still being handled
    "key_reused",  # 422: this key came before with another body
    "rate_limited",  # 429, with Retry-After
    "server_error",  # 500, 502, 503
    "timed_out",  # 504
]


class Plan(BaseModel):
    name: str


class CoveragePeriod(BaseModel):
    start: date
    end: date  # coverage runs up to, not including, this day


class Benefit(BaseModel):
    service: str
    copay_usd: int


class EligibilityReply(BaseModel):
    result: Result
    plan: Plan | None = None
    coverage_period: CoveragePeriod | None = None
    benefits: list[Benefit] = []


class ErrorReply(BaseModel):
    error: Error
