"""Fee scenarios with hand-worked numbers (spec 11, "Fees scenarios").

The school: Grade 4 tuition R12,000 and Grade 2 tuition R10,000, each with a R1,000
levy; lunch is optional. Discounts apply to tuition only: siblings 10% (2nd child) and
15% (3rd and later); staff children 50%.
"""

from __future__ import annotations

from typing import Any

import pytest

from finance.fees.billing import ExceptionCode, RunPreview, preview_run
from finance.fees.families import resolve_families
from finance.fees.model import (
    DiscountRule,
    DiscountType,
    FeeError,
    FeeItem,
    FeeSchedule,
    FeeSettings,
    GradeFees,
    Stacking,
    Student,
)

T1, T2 = "2027-T1", "2027-T2"


def _schedule(term: str = T1, overrides: dict[str, dict[str, int]] | None = None) -> FeeSchedule:
    return FeeSchedule(
        "SCH-001",
        term,
        1,
        "ZAR",
        {
            "4": GradeFees(
                "4",
                (
                    FeeItem("TUITION", 1_200_000),
                    FeeItem("LEVY", 100_000),
                    FeeItem("LUNCH", 150_000, mandatory=False),
                ),
                overrides or {},
            ),
            "2": GradeFees("2", (FeeItem("TUITION", 1_000_000), FeeItem("LEVY", 100_000))),
        },
    )


SIBLING = DiscountRule(
    "RULE-SIB",
    3,
    DiscountType.SIBLING,
    frozenset({"TUITION"}),
    sibling_rates={"2": 1000, "3+": 1500},
)
STAFF = DiscountRule(
    "RULE-STAFF", 1, DiscountType.STAFF_CHILD, frozenset({"TUITION"}), basis_points=5000
)

ANN = Student("STU-2025-00001", "4", "4A", "2025-01-15", ("GRD-1",))
BEN = Student("STU-2026-00002", "2", "2A", "2026-01-15", ("GRD-1",))


def _run(
    students: list[Student],
    *,
    stacking: Stacking = Stacking.BEST_SINGLE,
    cap: int = 100,
    staff: frozenset[str] = frozenset({"GRD-1"}),
    rules: list[DiscountRule] | None = None,
    primary: dict[str, str | None] | None = None,
    overrides: dict[str, str] | None = None,
    term: str = T1,
    schedule: FeeSchedule | None = None,
) -> RunPreview:
    contacts = (
        primary
        if primary is not None
        else {s.student_id: s.guardian_ids[0] if s.guardian_ids else None for s in students}
    )
    return preview_run(
        students,
        schedule or _schedule(term),
        rules if rules is not None else [SIBLING, STAFF],
        resolve_families(students, contacts, overrides or {}),
        staff,
        term_code=term,
        settings=FeeSettings(stacking=stacking, max_total_discount_percent=cap),
    )


def _discounts(preview: RunPreview, student: Student) -> list[tuple[str, int, str | None]]:
    return [
        (line.item_code, line.amount_minor, line.rule_id)
        for line in preview.drafts[student.student_id].lines
        if line.rule_id is not None
    ]


# --- two siblings, one family on staff, in each stacking mode ------------------------------


def test_best_single_staff_child_beats_sibling() -> None:
    preview = _run([ANN, BEN])
    # Ann: position 1 (higher tuition), staff 50% of R12,000.
    assert _discounts(preview, ANN) == [("TUITION", -600_000, "RULE-STAFF")]
    assert preview.drafts[ANN.student_id].net_minor == 1_300_000 - 600_000
    # Ben: staff R5,000 beats sibling R1,000; only one discount per line.
    assert _discounts(preview, BEN) == [("TUITION", -500_000, "RULE-STAFF")]
    assert preview.drafts[BEN.student_id].net_minor == 1_100_000 - 500_000
    assert preview.discount_by_type == {"STAFF_CHILD": 1_100_000}


def test_additive_stacks_up_to_the_cap() -> None:
    preview = _run([ANN, BEN], stacking=Stacking.ADDITIVE, cap=55)
    assert _discounts(preview, ANN) == [("TUITION", -600_000, "RULE-STAFF")]
    # Ben: staff R5,000 + sibling R1,000 = R6,000 > 55% cap (R5,500): sibling trimmed to R500.
    assert _discounts(preview, BEN) == [
        ("TUITION", -500_000, "RULE-STAFF"),
        ("TUITION", -50_000, "RULE-SIB"),
    ]
    assert preview.drafts[BEN.student_id].net_minor == 550_000
    assert (BEN.student_id, ExceptionCode.DISCOUNT_CAPPED) in preview.exceptions


def test_additive_without_a_binding_cap() -> None:
    preview = _run([ANN, BEN], stacking=Stacking.ADDITIVE, cap=100)
    assert _discounts(preview, BEN) == [
        ("TUITION", -500_000, "RULE-STAFF"),
        ("TUITION", -100_000, "RULE-SIB"),
    ]
    assert preview.exceptions == ()


def test_without_staff_only_the_younger_sibling_is_discounted() -> None:
    preview = _run([ANN, BEN], staff=frozenset())
    assert _discounts(preview, ANN) == []  # position 1 pays full price
    assert _discounts(preview, BEN) == [("TUITION", -100_000, "RULE-SIB")]


def test_third_and_later_children() -> None:
    cleo = Student("STU-2027-00003", "2", "2A", "2027-01-15", ("GRD-1",))
    dan = Student("STU-2027-00004", "2", "2A", "2027-01-16", ("GRD-1",))
    preview = _run([ANN, BEN, cleo, dan], staff=frozenset())
    # Ben, Cleo and Dan pay the same tuition: enrolment date breaks the tie.
    assert _discounts(preview, BEN) == [("TUITION", -100_000, "RULE-SIB")]  # 2nd: 10%
    assert _discounts(preview, cleo) == [("TUITION", -150_000, "RULE-SIB")]  # 3rd: 15%
    assert _discounts(preview, dan) == [("TUITION", -150_000, "RULE-SIB")]  # 4th: "3+"


def test_equal_discounts_tie_break_to_staff_child() -> None:
    staff_10 = DiscountRule(
        "RULE-STAFF", 1, DiscountType.STAFF_CHILD, frozenset({"TUITION"}), basis_points=1000
    )
    preview = _run([ANN, BEN], rules=[SIBLING, staff_10])
    assert _discounts(preview, BEN) == [("TUITION", -100_000, "RULE-STAFF")]


# --- class overrides, optional items, staff leaving, families ---------------------------------


def test_class_override() -> None:
    ann_4b = Student(ANN.student_id, "4", "4B", ANN.enrolment_date, ANN.guardian_ids)
    preview = _run(
        [ann_4b], staff=frozenset(), schedule=_schedule(overrides={"4B": {"TUITION": 1_300_000}})
    )
    assert preview.drafts[ann_4b.student_id].gross_minor == 1_300_000 + 100_000


def test_optional_items_only_when_opted_in() -> None:
    ann_lunch = Student(
        ANN.student_id, "4", "4A", ANN.enrolment_date, ANN.guardian_ids, frozenset({"LUNCH"})
    )
    assert _run([ANN], staff=frozenset()).drafts[ANN.student_id].gross_minor == 1_300_000
    assert _run([ann_lunch], staff=frozenset()).drafts[ANN.student_id].gross_minor == 1_450_000


def test_staff_discount_ends_when_the_staff_member_leaves() -> None:
    """The staff set comes from active pay profiles at each run: after the end date the
    guardian isn't in it, so the next run has no staff-child discount."""
    before, after = _run([ANN, BEN]), _run([ANN, BEN], staff=frozenset())
    assert ("TUITION", -600_000, "RULE-STAFF") in _discounts(before, ANN)
    assert _discounts(after, ANN) == []
    assert _discounts(after, BEN) == [("TUITION", -100_000, "RULE-SIB")]


def test_family_override_groups_separately_listed_children() -> None:
    """Separated parents: each child lists a different primary contact, so by default
    they're two families and neither gets a sibling discount. The bursar's override wins."""
    cleo = Student("STU-2026-00003", "2", "2A", "2026-01-15", ("GRD-2",))
    default = _run([ANN, cleo], staff=frozenset())
    assert _discounts(default, cleo) == []
    grouped = _run(
        [ANN, cleo],
        staff=frozenset(),
        overrides={ANN.student_id: "FAM-SMITH", cleo.student_id: "FAM-SMITH"},
    )
    assert _discounts(grouped, cleo) == [("TUITION", -100_000, "RULE-SIB")]


def test_bursary_ends_after_its_end_term() -> None:
    bursary = DiscountRule(
        "RULE-B1",
        1,
        DiscountType.MANUAL,
        frozenset({"TUITION"}),
        basis_points=2500,
        student_id=ANN.student_id,
        end_term=T1,
        reason="bursary",
    )
    assert _discounts(_run([ANN], staff=frozenset(), rules=[bursary]), ANN) == [
        ("TUITION", -300_000, "RULE-B1")
    ]
    assert _discounts(_run([ANN], staff=frozenset(), rules=[bursary], term=T2), ANN) == []


def test_fixed_discounts_never_exceed_the_line() -> None:
    big = DiscountRule(
        "RULE-B2",
        1,
        DiscountType.MANUAL,
        frozenset({"LEVY"}),
        fixed_minor=999_999,
        student_id=ANN.student_id,
    )
    assert _discounts(_run([ANN], staff=frozenset(), rules=[big]), ANN) == [
        ("LEVY", -100_000, "RULE-B2")
    ]


# --- exceptions and validation -------------------------------------------------------------------


def test_exceptions_for_the_bursar() -> None:
    orphan = Student("STU-2026-00009", "2", "2A", "2026-01-15", ())
    senior = Student("STU-2026-00010", "12", "12A", "2026-01-15", ("GRD-3",))
    preview = _run([ANN, orphan, senior], staff=frozenset())
    assert set(preview.exceptions) == {
        (orphan.student_id, ExceptionCode.MISSING_PRIMARY_CONTACT),
        (senior.student_id, ExceptionCode.NO_SCHEDULE_FOR_GRADE),
    }
    assert senior.student_id not in preview.drafts  # no invoice without a schedule
    assert orphan.student_id in preview.drafts  # billed, alone in their family


def test_the_preview_totals() -> None:
    preview = _run([ANN, BEN])
    assert (preview.gross_minor, preview.discount_minor, preview.net_minor) == (
        2_400_000,
        1_100_000,
        1_300_000,
    )


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (
            lambda: GradeFees("4", (FeeItem("TUITION", 1),), {"4B": {"LUNCH": 5}}),
            "not in the grade",
        ),
        (lambda: GradeFees("4", (FeeItem("TUITION", 1), FeeItem("TUITION", 2))), "duplicate"),
        (lambda: GradeFees("13", (FeeItem("TUITION", 1),)), "unknown grade"),
        (lambda: FeeItem("tuition", 1), "upper-case"),
        (lambda: FeeItem("TUITION", 0), "positive"),
        (lambda: FeeSchedule("SCH-001", "2027-1", 1, "ZAR", {}), "2027-T1"),
        (
            lambda: DiscountRule(
                "R", 1, DiscountType.SIBLING, frozenset({"TUITION"}), sibling_rates={"1": 500}
            ),
            "start at 2",
        ),
        (
            lambda: DiscountRule(
                "R",
                1,
                DiscountType.STAFF_CHILD,
                frozenset({"TUITION"}),
                basis_points=5,
                fixed_minor=5,
            ),
            "not both",
        ),
        (
            lambda: DiscountRule(
                "R", 1, DiscountType.MANUAL, frozenset({"TUITION"}), basis_points=5
            ),
            "names its student",
        ),
        (
            lambda: DiscountRule(
                "R", 1, DiscountType.STAFF_CHILD, frozenset({"TUITION"}), basis_points=10_001
            ),
            "basis points",
        ),
    ],
)
def test_invalid_fee_data_is_refused(build: Any, message: str) -> None:
    with pytest.raises(FeeError, match=message):
        build()


def test_one_sibling_rule_per_school() -> None:
    second = DiscountRule(
        "RULE-SIB2", 1, DiscountType.SIBLING, frozenset({"LEVY"}), sibling_rates={"2": 500}
    )
    with pytest.raises(FeeError, match="at most one sibling"):
        _run([ANN], rules=[SIBLING, second])
