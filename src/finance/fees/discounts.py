"""One student's invoice for a term (spec 5.3). A pure function.

1. Gross lines: the grade's mandatory items (with the class's overrides) plus the
   optional items the student is opted into.
2. For each line, every applicable discount: SIBLING by family position, STAFF_CHILD if
   a guardian is active staff, MANUAL if it names this student and hasn't ended.
3. BEST_SINGLE keeps the largest (ties: STAFF_CHILD, SIBLING, MANUAL, then rule ID).
   ADDITIVE applies them in that order, capped at maxTotalDiscountPercent of the line;
   the one that crosses the cap is trimmed to fit.
4. Each discount is half-up rounded to the increment and never exceeds its line.
5. Each applied discount is its own negative line naming its rule and version.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from finance.fees.model import (
    DISCOUNT_PRIORITY,
    DiscountRule,
    DiscountType,
    FeeError,
    FeeItem,
    FeeSchedule,
    FeeSettings,
    Stacking,
    Student,
)
from finance.fees.rounding import percent_of


class NoScheduleForGrade(FeeError):
    pass


@dataclass(frozen=True)
class InvoiceLine:
    item_code: str
    amount_minor: int  # fee lines positive, discount lines negative
    rule_id: str | None = None
    rule_version: int | None = None
    discount_type: DiscountType | None = None  # stored, never published


@dataclass(frozen=True)
class InvoiceDraft:
    student_id: str
    lines: tuple[InvoiceLine, ...]
    capped_items: tuple[str, ...] = ()  # ADDITIVE lines where the cap trimmed a discount

    @property
    def gross_minor(self) -> int:
        return sum(line.amount_minor for line in self.lines if line.rule_id is None)

    @property
    def discount_minor(self) -> int:
        return -sum(line.amount_minor for line in self.lines if line.rule_id is not None)

    @property
    def net_minor(self) -> int:
        return sum(line.amount_minor for line in self.lines)


def gross_items(student: Student, schedule: FeeSchedule) -> tuple[FeeItem, ...]:
    grade = schedule.grades.get(student.grade_code)
    if grade is None:
        raise NoScheduleForGrade(f"no fees for grade {student.grade_code}")
    return tuple(
        item
        for item in grade.items_for(student.class_code)
        if item.mandatory or item.code in student.optional_items
    )


def _priority(rule: DiscountRule) -> int:
    return DISCOUNT_PRIORITY.index(rule.type)


def _amount(rule: DiscountRule, gross: int, rate: int | None, increment: int) -> int:
    # A fixed discount applies per fee line.
    amount = percent_of(gross, rate, increment) if rate is not None else rule.fixed_minor or 0
    return min(amount, gross)  # never more than the line


def compute_invoice(
    student: Student,
    schedule: FeeSchedule,
    rules: Sequence[DiscountRule],
    *,
    term_code: str,
    position: int,
    staff_child: bool,
    settings: FeeSettings,
) -> InvoiceDraft:
    increment = settings.rounding_increment_minor
    lines: list[InvoiceLine] = []
    capped: list[str] = []
    for item in gross_items(student, schedule):
        lines.append(InvoiceLine(item.code, item.amount_minor))
        candidates: list[tuple[DiscountRule, int]] = []
        for rule in sorted(rules, key=lambda r: (_priority(r), r.rule_id)):
            if item.code not in rule.applies_to:
                continue
            if rule.type is DiscountType.SIBLING:
                rate = rule.sibling_rate(position)
                amount = percent_of(item.amount_minor, rate, increment) if rate else 0
                amount = min(amount, item.amount_minor)
            elif rule.type is DiscountType.STAFF_CHILD:
                amount = (
                    _amount(rule, item.amount_minor, rule.basis_points, increment)
                    if staff_child
                    else 0
                )
            else:  # MANUAL
                applies = rule.student_id == student.student_id and (
                    rule.end_term is None or term_code <= rule.end_term
                )
                amount = (
                    _amount(rule, item.amount_minor, rule.basis_points, increment) if applies else 0
                )
            if amount > 0:
                candidates.append((rule, amount))

        if settings.stacking is Stacking.BEST_SINGLE and candidates:
            # Largest amount; ties go to the earlier type in DISCOUNT_PRIORITY, then rule ID.
            ranked = sorted(candidates, key=lambda c: (-c[1], _priority(c[0]), c[0].rule_id))
            applied = [ranked[0]]
        else:
            cap = min(
                item.amount_minor,
                percent_of(item.amount_minor, settings.max_total_discount_percent * 100, increment),
            )
            applied, remaining = [], cap
            for rule, amount in candidates:  # already in priority order
                take = min(amount, remaining)
                if take < amount:
                    capped.append(item.code)
                if take > 0:
                    applied.append((rule, take))
                    remaining -= take
        for rule, amount in applied:
            lines.append(InvoiceLine(item.code, -amount, rule.rule_id, rule.version, rule.type))
    return InvoiceDraft(student.student_id, tuple(lines), tuple(dict.fromkeys(capped)))
