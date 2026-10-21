"""F2 and F6: the report must not contradict the plan about whether a component stays.

TWO FINDINGS, ONE TEST FILE, AND THAT IS THE POINT. The second is why the first survived: no
test in
`tests/view/` passed `store=`, so `_recommended_version`'s `newest_supported_concrete`
branch never executed, and with `store=None` it falls through to
`if COMPATIBLE: return decision.installed_version` -- which makes `stays` trivially and
correctly True. The defect was only reachable on the path no test took.

So this test does both things the existing ones do not:

1. **Supplies a fact store**, so the knowledge branch runs at all.
2. **Uses the version spelling a live EKS add-on actually reports** --
   `v1.13.2-eksbuild.21`, not the bare `1.13.2` of `conftest.py`. With a bare installed
   version the two sides compare equal by accident and the defect stays hidden even with
   a store wired.

WHAT THE DEFECT WAS. `view/build.py` compared `recommended == decision.installed_version`
as raw strings. `newest_supported_concrete` returns a bare concrete version (`1.13.2`);
the cluster reports `v1.13.2-eksbuild.21`. Stage 5 makes the same stay-or-move decision
and normalises both sides first (`order/interleave.py:_differs_from_installed`, whose
docstring names this exact case). So the plan emitted no move while the report rendered
the component as moving to the version it is already on -- report and plan disagreeing
about a displayed value, which is the defect class `view/model.py` opens by saying this
stage exists to remove.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from eks_addon_readiness.knowledge import ComponentKnowledge
from eks_addon_readiness.models import (
    ClusterReference,
    CompatibilityVerdict,
    Confidence,
    KubernetesHop,
)
from eks_addon_readiness.order.plan import PlanStep, StepKind, UpgradePlan
from eks_addon_readiness.report import ComponentReport, InventoryReport
from eks_addon_readiness.view import build_document, build_report_view

CLUSTER = ClusterReference(name="stays-fixture", region="us-east-1")
HOP = KubernetesHop(current_version="1.35", target_version="1.36")

# What a live EKS-managed add-on reports, read off the deployed cluster's inventory
# document. The `v` prefix and the `-eksbuild.N` suffix are the whole difficulty.
INSTALLED = "v1.13.2-eksbuild.21"
# What `newest_supported_concrete` returns for it: a bare concrete version.
RECOMMENDED = "1.13.2"
REF = "sha256:" + "e" * 64


class OneComponentStore:
    """A `FactStore` holding one covering fact. The store no existing view test supplies."""

    def __init__(self, knowledge: ComponentKnowledge) -> None:
        self._k = {knowledge.canonical_id: knowledge}

    def find(self, identity: str) -> ComponentKnowledge | None:
        return self._k.get((identity or "").strip())

    def digest(self) -> str:
        return "sha256:" + "5" * 64


@pytest.fixture
def covering_fact(synthetic_entry_payload: dict) -> ComponentKnowledge:
    """Recorded knowledge that covers the installed series and names `1.13.2` as newest.

    Derived from the recorded entry `tests/conftest.py` AUTHORS, so the shape and the
    evidence citations are still those of a real promoted fact, re-keyed to coredns and
    re-pointed at the series the fixture cluster is actually on. Authored rather than read
    from a packaged file store, which this sample does not ship -- the derivation below is
    unchanged, so what this fixture proves is unchanged with it.
    """
    payload = synthetic_entry_payload
    payload["canonical_id"] = "coredns"
    payload["aliases"] = []
    payload["compatibility_rules"] = [
        {
            **payload["compatibility_rules"][0],
            "component_version_series": "1.13.x",
            "kubernetes_versions": {"minimum_inclusive": "1.30", "maximum_exclusive": "1.40"},
        }
    ]
    payload["releases"] = [
        {**payload["releases"][0], "version": RECOMMENDED},
    ]
    payload["refreshed_at"] = datetime(2026, 9, 12, tzinfo=UTC).isoformat()
    return ComponentKnowledge.model_validate(payload)


@pytest.fixture
def inventory() -> InventoryReport:
    return InventoryReport(
        cluster=CLUSTER,
        kubernetes_version="1.35",
        snapshot_digest=REF,
        snapshot_captured_at="2026-09-12T10:00:00+00:00",
        ruleset_digest="sha256:" + "1" * 64,
        components=(
            ComponentReport(
                identity="coredns",
                identity_source="EKS_MANAGED_ADDON",
                identity_detail="fixture",
                classification="recognized",
                classification_reason="fixture",
                assessment_priority="high",
                version=INSTALLED,
                version_source="EKS_MANAGED_ADDON",
                confidence=Confidence.HIGH,
                workload_ref="Deployment/kube-system/coredns",
                namespace="kube-system",
                install_method="EKS_MANAGED_ADDON",
            ),
        ),
    )


@pytest.fixture
def ledger(inventory: InventoryReport) -> GapLedger:
    return GapLedger(
        cluster=CLUSTER,
        kubernetes_version="1.35",
        report_digest=inventory.digest(),
        snapshot_digest=REF,
        store_digest="sha256:" + "5" * 64,
        ruleset_digest="sha256:" + "2" * 64,
        entries=(
            GapEntry(
                workload_ref="Deployment/kube-system/coredns",
                identity="coredns",
                version=INSTALLED,
                version_series="1.13.x",
                assessment_priority="high",
                status=FactStatus.FACT_AVAILABLE,
                detail_code=GapDetail.RULE_MATCH,
                reason="fixture: a covering rule exists",
                fact_knowledge_base_revision="fixture-1",
                fact_kubernetes_versions={
                    "minimum_inclusive": "1.30",
                    "maximum_exclusive": "1.40",
                },
            ),
        ),
    )


@pytest.fixture
def assessment(inventory: InventoryReport, ledger: GapLedger) -> ReadinessAssessment:
    """coredns COMPATIBLE, which is the verdict that reaches the stays comparison."""
    return ReadinessAssessment(
        cluster=CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        hops=(HOP,),
        snapshot_digest=REF,
        report_digest=inventory.digest(),
        ledger_digest=ledger.digest(),
        store_digest=ledger.store_digest,
        ruleset_digest="sha256:" + "3" * 64,
        decisions=(
            ComponentDecision(
                workload_ref="Deployment/kube-system/coredns",
                identity="coredns",
                installed_version=INSTALLED,
                installed_series="1.13.x",
                assessment_priority="high",
                fact_status=str(FactStatus.FACT_AVAILABLE),
                hops=(
                    HopDecision(
                        hop=HOP,
                        verdict=CompatibilityVerdict.COMPATIBLE,
                        valid_targets=("1.13.x",),
                        reason="The installed series is permitted at 1.36.",
                        decided_from="stored-fact",
                    ),
                ),
            ),
        ),
    )


@pytest.fixture
def plan(assessment: ReadinessAssessment, ledger: GapLedger) -> UpgradePlan:
    """The plan Stage 5 actually produces here: a cluster hop and NO recommendation.

    Stage 5 normalises both versions before deciding, recognises `v1.13.2-eksbuild.21` and
    `1.13.2` as the same version, and so emits no COMPONENT_UPGRADE_RECOMMENDED step. That
    absence is what the report has to agree with.
    """
    return UpgradePlan(
        cluster=CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        feasible=True,
        steps=(
            PlanStep(
                order=1,
                kind=StepKind.CLUSTER_UPGRADE,
                description="Upgrade the cluster.",
                from_kubernetes="1.35",
                to_kubernetes="1.36",
            ),
        ),
        unsequenceable=(),
        snapshot_digest=REF,
        report_digest=assessment.report_digest,
        ledger_digest=ledger.digest(),
        assessment_digest=assessment.digest(),
        store_digest=ledger.store_digest,
        ruleset_digest="sha256:" + "4" * 64,
    )


def test_the_knowledge_branch_actually_runs(
    assessment, plan, inventory, ledger, covering_fact
):
    """F6 guard: prove the store is reached, or the test below proves nothing.

    With `store=None` the recommendation falls through to the installed version and the
    comparison is trivially equal. This asserts the recommendation came from the FACT --
    a bare `1.13.2`, not the decorated installed string -- so the branch containing F2 is
    genuinely on the path this test takes.
    """
    view = build_report_view(
        assessment, plan, inventory, ledger, OneComponentStore(covering_fact)
    )
    row = view.components[0]
    assert row.installed_version == INSTALLED
    assert row.recommended_version == RECOMMENDED, (
        "the recommendation must come from newest_supported_concrete via the store; if it "
        "equals the installed string the store branch did not run and F2 stays hidden"
    )
    assert row.confidence == "HIGH", "the store's evidence_confidence should reach the row"
    assert row.sources, "the fact's citations should reach the row"


def test_f2_a_component_the_plan_leaves_alone_is_reported_as_staying(
    assessment, plan, inventory, ledger, covering_fact
):
    """F2: `stays` must use the same normalisation Stage 5 uses.

    `v1.13.2-eksbuild.21` installed and `1.13.2` recommended are the same version. Stage 5
    says so by emitting no step; the report must say so by setting `stays`.
    """
    view = build_report_view(
        assessment, plan, inventory, ledger, OneComponentStore(covering_fact)
    )
    row = view.components[0]
    assert row.stays is True, (
        f"installed {row.installed_version!r} and recommended {row.recommended_version!r} "
        "are the same version modulo the v prefix and the eksbuild suffix, so the report "
        "must not render this component as moving"
    )


def test_f2_the_report_and_the_plan_agree_on_the_rendered_page(
    assessment, plan, inventory, ledger, covering_fact
):
    """The consequence the reviewer read from `stays` but did not render. Rendered here.

    The plan emits no recommended upgrade, so the page must say "stay on this version"
    and must not present the installed version as a move target.
    """
    document, html, markdown = build_document(
        assessment, plan, inventory, ledger, OneComponentStore(covering_fact)
    )
    assert plan.recommended_upgrades == (), "fixture premise: Stage 5 recommends nothing"
    assert document.view.totals.upgrade_recommended == 0

    assert "stay on this version" in html, (
        "the HTML must mark a staying component as staying; without it the row renders "
        "the version it is already on as a move"
    )
    # The Markdown view reads the same field, so it must agree.
    assert "(stay)" in markdown
