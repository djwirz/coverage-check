"""What goes in and out of the API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

MAX_CHECKS = 5000

CheckState = Literal["queued", "leased", "done", "exception"]
Outcome = Literal["covered", "not_covered"]

# Postgres text can't hold NUL, so refuse it here and not with a 500 at insert time.
_NO_NUL = r"^[^\x00]*$"
Name = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=_NO_NUL)]
MemberId = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=_NO_NUL)]


class CheckRequest(BaseModel):
    """One coverage question. Unknown fields are refused, so an expected answer can't ride along."""

    model_config = ConfigDict(extra="forbid")

    member_id: MemberId
    given: Annotated[list[Name], Field(min_length=1, max_length=10)]
    family: Name
    birth_date: date
    payer: Name
    service_date: date


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: Annotated[list[CheckRequest], Field(min_length=1, max_length=MAX_CHECKS)]


class CheckSummary(BaseModel):
    id: UUID
    position: int
    state: CheckState
    outcome: Outcome | None


class Batch(BaseModel):
    id: UUID
    created_at: datetime
    total: int
    done: bool
    states: dict[CheckState, int]
    outcomes: dict[Outcome, int]
    checks: list[CheckSummary]


class Check(BaseModel):
    id: UUID
    batch_id: UUID
    position: int
    member_id: str
    given: list[str]
    family: str
    birth_date: date
    payer: str
    service_date: date
    state: CheckState
    outcome: Outcome | None
    exception_reason: str | None
    result: dict[str, Any] | None


class Exceptions(BaseModel):
    exceptions: list[Check]
