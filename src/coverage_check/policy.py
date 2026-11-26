"""What a payer's reply, or its silence, means for a check. No I/O."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from pydantic import ValidationError

from coverage_check.models import Outcome
from coverage_check.protocol import EligibilityReply


@dataclass(frozen=True)
class Answered:
    """The payer sent an HTTP response."""

    status: int
    body: str
    retry_after: str | None = None


@dataclass(frozen=True)
class Failed:
    """No response. `sent` says whether the payer may have the request."""

    kind: str  # "timeout" or "connection"
    sent: bool


Observation = Answered | Failed


@dataclass(frozen=True)
class Done:
    outcome: Outcome
    reply: EligibilityReply


@dataclass(frozen=True)
class GiveUp:
    reason: str  # why a person has to look at this check


@dataclass(frozen=True)
class Retry:
    reason: str
    min_delay_seconds: float = 0


Decision = Done | GiveUp | Retry


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float:
    """Retry-After is seconds or an HTTP date (RFC 9110 §10.2.3). Nonsense counts as zero."""
    if value is None:
        return 0
    if value.strip().isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - (now or datetime.now(UTC))).total_seconds())
    except (ValueError, TypeError):
        return 0


def _readable(body: str) -> EligibilityReply | None:
    try:
        reply = EligibilityReply.model_validate_json(body)
    except ValidationError:
        return None
    # "Active" or "inactive" without saying under what plan and period can't become a result.
    if reply.result in ("active", "inactive") and (
        reply.plan is None or reply.coverage_period is None
    ):
        return None
    return reply


def classify(seen: Observation, malformed_before: int) -> Decision:
    """`malformed_before` counts earlier unreadable replies for this check."""
    if isinstance(seen, Failed):
        return Retry(seen.kind)
    if seen.status == 200:
        reply = _readable(seen.body)
        if reply is None:
            # It might have been transient, once.
            return GiveUp("malformed_response") if malformed_before else Retry("malformed")
        match reply.result:
            case "active":
                return Done("covered", reply)
            case "inactive":
                return Done("not_covered", reply)
            case "member_not_found" | "multiple_matches" as reason:
                return GiveUp(reason)
    match seen.status:
        case 400:
            return GiveUp("invalid_request")
        case 422:
            return GiveUp("key_reused")
        case 409:
            return Retry("in_progress", retry_after_seconds(seen.retry_after))
        case 429:
            return Retry("rate_limited", retry_after_seconds(seen.retry_after))
        case status if 500 <= status < 600:
            return Retry("server_error", retry_after_seconds(seen.retry_after))
    return GiveUp("unexpected_status")


def backoff_seconds(
    attempts: int, base: float, cap: float, rand: Callable[[], float] = random.random
) -> float:
    """Exponential with full jitter: anywhere from nothing to base * 2^(attempts - 1), capped."""
    return rand() * min(cap, base * float(2 ** min(attempts - 1, 30)))
