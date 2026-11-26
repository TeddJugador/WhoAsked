"""Families and sibling positions (spec 5.2 and 5.3).

Default family: students who share a primary-contact guardian; the family ID is that
guardian's ID. A bursar's override (e.g. separated parents who each list a different
primary contact) wins. A student with neither is a family of one, and is reported.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from finance.fees.model import FeeError, Student

OVERRIDE_FAMILY = re.compile(r"^FAM-[A-Z0-9-]{1,40}$")


@dataclass(frozen=True)
class Families:
    family_of: Mapping[str, str]  # student -> family
    missing_primary_contact: tuple[str, ...]  # students in a family of one by default


def resolve_families(
    students: Iterable[Student],
    primary_contact: Mapping[str, str | None],
    overrides: Mapping[str, str],
) -> Families:
    family_of: dict[str, str] = {}
    missing: list[str] = []
    for student in sorted(students, key=lambda s: s.student_id):
        override = overrides.get(student.student_id)
        if override is not None:
            if not OVERRIDE_FAMILY.fullmatch(override):
                raise FeeError("override family IDs look like FAM-SMITH-1")
            family_of[student.student_id] = override
        elif primary := primary_contact.get(student.student_id):
            family_of[student.student_id] = primary
        else:
            family_of[student.student_id] = f"SOLO#{student.student_id}"
            missing.append(student.student_id)
    return Families(family_of=family_of, missing_primary_contact=tuple(missing))


def sibling_positions(
    students: Iterable[Student],
    family_of: Mapping[str, str],
    discounted_gross: Callable[[Student], int],
) -> dict[str, int]:
    """Position 1 pays full price. Within a family: gross of the discounted items
    descending, then enrolment date, then student ID, so discounts land on the cheaper
    children and the order never depends on input order."""
    members: dict[str, list[Student]] = defaultdict(list)
    for student in students:
        members[family_of[student.student_id]].append(student)
    positions: dict[str, int] = {}
    for family in members.values():
        ordered = sorted(
            family, key=lambda s: (-discounted_gross(s), s.enrolment_date, s.student_id)
        )
        for index, student in enumerate(ordered, start=1):
            positions[student.student_id] = index
    return positions
