"""Property-based tests for the fee calculation (spec 11, "Fees calculation")."""

from __future__ import annotations

import random
from collections import defaultdict

from hypothesis import given, settings
from hypothesis import strategies as st

from finance.fees.billing import RunPreview, preview_run
from finance.fees.discounts import NoScheduleForGrade, gross_items
from finance.fees.families import resolve_families, sibling_positions
from finance.fees.model import (
    DiscountRule,
    DiscountType,
    FeeItem,
    FeeSchedule,
    FeeSettings,
    GradeFees,
    Stacking,
    Student,
)
from finance.fees.rounding import round_half_up

TERM = "2027-T1"
ITEMS = ("TUITION", "LEVY", "TRANSPORT", "LUNCH")


# --- rounding ---------------------------------------------------------------------------


@given(st.integers(0, 10**12), st.integers(1, 10**6), st.sampled_from([1, 5, 10, 100, 500]))
def test_round_half_up_matches_an_integer_reference(
    numerator: int, denominator: int, increment: int
) -> None:
    # Half-up with integers only: floor((2n + d*i) / (2*d*i)) * i.
    reference = (
        (2 * numerator + denominator * increment) // (2 * denominator * increment) * increment
    )
    assert round_half_up(numerator, denominator, increment) == reference


def test_half_up_examples() -> None:
    assert round_half_up(250, 100, 1) == 3  # 2.5 -> 3 (not banker's 2)
    assert round_half_up(249, 100, 1) == 2
    assert round_half_up(12_345, 1, 100) == 12_300
    assert round_half_up(12_350, 1, 100) == 12_400


# --- generated schools ----------------------------------------------------------------------


@st.composite
def schools(draw: st.DrawFn) -> dict[str, object]:
    grades = draw(
        st.lists(st.sampled_from(["R", "1", "2", "7"]), min_size=1, max_size=3, unique=True)
    )
    grade_fees = {}
    for grade in grades:
        codes = draw(st.lists(st.sampled_from(ITEMS), min_size=1, max_size=4, unique=True))
        items = tuple(
            FeeItem(code, draw(st.integers(1, 5_000_000)), mandatory=code != "LUNCH")
            for code in codes
        )
        overrides = {}
        if draw(st.booleans()):
            overrides = {f"{grade}B": {codes[0]: draw(st.integers(1, 5_000_000))}}
        grade_fees[grade] = GradeFees(grade, items, overrides)
    schedule = FeeSchedule("SCH-001", TERM, 1, "ZAR", grade_fees)

    guardians = [f"GRD-{i}" for i in range(draw(st.integers(1, 4)))]
    students = []
    primary: dict[str, str | None] = {}
    for i in range(draw(st.integers(1, 8))):
        sid = f"STU-2026-{i:05d}"
        grade = draw(st.sampled_from([*grades, "12"]))  # sometimes a grade with no fees
        contact = draw(st.sampled_from([*guardians, None]))
        students.append(
            Student(
                sid,
                grade,
                draw(st.sampled_from([f"{grade}A", f"{grade}B", None])),
                draw(st.sampled_from(["2026-01-15", "2026-02-01", "2027-01-15"])),
                (contact,) if contact else (),
                frozenset(draw(st.lists(st.sampled_from(ITEMS), max_size=2))),
            )
        )
        primary[sid] = contact
    rules = [
        DiscountRule(
            "SIB",
            1,
            DiscountType.SIBLING,
            frozenset(draw(st.lists(st.sampled_from(ITEMS), min_size=1, max_size=2, unique=True))),
            sibling_rates={"2": draw(st.integers(1, 5000)), "3+": draw(st.integers(1, 10000))},
        ),
        DiscountRule(
            "STAFF",
            1,
            DiscountType.STAFF_CHILD,
            frozenset({"TUITION"}),
            basis_points=draw(st.integers(1, 10000)),
        ),
        DiscountRule(
            "BURSARY",
            2,
            DiscountType.MANUAL,
            frozenset({"TUITION", "LEVY"}),
            fixed_minor=draw(st.integers(1, 6_000_000)),
            student_id=students[0].student_id,
        ),
    ]
    fee_settings = FeeSettings(
        rounding_increment_minor=draw(st.sampled_from([1, 5, 100])),
        stacking=draw(st.sampled_from(list(Stacking))),
        max_total_discount_percent=draw(st.integers(0, 100)),
    )
    staff = frozenset(draw(st.lists(st.sampled_from(guardians), max_size=2)))
    return {
        "schedule": schedule,
        "students": students,
        "primary": primary,
        "rules": rules,
        "settings": fee_settings,
        "staff": staff,
    }


def _preview(school: dict[str, object], students: list[Student] | None = None) -> RunPreview:
    roster = students if students is not None else school["students"]
    families = resolve_families(roster, school["primary"], {})  # type: ignore[arg-type]
    return preview_run(
        roster,  # type: ignore[arg-type]
        school["schedule"],  # type: ignore[arg-type]
        school["rules"],  # type: ignore[arg-type]
        families,
        school["staff"],  # type: ignore[arg-type]
        term_code=TERM,
        settings=school["settings"],  # type: ignore[arg-type]
    )


@settings(max_examples=300)
@given(schools())
def test_invoice_invariants(school: dict[str, object]) -> None:
    preview = _preview(school)
    fee_settings: FeeSettings = school["settings"]  # type: ignore[assignment]
    increment = fee_settings.rounding_increment_minor
    for draft in preview.drafts.values():
        # Net is never negative, and the total is the sum of the lines.
        assert draft.net_minor >= 0
        assert draft.net_minor == sum(line.amount_minor for line in draft.lines)
        assert draft.net_minor == draft.gross_minor - draft.discount_minor
        gross_by_item = {
            line.item_code: line.amount_minor for line in draft.lines if line.rule_id is None
        }
        discounts_by_item: dict[str, list[int]] = defaultdict(list)
        for line in draft.lines:
            if line.rule_id is not None:
                assert line.amount_minor < 0  # discounts are negative lines
                discounts_by_item[line.item_code].append(-line.amount_minor)
        for item, amounts in discounts_by_item.items():
            gross = gross_by_item[item]
            assert all(a <= gross for a in amounts)  # a discount never exceeds its line
            assert sum(amounts) <= gross
            if fee_settings.stacking is Stacking.BEST_SINGLE:
                assert len(amounts) == 1
            else:
                cap = min(
                    gross,
                    round_half_up(gross * fee_settings.max_total_discount_percent, 100, increment),
                )
                assert sum(amounts) <= cap
    # Rounding: a percent discount is a multiple of the increment unless capped at the line.
    for draft in preview.drafts.values():
        gross_by_item = {
            line.item_code: line.amount_minor for line in draft.lines if line.rule_id is None
        }
        for line in draft.lines:
            if line.rule_id in ("SIB", "STAFF") and fee_settings.stacking is Stacking.BEST_SINGLE:
                amount = -line.amount_minor
                assert amount % increment == 0 or amount == gross_by_item[line.item_code]


@settings(max_examples=200)
@given(schools())
def test_position_one_never_gets_a_sibling_discount(school: dict[str, object]) -> None:
    preview = _preview(school)
    students: list[Student] = school["students"]  # type: ignore[assignment]
    schedule: FeeSchedule = school["schedule"]  # type: ignore[assignment]
    sibling_items = next(r for r in school["rules"] if r.type is DiscountType.SIBLING).applies_to  # type: ignore[attr-defined]
    families = resolve_families(students, school["primary"], {})  # type: ignore[arg-type]

    def discounted_gross(student: Student) -> int:
        try:
            return sum(
                i.amount_minor for i in gross_items(student, schedule) if i.code in sibling_items
            )
        except NoScheduleForGrade:
            return 0

    positions = sibling_positions(students, families.family_of, discounted_gross)
    firsts = [s for s, p in positions.items() if p == 1]
    assert len(firsts) == len(set(families.family_of.values()))  # one position 1 per family
    for student_id in firsts:
        if student_id in preview.drafts:
            assert not any(line.rule_id == "SIB" for line in preview.drafts[student_id].lines)


@settings(max_examples=200)
@given(schools(), st.randoms(use_true_random=False))
def test_identical_inputs_give_identical_invoices(
    school: dict[str, object], rnd: random.Random
) -> None:
    shuffled = list(school["students"])  # type: ignore[call-overload]
    rnd.shuffle(shuffled)
    first, second = _preview(school), _preview(school, shuffled)
    assert first.drafts == second.drafts  # input order never matters
    assert first.exceptions == second.exceptions
    assert first.discount_by_type == second.discount_by_type
