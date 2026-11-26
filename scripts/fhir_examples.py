"""Write example results, made by the real mapping code, for the official FHIR validator.

Usage: python scripts/fhir_examples.py <good-dir> <broken-dir>

The good examples are every cohort member's answer and every exception reason. The broken one
has to be rejected, so that a validator that checks nothing can't pass the job.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from coverage_check import fhir
from coverage_check.mock_payer.members import Directory
from coverage_check.seed import sample_batch

CREATED = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
# Every reason a check can become an exception (policy.py).
REASONS = (
    "member_not_found",
    "multiple_matches",
    "invalid_request",
    "key_reused",
    "unexpected_status",
    "malformed_response",
    "payer_unavailable",
)


def examples() -> dict[str, dict[str, Any]]:
    directory = Directory()
    checks = sample_batch().checks
    made: dict[str, dict[str, Any]] = {}
    for position, check in enumerate(checks):
        check_id = uuid5(NAMESPACE_URL, f"member-{position}")
        reply = directory.answer(check)
        if reply.result in ("active", "inactive"):
            made[f"{reply.result}-{position}"] = fhir.answered(check_id, check, reply, CREATED)
    for reason in REASONS:
        check_id = uuid5(NAMESPACE_URL, reason)
        made[f"exception-{reason}"] = fhir.failed(check_id, checks[0], reason, CREATED)
    return made


def main(good: Path, broken: Path) -> None:
    good.mkdir(parents=True, exist_ok=True)
    broken.mkdir(parents=True, exist_ok=True)
    made = examples()
    for name, result in made.items():
        (good / f"{name}.json").write_text(json.dumps(result, indent=2))
    wrong = {k: v for k, v in next(iter(made.values())).items() if k != "insurer"}
    (broken / "no-insurer.json").write_text(json.dumps(wrong, indent=2))
    print(f"Wrote {len(made)} examples and 1 broken one")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
