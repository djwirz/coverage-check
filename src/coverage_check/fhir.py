"""Results as FHIR R4 CoverageEligibilityResponse (invariant 6).

Only the elements this service writes. The models refuse anything else, and check the rules
we rely on: required elements and codes, ces-1, and that contained references resolve.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from coverage_check.models import CheckRequest
from coverage_check.protocol import EligibilityReply

SYSTEMS = "urn:coverage-check:"
EXCEPTION_REASONS = SYSTEMS + "exception-reason"
COVERAGE_CLASS = "http://terminology.hl7.org/CodeSystem/coverage-class"


class Element(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Coding(Element):
    system: str
    code: str


class Concept(Element):
    coding: list[Coding] | None = None
    text: str | None = None


class Identifier(Element):
    system: str
    value: str


class Reference(Element):
    reference: str | None = None
    identifier: Identifier | None = None
    display: str | None = None


class HumanName(Element):
    family: str
    given: list[str]


class Patient(Element):
    resourceType: Literal["Patient"] = "Patient"
    id: str
    name: list[HumanName]
    birthDate: date


class Period(Element):
    start: date
    end: date


class CoverageClass(Element):
    type: Concept
    value: str
    name: str


class Coverage(Element):
    resourceType: Literal["Coverage"] = "Coverage"
    id: str
    status: Literal["active", "cancelled"]
    subscriberId: str
    beneficiary: Reference
    period: Period
    payor: Annotated[list[Reference], Field(min_length=1)]
    class_: Annotated[list[CoverageClass], Field(alias="class")]


class Money(Element):
    value: int
    currency: str


class Benefit(Element):
    type: Concept
    allowedMoney: Money


class Item(Element):
    category: Concept  # ces-1: a category or a billing code, never both
    benefit: list[Benefit]


class Insurance(Element):
    coverage: Reference
    inforce: bool
    item: list[Item] | None = None


class Error(Element):
    code: Concept


class CoverageEligibilityResponse(Element):
    resourceType: Literal["CoverageEligibilityResponse"] = "CoverageEligibilityResponse"
    id: str
    contained: list[Patient | Coverage]
    status: Literal["active"]
    purpose: Annotated[list[Literal["validation", "benefits"]], Field(min_length=1)]
    patient: Reference
    servicedDate: date
    created: datetime
    request: Reference
    outcome: Literal["complete", "error"]
    disposition: str
    insurer: Reference
    insurance: list[Insurance] | None = None
    error: list[Error] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        referenced = {self.patient.reference}
        referenced.update(i.coverage.reference for i in self.insurance or [])
        for resource in self.contained:
            if isinstance(resource, Coverage):
                referenced.add(resource.beneficiary.reference)
        if referenced - {None} != {f"#{r.id}" for r in self.contained}:
            raise ValueError("contained resources and references to them must match")
        if (self.outcome == "error") != bool(self.error):
            raise ValueError("an error outcome has errors, and only it does")
        if (self.outcome == "complete") != bool(self.insurance):
            raise ValueError("a complete outcome says what the insurance is, and only it does")
        return self


def _response(
    check_id: UUID,
    check: CheckRequest,
    created: datetime,
    contained: list[Patient | Coverage],
    **rest: Any,
) -> dict[str, Any]:
    patient = Patient(
        id="patient",
        name=[HumanName(family=check.family, given=check.given)],
        birthDate=check.birth_date,
    )
    response = CoverageEligibilityResponse(
        id=str(check_id),
        contained=[patient, *contained],
        status="active",
        purpose=["validation", "benefits"],
        patient=Reference(reference="#patient"),
        servicedDate=check.service_date,
        created=created,
        request=Reference(identifier=Identifier(system=SYSTEMS + "check", value=str(check_id))),
        insurer=Reference(display=check.payer),
        **rest,
    )
    return response.model_dump(mode="json", by_alias=True, exclude_none=True)


def answered(
    check_id: UUID, check: CheckRequest, reply: EligibilityReply, created: datetime
) -> dict[str, Any]:
    """For a check the payer answered: active or inactive."""
    if reply.plan is None or reply.coverage_period is None:
        raise ValueError("an answer about coverage needs its plan and period")
    active = reply.result == "active"
    plan = CoverageClass(
        type=Concept(coding=[Coding(system=COVERAGE_CLASS, code="plan")]),
        value=reply.plan.name,
        name=reply.plan.name,
    )
    coverage = Coverage.model_validate(
        {
            "id": "coverage",
            "status": "active" if active else "cancelled",
            "subscriberId": check.member_id,
            "beneficiary": Reference(reference="#patient"),
            "period": Period(start=reply.coverage_period.start, end=reply.coverage_period.end),
            "payor": [Reference(display=check.payer)],
            "class": [plan],
        }
    )
    items = [
        Item(
            category=Concept(coding=[Coding(system=SYSTEMS + "service", code=b.service)]),
            benefit=[
                Benefit(
                    type=Concept(text="copay"),
                    allowedMoney=Money(value=b.copay_usd, currency="USD"),
                )
            ],
        )
        for b in reply.benefits
    ]
    return _response(
        check_id,
        check,
        created,
        [coverage],
        outcome="complete",
        disposition="Coverage is active." if active else "Coverage is not active.",
        insurance=[
            Insurance(coverage=Reference(reference="#coverage"), inforce=active, item=items or None)
        ],
    )


def failed(check_id: UUID, check: CheckRequest, reason: str, created: datetime) -> dict[str, Any]:
    """For an exception: there is no answer, and this says why."""
    return _response(
        check_id,
        check,
        created,
        [],
        outcome="error",
        disposition="No answer. A person needs to look at this check.",
        error=[Error(code=Concept(coding=[Coding(system=EXCEPTION_REASONS, code=reason)]))],
    )
