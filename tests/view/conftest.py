"""Fixtures for the presentation stage.

THE FIXTURE THAT MATTERS. `assessment_with_gaps` / `plan_with_gaps` build a run in
which the undecided set and the unsequenceable set genuinely overlap -- two
components in both, one undecided but sequenced, one decided yet unsequenceable.

An earlier grouping fixture set `unsequenceable=()`
(`tests/research/test_render_grouping.py:134`), which is why the production defect
that motivated its de-duplication fix had no regression test at all. Every
overlap-shaped case a naive renderer could not see is exercised here.
"""

from __future__ import annotations

import pytest

from eks_addon_readiness.decide.assessment import (
    ComponentDecision,
    HopDecision,
    ReadinessAssessment,
)
from eks_addon_readiness.facts.ledger import (
    FactStatus,
    GapDetail,
    GapEntry,
    GapLedger,
)
from eks_addon_readiness.models import (
    ClusterReference,
    CompatibilityVerdict,
    Confidence,
    KubernetesHop,
)
from eks_addon_readiness.order.plan import (
    PlanStep,
    StepKind,
    UnsequenceableComponent,
    UpgradePlan,
)
from eks_addon_readiness.report import ComponentReport, InventoryReport

CLUSTER = ClusterReference(name="fixture-cluster", region="us-east-1")
HOP = KubernetesHop(current_version="1.35", target_version="1.36")

# Digests are fixtures too: the real pipeline threads them from document to
# document, and `build_document` refuses a mismatched set, so the fixtures have to
# be self-consistent. `_a`..`_f` keep them readable.
_D = {k: "sha256:" + c * 64 for k, c in zip("abcdef", "abcdef", strict=True)}


def _decision(
    ref: str,
    identity: str | None,
    version: str | None,
    verdict: CompatibilityVerdict,
    reason: str,
) -> ComponentDecision:
    return ComponentDecision(
        workload_ref=ref,
        identity=identity,
        installed_version=version,
        installed_series=None,
        assessment_priority="high",
        fact_status=str(
            FactStatus.FACT_MISSING
            if verdict is CompatibilityVerdict.UNRESOLVED
            else FactStatus.FACT_AVAILABLE
        ),
        hops=(
            HopDecision(
                hop=HOP,
                verdict=verdict,
                # An UNRESOLVED verdict must not carry valid_targets: the
                # contract forbids smuggling advice into a non-answer.
                valid_targets=(
                    () if verdict is CompatibilityVerdict.UNRESOLVED else ("1.9.x",)
                ),
                reason=reason,
                decided_from=(
                    "none"
                    if verdict is CompatibilityVerdict.UNRESOLVED
                    else "stored-fact"
                ),
            ),
        ),
    )


@pytest.fixture
def inventory() -> InventoryReport:
    """Six components: two managed add-ons, three Helm releases, one bare workload."""
    specs = [
        ("Deployment/kube-system/coredns", "coredns", "EKS_MANAGED_ADDON"),
        ("DaemonSet/kube-system/aws-node", "vpc-cni", "EKS_MANAGED_ADDON"),
        ("HelmRelease/observability/metrics-server", "metrics-server", "HELM"),
        ("HelmRelease/keda/keda", "keda", "HELM"),
        ("HelmRelease/cert-manager/cert-manager", "cert-manager", "HELM"),
        ("Deployment/apps/bespoke-controller", None, "MANIFEST"),
    ]
    return InventoryReport(
        cluster=CLUSTER,
        kubernetes_version="1.35",
        snapshot_digest=_D["a"],
        snapshot_captured_at="2026-09-12T10:00:00+00:00",
        ruleset_digest="sha256:" + "1" * 64,
        components=tuple(
            ComponentReport(
                identity=identity,
                identity_source="HELM_RELEASE",
                identity_detail="fixture",
                classification="recognized",
                classification_reason="fixture",
                assessment_priority="high",
                version="1.0.0",
                version_source="HELM_RELEASE",
                confidence=Confidence.HIGH,
                workload_ref=ref,
                namespace=ref.split("/")[1] if "/" in ref else None,
                install_method=method,
            )
            for ref, identity, method in specs
        ),
    )


@pytest.fixture
def ledger(inventory: InventoryReport) -> GapLedger:
    return GapLedger(
        cluster=CLUSTER,
        kubernetes_version="1.35",
        report_digest=inventory.digest(),
        snapshot_digest=_D["a"],
        store_digest="sha256:" + "5" * 64,
        ruleset_digest="sha256:" + "2" * 64,
        entries=tuple(
            GapEntry(
                workload_ref=c.workload_ref,
                identity=c.identity,
                version=c.version,
                version_series="1.0.x",
                assessment_priority="high",
                status=FactStatus.FACT_MISSING,
                detail_code=GapDetail.NO_ENTRY,
                reason="fixture: no entry",
            )
            for c in inventory.components
        ),
    )


@pytest.fixture
def assessment(inventory: InventoryReport, ledger: GapLedger) -> ReadinessAssessment:
    """Three undecided, three decided. `keda` and `cert-manager` are also blocked."""
    return ReadinessAssessment(
        cluster=CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        hops=(HOP,),
        snapshot_digest=_D["a"],
        report_digest=inventory.digest(),
        ledger_digest=ledger.digest(),
        store_digest=ledger.store_digest,
        ruleset_digest="sha256:" + "3" * 64,
        decisions=(
            _decision(
                "Deployment/kube-system/coredns",
                "coredns",
                "1.13.2",
                CompatibilityVerdict.COMPATIBLE,
                "AWS lists the installed version as compatible.",
            ),
            _decision(
                "DaemonSet/kube-system/aws-node",
                "vpc-cni",
                "1.22.4",
                CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE,
                "The installed series is not permitted at 1.36.",
            ),
            _decision(
                "HelmRelease/observability/metrics-server",
                "metrics-server",
                "0.6.4",
                CompatibilityVerdict.UNRESOLVED,
                "No compatibility fact is recorded for series 0.6.x.",
            ),
            _decision(
                "HelmRelease/keda/keda",
                "keda",
                "2.14.0",
                CompatibilityVerdict.UNRESOLVED,
                "No compatibility fact is recorded for series 2.14.x.",
            ),
            _decision(
                "HelmRelease/cert-manager/cert-manager",
                "cert-manager",
                "1.14.0",
                CompatibilityVerdict.UNRESOLVED,
                "No compatibility fact is recorded for series 1.14.x.",
            ),
            _decision(
                "Deployment/apps/bespoke-controller",
                None,
                None,
                CompatibilityVerdict.INCOMPATIBLE,
                "No version of this component supports 1.36.",
            ),
        ),
    )


@pytest.fixture
def plan(assessment: ReadinessAssessment, ledger: GapLedger) -> UpgradePlan:
    """The overlap fixture that exercises the de-duplication.

    `unsequenceable` names FOUR components:
      - keda and cert-manager, which are ALSO UNRESOLVED in Stage 4 (the overlap);
      - bespoke-controller, which was DECIDED (INCOMPATIBLE) and is blocked for a
        different reason -- the only genuinely additional row;
      - metrics-server is deliberately absent, so an undecided-but-not-listed
        component is covered too.
    """
    return UpgradePlan(
        cluster=CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        feasible=False,
        steps=(
            PlanStep(
                order=1,
                kind=StepKind.COMPONENT_MOVE,
                description="Move vpc-cni to 1.23.x before the cluster hop.",
                component="vpc-cni",
                from_series="1.22.x",
                to_series="1.23.x",
                recommended_version="1.23.1",
                at_kubernetes="1.35",
                reversibility="ONE_WAY",
                reversibility_reason="A CRD storage-version migration cannot be undone.",
            ),
            PlanStep(
                order=2,
                kind=StepKind.CLUSTER_UPGRADE,
                description="Upgrade the cluster.",
                from_kubernetes="1.35",
                to_kubernetes="1.36",
                unordered_with=(1,),
            ),
            PlanStep(
                order=3,
                kind=StepKind.COMPONENT_UPGRADE_RECOMMENDED,
                description="Optional: move coredns to its newest supported series.",
                component="coredns",
                from_series="1.13.x",
                to_series="1.14.x",
                recommended_version="1.14.3",
                at_kubernetes="1.36",
            ),
        ),
        unsequenceable=(
            UnsequenceableComponent(
                workload_ref="HelmRelease/keda/keda",
                identity="keda",
                verdict="UNRESOLVED",
                reason="Undecided in Stage 4, so it cannot be placed.",
            ),
            UnsequenceableComponent(
                workload_ref="HelmRelease/cert-manager/cert-manager",
                identity="cert-manager",
                verdict="UNRESOLVED",
                reason="Undecided in Stage 4, so it cannot be placed.",
            ),
            UnsequenceableComponent(
                workload_ref="Deployment/apps/bespoke-controller",
                identity=None,
                verdict="INCOMPATIBLE",
                reason="Decided incompatible; no feasible move window exists.",
            ),
        ),
        snapshot_digest=_D["a"],
        report_digest=assessment.report_digest,
        ledger_digest=ledger.digest(),
        assessment_digest=assessment.digest(),
        store_digest=ledger.store_digest,
        ruleset_digest="sha256:" + "4" * 64,
    )
