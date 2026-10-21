"""A plan never asks twice for the same install, and never collapses a real multi-hop.

WHAT WENT WRONG, AND WHY NO TEST SAW IT. For a cluster targeting
Kubernetes 1.36 the plan carried keda twice, the second step installing the version the
first already installs:

    s3  keda  2.17.x -> install 2.20.2  at Kubernetes 1.35
    s4  keda  2.20.x -> install 2.20.2  at Kubernetes 1.35

Nothing in the suite could have caught it, because every existing Stage 5 test asserts on a
plan built from a fact whose series each appear in exactly ONE `CompatibilityRule`. The
defect needs a fact whose series appears in two -- the shape a compatibility matrix scraped
one row per Kubernetes minor produces -- so the fixture below authors one deliberately, and
`test_the_sequencer_still_emits_the_duplicate` pins that the fixture reproduces the live
pair rather than merely being a plausible input.

THE TWO CASES ARE ASSERTED TOGETHER ON PURPOSE. `cert-manager` in that same live plan has
two legitimate steps -- 1.13.x -> 1.19.6, then 1.19.x -> 1.21.2 -- because they install
different versions. A rule that counted steps per component would have deleted the multi-hop
GAP-2 and `moves.plan_moves` exist to express, so the fix is only correct if the second half
of this file passes at the same time as the first.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eks_addon_readiness.knowledge import (
    Citation,
    CompatibilityRule,
    ComponentKnowledge,
    Confidence,
    ReleaseRecord,
    Reversibility,
    UpgradeStrategy,
    VerificationCheck,
    VersionRange,
)
from eks_addon_readiness.models import ClusterReference
from eks_addon_readiness.order.dedupe import without_redundant_steps
from eks_addon_readiness.order.moves import plan_moves
from eks_addon_readiness.order.plan import PlanStep, StepKind, UpgradePlan

_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_CITE = (
    Citation(
        url="https://example.test/compatibility",
        title="official compatibility matrix",
        retrieved_at=_NOW,
    ),
)
_D = {name: "sha256:" + char * 64 for name, char in zip("abcde", "abcde", strict=True)}
_CLUSTER = ClusterReference(name="fixture-cluster", region="us-east-1")


def _rule(spec: str, minimum: str, maximum: str) -> CompatibilityRule:
    return CompatibilityRule(
        component_version_series=spec,
        kubernetes_versions=VersionRange(
            minimum_inclusive=minimum, maximum_exclusive=maximum
        ),
        evidence=_CITE,
    )


def _knowledge(
    canonical_id: str, rules: tuple[CompatibilityRule, ...], versions: tuple[str, ...]
) -> ComponentKnowledge:
    return ComponentKnowledge(
        knowledge_base_revision="rev-1",
        canonical_id=canonical_id,
        canonical_repository=f"https://example.test/{canonical_id}",
        official_documentation=(f"https://example.test/{canonical_id}/docs",),
        compatibility_rules=rules,
        releases=tuple(
            ReleaseRecord(
                version=version,
                released_at=_NOW,
                source_revision="rev",
                evidence=_CITE,
            )
            for version in versions
        ),
        verification_checks=(
            VerificationCheck(
                check_id="pods.ready", description="pods are ready", evidence=_CITE
            ),
        ),
        default_upgrade_strategy=UpgradeStrategy.IN_PLACE,
        refreshed_at=_NOW,
        evidence_confidence=Confidence.MEDIUM,
    )


# The live keda shape, reduced to the smallest fact that produces it. `2.20.x` is described
# by TWO rules, one per Kubernetes minor, which is what makes `moves._series_covers` (first
# matching rule only) and `moves._series_supported_at` (every rule) disagree about whether
# 2.20.x is supported at 1.36.
_ONE_HOP_REACHES_IT = _knowledge(
    "reached-in-one-hop",
    (
        _rule("2.17.x", "1.31", "1.35"),
        _rule("2.20.x", "1.31", "1.36"),
        _rule("2.20.x", "1.36", "1.37"),
    ),
    ("2.17.1", "2.20.2"),
)

# The live cert-manager shape: no single release spans 1.35 through 1.36 from where 1.13.x
# leaves off, so the component has to stop on the way and TWO DIFFERENT versions are
# installed. The overlapping windows are what make the sequencer pick the lowest series that
# clears the hop it is moving at, then move again -- exactly what `moves.plan_moves` is for,
# and what a rule counting steps per component would destroy.
_GENUINE_MULTI_HOP = _knowledge(
    "needs-two-hops",
    (
        _rule("1.13.x", "1.31", "1.35"),
        _rule("1.19.x", "1.34", "1.36"),
        _rule("1.21.x", "1.35", "1.38"),
    ),
    ("1.13.7", "1.19.6", "1.21.2"),
)


def _plan(*steps: PlanStep) -> UpgradePlan:
    return UpgradePlan(
        cluster=_CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        steps=steps,
        snapshot_digest=_D["a"],
        report_digest=_D["b"],
        ledger_digest=_D["c"],
        assessment_digest=_D["d"],
        store_digest=_D["e"],
        ruleset_digest=_D["e"],
    )


def _move(
    order: int,
    component: str,
    from_series: str,
    to_series: str,
    recommended_version: str | None,
    *,
    unordered_with: tuple[int, ...] = (),
) -> PlanStep:
    return PlanStep(
        order=order,
        kind=StepKind.COMPONENT_MOVE,
        description=f"Move {component} from {from_series} to {to_series}.",
        component=component,
        from_series=from_series,
        to_series=to_series,
        recommended_version=recommended_version,
        at_kubernetes="1.35",
        reversibility=Reversibility.REVERSIBLE,
        reversibility_reason="fixture",
        unordered_with=unordered_with,
    )


def _steps_for(knowledge: ComponentKnowledge, installed: str) -> list[PlanStep]:
    """The plan steps one component's moves become, numbered from 1.

    Built through `plan_moves` rather than hand-written, so the input to the rule under test
    is what the sequencer really emits for this fact and not a guess about it.
    """
    series = ".".join(installed.split(".")[:2]) + ".x"
    moves = plan_moves(
        knowledge, series, ("1.35", "1.36"), installed_version=installed
    ).moves
    return [
        _move(
            order,
            knowledge.canonical_id,
            move.from_series or series,
            move.to_series,
            move.recommended_version,
            unordered_with=tuple(o for o in range(1, len(moves) + 1) if o != order),
        )
        for order, move in enumerate(moves, start=1)
    ]


# ---------------------------------------------------------------------------
# The fixture reproduces the defect. Asserted first, because everything below is
# vacuous if the sequencer no longer emits the duplicate this rule removes.
# ---------------------------------------------------------------------------


def test_the_sequencer_still_emits_the_duplicate() -> None:
    """`moves` is digest-frozen, so the duplicate is still produced upstream.

    If this ever fails, the frozen sequencer changed and the rule under test has become dead
    code -- which is a thing to find out here rather than by noticing the module is never hit.
    """
    moves = plan_moves(
        _ONE_HOP_REACHES_IT, "2.17.x", ("1.35", "1.36"), installed_version="2.17.1"
    ).moves
    assert [(m.from_series, m.to_series, m.recommended_version) for m in moves] == [
        ("2.17.x", "2.20.x", "2.20.2"),
        ("2.20.x", "2.20.x", "2.20.2"),
    ], "the fixture no longer reproduces the live keda pair, so this file proves nothing"


# ---------------------------------------------------------------------------
# THE DEFECT: a recommendation reached in one hop is one step, not two.
# ---------------------------------------------------------------------------


def test_a_recommendation_reached_in_one_hop_is_exactly_one_step() -> None:
    plan = without_redundant_steps(_plan(*_steps_for(_ONE_HOP_REACHES_IT, "2.17.1")))
    steps = [s for s in plan.steps if s.component == "reached-in-one-hop"]
    assert len(steps) == 1, (
        "a component whose recommendation is reached in one hop must produce one step; "
        f"got {[(s.from_series, s.recommended_version) for s in steps]}"
    )
    assert steps[0].from_series == "2.17.x"
    assert steps[0].recommended_version == "2.20.2"


def test_the_surviving_steps_are_renumbered_and_the_hints_follow() -> None:
    """A plan numbered 1, 2, 4 reads as a step that went missing.

    `unordered_with` is remapped with the renumbering, and a reference to a dropped step is
    dropped rather than remapped -- there is nothing left to be unordered against.
    """
    plan = without_redundant_steps(
        _plan(
            _move(1, "keda", "2.17.x", "2.20.x", "2.20.2", unordered_with=(2, 3)),
            _move(2, "keda", "2.20.x", "2.20.x", "2.20.2", unordered_with=(1, 3)),
            _move(3, "other", "1.0.x", "1.1.x", "1.1.4", unordered_with=(1, 2)),
        )
    )
    assert [s.order for s in plan.steps] == [1, 2]
    assert [s.component for s in plan.steps] == ["keda", "other"]
    assert plan.steps[0].unordered_with == (2,)
    assert plan.steps[1].unordered_with == (1,)


# ---------------------------------------------------------------------------
# THE CASE THAT MUST SURVIVE: a real multi-hop keeps both of its steps.
# ---------------------------------------------------------------------------


def test_a_genuine_multi_hop_still_produces_two_steps() -> None:
    plan = without_redundant_steps(_plan(*_steps_for(_GENUINE_MULTI_HOP, "1.13.7")))
    steps = [s for s in plan.steps if s.component == "needs-two-hops"]
    assert [s.recommended_version for s in steps] == ["1.19.6", "1.21.2"], (
        "a component that must stop on the way keeps a step per version it installs; "
        "counting steps per component instead of comparing versions would delete one"
    )
    assert [s.order for s in steps] == [1, 2]


def test_a_plan_with_nothing_redundant_is_returned_unchanged() -> None:
    """Identity, not an equal copy: an untouched plan keeps the digest `interleave` gave it."""
    plan = _plan(*_steps_for(_GENUINE_MULTI_HOP, "1.13.7"))
    assert without_redundant_steps(plan) is plan


# ---------------------------------------------------------------------------
# The three conditions the rule turns on, each one asserted rather than assumed.
# ---------------------------------------------------------------------------


def test_two_components_installing_the_same_version_both_survive() -> None:
    """Same version number, different components. A coincidence of numbering, not a repeat."""
    plan = without_redundant_steps(
        _plan(
            _move(1, "alpha", "1.0.x", "1.2.x", "1.2.3"),
            _move(2, "beta", "1.0.x", "1.2.x", "1.2.3"),
        )
    )
    assert [s.component for s in plan.steps] == ["alpha", "beta"]


def test_two_steps_that_name_no_version_to_install_both_survive() -> None:
    """`recommended_version` is None when the fact records no release to name in the series.

    Comparing those two Nones as equal would drop the second step of a real multi-hop over a
    value that was never a version -- the one way this rule could delete something true.
    """
    plan = without_redundant_steps(
        _plan(
            _move(1, "alpha", "1.0.x", "1.2.x", None),
            _move(2, "alpha", "1.2.x", "1.4.x", None),
        )
    )
    assert [s.to_series for s in plan.steps] == ["1.2.x", "1.4.x"]


def test_a_cluster_hop_is_never_dropped() -> None:
    """A hop installs no component version, so it can never be a repeat of anything."""
    plan = without_redundant_steps(
        _plan(
            _move(1, "alpha", "1.0.x", "1.2.x", "1.2.3"),
            PlanStep(
                order=2,
                kind=StepKind.CLUSTER_UPGRADE,
                description="Upgrade the cluster from Kubernetes 1.35 to 1.36.",
                from_kubernetes="1.35",
                to_kubernetes="1.36",
            ),
            PlanStep(
                order=3,
                kind=StepKind.CLUSTER_UPGRADE,
                description="Upgrade the cluster from Kubernetes 1.36 to 1.37.",
                from_kubernetes="1.36",
                to_kubernetes="1.37",
            ),
        )
    )
    assert [s.kind for s in plan.steps] == [
        StepKind.COMPONENT_MOVE,
        StepKind.CLUSTER_UPGRADE,
        StepKind.CLUSTER_UPGRADE,
    ]


@pytest.mark.parametrize(
    ("second_version", "expected_steps"),
    [
        pytest.param("2.20.2", 1, id="same-install-dropped"),
        pytest.param("2.21.0", 2, id="different-install-kept"),
    ],
)
def test_the_rule_turns_on_the_version_installed_and_nothing_else(
    second_version: str, expected_steps: int
) -> None:
    """Two steps identical but for the version they install: one is a repeat, one is not."""
    plan = without_redundant_steps(
        _plan(
            _move(1, "keda", "2.17.x", "2.20.x", "2.20.2"),
            _move(2, "keda", "2.20.x", "2.20.x", second_version),
        )
    )
    assert len(plan.steps) == expected_steps
