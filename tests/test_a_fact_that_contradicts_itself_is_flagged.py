"""A fact whose own rules disagree must reach a person marked as contradictory.

WHY THIS FILE EXISTS, and it is a real approved fact rather than a hypothetical. keda's
stored fact carries both of these:

    2.20.x  ->  Kubernetes [1.33, 1.36)      the series stops before 1.36
    2.20.2  ->  Kubernetes [1.23, unbounded) the release inside it never stops

2.20.2 is a member of 2.20.x, so both cannot be true. Nothing flagged it, so it was
approved, and the consequence was visible in the plan rather than in the fact:
`order/moves.py::_series_covers("2.20.x", "1.36")` is False while
`_series_supported_at("1.36")` returns `2.20.2`. The sequencer moved to the series at 1.35
and moved AGAIN at 1.36 -- to the same version -- producing two steps installing 2.20.2.

The plan looked duplicated. The fact was contradictory. `order/dedupe.py` suppresses the
duplicate step; this is the check that stops the contradiction reaching the store unremarked.

WHY THE CHECK IS ON THE RULES AND NOT THE STATEMENTS, which is the subtle part and the
reason a first attempt at this could not fire. The unbounded window is DERIVED from a
floor-only statement ("requires Kubernetes 1.23+"), whose `kind` differs from the series'
`SUPPORTED_RANGE`. `find_contradictions` refuses to compare across kinds -- correctly, a
tested window and a supported window are different facts -- and a floor-only statement does
not claim an unbounded ceiling anyway; it is SILENT about the ceiling. `bounds.py` turns that
silence into a window with no maximum, which does assert coverage of every later Kubernetes
version. So the contradiction exists only in the assembled rules, and only there can it be
seen.

IT IS REPORTED, NOT RAISED. A contradiction routes through `receipt.contradictions`, which
`acceptance.py` already uses to cap confidence and the approval page already renders -- so a
person sees the fact contradicting itself BEFORE approving. Refusing outright would leave the
component FACT_MISSING, researched again next run, contradicting itself again: a loop.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eks_addon_readiness.knowledge import CompatibilityRule, VersionRange
from eks_addon_readiness.models import Citation
from eks_addon_readiness.research.signals import (
    _minor,
    _series_of,
    rule_containment_contradictions,
)

KEDA_DOCS = (
    "https://raw.githubusercontent.com/kedacore/keda-docs/main/content/docs/"
    "2.21/operate/cluster.md"
)


def _cite(url: str = KEDA_DOCS) -> Citation:
    return Citation(
        url=url,
        title="compatibility table in the component's own source",
        source_revision="main",
        retrieved_at=datetime(2026, 9, 15, 9, 12, 19, tzinfo=UTC),
    )


def _rule(spec: str, low: str, high: str | None, url: str = KEDA_DOCS) -> CompatibilityRule:
    return CompatibilityRule(
        component_version_series=spec,
        kubernetes_versions=VersionRange(
            minimum_inclusive=low, maximum_exclusive=high
        ),
        evidence=(_cite(url),),
    )


def test_the_shape_kedas_real_fact_carried_is_flagged() -> None:
    """The live case, transcribed from the approved fact in the store."""
    found = rule_containment_contradictions(
        (_rule("2.20.x", "1.33", "1.36"), _rule("2.20.2", "1.23", None))
    )
    assert len(found) == 1
    assert "2.20.2" in found[0].field
    assert "2.20.x" in found[0].field
    # The reader has to be able to tell WHICH way round it is, so both bounds are stated.
    assert found[0].left_value == "no ceiling"
    assert found[0].right_value == "below 1.36"


def test_a_release_reaching_one_minor_past_its_series_is_flagged() -> None:
    """The quieter half: a ceiling that is stated, and too high.

    Worth its own case because the unbounded shape could be caught by a `None` check alone,
    which would miss this one entirely.
    """
    found = rule_containment_contradictions(
        (_rule("2.20.x", "1.33", "1.36"), _rule("2.20.2", "1.33", "1.37"))
    )
    assert len(found) == 1
    assert found[0].left_value == "below 1.37"


def test_a_release_inside_its_series_window_is_not_flagged() -> None:
    found = rule_containment_contradictions(
        (_rule("2.20.x", "1.33", "1.36"), _rule("2.20.2", "1.33", "1.36"))
    )
    assert found == ()


def test_a_release_with_a_later_floor_is_not_flagged() -> None:
    """A patch published after its series opened is ordinary, not a contradiction.

    Only the CEILING is compared. The series ceiling is a claim about every release in the
    series; the series floor is not -- a 2.20.5 shipping once the cluster is already at 1.34
    says nothing inconsistent.
    """
    found = rule_containment_contradictions(
        (_rule("2.20.x", "1.33", "1.36"), _rule("2.20.2", "1.34", "1.36"))
    )
    assert found == ()


def test_duplicate_identical_series_rules_are_not_flagged_here() -> None:
    """cert-manager's real shape: `1.21.x` twice with the SAME window.

    Harmless, and deliberately not this check's business -- it compares a release against its
    series, and there is no release spec here at all. Pinned so a future widening of this rule
    does not start reporting cert-manager's fact as contradictory.
    """
    found = rule_containment_contradictions(
        (_rule("1.21.x", "1.33", "1.37"), _rule("1.21.x", "1.33", "1.37"))
    )
    assert found == ()


def test_a_release_whose_series_is_absent_is_not_flagged() -> None:
    """No series rule means nothing to contradict.

    A release-keyed fact with no series rules is a supported shape (Stage 6.5 widened the
    spec for exactly that), so this must stay silent rather than treating absence as a
    ceiling of zero.
    """
    found = rule_containment_contradictions((_rule("2.20.2", "1.23", None),))
    assert found == ()


def test_an_unbounded_series_does_not_bound_its_releases() -> None:
    """A series with no ceiling cannot be outlived."""
    found = rule_containment_contradictions(
        (_rule("2.20.x", "1.33", None), _rule("2.20.2", "1.23", None))
    )
    assert found == ()


def test_the_contradiction_cites_both_rules_it_compared() -> None:
    """A contradiction a reader cannot check is not evidence.

    Both citations come off the rules themselves, so the reader can open the two pages that
    disagree rather than being told they disagree.
    """
    other = "https://keda.sh/docs/2.20/operate/cluster/"
    found = rule_containment_contradictions(
        (
            _rule("2.20.x", "1.33", "1.36"),
            _rule("2.20.2", "1.23", None, url=other),
        )
    )
    assert str(found[0].left_citation.url) == other
    assert str(found[0].right_citation.url) == KEDA_DOCS


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("2.20.2", "2.20.x"), ("2.20.x", "2.20.x"), ("1.13.0", "1.13.x")],
)
def test_a_spec_knows_which_series_it_belongs_to(spec: str, expected: str) -> None:
    assert _series_of(spec) == expected


def test_minors_compare_as_numbers_and_not_as_text() -> None:
    """`"1.9" > "1.36"` as text and False as versions.

    Pinned because getting this wrong reports the contradiction BACKWARDS -- a ceiling of 1.9
    would read as above a ceiling of 1.36 -- rather than failing visibly.
    """
    assert _minor("1.9") < _minor("1.36")
    assert _minor("1.36") < _minor("1.40")
    assert sorted(["1.9", "1.36", "1.10"], key=_minor) == ["1.9", "1.10", "1.36"]
