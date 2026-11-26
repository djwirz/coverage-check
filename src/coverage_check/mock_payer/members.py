"""What the payer knows: the synthetic-cohort members (ADR 0008)."""

from __future__ import annotations

from datetime import date

import synthetic_cohort

from coverage_check.models import CheckRequest
from coverage_check.protocol import Benefit, CoveragePeriod, EligibilityReply, Plan

# Made up: the cohort has a payer and a period, but no plan or benefits.
BENEFITS = [
    Benefit(service="primary_care_visit", copay_usd=20),
    Benefit(service="specialist_visit", copay_usd=40),
    Benefit(service="generic_drug", copay_usd=10),
]


class Directory:
    def __init__(self) -> None:
        self._members = {m["member_id"]: m for m in synthetic_cohort.members()}

    def answer(self, request: CheckRequest) -> EligibilityReply:
        member = self._members.get(request.member_id)
        if (
            member is None
            or member["coverage_state"] == "missing"
            or date.fromisoformat(member["birth_date"]) != request.birth_date
            or member["payer"] != request.payer
        ):
            return EligibilityReply(result="member_not_found")
        # The cohort only labels these two states; the payer is what makes them happen.
        if member["coverage_state"] == "ambiguous":
            return EligibilityReply(result="multiple_matches")
        period = CoveragePeriod(
            start=date.fromisoformat(member["coverage_start"]),
            end=date.fromisoformat(member["coverage_end"]),
        )
        active = period.start <= request.service_date < period.end
        return EligibilityReply(
            result="active" if active else "inactive",
            plan=Plan(name=f"{member['payer']} synthetic plan"),
            coverage_period=period,
            benefits=BENEFITS if active else [],
        )
