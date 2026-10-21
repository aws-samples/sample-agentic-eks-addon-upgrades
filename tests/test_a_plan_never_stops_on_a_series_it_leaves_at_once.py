"""A plan never installs a version series it abandons before the cluster has moved.

WHAT WENT WRONG. For a cluster at Kubernetes 1.35 targeting 1.36, the plan
carried five component moves for three
components:

    s1  cert-manager  1.13.x -> 1.19.x  install 1.19.6  at Kubernetes 1.35
    s2  cert-manager  1.19.x -> 1.21.x  install 1.21.2  at Kubernetes 1.35
    s3  keda          2.17.x -> 2.20.x  install 2.20.2  at Kubernetes 1.35
    s4  keda          2.20.x -> 2.21.x  install 2.21.0  at Kubernetes 1.35
    s5  metrics-server 0.6.x -> 0.9.x   install 0.9.0   at Kubernetes 1.35

`s1` and `s3` are unforced. Read off the SAME stored facts the plan was built from,
`cert-manager` 1.21.x spans `[1.33, 1.37)` and `keda` 2.21.x spans `[1.34, 1.37)` -- each
covers 1.35 AND 1.36 -- so one move reaches the target from where the component is installed
and the intermediate series is entered and left while the cluster has not moved at all. Both
steps in each pair carry the SAME `at_kubernetes`, which is the observable form of that: the
intermediate is not something the component rides across a hop, it is something installed and
replaced before the first hop begins.

WHERE IT COMES FROM. `order/moves.py::plan_moves` walks the hop versions one at a time and,
when the held series fails, takes `candidates[0]` -- the LOWEST series covering both the hop
being unblocked and the version being moved at (`moves.py:248`, ordered ascending by
`moves.py:108`). There is no lookahead: a candidate that also clears every later hop is not
preferred over one that does not. So at the first hop the sequencer picks the lowest legal
series, and at the next hop that series is out of window and a second move is forced.

The docstring's rationale for the lowest tie-break -- "a smaller jump crosses fewer breaking
changes" -- does not hold for this shape, and `resolve_reversibility` is why: reversibility is
a pure function of the (from, to) span, so 2.17.x -> 2.20.x -> 2.21.x crosses the identical set
of breaking changes that 2.17.x -> 2.21.x crosses, and adds one more install on top. Both
paths end on 2.21.x.

WHAT THIS FILE PINS. Three things, and they only mean something together:

  * the frozen sequencer still emits the unforced pair, so the rule under test is reachable;
  * the pair collapses to one move whose reversibility is RE-RESOLVED over the widened span,
    because the surviving step's own span is narrower and would understate a one-way
    migration crossed by the part that was dropped;
  * a TRUE interleaved multi-hop -- moves at DIFFERENT `at_kubernetes`, separated by a cluster
    hop, which is the shape GAP-2 and `moves.plan_moves` exist to express -- survives whole.

THE THIRD ONE IS NOT COVERED ANYWHERE ELSE. `tests/test_a_step_never_installs_what_a_step_\
before_it_installs.py` and `tests/test_a_plan_never_moves_a_cluster_backwards.py` both call
their fixture `_GENUINE_MULTI_HOP`, and in both it is windows `1.19.x [1.34, 1.36)` and
`1.21.x [1.35, 1.38)` against hops `(1.35, 1.36)` -- where 1.21.x covers both hops, so it is
the unforced shape rather than a multi-hop. Those files assert on `without_redundant_steps`
and `without_backward_steps` directly, both of which correctly keep both steps at their own
layer, so they are unaffected. But it means no test before this one held a real interleaved
multi-hop, and `_TRUE_MULTI_HOP` below is one: no single series in it spans the whole path.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from eks_addon_readiness.decide.assessment import (
    ComponentDecision,
    HopDecision,
    ReadinessAssessment,
)
from eks_addon_readiness.knowledge import (
    ChangeRecord,
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
from eks_addon_readiness.order import build_plan
from eks_addon_readiness.order.collapse import without_unforced_intermediates
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


def _rule(spec: str, minimum: str, maximum: str | None) -> CompatibilityRule:
    return CompatibilityRule(
        component_version_series=spec,
        kubernetes_versions=VersionRange(
            minimum_inclusive=minimum, maximum_exclusive=maximum
        ),
        evidence=_CITE,
    )


def _knowledge(
    canonical_id: str,
    rules: tuple[CompatibilityRule, ...],
    versions: tuple[str, ...],
    breaking: tuple[ChangeRecord, ...] = (),
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
        breaking_changes=breaking,
        verification_checks=(
            VerificationCheck(
                check_id="pods.ready", description="pods are ready", evidence=_CITE
            ),
        ),
        default_upgrade_strategy=UpgradeStrategy.IN_PLACE,
        refreshed_at=_NOW,
        evidence_confidence=Confidence.MEDIUM,
    )


# THE KEDA FACT, reduced to the three series that decide the 1.35 -> 1.36 path. Windows
# and releases are the ones scanned out of a deployed fact store; the series
# between 2.17.x and 2.20.x are dropped because none of them covers 1.35, so none of them can
# change the answer.
_LIVE_KEDA = _knowledge(
    "keda",
    (
        _rule("2.17.x", "1.30", "1.33"),
        _rule("2.20.x", "1.33", "1.36"),
        _rule("2.21.x", "1.34", "1.37"),
    ),
    ("2.17.2", "2.20.2", "2.21.0"),
)

# THE LIVE CERT-MANAGER FACT, reduced the same way. Three series cover 1.35, so the lowest
# tie-break has a wider field to get wrong here than it does for keda.
_LIVE_CERT_MANAGER = _knowledge(
    "cert-manager",
    (
        _rule("1.13.x", "1.21", "1.28"),
        _rule("1.19.x", "1.31", "1.36"),
        _rule("1.20.x", "1.32", "1.36"),
        _rule("1.21.x", "1.33", "1.37"),
    ),
    ("1.13.2", "1.19.6", "1.20.4", "1.21.2"),
)

# The same keda windows with a ONE_WAY change introduced in 2.18.0 -- inside the span the
# unforced first move covers and OUTSIDE the span of the move that survives it. So the
# surviving step's own reversibility reads REVERSIBLE while the collapsed move is ONE_WAY, and
# carrying the survivor's value forward would tell an approver a CRD migration can be undone.
_ONE_WAY_IN_THE_DROPPED_SPAN = _knowledge(
    "keda-one-way",
    (
        _rule("2.17.x", "1.30", "1.33"),
        _rule("2.20.x", "1.33", "1.36"),
        _rule("2.21.x", "1.34", "1.37"),
    ),
    ("2.17.2", "2.20.2", "2.21.0"),
    breaking=(
        ChangeRecord(
            introduced_in="2.18.0",
            summary="The stored version of the ScaledObject CRD is rewritten.",
            impact="Existing ScaledObjects are rewritten in place.",
            reversibility=Reversibility.ONE_WAY,
            evidence=_CITE,
        ),
    ),
)

# A TRUE INTERLEAVED MULTI-HOP: no single series spans 1.31 through 1.33, so the component
# must change while the cluster is at 1.31 and change again while it is at 1.32, with a cluster
# hop in between. 1.15.x cannot reach 1.33 and 1.16.x cannot serve 1.31, which is exactly the
# forcing case `order/moves.py`'s docstring is written around.
_TRUE_MULTI_HOP = _knowledge(
    "needs-a-real-stop",
    (
        _rule("1.14.x", "1.24", "1.32"),
        _rule("1.15.x", "1.30", "1.33"),
        _rule("1.16.x", "1.32", "1.35"),
    ),
    ("1.14.7", "1.15.3", "1.16.1"),
)


class _Store:
    """A fact store over a fixed set of entries. `digest` is what the assessment cites."""

    def __init__(self, *facts: ComponentKnowledge) -> None:
        self._facts = facts

    def find(self, identity: str) -> ComponentKnowledge | None:
        return next(
            (f for f in self._facts if f.canonical_id == identity.strip()), None
        )

    def digest(self) -> str:
        hasher = hashlib.sha256()
        for fact in sorted(self._facts, key=lambda f: f.canonical_id):
            hasher.update(fact.canonical_json().encode("utf-8"))
        return "sha256:" + hasher.hexdigest()


def _decision(
    component: str,
    installed_version: str,
    installed_series: str,
    hops: tuple[KubernetesHop, ...],
) -> ComponentDecision:
    return ComponentDecision(
        workload_ref=f"HelmRelease/{component}/{component}",
        identity=component,
        installed_version=installed_version,
        installed_series=installed_series,
        assessment_priority="high",
        fact_status="FACT_AVAILABLE",
        knowledge_base_revision="rev-1",
        hops=tuple(
            HopDecision(
                hop=hop,
                verdict=CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE,
                valid_targets=("1.21.x",),
                reason="fixture",
                decided_from="stored-fact",
            )
            for hop in hops
        ),
    )


def _assessment(
    hops: tuple[KubernetesHop, ...],
    store: _Store,
    *decisions: ComponentDecision,
) -> ReadinessAssessment:
    return ReadinessAssessment(
        cluster=_CLUSTER,
        current_kubernetes_version=hops[0].current_version,
        target_kubernetes_version=hops[-1].target_version,
        hops=hops,
        snapshot_digest=_D["a"],
        report_digest=_D["b"],
        ledger_digest=_D["c"],
        store_digest=store.digest(),
        ruleset_digest=_D["e"],
        decisions=decisions,
    )


def _plan(*steps: PlanStep, current: str = "1.35", target: str = "1.36") -> UpgradePlan:
    return UpgradePlan(
        cluster=_CLUSTER,
        current_kubernetes_version=current,
        target_kubernetes_version=target,
        steps=steps,
        snapshot_digest=_D["a"],
        report_digest=_D["b"],
        ledger_digest=_D["c"],
        assessment_digest=_D["d"],
        store_digest=_D["e"],
        ruleset_digest=_D["e"],
    )


def _steps_for(
    knowledge: ComponentKnowledge,
    installed: str,
    hops: tuple[str, ...],
    *,
    component: str | None = None,
) -> list[PlanStep]:
    """One component's moves as plan steps, built through the frozen sequencer.

    The input to the rule under test is what `plan_moves` really emits for this fact, not a
    hand-written guess about it -- the same reason `test_a_plan_never_moves_a_cluster_\
    backwards.py` builds its inputs this way. `unordered_with` names every sibling, which is
    what `interleave._weave` does for moves sharing a cluster version.
    """
    series = ".".join(installed.split(".")[:2]) + ".x"
    moves = plan_moves(knowledge, series, hops, installed_version=installed).moves
    orders = list(range(1, len(moves) + 1))
    return [
        PlanStep(
            order=order,
            kind=StepKind.COMPONENT_MOVE,
            description=(
                f"Move {component or knowledge.canonical_id} from {move.from_series} to "
                f"{move.to_series} while the cluster is at Kubernetes "
                f"{move.at_kubernetes}. Install {move.recommended_version}."
            ),
            component=component or knowledge.canonical_id,
            from_series=move.from_series,
            to_series=move.to_series,
            recommended_version=move.recommended_version,
            at_kubernetes=move.at_kubernetes,
            reversibility=Reversibility.REVERSIBLE,
            reversibility_reason="fixture",
            unordered_with=tuple(o for o in orders if o != order),
        )
        for order, move in zip(orders, moves, strict=True)
    ]


# ---------------------------------------------------------------------------
# The fixtures reproduce the live plan. Asserted first, because everything below is vacuous
# if the frozen sequencer no longer emits the pair this rule collapses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("knowledge", "installed", "expected"),
    [
        pytest.param(
            _LIVE_KEDA,
            "2.17.2",
            [("2.17.x", "2.20.x", "2.20.2", "1.35"), ("2.20.x", "2.21.x", "2.21.0", "1.35")],
            id="keda",
        ),
        pytest.param(
            _LIVE_CERT_MANAGER,
            "1.13.2",
            [("1.13.x", "1.19.x", "1.19.6", "1.35"), ("1.19.x", "1.21.x", "1.21.2", "1.35")],
            id="cert-manager",
        ),
    ],
)
def test_the_sequencer_still_emits_the_unforced_intermediate(
    knowledge: ComponentKnowledge, installed: str, expected: list[tuple[str, ...]]
) -> None:
    """`moves` is digest-frozen, so the pair is still produced upstream of the rule.

    Both moves carry the same `at_kubernetes`, which is the observable form of "entered and
    left before the cluster moved". If this fails the frozen sequencer changed and the rule
    under test is dead code -- better found here than by noticing the module is never hit.
    """
    moves = plan_moves(knowledge, ".".join(installed.split(".")[:2]) + ".x",
                       ("1.35", "1.36"), installed_version=installed).moves
    assert [
        (m.from_series, m.to_series, m.recommended_version, m.at_kubernetes)
        for m in moves
    ] == expected, "the fixture no longer reproduces the live pair, so this file proves nothing"


@pytest.mark.parametrize(
    ("knowledge", "final_series"),
    [
        pytest.param(_LIVE_KEDA, "2.21.x", id="keda"),
        pytest.param(_LIVE_CERT_MANAGER, "1.21.x", id="cert-manager"),
    ],
)
def test_the_series_the_pair_ends_on_was_legal_at_the_start_too(
    knowledge: ComponentKnowledge, final_series: str
) -> None:
    """The evidence that the intermediate is unforced, read off the fixture's own windows.

    One move to `final_series` is legal on the cluster as it stands AND at the target, so the
    stop in between is not something the compatibility windows require.
    """
    from eks_addon_readiness.order.moves import _series_covers

    assert _series_covers(knowledge, final_series, "1.35")
    assert _series_covers(knowledge, final_series, "1.36")


# ---------------------------------------------------------------------------
# THE DEFECT: a series entered and left at one cluster version is not a stop.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("knowledge", "installed", "collapsed"),
    [
        pytest.param(_LIVE_KEDA, "2.17.2", ("2.17.x", "2.21.x", "2.21.0"), id="keda"),
        pytest.param(
            _LIVE_CERT_MANAGER, "1.13.2", ("1.13.x", "1.21.x", "1.21.2"), id="cert-manager"
        ),
    ],
)
def test_the_pair_becomes_one_move_from_where_the_component_is_installed(
    knowledge: ComponentKnowledge, installed: str, collapsed: tuple[str, str, str]
) -> None:
    steps = _steps_for(knowledge, installed, ("1.35", "1.36"))
    plan = without_unforced_intermediates(_plan(*steps), _Store(knowledge))
    assert [
        (s.from_series, s.to_series, s.recommended_version) for s in plan.steps
    ] == [collapsed]
    assert plan.steps[0].order == 1
    assert plan.steps[0].at_kubernetes == "1.35"


def test_the_collapsed_step_reads_as_one_move_and_names_the_version_to_install() -> None:
    """The description is rebuilt, not inherited: the survivor's said the wrong origin.

    Left alone it would read "Move keda from 2.20.x", naming a series the cluster is not on
    and never reaches -- which is worse than the two rows it replaced, because it is a single
    row that is false rather than two that are merely redundant.
    """
    steps = _steps_for(_LIVE_KEDA, "2.17.2", ("1.35", "1.36"))
    plan = without_unforced_intermediates(_plan(*steps), _Store(_LIVE_KEDA))
    assert plan.steps[0].description == (
        "Move keda from 2.17.x to 2.21.x while the cluster is at Kubernetes 1.35. "
        "Install 2.21.0."
    )


def test_the_reversibility_is_re_resolved_over_the_whole_span() -> None:
    """A one-way change inside the dropped span must reach the collapsed step.

    `resolve_reversibility` reads the (from, to) span, so the surviving step's own value was
    computed for 2.20.x -> 2.21.x and does not see the CRD rewrite introduced in 2.18.0.
    Inheriting it would tell an approver a stored-version bump can be rolled back.
    """
    from eks_addon_readiness.order.reversibility import resolve_reversibility

    survivor_alone, _ = resolve_reversibility(
        _ONE_WAY_IN_THE_DROPPED_SPAN, "2.20.x", "2.21.x"
    )
    assert survivor_alone is Reversibility.REVERSIBLE, (
        "fixture no longer isolates the one-way change to the dropped span"
    )

    steps = _steps_for(_ONE_WAY_IN_THE_DROPPED_SPAN, "2.17.2", ("1.35", "1.36"))
    plan = without_unforced_intermediates(
        _plan(*steps), _Store(_ONE_WAY_IN_THE_DROPPED_SPAN)
    )
    assert len(plan.steps) == 1
    assert plan.steps[0].reversibility is Reversibility.ONE_WAY
    assert "2.18.0" in (plan.steps[0].reversibility_reason or "")


# ---------------------------------------------------------------------------
# THE CASES THAT MUST SURVIVE.
# ---------------------------------------------------------------------------


def test_a_true_interleaved_multi_hop_keeps_every_step() -> None:
    """Moves at DIFFERENT cluster versions are the shape GAP-2 exists for.

    No series in `_TRUE_MULTI_HOP` spans 1.31 through 1.33, so the component changes at 1.31
    and again at 1.32 with a cluster hop between them. A rule that counted steps per component
    would delete exactly this.
    """
    steps = _steps_for(_TRUE_MULTI_HOP, "1.14.7", ("1.31", "1.32", "1.33"))
    assert [s.at_kubernetes for s in steps] == ["1.31", "1.32"], (
        "fixture no longer produces moves at two different cluster versions"
    )
    plan = without_unforced_intermediates(
        _plan(*steps, current="1.31", target="1.33"), _Store(_TRUE_MULTI_HOP)
    )
    assert [(s.from_series, s.to_series, s.at_kubernetes) for s in plan.steps] == [
        ("1.14.x", "1.15.x", "1.31"),
        ("1.15.x", "1.16.x", "1.32"),
    ]


def test_two_steps_that_do_not_join_are_both_kept() -> None:
    """`to_series` of one step not matching `from_series` of the next is not a chain.

    Collapsing them would invent a move from an origin no step named. Two independent steps
    at one cluster version for one component is not a shape `interleave` emits, and if some
    later producer does emit it this declines to guess what it meant.
    """
    plan = without_unforced_intermediates(
        _plan(
            _step(1, "keda", "2.17.x", "2.20.x", "2.20.2"),
            _step(2, "keda", "2.19.x", "2.21.x", "2.21.0"),
        ),
        _Store(_LIVE_KEDA),
    )
    assert len(plan.steps) == 2


def test_a_component_the_store_cannot_answer_for_keeps_its_steps() -> None:
    """Without the fact there is no way to re-resolve reversibility over the wider span.

    Carrying the survivor's narrower value forward could understate a one-way move, so the
    rule declines rather than guessing. Nothing is lost: the pair is redundant, not wrong.
    """
    plan = without_unforced_intermediates(
        _plan(
            _step(1, "unknown-to-the-store", "2.17.x", "2.20.x", "2.20.2"),
            _step(2, "unknown-to-the-store", "2.20.x", "2.21.x", "2.21.0"),
        ),
        _Store(_LIVE_KEDA),
    )
    assert len(plan.steps) == 2


def test_an_advisory_upgrade_is_never_collapsed_into_a_forced_move() -> None:
    """A `COMPONENT_UPGRADE_RECOMMENDED` is optional and carries no reversibility.

    Folding one into a required move would make an optional change mandatory, and folding a
    required move into one would make a mandatory change optional. Neither may happen, so the
    rule only ever looks at `COMPONENT_MOVE`.
    """
    advisory = PlanStep(
        order=2,
        kind=StepKind.COMPONENT_UPGRADE_RECOMMENDED,
        description="Recommended: upgrade keda.",
        component="keda",
        from_series="2.20.x",
        to_series="2.21.x",
        recommended_version="2.21.0",
        at_kubernetes="1.35",
    )
    plan = without_unforced_intermediates(
        _plan(_step(1, "keda", "2.17.x", "2.20.x", "2.20.2"), advisory),
        _Store(_LIVE_KEDA),
    )
    assert [s.kind for s in plan.steps] == [
        StepKind.COMPONENT_MOVE,
        StepKind.COMPONENT_UPGRADE_RECOMMENDED,
    ]


def test_a_cluster_hop_between_two_moves_is_never_crossed() -> None:
    """Two moves for one component at two versions stay two, hop and all."""
    hop = PlanStep(
        order=2,
        kind=StepKind.CLUSTER_UPGRADE,
        description="Upgrade the cluster from Kubernetes 1.35 to 1.36.",
        from_kubernetes="1.35",
        to_kubernetes="1.36",
    )
    plan = without_unforced_intermediates(
        _plan(
            _step(1, "keda", "2.17.x", "2.20.x", "2.20.2", at="1.35"),
            hop,
            _step(3, "keda", "2.20.x", "2.21.x", "2.21.0", at="1.36"),
        ),
        _Store(_LIVE_KEDA),
    )
    assert [s.order for s in plan.steps] == [1, 2, 3]


def test_a_plan_with_nothing_to_collapse_is_returned_unchanged() -> None:
    """Identity, not an equal copy: an untouched plan keeps the digest it arrived with."""
    plan = _plan(*_steps_for(_TRUE_MULTI_HOP, "1.14.7", ("1.31", "1.32", "1.33")),
                 current="1.31", target="1.33")
    assert without_unforced_intermediates(plan, _Store(_TRUE_MULTI_HOP)) is plan


# ---------------------------------------------------------------------------
# The renumbering, shared with `dedupe` and `floor`.
# ---------------------------------------------------------------------------


def test_the_survivors_are_renumbered_and_the_hints_follow() -> None:
    """A plan numbered 1, 3 reads as a step that went missing.

    Steps 1 and 2 are keda's unforced pair and collapse to one. `other`, numbered 3 and
    unordered with both, must end up numbered 2 and unordered with the survivor alone rather
    than with a step number nothing occupies.
    """
    plan = without_unforced_intermediates(
        _plan(
            _step(1, "keda", "2.17.x", "2.20.x", "2.20.2", unordered_with=(2, 3)),
            _step(2, "keda", "2.20.x", "2.21.x", "2.21.0", unordered_with=(1, 3)),
            _step(3, "other", "1.0.x", "1.1.x", "1.1.4", unordered_with=(1, 2)),
        ),
        _Store(_LIVE_KEDA, _knowledge("other", (_rule("1.1.x", "1.30", None),), ("1.1.4",))),
    )
    assert [s.order for s in plan.steps] == [1, 2]
    assert [s.component for s in plan.steps] == ["keda", "other"]
    assert plan.steps[0].unordered_with == (2,)
    assert plan.steps[1].unordered_with == (1,)


# ---------------------------------------------------------------------------
# Through Stage 5's real entry point, which is where the live plan came from.
# ---------------------------------------------------------------------------


def test_stage_five_plans_one_move_per_component_for_the_live_cluster() -> None:
    """`order.build_plan` on the live shapes: three component moves, not five.

    The pipeline, not the rule in isolation: `interleave` -> `dedupe` -> `recommend` ->
    `collapse` -> `floor`. Every step names a series that covers the cluster as it stands and
    the target, so nothing on the page is a stop the plan leaves at once.
    """
    hops = (KubernetesHop(current_version="1.35", target_version="1.36"),)
    store = _Store(_LIVE_KEDA, _LIVE_CERT_MANAGER)
    assessment = _assessment(
        hops,
        store,
        _decision("keda", "2.17.2", "2.17.x", hops),
        _decision("cert-manager", "1.13.2", "1.13.x", hops),
    )
    plan = build_plan(assessment, store, "1.36")
    moves = [s for s in plan.steps if s.kind is StepKind.COMPONENT_MOVE]
    assert [
        (s.component, s.from_series, s.to_series, s.recommended_version) for s in moves
    ] == [
        ("cert-manager", "1.13.x", "1.21.x", "1.21.2"),
        ("keda", "2.17.x", "2.21.x", "2.21.0"),
    ]
    assert plan.counts()["component_moves"] == 2
    assert [s.kind for s in plan.steps][-1] is StepKind.CLUSTER_UPGRADE


def _step(
    order: int,
    component: str,
    from_series: str | None,
    to_series: str,
    recommended_version: str | None,
    *,
    at: str = "1.35",
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
        at_kubernetes=at,
        reversibility=Reversibility.REVERSIBLE,
        reversibility_reason="fixture",
        unordered_with=unordered_with,
    )
