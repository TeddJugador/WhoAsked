"""A billing run's preview (spec 5.4): one draft invoice per active student, totals,
discount totals by type, and the exceptions a bursar should look at before issuing.
Pure: the billing worker loads the inputs, calls this, and stores the result."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from finance.fees.discounts import InvoiceDraft, NoScheduleForGrade, compute_invoice, gross_items
from finance.fees.families import Families, sibling_positions
from finance.fees.model import (
    DiscountRule,
    DiscountType,
    FeeError,
    FeeSchedule,
    FeeSettings,
    Student,
)


class ExceptionCode(StrEnum):
    NO_SCHEDULE_FOR_GRADE = "NO_SCHEDULE_FOR_GRADE"
    MISSING_PRIMARY_CONTACT = "MISSING_PRIMARY_CONTACT"
    DISCOUNT_CAPPED = "DISCOUNT_CAPPED"


@dataclass(frozen=True)
class RunPreview:
    drafts: dict[str, InvoiceDraft]
    exceptions: tuple[tuple[str, ExceptionCode], ...]  # (student, code)
    discount_by_type: dict[str, int] = field(default_factory=dict)

    @property
    def gross_minor(self) -> int:
        return sum(d.gross_minor for d in self.drafts.values())

    @property
    def discount_minor(self) -> int:
        return sum(d.discount_minor for d in self.drafts.values())

    @property
    def net_minor(self) -> int:
        return sum(d.net_minor for d in self.drafts.values())


def preview_run(
    students: Sequence[Student],
    schedule: FeeSchedule,
    rules: Sequence[DiscountRule],
    families: Families,
    staff_guardian_ids: frozenset[str],
    *,
    term_code: str,
    settings: FeeSettings,
) -> RunPreview:
    if schedule.term_code != term_code:
        raise FeeError("the schedule is for a different term")
    if schedule.currency != settings.currency:
        raise FeeError("the schedule's currency differs from the school's")
    sibling_rules = [r for r in rules if r.type is DiscountType.SIBLING]
    if len(sibling_rules) > 1:
        raise FeeError("a school has at most one sibling discount rule")
    sibling_items = sibling_rules[0].applies_to if sibling_rules else frozenset()

    def discounted_gross(student: Student) -> int:
        try:
            return sum(
                i.amount_minor for i in gross_items(student, schedule) if i.code in sibling_items
            )
        except NoScheduleForGrade:
            return 0

    # Every active student counts for positions, a staff child included (spec 5.3).
    positions = sibling_positions(students, families.family_of, discounted_gross)
    drafts: dict[str, InvoiceDraft] = {}
    exceptions: list[tuple[str, ExceptionCode]] = [
        (s, ExceptionCode.MISSING_PRIMARY_CONTACT) for s in families.missing_primary_contact
    ]
    by_type: dict[str, int] = {}
    for student in sorted(students, key=lambda s: s.student_id):
        try:
            draft = compute_invoice(
                student,
                schedule,
                rules,
                term_code=term_code,
                position=positions[student.student_id],
                staff_child=any(g in staff_guardian_ids for g in student.guardian_ids),
                settings=settings,
            )
        except NoScheduleForGrade:
            exceptions.append((student.student_id, ExceptionCode.NO_SCHEDULE_FOR_GRADE))
            continue
        drafts[student.student_id] = draft
        if draft.capped_items:
            exceptions.append((student.student_id, ExceptionCode.DISCOUNT_CAPPED))
        for line in draft.lines:
            if line.discount_type is not None:
                by_type[line.discount_type.value] = (
                    by_type.get(line.discount_type.value, 0) - line.amount_minor
                )
    return RunPreview(drafts=drafts, exceptions=tuple(sorted(exceptions)), discount_by_type=by_type)
