"""The members from the synthetic-cohort tag, and the sample batch built from them."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import synthetic_cohort

from coverage_check.cli import main
from coverage_check.models import MAX_CHECKS, BatchRequest
from coverage_check.seed import reference_date, sample_batch

STATES = {"covered", "inactive", "missing", "ambiguous"}


def states_in(batch: BatchRequest) -> set[str]:
    by_member = {m["member_id"]: m["coverage_state"] for m in synthetic_cohort.members()}
    return {by_member[check.member_id] for check in batch.checks}


def test_every_coverage_state_is_present() -> None:
    assert {m["coverage_state"] for m in synthetic_cohort.members()} == STATES


def test_member_ids_are_unique() -> None:
    ids = [m["member_id"] for m in synthetic_cohort.members()]
    assert len(ids) == len(set(ids))


def test_default_batch_asks_about_every_member_once() -> None:
    batch = sample_batch()
    assert [c.member_id for c in batch.checks] == [
        m["member_id"] for m in synthetic_cohort.members()
    ]
    assert states_in(batch) == STATES


def test_large_batch_repeats_the_members() -> None:
    batch = sample_batch(3000)
    assert len(batch.checks) == 3000
    assert {c.member_id for c in batch.checks} == {
        m["member_id"] for m in synthetic_cohort.members()
    }
    assert states_in(batch) == STATES


@pytest.mark.parametrize("count", [0, -1, MAX_CHECKS + 1])
def test_count_out_of_range_is_refused(count: int) -> None:
    with pytest.raises(ValueError, match="count must be"):
        sample_batch(count)


def test_service_date_is_the_cohort_reference_date() -> None:
    assert reference_date() == date(2026, 11, 1)
    assert {c.service_date for c in sample_batch().checks} == {reference_date()}


def test_expected_answers_hold_on_the_service_date() -> None:
    """Every state but inactive has a period that includes the day we ask about."""
    day = reference_date()
    for m in synthetic_cohort.members():
        start = date.fromisoformat(m["coverage_start"])
        end = date.fromisoformat(m["coverage_end"])
        assert (start <= day < end) == (m["coverage_state"] != "inactive"), m["member_id"]


def test_seed_writes_a_request_without_the_expected_answers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "batch.json"
    assert main(["seed", "--out", str(out)]) == 0
    body = json.loads(out.read_text())
    assert set(body["checks"][0]) == {
        "member_id",
        "given",
        "family",
        "birth_date",
        "payer",
        "service_date",
    }
    assert "coverage_state" not in out.read_text()
    assert BatchRequest.model_validate(body) == sample_batch()
    assert "35 checks" in capsys.readouterr().err


def test_seed_prints_to_standard_output(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["seed", "--count", "2"]) == 0
    assert len(json.loads(capsys.readouterr().out)["checks"]) == 2


def test_seed_reports_a_bad_count(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["seed", "--count", "0"])
    assert "count must be" in capsys.readouterr().err
