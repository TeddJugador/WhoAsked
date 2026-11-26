"""What fee calculations work on. Money is integer minor units; rates are basis points."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

GRADE = re.compile(r"^(R|[1-9]|1[0-2])$")
TERM = re.compile(r"^\d{4}-T[1-4]$")
ITEM = re.compile(r"^[A-Z][A-Z0-9_]{0,59}$")


class Stacking(StrEnum):
    BEST_SINGLE = "BEST_SINGLE"
    ADDITIVE = "ADDITIVE"


class DiscountType(StrEnum):
    STAFF_CHILD = "STAFF_CHILD"
    SIBLING = "SIBLING"
    MANUAL = "MANUAL"


# BEST_SINGLE ties, and the ADDITIVE order of application (spec 5.3).
DISCOUNT_PRIORITY = (DiscountType.STAFF_CHILD, DiscountType.SIBLING, DiscountType.MANUAL)


class FeeError(ValueError):
    """Invalid fee data. Messages name fields and codes, never amounts or people."""


@dataclass(frozen=True)
class FeeItem:
    code: str  # TUITION, BUILDING_LEVY, TRANSPORT...
    amount_minor: int
    mandatory: bool = True

    def __post_init__(self) -> None:
        if not ITEM.fullmatch(self.code):
            raise FeeError(f"fee item codes are upper-case: {self.code!r}")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise FeeError(f"{self.code}: amounts are integer minor units")
        if self.amount_minor <= 0:
            raise FeeError(f"{self.code}: amounts are positive")


@dataclass(frozen=True)
class GradeFees:
    grade_code: str
    items: tuple[FeeItem, ...]
    # classCode -> {item code -> amount}: classes in one grade can have different rates.
    class_overrides: Mapping[str, Mapping[str, int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not GRADE.fullmatch(self.grade_code):
            raise FeeError(f"unknown grade {self.grade_code!r}")
        codes = [i.code for i in self.items]
        if len(codes) != len(set(codes)):
            raise FeeError(f"grade {self.grade_code}: duplicate fee items")
        for class_code, overrides in self.class_overrides.items():
            for code, amount in overrides.items():
                if code not in codes:
                    raise FeeError(
                        f"class {class_code}: override for an item not in the grade: {code}"
                    )
                if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
                    raise FeeError(
                        f"class {class_code}: {code} override must be a positive integer"
                    )

    def items_for(self, class_code: str | None) -> tuple[FeeItem, ...]:
        overrides = self.class_overrides.get(class_code or "", {})
        return tuple(
            FeeItem(i.code, overrides.get(i.code, i.amount_minor), i.mandatory) for i in self.items
        )


@dataclass(frozen=True)
class FeeSchedule:
    school_id: str
    term_code: str
    version: int
    currency: str
    grades: Mapping[str, GradeFees]

    def __post_init__(self) -> None:
        if not TERM.fullmatch(self.term_code):
            raise FeeError(f"term codes look like 2027-T1: {self.term_code!r}")
        for code, grade in self.grades.items():
            if code != grade.grade_code:
                raise FeeError(f"grade key {code!r} doesn't match its fees")


@dataclass(frozen=True)
class DiscountRule:
    rule_id: str
    version: int
    type: DiscountType
    applies_to: frozenset[str]  # fee item codes, e.g. {"TUITION"}
    basis_points: int | None = None  # STAFF_CHILD / MANUAL percent
    fixed_minor: int | None = None  # STAFF_CHILD / MANUAL fixed amount, per fee line
    sibling_rates: Mapping[str, int] = field(default_factory=dict)  # {"2": 1000, "4+": 2000}
    student_id: str | None = None  # MANUAL: whose bursary
    end_term: str | None = None  # MANUAL: last term it applies to
    reason: str | None = None  # MANUAL: never in events or logs

    def __post_init__(self) -> None:
        if not self.applies_to:
            raise FeeError(f"rule {self.rule_id}: applies to no fee items")
        if self.type is DiscountType.SIBLING:
            if not self.sibling_rates:
                raise FeeError(f"rule {self.rule_id}: sibling rates are required")
            for key, rate in self.sibling_rates.items():
                if not re.fullmatch(r"[2-9]\+?|[1-9][0-9]\+?", key) or key in ("1", "1+"):
                    raise FeeError(f"rule {self.rule_id}: positions start at 2: {key!r}")
                _check_rate(self.rule_id, rate)
        else:
            if (self.basis_points is None) == (self.fixed_minor is None):
                raise FeeError(f"rule {self.rule_id}: give a percent or a fixed amount, not both")
            if self.basis_points is not None:
                _check_rate(self.rule_id, self.basis_points)
            if self.fixed_minor is not None and self.fixed_minor <= 0:
                raise FeeError(f"rule {self.rule_id}: fixed amounts are positive")
        if self.type is DiscountType.MANUAL and not self.student_id:
            raise FeeError(f"rule {self.rule_id}: a manual discount names its student")

    def sibling_rate(self, position: int) -> int:
        """The rate for a family position (1 = full price). Exact keys win over "N+"."""
        if position < 2:
            return 0
        if str(position) in self.sibling_rates:
            return self.sibling_rates[str(position)]
        open_ended = [
            int(k[:-1]) for k in self.sibling_rates if k.endswith("+") and int(k[:-1]) <= position
        ]
        return self.sibling_rates[f"{max(open_ended)}+"] if open_ended else 0


def _check_rate(rule_id: str, rate: int) -> None:
    if isinstance(rate, bool) or not isinstance(rate, int) or not 0 < rate <= 10_000:
        raise FeeError(f"rule {rule_id}: rates are basis points between 1 and 10000")


@dataclass(frozen=True)
class Student:
    student_id: str
    grade_code: str
    class_code: str | None
    enrolment_date: str  # ISO date
    guardian_ids: tuple[str, ...] = ()
    optional_items: frozenset[str] = frozenset()


@dataclass(frozen=True)
class FeeSettings:
    """The parts of SchoolFinanceConfig fee calculations use (spec 3)."""

    currency: str = "ZAR"
    rounding_increment_minor: int = 1
    stacking: Stacking = Stacking.BEST_SINGLE
    max_total_discount_percent: int = 100
