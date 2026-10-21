"""The brief is made against the APPROVED FACT, and a managed add-on that is behind gets a step.

TWO DEFECTS THIS GUARDS AGAINST, on a cluster targeting 1.36.

argo-rollouts. The model found no compatibility table, recommended 1.10.0, and the approval page
showed 1.10.0; the person approved. The plan then said "stay on 1.6.6". The fact carried one rule
(1.6.x, floor 1.7, no ceiling -- a chart README's "Kubernetes 1.7+") and seven releases up to
1.10.0; `moves.newest_supported_concrete` recommends from rules only, found nothing above 1.6.x,
and the brief was made against a version nobody approved. `order/latest.py` now holds ONE
recommendation policy that the plan, the readiness report and the approval page all call, so the
number approved is the number planned -- and when the fact is silent above the installed series it
answers the newest recorded release, marked unverified, which is what the research prompt's Tier 5
promised and never delivered.

vpc-cni. Downgraded to 1.22.4; AWS lists exactly v1.23.1-eksbuild.1 for 1.36; verdict
REQUIRES_COMPONENT_UPGRADE. The frozen sequencer plans required moves from stored windows, an
insight is a flat list, so the add-on sat in `plan.unsequenceable` and the report showed "Upgrade
needed" with no version to go to. `recommend.with_required_catalogue_moves` now gives it a required
step to the newest version AWS lists, before the hop the installed version cannot survive.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from eks_addon_readiness.models import ClusterReference, CompatibilityVerdict, KubernetesHop
from eks_addon_readiness.order.latest import (
    Basis,
    fact_recommendation,
    with_unverified_latest,
)
from eks_addon_readiness.order.plan import (
    PlanStep,
    StepKind,
    UnsequenceableComponent,
    UpgradePlan,
)
from eks_addon_readiness.order.recommend import (
    required_catalogue_move,
    with_required_catalogue_moves,
)

_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_CITE = (Citation(url="https://example.test/readme", title="readme", retrieved_at=_NOW),)
_D = {name: "sha256:" + char * 64 for name, char in zip("abcde", "abcde", strict=True)}
_CLUSTER = ClusterReference(name="fixture-cluster", region="us-east-1")
_HOPS = ("1.35", "1.36")


def _rule(spec: str, minimum: str, maximum: str | None) -> CompatibilityRule:
    return CompatibilityRule(
        component_version_series=spec,
        kubernetes_versions=VersionRange(minimum_inclusive=minimum, maximum_exclusive=maximum),
        evidence=_CITE,
    )


def _knowledge(
    canonical_id: str,
    rules: tuple[CompatibilityRule, ...],
    versions: tuple[str, ...],
    confidence: Confidence = Confidence.LOW,
) -> ComponentKnowledge:
    return ComponentKnowledge(
        knowledge_base_revision="rev-1",
        canonical_id=canonical_id,
        canonical_repository=f"https://example.test/{canonical_id}",
        official_documentation=(f"https://example.test/{canonical_id}/docs",),
        compatibility_rules=rules,
        releases=tuple(
            ReleaseRecord(version=v, released_at=_NOW, source_revision="rev", evidence=_CITE)
            for v in versions
        ),
        verification_checks=(
            VerificationCheck(check_id="pods.ready", description="ready", evidence=_CITE),
        ),
        default_upgrade_strategy=UpgradeStrategy.IN_PLACE,
        refreshed_at=_NOW,
        evidence_confidence=confidence,
    )


# THE ARGO-ROLLOUTS FACT AS APPROVED: one rule for the installed series with no ceiling, releases
# through 1.10.0, and a release candidate that must never be recommended.
_SILENT_ABOVE_INSTALLED = _knowledge(
    "argo-rollouts",
    (_rule("1.6.x", "1.7", None),),
    ("1.6.6", "1.7.2", "1.8.4", "1.9.1", "1.10.0", "1.11.0-rc1"),
)

# A fact that SPEAKS to newer series and says they do not reach the target: rising floors.
_RISING_FLOORS = _knowledge(
    "rising-floors",
    (_rule("2.17.x", "1.30", None), _rule("2.19.x", "1.37", None)),
    ("2.17.2", "2.19.0"),
    Confidence.HIGH,
)

# A fact where a newer series covers the path: the frozen policy answers, unchanged.
_COVERED = _knowledge(
    "covered",
    (_rule("0.6.x", "1.30", "1.36"), _rule("0.9.x", "1.34", None)),
    ("0.6.4", "0.9.0"),
    Confidence.HIGH,
)


def _decision(
    component: str,
    installed: str,
    *,
    verdict: CompatibilityVerdict,
    decided_from: str,
    knowledge_base_revision: str | None,
    valid_targets: tuple[str, ...] = (),
) -> ComponentDecision:
    return ComponentDecision(
        workload_ref=f"HelmRelease/{component}/{component}",
        identity=component,
        installed_version=installed,
        installed_series=".".join(installed.lstrip("v").split(".")[:2]) + ".x",
        assessment_priority="high",
        fact_status="FACT_AVAILABLE" if knowledge_base_revision else "ANSWERED_BY_CLUSTER",
        knowledge_base_revision=knowledge_base_revision,
        hops=(
            HopDecision(
                hop=KubernetesHop(current_version="1.35", target_version="1.36"),
                verdict=verdict,
                valid_targets=valid_targets,
                reason="fixture",
                decided_from=decided_from,
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


def _plan(*steps: PlanStep, unsequenceable=()) -> UpgradePlan:
    return UpgradePlan(
        cluster=_CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        steps=steps,
        unsequenceable=unsequenceable,
        snapshot_digest=_D["a"],
        report_digest=_D["b"],
        ledger_digest=_D["c"],
        assessment_digest=_D["d"],
        store_digest=_D["e"],
        ruleset_digest=_D["e"],
    )


def _hop(order: int) -> PlanStep:
    return PlanStep(
        order=order,
        kind=StepKind.CLUSTER_UPGRADE,
        description="Upgrade the cluster from 1.35 to 1.36.",
        from_kubernetes="1.35",
        to_kubernetes="1.36",
    )


def _move(order: int, component: str, to: str) -> PlanStep:
    return PlanStep(
        order=order,
        kind=StepKind.COMPONENT_MOVE,
        description=f"Move {component} to {to}.",
        component=component,
        from_series="1.0.x",
        to_series=to,
        recommended_version=to,
        at_kubernetes="1.35",
        reversibility=Reversibility.REVERSIBLE,
        reversibility_reason="fixture",
    )


class _Store:
    def __init__(self, *facts: ComponentKnowledge) -> None:
        self._facts = {f.canonical_id: f for f in facts}

    def find(self, identity: str) -> ComponentKnowledge | None:
        return self._facts.get(identity)


# ---------------------------------------------------------------------------
# The one policy.
# ---------------------------------------------------------------------------


def test_a_fact_silent_above_the_installed_series_recommends_its_newest_stable_release() -> None:
    recommendation = fact_recommendation(_SILENT_ABOVE_INSTALLED, "1.6.6", _HOPS)
    assert recommendation == ("1.10.0", Basis.LATEST_UNVERIFIED)


def test_a_release_candidate_is_never_the_recommendation() -> None:
    assert fact_recommendation(_SILENT_ABOVE_INSTALLED, "1.6.6", _HOPS).version != "1.11.0-rc1"


def test_a_fact_that_says_newer_series_do_not_reach_the_target_keeps_the_installed_one() -> None:
    """Rising floors are the fact saying 'not yet'; the latest release would contradict it."""
    recommendation = fact_recommendation(_RISING_FLOORS, "2.17.2", _HOPS)
    assert recommendation.basis is Basis.KEEP
    assert recommendation.version != "2.19.0"


def test_a_covered_newer_series_is_the_frozen_policys_answer_unchanged() -> None:
    assert fact_recommendation(_COVERED, "0.6.4", _HOPS) == ("0.9.0", Basis.COVERED)


def test_already_on_the_newest_recorded_release_is_keep_not_unverified() -> None:
    recommendation = fact_recommendation(_SILENT_ABOVE_INSTALLED, "1.10.0", _HOPS)
    assert recommendation.basis is Basis.KEEP


# ---------------------------------------------------------------------------
# The plan carries it, as an optional step worded as unverified.
# ---------------------------------------------------------------------------


def test_the_plan_gets_an_optional_unverified_step_for_the_argo_rollouts_shape() -> None:
    decision = _decision(
        "argo-rollouts",
        "1.6.6",
        verdict=CompatibilityVerdict.COMPATIBLE,
        decided_from="stored-fact",
        knowledge_base_revision="rev-1",
    )
    plan = with_unverified_latest(
        _plan(_hop(1)), _assessment(decision), _Store(_SILENT_ABOVE_INSTALLED)
    )
    (step,) = [s for s in plan.steps if s.component == "argo-rollouts"]
    assert step.kind is StepKind.COMPONENT_UPGRADE_RECOMMENDED
    assert step.recommended_version == "1.10.0"
    assert step.order == 2
    assert "UNVERIFIED" in step.description and "OPTIONAL" in step.description


def test_a_component_that_already_has_a_step_gets_no_second_one() -> None:
    decision = _decision(
        "argo-rollouts",
        "1.6.6",
        verdict=CompatibilityVerdict.COMPATIBLE,
        decided_from="stored-fact",
        knowledge_base_revision="rev-1",
    )
    plan = _plan(_move(1, "argo-rollouts", "1.9.1"), _hop(2))
    assert (
        with_unverified_latest(plan, _assessment(decision), _Store(_SILENT_ABOVE_INSTALLED)) is plan
    )


def test_a_fact_with_rising_floors_adds_nothing() -> None:
    decision = _decision(
        "rising-floors",
        "2.17.2",
        verdict=CompatibilityVerdict.COMPATIBLE,
        decided_from="stored-fact",
        knowledge_base_revision="rev-1",
    )
    plan = _plan(_hop(1))
    assert with_unverified_latest(plan, _assessment(decision), _Store(_RISING_FLOORS)) is plan


# ---------------------------------------------------------------------------
# A managed add-on that is behind gets a REQUIRED step before the hop.
# ---------------------------------------------------------------------------


def _vpc_cni() -> ComponentDecision:
    return _decision(
        "vpc-cni",
        "v1.22.4-eksbuild.3",
        verdict=CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE,
        decided_from="eks-insight",
        knowledge_base_revision=None,
        valid_targets=("v1.23.1-eksbuild.1",),
    )


def test_aws_says_move_names_the_newest_listed_version() -> None:
    assert required_catalogue_move(_vpc_cni()) == "v1.23.1-eksbuild.1"


def test_a_compatible_or_fact_decided_component_gets_no_required_catalogue_move() -> None:
    compatible = _decision(
        "coredns",
        "v1.13.2-eksbuild.21",
        verdict=CompatibilityVerdict.COMPATIBLE,
        decided_from="eks-insight",
        knowledge_base_revision=None,
        valid_targets=("v1.13.2-eksbuild.21", "v1.14.6-eksbuild.4"),
    )
    fact_decided = _decision(
        "keda",
        "2.17.2",
        verdict=CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE,
        decided_from="stored-fact",
        knowledge_base_revision="rev-1",
        valid_targets=("2.21.x",),
    )
    assert required_catalogue_move(compatible) is None
    assert required_catalogue_move(fact_decided) is None


def test_the_required_step_lands_before_the_hop_and_leaves_unsequenceable() -> None:
    before = _plan(
        _move(1, "keda", "2.21.0"),
        _hop(2),
        PlanStep(
            order=3,
            kind=StepKind.COMPONENT_UPGRADE_RECOMMENDED,
            description="Optional coredns.",
            component="coredns",
            from_series="1.13.x",
            to_series="1.14.x",
            recommended_version="1.14.6",
            at_kubernetes="1.36",
        ),
        unsequenceable=(
            UnsequenceableComponent(
                workload_ref="DaemonSet/kube-system/aws-node",
                identity="vpc-cni",
                verdict="REQUIRES_COMPONENT_UPGRADE",
                reason="single-hop cluster insight, which cannot plan a sequence",
            ),
        ),
    )
    after = with_required_catalogue_moves(before, _assessment(_vpc_cni()))
    kinds = [(s.order, s.component or s.kind.value) for s in after.steps]
    assert kinds == [
        (1, "keda"),
        (2, "vpc-cni"),
        (3, "CLUSTER_UPGRADE"),
        (4, "coredns"),
    ]
    step = after.steps[1]
    assert step.kind is StepKind.COMPONENT_MOVE
    assert step.recommended_version == "1.23.1"
    assert step.to_series == "1.23.x"
    assert step.at_kubernetes == "1.35", "the move belongs on the cluster as it stands"
    assert step.reversibility is Reversibility.REVERSIBLE
    assert after.unsequenceable == ()
    assert after.feasible is before.feasible


def test_nothing_to_add_returns_the_same_plan_object() -> None:
    plan = _plan(_hop(1))
    coredns = _decision(
        "coredns",
        "v1.13.2-eksbuild.21",
        verdict=CompatibilityVerdict.COMPATIBLE,
        decided_from="eks-insight",
        knowledge_base_revision=None,
        valid_targets=("v1.14.6-eksbuild.4",),
    )
    assert with_required_catalogue_moves(plan, _assessment(coredns)) is plan


# ---------------------------------------------------------------------------
# The fact approval page shows what the plan will do, not what the model proposed.
# ---------------------------------------------------------------------------


def test_the_approval_page_recommends_what_the_plan_will_recommend() -> None:
    """The model said 1.10.0 with action UPGRADE; the fact supports no such coverage. The page
    must show the number the plan will carry -- here the same 1.10.0, but as LATEST_UNVERIFIED
    with no coverage claim -- and a model number the fact does not support must not appear."""
    from eks_addon_readiness.research.boundary import (
        Assessment,
        EvidenceTier,
        InstalledVersionStatus,
        RecommendationAction,
    )
    from eks_addon_readiness.research.candidate import CandidateFact
    from eks_addon_readiness.research.receipt import (
        AcceptanceDecision,
        ChangeDirection,
        ResearchReceipt,
        SignalName,
        SignalResult,
    )
    from eks_addon_readiness.research.report import build_report

    candidate = CandidateFact(
        component_id="argo-rollouts",
        version_series="1.6.x",
        knowledge=_SILENT_ABOVE_INSTALLED,
        receipt=ResearchReceipt(
            confidence=Confidence.LOW,
            signals=(
                SignalResult(
                    signal=SignalName.SUPPORT_WINDOW_DERIVED, passed=True, detail="floor only"
                ),
            ),
            change_direction=ChangeDirection.NEW,
            decision=AcceptanceDecision.PENDING_APPROVAL,
            summary="one floor, no ceiling",
            evidence_digest="sha256:" + "f" * 64,
        ),
    )

    def page(model_says: str, action: RecommendationAction):
        return build_report(
            candidate,
            current=None,
            quoted_claims=(),
            proposed_bases={},
            researched_at=_NOW,
            installed_version="1.6.6",
            hops=(KubernetesHop(current_version="1.35", target_version="1.36"),),
            advisory="advice",
            assessment=Assessment(
                current_kubernetes="1.35",
                target_kubernetes="1.36",
                installed_version_status=InstalledVersionStatus.NOT_STATED,
                action=action,
                recommended_version=model_says,
                evidence_tier=EvidenceTier.NONE,
                confidence_rationale="nothing states compatibility",
            ),
        )

    shown = page("1.10.0", RecommendationAction.UPGRADE)
    assert shown.recommended_version == "1.10.0"
    assert shown.action == "LATEST_UNVERIFIED", (
        "the page must not call an unverified release an UPGRADE"
    )
    assert shown.covers_target_kubernetes is None, "no evidence says it covers the target"

    # A number the model made up that the fact does not record never reaches the approver.
    invented = page("1.12.0", RecommendationAction.UPGRADE)
    assert invented.recommended_version == "1.10.0"


def test_a_rejected_candidate_with_no_knowledge_still_renders_a_page() -> None:
    """The judge rejected the fact, so there is nothing to plan from -- and the approver still
    needs the page that says why. If a component's judge crashes here, it must appear as
    rejected rather than vanish from the approval page."""
    from eks_addon_readiness.research.boundary import (
        Assessment,
        EvidenceTier,
        InstalledVersionStatus,
        RecommendationAction,
    )
    from eks_addon_readiness.research.candidate import CandidateFact
    from eks_addon_readiness.research.receipt import (
        AcceptanceDecision,
        ChangeDirection,
        ResearchReceipt,
        SignalName,
        SignalResult,
    )
    from eks_addon_readiness.research.report import build_report

    rejected = CandidateFact(
        component_id="cert-manager",
        version_series="1.13.x",
        knowledge=None,
        receipt=ResearchReceipt(
            confidence=Confidence.UNRESOLVED,
            signals=(
                SignalResult(signal=SignalName.SUPPORT_WINDOW_DERIVED, passed=False, detail="none"),
            ),
            change_direction=ChangeDirection.NEW,
            decision=AcceptanceDecision.REJECTED,
            summary="no window could be derived",
            evidence_digest="sha256:" + "e" * 64,
        ),
    )
    page = build_report(
        rejected,
        current=None,
        quoted_claims=(),
        proposed_bases={},
        researched_at=_NOW,
        installed_version="1.13.2",
        hops=(KubernetesHop(current_version="1.35", target_version="1.36"),),
        advisory="rejected",
        assessment=Assessment(
            current_kubernetes="1.35",
            target_kubernetes="1.36",
            installed_version_status=InstalledVersionStatus.NOT_STATED,
            action=RecommendationAction.INSUFFICIENT_EVIDENCE,
            evidence_tier=EvidenceTier.NONE,
            confidence_rationale="nothing verified",
        ),
    )
    assert page.component_id == "cert-manager"
    assert page.action == "INSUFFICIENT_EVIDENCE"
    assert page.recommended_version is None
