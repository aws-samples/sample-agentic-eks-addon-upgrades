"""A plan never asks an operator to install a version at or behind the one running.

WHAT WENT WRONG, AND WHY NO TEST SAW IT. `PlanStep.recommended_version` is the newest
release THE STORED FACT RECORDS in the series the step moves to
(`order/moves.py::_concrete_version_for`), and it is never compared against what is
installed. So a fact whose catalogue is thinner than the cluster names a version the cluster
is already past. Reproduced against the keda fact approved during the 2026-09-20 run of
`my-eks-cluster`: that fact
records exactly one release in 2.20.x, `2.20.0`, so for a cluster running `2.20.2` targeting
Kubernetes 1.36 the sequencer emits

    keda  2.20.x -> 2.20.x  install 2.20.0  at Kubernetes 1.35

a required `COMPONENT_MOVE` installing 2.20.0 over 2.20.2. `order/dedupe.py` keeps it,
correctly by its own rule -- no EARLIER step installs 2.20.0, so it is not a repeat. It
reaches the approval page beside a live Approve button, and an executor that honours it moves
the cluster backwards.

Nothing in the suite could have caught it. The Stage 5 tests assert on plans whose every
recommendation is ahead of the installed version, and the one "strictly ahead of installed"
rule that exists -- `view/build.py::_further_than_installed` -- is only ever reached from
`plan.unsequenceable`, never from `plan.steps`. So the fixtures below author the two shapes
deliberately, and `test_the_sequencer_still_emits_the_backward_step` pins that the fixture
reproduces the live step rather than merely being a plausible input.

THE CASES ARE ASSERTED TOGETHER ON PURPOSE. A rule that dropped every step whose version is
not ahead of the installed one would also delete a compatibility-forced downgrade across a
series boundary, and `cert-manager`'s genuine multi-hop in that same live plan must survive
untouched. The fix is only correct if the whole file passes at once.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eks_addon_readiness.decide.assessment import (
    ComponentDecision,
    HopDecision,
    ReadinessAssessment,
)
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
from eks_addon_readiness.models import (
    ClusterReference,
    CompatibilityVerdict,
    KubernetesHop,
)
from eks_addon_readiness.order.floor import (
    installed_versions,
    without_backward_steps,
)
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
_HOPS = ("1.35", "1.36")


def _rule(spec: str, minimum: str, maximum: str | None) -> CompatibilityRule:
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


# THE LIVE KEDA FACT, REDUCED TO THE SMALLEST SHAPE THAT PRODUCES THE BACKWARD STEP, and both
# halves come from the approved fact rather than from convenience:
#
#   * `2.20.x` is described TWICE with windows that cannot both be true -- the compatibility
#     table's `[1.33, 1.36)` and the install prose's `[1.30, unbounded)`. That is what makes
#     `moves._series_covers` (first matching rule only) say 2.20.x is not supported at 1.36
#     while `moves._series_supported_at` (every rule) lists it as supported there, so the
#     sequencer forces a move to the series the component is already in.
#   * the catalogue records ONE release in 2.20.x, `2.20.0`, because the real fact's releases
#     were harvested one per documentation page. A cluster on `2.20.2` is therefore past the
#     newest release the fact can name.
_THIN_CATALOGUE = _knowledge(
    "thin-catalogue",
    (
        _rule("2.17.x", "1.30", "1.33"),
        _rule("2.20.x", "1.33", "1.36"),
        _rule("2.20.x", "1.30", None),
    ),
    ("2.17.3", "2.19.0", "2.20.0", "2.21.0"),
)

# The live cert-manager shape: no single release spans 1.35 through 1.36 from where 1.13.x
# leaves off, so two DIFFERENT and strictly increasing versions are installed. Nothing here
# is at or behind the installed version, so the rule under test must not touch it.
_GENUINE_MULTI_HOP = _knowledge(
    "needs-two-hops",
    (
        _rule("1.13.x", "1.31", "1.35"),
        _rule("1.19.x", "1.34", "1.36"),
        _rule("1.21.x", "1.35", "1.38"),
    ),
    ("1.13.7", "1.19.6", "1.21.2"),
)


def _decision(
    component: str, installed_version: str | None, installed_series: str | None
) -> ComponentDecision:
    return ComponentDecision(
        workload_ref=f"HelmRelease/{component}/{component}",
        identity=component,
        installed_version=installed_version,
        installed_series=installed_series,
        assessment_priority="high",
        fact_status="FACT_AVAILABLE",
        knowledge_base_revision="rev-1",
        hops=(
            HopDecision(
                hop=KubernetesHop(current_version="1.35", target_version="1.36"),
                verdict=CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE,
                valid_targets=("2.20.x",),
                reason="fixture",
                decided_from="stored-fact",
            ),
        ),
    )


def _assessment(*decisions: ComponentDecision) -> ReadinessAssessment:
    return ReadinessAssessment(
        cluster=_CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        hops=(KubernetesHop(current_version="1.35", target_version="1.36"),),
        snapshot_digest=_D["a"],
        report_digest=_D["b"],
        ledger_digest=_D["c"],
        store_digest=_D["e"],
        ruleset_digest=_D["e"],
        decisions=decisions,
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
    from_series: str | None,
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
    is what the frozen sequencer really emits for this fact and not a guess about it.
    """
    series = ".".join(installed.split(".")[:2]) + ".x"
    moves = plan_moves(knowledge, series, _HOPS, installed_version=installed).moves
    return [
        _move(
            order,
            knowledge.canonical_id,
            move.from_series,
            move.to_series,
            move.recommended_version,
            unordered_with=tuple(o for o in range(1, len(moves) + 1) if o != order),
        )
        for order, move in enumerate(moves, start=1)
    ]


# ---------------------------------------------------------------------------
# The fixture reproduces the defect. Asserted first, because everything below is
# vacuous if the frozen sequencer no longer emits the step this rule removes.
# ---------------------------------------------------------------------------


def test_the_sequencer_still_emits_the_backward_step() -> None:
    """`moves` is digest-frozen, so the backward step is still produced upstream.

    If this ever fails, the frozen sequencer changed and the rule under test has become dead
    code -- which is a thing to find out here rather than by noticing the module is never hit.
    """
    moves = plan_moves(
        _THIN_CATALOGUE, "2.20.x", _HOPS, installed_version="2.20.2"
    ).moves
    assert [(m.from_series, m.to_series, m.recommended_version) for m in moves] == [
        ("2.20.x", "2.20.x", "2.20.0"),
    ], "the fixture no longer reproduces the live keda step, so this file proves nothing"


# ---------------------------------------------------------------------------
# THE DEFECT: a step that installs at or behind the running version is not a move.
# ---------------------------------------------------------------------------


def test_a_step_installing_behind_the_running_version_is_dropped() -> None:
    steps = _steps_for(_THIN_CATALOGUE, "2.20.2")
    plan = without_backward_steps(
        _plan(*steps), _assessment(_decision("thin-catalogue", "2.20.2", "2.20.x"))
    )
    assert plan.steps == (), (
        "a step installing 2.20.0 on a cluster running 2.20.2 moves it backwards and must "
        f"not reach the plan; got {[(s.from_series, s.recommended_version) for s in plan.steps]}"
    )


@pytest.mark.parametrize(
    ("installed", "recommended", "survives"),
    [
        pytest.param("2.20.2", "2.20.0", False, id="behind-dropped"),
        pytest.param("2.20.2", "2.20.2", False, id="equal-dropped"),
        pytest.param("2.20.2", "2.20.3", True, id="ahead-kept"),
        pytest.param("2.20.2", "2.20.10", True, id="ahead-by-two-digit-patch-kept"),
        pytest.param("2.20.9", "2.20.10", True, id="ahead-numerically-not-textually-kept"),
    ],
)
def test_the_rule_turns_on_version_order_and_is_numeric(
    installed: str, recommended: str, survives: bool
) -> None:
    """Strictly ahead survives, at-or-behind does not, and the comparison is numeric.

    The two-digit patch cases are the reason `order.moves._spec_sort_key` is reused rather
    than a string compare written here: `"2.20.10" > "2.20.2"` is False as text and True as
    versions, so a textual rule would delete a real upgrade.
    """
    plan = without_backward_steps(
        _plan(_move(1, "keda", "2.20.x", "2.20.x", recommended)),
        _assessment(_decision("keda", installed, "2.20.x")),
    )
    assert bool(plan.steps) is survives


def test_a_v_prefixed_installed_version_is_still_compared() -> None:
    """A label or a fact may spell the installed version `v2.20.2`.

    Normalised through `facts.series.exact_version` first, the same way
    `view/build.py::_further_than_installed` does it, so a cosmetic spelling cannot smuggle a
    backward step past the rule.
    """
    plan = without_backward_steps(
        _plan(_move(1, "keda", "2.20.x", "2.20.x", "2.20.0")),
        _assessment(_decision("keda", "v2.20.2", "2.20.x")),
    )
    assert plan.steps == ()


# ---------------------------------------------------------------------------
# THE CASES THAT MUST SURVIVE.
# ---------------------------------------------------------------------------


def test_a_genuine_multi_hop_still_produces_two_steps() -> None:
    steps = _steps_for(_GENUINE_MULTI_HOP, "1.13.7")
    plan = without_backward_steps(
        _plan(*steps), _assessment(_decision("needs-two-hops", "1.13.7", "1.13.x"))
    )
    kept = [s for s in plan.steps if s.component == "needs-two-hops"]
    assert [s.recommended_version for s in kept] == ["1.19.6", "1.21.2"], (
        "a component that must stop on the way keeps a step per version it installs"
    )


def test_a_backward_step_that_genuinely_changes_series_is_kept() -> None:
    """A compatibility-forced downgrade across a series boundary stays on the page.

    `moves.plan_moves` takes the LOWEST series that clears every hop, and on some windows
    that series is below the installed one -- in which case the downgrade may be the only way
    across the boundary. Dropping it would leave the plan claiming `feasible` with nothing to
    make it so, which is the silent error rather than the readable one. See
    `order/floor.py`'s module docstring.
    """
    plan = without_backward_steps(
        _plan(_move(1, "keda", "2.20.x", "2.17.x", "2.17.3")),
        _assessment(_decision("keda", "2.20.2", "2.20.x")),
    )
    assert [s.to_series for s in plan.steps] == ["2.17.x"]


def test_a_step_with_no_version_to_install_is_kept() -> None:
    """`recommended_version` is None when the fact records no release in the target series.

    The series is still the answer; only the patch cannot be named. Comparing None against
    the installed version would delete a real move over a value that was never a version.
    """
    plan = without_backward_steps(
        _plan(_move(1, "keda", "2.20.x", "2.20.x", None)),
        _assessment(_decision("keda", "2.20.2", "2.20.x")),
    )
    assert len(plan.steps) == 1


@pytest.mark.parametrize(
    "installed",
    [
        pytest.param(None, id="installed-unknown"),
        pytest.param("UNRESOLVED", id="installed-unparseable"),
    ],
)
def test_a_step_is_never_dropped_on_a_comparison_that_did_not_happen(
    installed: str | None,
) -> None:
    plan = without_backward_steps(
        _plan(_move(1, "keda", "2.20.x", "2.20.x", "2.20.0")),
        _assessment(_decision("keda", installed, "2.20.x")),
    )
    assert len(plan.steps) == 1


def test_a_component_with_no_decision_is_never_dropped() -> None:
    """No decision means no installed version to compare, which is not evidence of a repeat."""
    plan = without_backward_steps(
        _plan(_move(1, "unassessed", "2.20.x", "2.20.x", "2.20.0")),
        _assessment(_decision("keda", "2.20.2", "2.20.x")),
    )
    assert len(plan.steps) == 1


def test_a_cluster_hop_is_never_dropped() -> None:
    """A hop installs no component version, so it can never be behind anything."""
    plan = without_backward_steps(
        _plan(
            _move(1, "keda", "2.20.x", "2.20.x", "2.20.0"),
            PlanStep(
                order=2,
                kind=StepKind.CLUSTER_UPGRADE,
                description="Upgrade the cluster from Kubernetes 1.35 to 1.36.",
                from_kubernetes="1.35",
                to_kubernetes="1.36",
            ),
        ),
        _assessment(_decision("keda", "2.20.2", "2.20.x")),
    )
    assert [s.kind for s in plan.steps] == [StepKind.CLUSTER_UPGRADE]
    assert [s.order for s in plan.steps] == [1]


def test_a_plan_with_nothing_backward_is_returned_unchanged() -> None:
    """Identity, not an equal copy: an untouched plan keeps the digest it arrived with."""
    plan = _plan(*_steps_for(_GENUINE_MULTI_HOP, "1.13.7"))
    assessment = _assessment(_decision("needs-two-hops", "1.13.7", "1.13.x"))
    assert without_backward_steps(plan, assessment) is plan


# ---------------------------------------------------------------------------
# The renumbering, shared with `dedupe`, and the key the two rules join on.
# ---------------------------------------------------------------------------


def test_the_surviving_steps_are_renumbered_and_the_hints_follow() -> None:
    """A plan numbered 1, 3 reads as a step that went missing."""
    plan = without_backward_steps(
        _plan(
            _move(1, "keda", "2.20.x", "2.20.x", "2.20.0", unordered_with=(2, 3)),
            _move(2, "other", "1.0.x", "1.1.x", "1.1.4", unordered_with=(1, 3)),
            _move(3, "third", "3.0.x", "3.1.x", "3.1.1", unordered_with=(1, 2)),
        ),
        _assessment(
            _decision("keda", "2.20.2", "2.20.x"),
            _decision("other", "1.0.1", "1.0.x"),
            _decision("third", "3.0.1", "3.0.x"),
        ),
    )
    assert [s.order for s in plan.steps] == [1, 2]
    assert [s.component for s in plan.steps] == ["other", "third"]
    assert plan.steps[0].unordered_with == (2,)
    assert plan.steps[1].unordered_with == (1,)


def test_two_decisions_sharing_one_identity_resolve_to_unknown_and_the_step_survives() -> None:
    """Two Helm releases of one component in two namespaces share an `identity`.

    Keeping whichever decision came last would compare the step against the wrong cluster and
    could delete it on a comparison no part of the plan is about -- a wrong drop, which is the
    one failure this rule must not have. So the key resolves to unknown and the step survives.
    """
    first = _decision("keda", "2.20.2", "2.20.x")
    second = first.model_copy(
        update={"workload_ref": "HelmRelease/other-ns/keda", "installed_version": "2.17.2"}
    )
    assessment = _assessment(first, second)
    assert installed_versions(assessment) == {"keda": None}
    plan = without_backward_steps(
        _plan(_move(1, "keda", "2.20.x", "2.20.x", "2.20.0")), assessment
    )
    assert len(plan.steps) == 1


def test_two_decisions_agreeing_on_the_installed_version_still_compare() -> None:
    """Agreement is not ambiguity: the same version twice is still a usable answer."""
    first = _decision("keda", "2.20.2", "2.20.x")
    second = first.model_copy(update={"workload_ref": "HelmRelease/other-ns/keda"})
    assessment = _assessment(first, second)
    assert installed_versions(assessment) == {"keda": "2.20.2"}
    plan = without_backward_steps(
        _plan(_move(1, "keda", "2.20.x", "2.20.x", "2.20.0")), assessment
    )
    assert plan.steps == ()


def test_the_installed_version_is_keyed_the_way_a_step_names_its_component() -> None:
    """`identity or workload_ref` -- the same expression `interleave` and `recommend` use.

    A second convention here would fail to join silently: the step would keep its backward
    version and no error would be raised anywhere.
    """
    keyed = installed_versions(
        _assessment(
            _decision("keda", "2.20.2", "2.20.x"),
            ComponentDecision(
                workload_ref="Deployment/ns/anonymous",
                identity=None,
                installed_version="1.2.3",
                assessment_priority="low",
                fact_status="FACT_MISSING",
                hops=(
                    HopDecision(
                        hop=KubernetesHop(current_version="1.35", target_version="1.36"),
                        verdict=CompatibilityVerdict.UNRESOLVED,
                        reason="fixture",
                        decided_from="none",
                    ),
                ),
            ),
        )
    )
    assert keyed == {"keda": "2.20.2", "Deployment/ns/anonymous": "1.2.3"}
