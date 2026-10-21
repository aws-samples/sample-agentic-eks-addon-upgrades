"""A component that cannot reach the target gets a row a person can decide.

THE DEFECT THESE PIN, in the shape it was found. keda's approved fact records no version
series supporting Kubernetes 1.36 -- its newest, `2.20.x`, stops at `maximum_exclusive: 1.36`.
So Stage 4 says INCOMPATIBLE with no valid targets, Stage 5 puts keda in the plan's
`unsequenceable` array and leaves `feasible` false, and both documents are telling the truth.
The consequence was that keda reached the upgrade approval page only inside the
could-not-be-sequenced table, which carries no Approve and no Reject: the run asked a person
about every move in the upgrade and offered them nothing to answer about the one component that
would not survive it. The only paths were to abandon the upgrade, or to proceed with no record
that anybody had accepted the risk.

WHAT IS FIXED AND WHAT IS DELIBERATELY NOT. The plan document is not touched and `feasible`
stays false -- it is a digest-cited statement that not every component can ride to the target,
and rewriting it would make the document assert something untrue. The consent to proceed anyway
is a PER-CLUSTER decision, so it is mapped onto the per-cluster approval in `view/build.py` and
nowhere upstream.

THE ROW IS A REAL MOVE NOW, AND THAT IS THE SECOND HALF OF THIS FILE. The first version said
keda "stays where it is", which was true of the target and false of the component: keda can go
from 2.17.2 to 2.20.2, that upgrade is worth taking, and a person who has decided to keep
running keda unsupported is exactly the person who wants its release notes. So the row carries
`2.17.x -> 2.20.2` and a brief, and approving it means BOTH THINGS AT ONCE -- upgrade keda as
far as it goes, AND accept that 1.36 stays unsupported. The row must therefore be legible as a
move and as a warning simultaneously; a version in the Recommended column beside an Approve
button reads as "press this and you are done", which is still false.

WHERE THE MOVE SHAPE STOPS. A component already running the newest release anybody recorded has
nothing to move to, and the row keeps its original dead-end shape byte for byte -- inventing
`2.20.x -> 2.20.2` beside a live Approve button would be a fabricated step. Both shapes carry
the same kind, because the kind is what makes the row separately identifiable to
`AUTHORISABLE_KINDS`, to the pill and to the note above the table.

THE WORDING IS UNDER TEST, NOT JUST THE PLUMBING, and that is the point of half this file. So
these tests assert the row does not claim to resolve the target, in the phrasings a future edit
is most likely to reintroduce.

AND THE STEP NUMBER IS UNDER TEST FROM BOTH ENDS. The row is numbered after the last plan step
by `view/build.py`, and `stages/upgrade_brief.py` has to key its brief request on the SAME
number, because `AdviceView.for_step(order)` joins a brief to a row by `step_order`. Two places
computing that number is two places for it to diverge, and the failure is silent: the row says
`No brief` and nobody learns why. The last section of this file fails if they ever disagree.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from eks_addon_readiness.decide.assessment import (
    ComponentDecision,
    HopDecision,
    ReadinessAssessment,
)
from eks_addon_readiness.facts.ledger import FactStatus, GapDetail, GapEntry, GapLedger
from eks_addon_readiness.knowledge import (
    Citation,
    CompatibilityRule,
    ComponentKnowledge,
    ReleaseRecord,
    UpgradeStrategy,
    VerificationCheck,
    VersionRange,
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
from eks_addon_readiness.stages.upgrade_approval import AUTHORISABLE_KINDS, _step_parts
from eks_addon_readiness.storage.step_tokens import StepGrant
from eks_addon_readiness.upgrade import Decision, StepDecision
from eks_addon_readiness.view import build_document
from eks_addon_readiness.view.model import UNSUPPORTED_AT_TARGET_KIND
from eks_addon_readiness.view.render.html import to_html

# THE LIVE SHAPE, REDUCED TO THE SMALLEST FACT THAT PRODUCES IT. keda 2.17.2 installed, a
# window on 2.20.x that stops one minor short of the target, and 2.20.2 as the newest release
# anybody recorded. The reason string is the one the deployed Stage 5 wrote.
_NOW = datetime(2026, 9, 15, tzinfo=UTC)
_CITE = (
    Citation(
        url="https://example.test/keda/compatibility",
        title="official compatibility matrix",
        retrieved_at=_NOW,
    ),
)
_CLUSTER = ClusterReference(name="my-eks-cluster", region="us-east-1")
_HOP = KubernetesHop(current_version="1.35", target_version="1.36")
_SNAPSHOT = "sha256:" + "a" * 64
_STORE_DIGEST = "sha256:" + "5" * 64
_UNSEQUENCEABLE_REASON = (
    "No known version series supports Kubernetes 1.36, so the component cannot ride "
    "the upgrade past that point."
)


def _knowledge() -> ComponentKnowledge:
    return ComponentKnowledge(
        knowledge_base_revision="keda-researched-2026-09-15",
        canonical_id="keda",
        canonical_repository="https://example.test/keda",
        official_documentation=("https://example.test/keda/docs",),
        compatibility_rules=(
            CompatibilityRule(
                component_version_series="2.17.x",
                kubernetes_versions=VersionRange(
                    minimum_inclusive="1.30", maximum_exclusive="1.33"
                ),
                evidence=_CITE,
            ),
            CompatibilityRule(
                component_version_series="2.20.x",
                kubernetes_versions=VersionRange(
                    minimum_inclusive="1.33", maximum_exclusive="1.36"
                ),
                evidence=_CITE,
            ),
        ),
        releases=tuple(
            ReleaseRecord(
                version=version,
                released_at=_NOW,
                source_revision="main",
                evidence=_CITE,
            )
            # DELIBERATELY NOT IN ORDER, and 2.20.2 is not last. The newest recorded release has
            # to come out of a comparison rather than out of the tuple's tail, or a fact that
            # happened to list its releases newest-first would silently report its oldest.
            for version in ("2.17.2", "2.20.2", "2.20.1", "2.19.0")
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


class _Store:
    """The two components' facts, in the shape `view/build.py` asks a `FactStore` for."""

    def __init__(self, facts: dict[str, ComponentKnowledge]) -> None:
        self._facts = facts

    def find(self, identity):
        return self._facts.get(identity or "")

    def digest(self):
        return _STORE_DIGEST

    def load_errors(self):
        return ()


@pytest.fixture
def store() -> _Store:
    return _Store({"keda": _knowledge()})


def _inventory_at(installed: dict[str, str] | None = None) -> InventoryReport:
    """keda and metrics-server: one that cannot cross, one that can.

    A BUILDER RATHER THAN ONLY A FIXTURE, because the installed version is now read from this
    document by the brief stage as well as from the assessment by the page. A test that changes what
    keda is running has to change it in BOTH or it builds a run that cannot happen -- Stage 4 copies
    this value, it does not re-derive it.
    """
    installed = installed or {}
    specs = [
        ("HelmRelease/keda/keda", "keda", "2.17.2"),
        ("HelmRelease/observability/metrics-server", "metrics-server", "0.6.4"),
    ]
    specs = [(ref, identity, installed.get(ref, v)) for ref, identity, v in specs]
    return InventoryReport(
        cluster=_CLUSTER,
        kubernetes_version="1.35",
        snapshot_digest=_SNAPSHOT,
        snapshot_captured_at="2026-09-15T10:00:00+00:00",
        ruleset_digest="sha256:" + "1" * 64,
        components=tuple(
            ComponentReport(
                identity=identity,
                identity_source="HELM_RELEASE",
                identity_detail="fixture",
                classification="recognized",
                classification_reason="fixture",
                assessment_priority="high",
                version=version,
                version_source="HELM_RELEASE",
                confidence=Confidence.HIGH,
                workload_ref=ref,
                namespace=ref.split("/")[1],
                install_method="HELM",
            )
            for ref, identity, version in specs
        ),
    )


@pytest.fixture
def keda_inventory() -> InventoryReport:
    return _inventory_at()


def _ledger_for(inventory: InventoryReport) -> GapLedger:
    return GapLedger(
        cluster=_CLUSTER,
        kubernetes_version="1.35",
        report_digest=inventory.digest(),
        snapshot_digest=_SNAPSHOT,
        store_digest=_STORE_DIGEST,
        ruleset_digest="sha256:" + "2" * 64,
        entries=tuple(
            GapEntry(
                workload_ref=c.workload_ref,
                identity=c.identity,
                version=c.version,
                version_series="2.17.x",
                assessment_priority="high",
                # The LEDGER's own view of these components is beside the point here -- it feeds
                # only the answered/researchable gap tiles -- so it takes the shape that needs
                # no revision or range, exactly as the shared fixture does.
                status=FactStatus.FACT_MISSING,
                detail_code=GapDetail.NO_ENTRY,
                reason="fixture: not the subject of this test",
            )
            for c in inventory.components
        ),
    )


@pytest.fixture
def keda_ledger(keda_inventory: InventoryReport) -> GapLedger:
    return _ledger_for(keda_inventory)


@pytest.fixture
def keda_assessment(
    keda_inventory: InventoryReport, keda_ledger: GapLedger
) -> ReadinessAssessment:
    """keda INCOMPATIBLE with no valid targets; metrics-server needs a move."""
    return ReadinessAssessment(
        cluster=_CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        hops=(_HOP,),
        snapshot_digest=_SNAPSHOT,
        report_digest=keda_inventory.digest(),
        ledger_digest=keda_ledger.digest(),
        store_digest=_STORE_DIGEST,
        ruleset_digest="sha256:" + "3" * 64,
        decisions=(
            ComponentDecision(
                workload_ref="HelmRelease/keda/keda",
                identity="keda",
                installed_version="2.17.2",
                installed_series="2.17.x",
                assessment_priority="high",
                fact_status=str(FactStatus.FACT_AVAILABLE),
                knowledge_base_revision="keda-researched-2026-09-15",
                hops=(
                    HopDecision(
                        hop=_HOP,
                        verdict=CompatibilityVerdict.INCOMPATIBLE,
                        valid_targets=(),
                        reason=(
                            "No series recorded in keda-researched-2026-09-15 supports "
                            "Kubernetes 1.36."
                        ),
                        decided_from="stored-fact",
                    ),
                ),
            ),
            ComponentDecision(
                workload_ref="HelmRelease/observability/metrics-server",
                identity="metrics-server",
                installed_version="0.6.4",
                installed_series="0.6.x",
                assessment_priority="high",
                fact_status=str(FactStatus.FACT_AVAILABLE),
                hops=(
                    HopDecision(
                        hop=_HOP,
                        verdict=CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE,
                        valid_targets=("0.9.x",),
                        reason="The installed series is not permitted at 1.36.",
                        decided_from="stored-fact",
                    ),
                ),
            ),
        ),
    )


def _hop(order: int) -> PlanStep:
    """A cluster hop at a chosen order. Used to build plans whose orders are not contiguous."""
    return PlanStep(
        order=order,
        kind=StepKind.CLUSTER_UPGRADE,
        description="Upgrade the cluster.",
        from_kubernetes="1.35",
        to_kubernetes="1.36",
    )


_DEFAULT_STEPS = (
    PlanStep(
        order=1,
        kind=StepKind.COMPONENT_MOVE,
        description="Move metrics-server to 0.9.x before the cluster hop.",
        component="metrics-server",
        from_series="0.6.x",
        to_series="0.9.x",
        recommended_version="0.9.0",
        at_kubernetes="1.35",
        reversibility="REVERSIBLE",
        reversibility_reason="No storage-version migration crosses this jump.",
    ),
    _hop(2),
)


def _plan(
    assessment: ReadinessAssessment,
    ledger: GapLedger,
    unsequenceable: tuple[UnsequenceableComponent, ...],
    *,
    feasible: bool,
    steps: tuple[PlanStep, ...] | None = None,
) -> UpgradePlan:
    return UpgradePlan(
        cluster=_CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        feasible=feasible,
        steps=_DEFAULT_STEPS if steps is None else steps,
        unsequenceable=unsequenceable,
        snapshot_digest=_SNAPSHOT,
        report_digest=assessment.report_digest,
        ledger_digest=ledger.digest(),
        assessment_digest=assessment.digest(),
        store_digest=_STORE_DIGEST,
        ruleset_digest="sha256:" + "4" * 64,
    )


_KEDA_UNSUPPORTED = UnsequenceableComponent(
    workload_ref="HelmRelease/keda/keda",
    identity="keda",
    verdict="INCOMPATIBLE",
    reason=_UNSEQUENCEABLE_REASON,
)


@pytest.fixture
def keda_plan(keda_assessment, keda_ledger) -> UpgradePlan:
    return _plan(
        keda_assessment, keda_ledger, (_KEDA_UNSUPPORTED,), feasible=False
    )


def _document(assessment, plan, inventory, ledger, store):
    document, _html, _markdown = build_document(
        assessment=assessment,
        plan=plan,
        inventory=inventory,
        ledger=ledger,
        store=store,
    )
    return document


@pytest.fixture
def document(keda_assessment, keda_plan, keda_inventory, keda_ledger, store):
    return _document(keda_assessment, keda_plan, keda_inventory, keda_ledger, store)


def _forms(document) -> dict[int, dict[str, dict]]:
    """A presigned form per authorisable row, as the stage builds them."""
    return {
        s.order: {
            action: {
                "url": "https://bucket.s3.amazonaws.com/",
                "fields": {"key": f"upgrade-clicks/run=run-7/nonce=N{s.order}{action[0]}"},
            }
            for action in ("approve", "reject")
        }
        for s in document.view.steps
        if s.kind in AUTHORISABLE_KINDS
    }


def _row_for(page: str, order: int) -> str:
    found = re.search(rf'<tr class="step" id="s{order}">.*?</tr>', page, re.DOTALL)
    assert found is not None, f"step {order} has no row on the page"
    return found[0]


# ---------------------------------------------------------------------------
# The row exists, and it is one row
# ---------------------------------------------------------------------------


def test_an_incompatible_unsequenceable_entry_becomes_exactly_one_authorisable_row(
    document,
) -> None:
    """One entry, one row, and the row is one the approval stage will ask about.

    "Exactly one" is the assertion that matters. The same component is ALSO in the report's
    unresolved list -- it is decided yet unsequenceable, which is `UnresolvedOrigin.PLAN_ONLY` --
    and a second step row would give a person two controls for one decision, which is the shape
    the pending-approval validator refuses on the other page for the same reason.
    """
    rows = [s for s in document.view.steps if s.unsupported_at_target]
    assert len(rows) == 1
    assert rows[0].kind == UNSUPPORTED_AT_TARGET_KIND
    assert rows[0].kind in AUTHORISABLE_KINDS
    # And it is the component the plan named, not a name derived from somewhere else.
    assert _step_parts(rows[0])[0] == "keda"
    # Still counted once in the unresolved list, which is a different list answering a
    # different question.
    assert [u.workload_ref for u in document.view.unresolved] == ["HelmRelease/keda/keda"]
    assert document.view.totals.unresolved == 1


def test_the_plans_own_steps_keep_the_order_they_had(document, keda_plan) -> None:
    """THE ASSERTION A RECORDED DECISION DEPENDS ON.

    A `StepDecision` is keyed on `step_order` and the `StepGrant` behind it is minted against
    that number. Inserting the new row anywhere but after the last plan step would renumber the
    steps below it, and every decision already recorded against this plan would then point at a
    different step than the person read -- silently, because nothing about a decision document
    says which step numbering it was written under.
    """
    plan_orders = [s.order for s in keda_plan.steps]
    view_orders = [
        s.order for s in document.view.steps if not s.unsupported_at_target
    ]
    assert view_orders == plan_orders
    added = [s.order for s in document.view.steps if s.unsupported_at_target]
    assert added == [max(plan_orders) + 1]
    # Orders are unique across the whole list, or two rows would share a grant key.
    orders = [s.order for s in document.view.steps]
    assert len(orders) == len(set(orders))


def test_the_row_carries_approve_and_reject(document) -> None:
    """The point of the whole change: a person can now decide something about keda."""
    page = to_html(
        document.view,
        document.provenance(),
        advice=_advice_for_moves(document),
        forms=_forms(document),
    )
    row = _row_for(page, _unsupported_order(document))
    assert row.count("<form") == 2
    assert 'class="btn btn-approve"' in row
    assert 'class="btn btn-reject"' in row


def test_the_grant_records_the_component_and_versions_not_the_description(
    document,
) -> None:
    """What makes the durable decision legible a month later.

    `stages/upgrade_approval.py::_step_parts` is what a `StepGrant` -- and therefore the
    `StepDecision` built from it -- takes its component, installed and recommended fields from.
    Unparsed, all four collapse into "the whole description went in the component field", and the
    one row whose meaning is hardest to reconstruct later would be the least legible of the lot.
    """
    row = next(s for s in document.view.steps if s.unsupported_at_target)
    component, installed, recommended, at_kubernetes = _step_parts(row)
    assert component == "keda"
    # THE SERIES, NOT THE EXACT RELEASE, and the change is the point of the move shape. The row
    # is `2.17.x -> 2.20.2`, so the grant records the same two ends every other move in the
    # decision trail records: the series moved from and the version moved to. The exact installed
    # release stays in the sentence -- see the row's own Installed cell, which draws it.
    assert installed == "2.17.x"
    assert recommended == "2.20.2"
    assert at_kubernetes == "1.36"
    assert "Installed 2.17.2." in row.description
    # The fallback would have put the whole sentence in the component field.
    assert component != row.description


def test_the_newest_recorded_release_is_the_newest_and_not_the_last_listed(
    document,
) -> None:
    """2.20.2, from a fact that lists 2.17.2 first and 2.19.0 last.

    Ordered by the same key Stage 5 orders versions with, so the report and the plan can never
    disagree about which recorded release is the newest.
    """
    row = next(s for s in document.view.steps if s.unsupported_at_target)
    assert _step_parts(row)[2] == "2.20.2"


def test_the_durable_decision_carries_the_component_and_not_a_paragraph(
    document,
) -> None:
    """The whole chain: row -> `_step_parts` -> `StepGrant` -> `StepDecision`.

    The grant is what the stage mints and the watcher redeems, and the `StepDecision` written to
    S3 is built from its fields. This is the record a person reads six weeks later to answer "who
    accepted running keda unsupported on this cluster, and what were they shown" -- so the fields
    are asserted here at the end of the chain rather than only where they are parsed.
    """
    row = next(s for s in document.view.steps if s.unsupported_at_target)
    component, installed, recommended, at_kubernetes = _step_parts(row)
    grant = StepGrant(
        action="approve",
        approver="someone@example.test",
        step_order=row.order,
        step_kind=row.kind,
        component=component,
        installed=installed,
        recommended=recommended,
        at_kubernetes=at_kubernetes,
    )
    decision = StepDecision(
        cluster=_CLUSTER,
        target_kubernetes_version="1.36",
        plan_digest="sha256:" + "a" * 64,
        presented_page_digest="sha256:" + "b" * 64,
        run_id="run-7",
        step_order=grant.step_order,
        step_kind=grant.step_kind or "COMPONENT_MOVE",
        component=grant.component or "unknown",
        installed=grant.installed,
        recommended=grant.recommended,
        at_kubernetes=grant.at_kubernetes,
        decision=Decision.APPROVED,
        approver=grant.approver,
        decided_at=datetime(2026, 9, 15, 21, 0, tzinfo=UTC),
    )
    assert decision.component == "keda"
    # THE KIND IS STILL ITS OWN KIND, and that matters most here of anywhere. The row is a move
    # now, so the temptation is to record it as `COMPONENT_MOVE` -- and then six weeks later this
    # decision is indistinguishable from a move that carried its component to the target, which
    # is the one thing it did not do.
    assert decision.step_kind == UNSUPPORTED_AT_TARGET_KIND
    assert decision.installed == "2.17.x"
    assert decision.recommended == "2.20.2"
    assert decision.at_kubernetes == "1.36"


def test_a_component_with_no_recorded_release_still_gets_a_decidable_row(
    assessment, plan, inventory, ledger
) -> None:
    """The other half of the parse, on the shared fixture's own INCOMPATIBLE entry.

    `bespoke-controller` has no resolved identity, no installed version and no fact, so there is
    no release to name and nothing to look up. That must produce a row -- a component nobody
    knows anything about is exactly the one a person most needs to be asked about -- and it must
    parse, because the alternative is a `StepGrant` whose component field is the whole sentence.
    """
    document, _html, _md = build_document(assessment, plan, inventory, ledger)
    rows = [s for s in document.view.steps if s.unsupported_at_target]
    assert len(rows) == 1
    component, installed, recommended, at_kubernetes = _step_parts(rows[0])
    assert component == "Deployment/apps/bespoke-controller"
    assert installed == "unknown"
    assert recommended == ""
    assert at_kubernetes == "1.36"
    # And the page says so rather than drawing an empty cell that looks like a value.
    page = to_html(
        document.view,
        document.provenance(),
        advice=_advice_for_moves(document),
        forms=_forms(document),
    )
    row = _row_for(page, rows[0].order)
    assert "no release is recorded, and none would cover the target" in row
    assert row.count("<form") == 2


# ---------------------------------------------------------------------------
# The wording
# ---------------------------------------------------------------------------


def test_the_row_does_not_claim_to_resolve_the_target(document) -> None:
    """THE ONE HARD WORDING REQUIREMENT, asserted on the row's own bytes.

    The row sits in a table whose other rows carry a cluster to its target, in a column layout
    shaped to say "this version becomes that version at this minor". Left to those cells, a
    recorded release beside an Approve button reads as "approve and keda is fine and you are
    done" -- and that is false however far keda moves. So the row must state that no version
    supports the target, must say what approving it actually accepts, and must not read as a step
    that carries anything to 1.36.
    """
    page = to_html(
        document.view,
        document.provenance(),
        advice=_advice_for_moves(document),
        forms=_forms(document),
    )
    row = _row_for(page, _unsupported_order(document))

    # It states the fact, in the register the fact-approval page already uses.
    assert "No recorded version series supports Kubernetes 1.36." in row
    # AND IT STATES IT AGAIN AGAINST THE VERY RELEASE IT IS MOVING TO, which is the assertion
    # that keeps the move shape honest. "either" is load-bearing: it says the release in the
    # Recommended column is subject to the same sentence as the series above it.
    assert "does NOT cover Kubernetes 1.36 either" in row
    # It states what approving accepts, and that it is not progress.
    assert "accepts running keda unsupported on this cluster" in row
    assert "does not make the upgrade reach 1.36" in row
    # It does not borrow the words that mean a move that reaches the target. `_KIND_WORDS` gives
    # the other three kinds "required move", "recommended move" and "control-plane hop"; this row
    # must not read as any of them.
    assert "required move" not in row
    assert "recommended move" not in row
    assert "no supported version at target" in row
    # NOT "INSTALL", ANYWHERE, EVEN THOUGH THIS IS NOW A MOVE. "(install 2.20.2)" is the plan's
    # phrasing for a step that carries a component to a supported version, and the word invites
    # "install this and you are done". The row says what it takes the component to, and what that
    # still does not reach, in its own words.
    assert "install" not in row.lower()
    # AND THE MOVE IS DRAWN AS A MOVE. `ver-move` is the class the page paints a move target
    # with; the first version of this row refused it because there was no move to paint. There is
    # one now -- 2.17.x to 2.20.2 -- and refusing the emphasis would hide half of what approving
    # this row does. The warning under it is what stops that reading as arrival.
    assert "ver-move" in row
    assert "2.17.x" in row and "2.20.2" in row
    assert "running 2.17.2" in row
    assert "stays where it is" not in row


def test_a_move_with_no_brief_says_nothing_was_read_and_keeps_its_controls(
    document,
) -> None:
    """THE ADVISORY STAGE MAY NOT GATE THIS DECISION, so its silence is a sentence.

    `_advice_for_moves` deliberately advises only the plan's real moves, which is what a run whose
    brief stage is switched off, whose assemble step could not write, or whose brief document could
    not be read looks like from here. The row still has to render, still has to carry Approve and
    Reject, and still has to say that nothing was read -- rather than reporting the absence as a
    decision nobody made.

    NOT `Brief: N/A`, which was right while there was no move and is wrong now. That badge says
    there is nothing to read release notes ACROSS; there is -- 2.17.2 to 2.20.2 -- and nobody read
    them.

    NEVER THE AWS-MANAGED REASON EITHER. That badge says AWS has already qualified this version
    against this Kubernetes minor, so no release notes were owed. Nothing has qualified anything
    here, and reusing that sentence would tell a reader the component is fine.
    """
    page = to_html(
        document.view,
        document.provenance(),
        advice=_advice_for_moves(document),
        forms=_forms(document),
    )
    row = _row_for(page, _unsupported_order(document))
    assert "No brief" in row
    assert "nothing was read about this move" in row
    assert "Brief: N/A" not in row
    assert "AWS publishes and qualifies" not in row
    assert row.count("<form") == 2


def test_a_dead_end_row_still_says_there_is_nothing_to_brief_across(
    assessment, plan, inventory, ledger
) -> None:
    """The other shape keeps `Brief: N/A` and its own reason, byte for byte.

    `bespoke-controller` has no fact and no recorded release, so there is no move and never was
    one. Reporting that as "nothing was read" would say research failed, when in fact there is
    nothing for research to read across -- the distinction `brief/document.py` draws between a gap
    and a decision, on the one row where both look alike.
    """
    document, _html, _md = build_document(assessment, plan, inventory, ledger)
    row = _row_for(
        to_html(
            document.view,
            document.provenance(),
            advice=_advice_for_moves(document),
            forms=_forms(document),
        ),
        _unsupported_order(document),
    )
    assert "Brief: N/A" in row
    assert "There is no move here for one to describe" in row
    assert "AWS publishes and qualifies" not in row
    assert "stays where it is" in row
    assert "ver-move" not in row


def test_the_page_still_says_the_plan_does_not_reach_the_target(document) -> None:
    """`feasible: false` stays legible, in words, beside the controls.

    The header pill was enough while this table held only steps that carry the cluster forward.
    A table of Approve buttons is a task, and the reasonable inference from answering every row
    is that answering them all completes the thing -- which it does not.
    """
    page = to_html(
        document.view,
        document.provenance(),
        advice=_advice_for_moves(document),
        forms=_forms(document),
    )
    assert document.view.feasible is False
    assert "Kubernetes 1.36: 1 unresolved (keda)" in page, (
        "the header pill is the other half of this claim, and it names the unresolved component"
    )
    assert "This plan does <strong>not</strong> reach Kubernetes 1.36" in page
    assert "Whether to upgrade the cluster anyway is your decision" in page


# ---------------------------------------------------------------------------
# What must NOT change
# ---------------------------------------------------------------------------


def test_a_plan_with_nothing_unsequenceable_renders_the_same_bytes(
    keda_assessment, keda_inventory, keda_ledger, store
) -> None:
    """No entry, no row, no new markup -- byte for byte the page it was.

    Asserted as an EQUALITY BETWEEN TWO RENDERS rather than against a stored snapshot, because
    the interesting half is the second plan: an UNRESOLVED entry in `unsequenceable` must also
    produce nothing. Offering "accept running it unsupported" for a component whose
    compatibility question is merely open would ask a person to consent to a risk nobody has
    measured; the honest answer there is research, and it stays in the Unresolved section.
    """
    empty = _plan(keda_assessment, keda_ledger, (), feasible=True)
    undecided_only = _plan(
        keda_assessment,
        keda_ledger,
        (
            UnsequenceableComponent(
                workload_ref="HelmRelease/keda/keda",
                identity="keda",
                verdict="UNRESOLVED",
                reason="Undecided in Stage 4, so it cannot be placed.",
            ),
        ),
        feasible=True,
    )
    without = _document(keda_assessment, empty, keda_inventory, keda_ledger, store)
    with_undecided = _document(
        keda_assessment, undecided_only, keda_inventory, keda_ledger, store
    )

    assert [s.order for s in without.view.steps] == [1, 2]
    assert not any(s.unsupported_at_target for s in with_undecided.view.steps)

    # The plan digest differs between the two (one names a component), so compare the rendered
    # sequence card rather than the whole page, which prints the digest chain.
    assert _sequence_card(without) == _sequence_card(with_undecided)
    for page in (_sequence_card(without), _sequence_card(with_undecided)):
        assert UNSUPPORTED_AT_TARGET_KIND not in page
        assert "nocover" not in page
        assert "notreached" not in page
        assert "Unsupported at target" not in page

    # AND THE APPROVAL PAGE'S BODY, which is where the note and the row would appear. Only the
    # BODY: `_ADVICE_CSS` is emitted whole for any page carrying advice, so the two stylesheet
    # rules the note needs travel with every upgrade approval page whether it draws one or not.
    # Asserted on the markup because that is what a reader sees and what a decision is made
    # from; the shared `_CSS` and `_CONTROL_CSS` the read-only report and the FACT approval page
    # depend on are untouched, which is the byte guarantee that actually has a dependent.
    card = _approval_sequence(without, _forms(without))
    assert card == _approval_sequence(with_undecided, _forms(with_undecided))
    assert "<form" in card, "the fixture must still carry controls, or this proves nothing"
    for marker in ("nocover", "notreached", "Unsupported at target", "Brief: N/A"):
        assert marker not in card


def test_the_read_only_report_grows_no_control(document) -> None:
    """The row reaches the plain report as a row, and never as a button.

    `view.steps` feeds every renderer, so the new row appears in the read-only report too -- and
    it should: a report that omits the component nobody can upgrade is the report that made this
    invisible in the first place. What it must not grow is a control, which is what
    `render/approval.py` shares this module's stylesheet and cell helpers with.
    """
    page = to_html(document.view, document.provenance())
    assert "<form" not in page
    assert "<button" not in page
    assert 'table class="seq"' not in page
    assert "table.seq" not in page
    assert 'class="chip' not in page
    # The row is there, stated as a dead end rather than as a step.
    assert "Unsupported at target: keda" in page
    assert "Unsupported" in page
    assert "Kubernetes 1.36: 1 unresolved (keda)" in page


def test_two_builds_of_the_unsupported_row_are_byte_identical(
    keda_assessment, keda_plan, keda_inventory, keda_ledger, store
) -> None:
    """No clock and no iteration order in the new rows either.

    The property every document in this pipeline has. The row is derived from a fact's release
    list and a plan's `unsequenceable` array, and both are iterated -- so a set anywhere in that
    path would make the report non-reproducible for a reason no reader could diagnose.
    """
    first, first_html, first_md = build_document(
        keda_assessment, keda_plan, keda_inventory, keda_ledger, store
    )
    second, second_html, second_md = build_document(
        keda_assessment, keda_plan, keda_inventory, keda_ledger, store
    )
    assert first.digest() == second.digest()
    assert first_html == second_html
    assert first_md == second_md


# ---------------------------------------------------------------------------
# Helpers that need the document
# ---------------------------------------------------------------------------


def _unsupported_order(document) -> int:
    return next(s.order for s in document.view.steps if s.unsupported_at_target)


def _approval_sequence(document, forms) -> str:
    """The approval page's sequence card: the note, the rows and the controls.

    NARROWED TO THAT CARD DELIBERATELY, and the two things it excludes are both legitimate
    differences rather than noise. The digest chain moves because two plans that differ only in
    `unsequenceable` have different plan digests. The Unresolved card and its tile move because
    an UNRESOLVED entry in `unsequenceable` DOES belong there -- that is the pre-existing,
    correct behaviour this change leaves alone, and it is the reason such an entry gets no
    control here. What must not move is the card a person decides from.
    """
    page = to_html(
        document.view,
        document.provenance(),
        advice=_advice_for_moves(document),
        forms=forms,
    )
    start = page.index('id="sequence"')
    return page[start : page.index('id="unresolved"', start)]


def _sequence_card(document) -> str:
    """The upgrade sequence card of the read-only report, isolated from the digest chain."""
    page = to_html(document.view, document.provenance())
    start = page.index("Upgrade sequence")
    return page[start : page.index("</details>", start)]


def _advice_for_moves(document):
    """Advice for the plan's real moves and nothing else: the degraded run, as a fixture.

    THIS IS NOW A DELIBERATELY INCOMPLETE ADVICE VIEW rather than the only one the pipeline can
    produce. The brief stage does request a brief for an unsupported-at-target row -- see the last
    section of this file -- so this fixture stands for the runs where it could not deliver one:
    the stage switched off, an assemble step that could not write, a brief document that would not
    read back. The row still has to render and still has to carry its controls, because an
    advisory stage may never gate an approval.
    """
    from eks_addon_readiness.view.model import (
        AdvicePanel,
        AdviceView,
        StepAdvice,
    )

    return AdviceView(
        observed_at="2026-09-15T16:40:19+00:00",
        steps=tuple(
            StepAdvice(
                step_order=s.order,
                component="metrics-server",
                panels=(
                    AdvicePanel(
                        section="BREAKING_CHANGES",
                        state="NONE_FOUND",
                        reason="the project announced nothing",
                    ),
                ),
                observed_at="2026-09-15T16:40:19+00:00",
                reuse_note="researched fresh",
            )
            for s in document.view.steps
            if s.kind == "COMPONENT_MOVE"
        ),
    )


# ---------------------------------------------------------------------------
# THE BRIEF, AND THE ONE NUMBER TWO STAGES HAVE TO AGREE ABOUT
#
# THE HAZARD THESE PIN, and it is a hazard created by giving this row a brief. The row is not a
# plan step, so its step order is MINTED -- `view/build.py` numbers it after the last real step.
# The brief stage has to key its request on the same number, because
# `view/model.py::AdviceView.for_step(order)` joins a brief to a row BY `step_order`. Two places
# computing `max(order) + 1` is two places for it to drift, and the failure is silent in the worst
# way available: the page renders `No brief`, the brief document contains a perfectly good brief
# nothing displays, and no exception is raised anywhere. Nobody learns why.
#
# THE FIX IS STRUCTURAL AND THESE TESTS ARE WHAT MAKES IT CHECKABLE. There is exactly one
# numbering rule, `view.build.unsupported_at_target`, and both callers read it. The tests below
# derive the expected number a THIRD time -- from the plan, in the test -- so they fail if either
# side drifts from the stated rule and not merely if the two drift apart.
# ---------------------------------------------------------------------------


def _brief_plan(plan, inventory, fact_store, monkeypatch) -> dict:
    """Run `stages/upgrade_brief.py` in `plan` mode over these documents, with no AWS at all.

    The two reads and the store are the stage's only outside dependencies on this path: the reuse
    lookup lists an S3 prefix and returns empty for a falsy bucket, which is what makes the whole
    work-list decision exercisable here.

    THE INVENTORY AND NOT THE ASSESSMENT, which is the document this stage's IAM role is allowed to
    read (`infra/terraform/modules/brief/main.tf` grants `plans/*` and `inventories/*`). The installed version it
    carries is the same string Stage 4 records -- see `view/build.py::installed_from_assessment` --
    and `test_the_two_installed_version_sources_are_the_same_number` is what holds that true.
    """
    from eks_addon_readiness.stages import upgrade_brief

    # ASSERTED HERE RATHER THAN DISCOVERED LATER. The stage treats an unreadable inventory as an
    # absent one -- it is advisory and may not fail a run -- so a caller that hands this helper the
    # wrong object gets an empty work list and a test failure that names neither. This turns that
    # into one sentence at the point of the mistake.
    assert isinstance(plan, UpgradePlan) and isinstance(inventory, InventoryReport), (
        "_brief_plan takes (plan, inventory): the stage reads the Stage 2 report for the installed "
        f"version, and it was handed {type(inventory).__name__}"
    )
    documents = {"s3://b/plan.json": plan, "s3://b/inventory.json": inventory}

    def _read(uri, model):
        document = documents[uri]
        assert isinstance(document, model), f"{uri} was read as the wrong model"
        return document

    monkeypatch.setattr(upgrade_brief, "read_document", _read)
    monkeypatch.setattr(upgrade_brief, "build_fact_store", lambda: fact_store)
    monkeypatch.delenv("DOCUMENT_BUCKET", raising=False)
    return upgrade_brief.handler(
        {
            "plan_uri": "s3://b/plan.json",
            "inventory_uri": "s3://b/inventory.json",
            "mode": "plan",
        }
    )


def _requested(payload: dict, component: str) -> dict:
    """The one work item for a component, or a failure naming what was actually emitted."""
    items = [i for i in payload["brief_to_research"] if i["component"] == component]
    assert len(items) == 1, (
        f"expected exactly one brief request for {component}, got "
        f"{[i['component'] for i in payload['brief_to_research']]}"
    )
    return items[0]


def test_the_brief_stage_asks_for_a_brief_for_a_component_it_has_no_step_for(
    keda_plan, keda_inventory, store, monkeypatch
) -> None:
    """THE FEATURE: keda gets briefed on the move it can still make.

    `stages/upgrade_brief.py` used to walk `plan.steps` only, and keda has none -- it is in
    `unsequenceable`. So the one row on the approval page whose meaning is hardest to reconstruct
    was also the only decision on it with nothing read behind it. The request now goes out for the
    jump that does exist, installed release to newest recorded release, which is exactly what
    somebody who has decided to keep running keda unsupported needs to read.
    """
    payload = _brief_plan(keda_plan, keda_inventory, store, monkeypatch)
    item = _requested(payload, "keda")
    step = PlanStep.model_validate(item["step"])

    assert step.to_series == "2.20.2"
    assert step.recommended_version == "2.20.2"
    # THE EXACT INSTALLED RELEASE, not the series the row shows: the walk reads release notes
    # between two points, and starting at 2.17.0 would read notes for patches already installed.
    assert step.from_series == "2.17.2"
    assert item["installed_version"] == "2.17.2"
    # NOT AT THE TARGET. The move happens on the cluster as it stands; claiming it applies at 1.36
    # would assert the component reaches a minor no recorded series of it supports.
    assert step.at_kubernetes == "1.35"
    assert step.to_series != keda_plan.target_kubernetes_version
    # The fact behind it was read once, here, not once per Map iteration.
    assert item["repository"] == "https://example.test/keda"
    assert "2.20.2" in item["known_releases"]
    # And the payload says which requests belong to a component that cannot reach the target, so a
    # run whose numbers do not line up can be diagnosed from the log.
    assert payload["brief_unsupported_at_target"] == {step.order: "keda"}
    assert "cannot reach Kubernetes 1.36" in payload["summary"]


def test_the_brief_lands_on_the_row_the_page_draws_and_not_beside_it(
    document, keda_plan, keda_inventory, store, monkeypatch
) -> None:
    """THE WHOLE JOIN, END TO END, with the step number taken only from the stage.

    Nothing in this test tells the page which row to attach the brief to. The order comes out of
    the brief stage's own work item, travels through `ComponentBrief`, `UpgradeBrief` and
    `advice_from_brief` exactly as it does in a run, and the page has to find the row. If the two
    numbering rules ever diverge, the Details toggle disappears and the assertions below fail.
    """
    from eks_addon_readiness.brief.document import UpgradeBrief, component_brief
    from eks_addon_readiness.research.pages import PageSet
    from eks_addon_readiness.view.advice import advice_from_brief

    payload = _brief_plan(keda_plan, keda_inventory, store, monkeypatch)
    step = PlanStep.model_validate(_requested(payload, "keda")["step"])

    # The document the harness would write for that work item. `extraction=None` is the honest
    # total failure -- five NOT_LOOKED sections -- which is enough to prove the JOIN and keeps this
    # test out of the business of inventing an agent's answer.
    brief = component_brief(
        step,
        walk=None,
        extraction=None,
        pages=PageSet(),
        session_id=None,
        observed_at="2026-09-15T16:40:19+00:00",
        failure_reason="the fixture supplied no extraction",
    )
    advice = advice_from_brief(
        UpgradeBrief(
            observed_at="2026-09-15T16:40:19+00:00",
            cluster=_CLUSTER,
            target_kubernetes_version="1.36",
            plan_digest=keda_plan.digest(),
            store_digest=_STORE_DIGEST,
            ruleset_digest="sha256:" + "6" * 64,
            briefs=(brief,),
        )
    )

    order = _unsupported_order(document)
    assert brief.step_order == order, (
        "the brief stage keyed its request on a different step than the page drew the row at, so "
        "the brief would attach to nothing and the row would read 'No brief' with no explanation"
    )
    assert advice.for_step(order) is not None

    page = to_html(
        document.view, document.provenance(), advice=advice, forms=_forms(document)
    )
    row = _row_for(page, order)
    assert 'class="detbtn"' in row, "the row that has a brief must offer the toggle that opens it"
    assert "No brief" not in row
    # The brief's own row follows immediately, which is what `:has() +` relies on to open it.
    assert f'<tr class="step" id="s{order}">' in page
    assert page.index('tr class="briefrow"', page.index(f'id="s{order}"')) > 0
    # AND THE ROW STILL CARRIES BOTH HALVES. A brief does not soften what approving means.
    assert "does NOT cover Kubernetes 1.36 either" in row
    assert "accepts running keda unsupported on this cluster" in row


@pytest.mark.parametrize(
    "steps",
    [
        pytest.param((), id="no-steps"),
        pytest.param((_hop(1),), id="one-step"),
        pytest.param(_DEFAULT_STEPS, id="two-steps"),
        pytest.param((*_DEFAULT_STEPS, _hop(3)), id="three-steps"),
        # THE CASE THAT BREAKS A REIMPLEMENTATION. Plan orders need not be contiguous --
        # `order/recommend.py` appends its optional steps by the same `max(order) + 1` -- so a
        # second copy of the arithmetic written as `len(steps) + 1` would pass every case above
        # and collide with an existing step here.
        pytest.param((*_DEFAULT_STEPS, _hop(5)), id="gap-in-the-orders"),
    ],
)
def test_the_row_and_the_brief_request_cannot_disagree_about_the_step_number(
    steps, keda_assessment, keda_ledger, keda_inventory, store, monkeypatch
) -> None:
    """THE DELIVERABLE OF THIS SECTION: one number, derived three ways, all three equal.

    The plan's own steps are numbered by Stage 5. The row is numbered by `view/build.py`. The brief
    request is numbered by `stages/upgrade_brief.py`. The expected value is computed a third time
    here, from the plan, so this fails if EITHER caller drifts from the rule rather than only if
    they drift apart -- which is the failure a "they agree with each other" assertion would miss on
    the day both are rewritten together.
    """
    plan = _plan(
        keda_assessment, keda_ledger, (_KEDA_UNSUPPORTED,), feasible=False, steps=steps
    )
    expected = max((s.order for s in plan.steps), default=0) + 1

    document = _document(keda_assessment, plan, keda_inventory, keda_ledger, store)
    rows = [s for s in document.view.steps if s.unsupported_at_target]
    assert [s.order for s in rows] == [expected]

    payload = _brief_plan(plan, keda_inventory, store, monkeypatch)
    assert payload["brief_unsupported_at_target"] == {expected: "keda"}
    assert _requested(payload, "keda")["step"]["order"] == expected

    # And the join really closes at that number, on the artifacts rather than on the arithmetic.
    assert PlanStep.model_validate(_requested(payload, "keda")["step"]).order == rows[0].order


def test_two_unreachable_components_are_numbered_and_briefed_in_one_stable_order(
    keda_assessment, keda_ledger, keda_inventory, store, monkeypatch
) -> None:
    """Two entries, two consecutive numbers, ordered by the key neither document derives.

    `workload_ref` orders them, because it is carried verbatim by the plan: two runs over the same
    plan must mint the same rows in the same order, or the page's bytes and the brief's step
    numbers would depend on the order the plan happened to list them in.

    The second component has no fact, so it has nothing to move to -- it takes a row and a number,
    and correctly no brief request. That asymmetry is the reason the numbers cannot be inferred
    from the length of the request list.
    """
    second = UnsequenceableComponent(
        workload_ref="Deployment/apps/aardvark-controller",
        identity="aardvark-controller",
        verdict="INCOMPATIBLE",
        reason=_UNSEQUENCEABLE_REASON,
    )
    plan = _plan(
        keda_assessment, keda_ledger, (_KEDA_UNSUPPORTED, second), feasible=False
    )
    document = _document(keda_assessment, plan, keda_inventory, keda_ledger, store)
    rows = [s for s in document.view.steps if s.unsupported_at_target]
    # Ordered by `workload_ref` -- `Deployment/...` before `HelmRelease/...` -- and NOT by the
    # display name, which is what each row is drawn with and is a different string.
    assert [(_step_parts(r)[0], r.order) for r in rows] == [
        ("aardvark-controller", 3),
        ("keda", 4),
    ]

    payload = _brief_plan(plan, keda_inventory, store, monkeypatch)
    assert payload["brief_unsupported_at_target"] == {4: "keda"}


def test_a_component_with_nothing_newer_recorded_is_a_stay_and_asks_for_no_brief(
    store, monkeypatch
) -> None:
    """Already on 2.20.2: no invented move, no brief request, and the original row shape.

    A component no series of which reaches the target may already be running the last release
    anybody published. A row reading `2.20.x -> 2.20.2` for that cluster would be a fabricated
    step beside a live Approve button, and the brief request behind it would ask an agent to read
    the release notes of a jump with no releases in it.

    THE INVENTORY IS REBUILT AT 2.20.2 TOO, and that is not fixture bookkeeping. The page reads the
    installed version from the assessment and the brief stage reads it from the inventory, which
    are the same string in a real run because Stage 4 copies it (see
    `view/build.py::installed_from_assessment`). Leaving the shared inventory at 2.17.2 here would
    build a run that cannot happen, and then assert about how the two disagree.
    """
    from eks_addon_readiness.decide.assessment import ComponentDecision

    keda_inventory = _inventory_at({"HelmRelease/keda/keda": "2.20.2"})
    keda_ledger = _ledger_for(keda_inventory)
    assessment = ReadinessAssessment(
        cluster=_CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        hops=(_HOP,),
        snapshot_digest=_SNAPSHOT,
        report_digest=keda_inventory.digest(),
        ledger_digest=keda_ledger.digest(),
        store_digest=_STORE_DIGEST,
        ruleset_digest="sha256:" + "3" * 64,
        decisions=(
            ComponentDecision(
                workload_ref="HelmRelease/keda/keda",
                identity="keda",
                installed_version="2.20.2",
                installed_series="2.20.x",
                assessment_priority="high",
                fact_status=str(FactStatus.FACT_AVAILABLE),
                hops=(
                    HopDecision(
                        hop=_HOP,
                        verdict=CompatibilityVerdict.INCOMPATIBLE,
                        valid_targets=(),
                        reason="No series supports Kubernetes 1.36.",
                        decided_from="stored-fact",
                    ),
                ),
            ),
        ),
    )
    plan = _plan(assessment, keda_ledger, (_KEDA_UNSUPPORTED,), feasible=False)
    document = _document(assessment, plan, keda_inventory, keda_ledger, store)
    row = next(s for s in document.view.steps if s.unsupported_at_target)
    assert "Move 2.20.x" not in row.description
    assert _step_parts(row)[1] == "2.20.2"

    page = to_html(
        document.view,
        document.provenance(),
        advice=_advice_for_moves(document),
        forms=_forms(document),
    )
    rendered = _row_for(page, row.order)
    assert "stays where it is" in rendered
    assert "ver-move" not in rendered
    assert "Brief: N/A" in rendered

    payload = _brief_plan(plan, keda_inventory, store, monkeypatch)
    assert payload["brief_unsupported_at_target"] == {}
    assert not [i for i in payload["brief_to_research"] if i["component"] == "keda"]


def test_a_brief_the_stage_could_not_produce_leaves_the_row_decidable(document) -> None:
    """The advisory stage may fail; the decision may not disappear with it.

    An `unadvised` entry is the brief stage saying it tried and could not. The row names the reason
    and keeps both controls -- the acceptance being asked for is about this cluster, and it is not
    made safer or less necessary by whether anybody managed to read a release note.
    """
    from eks_addon_readiness.view.model import AdviceView, UnadvisedStep

    order = _unsupported_order(document)
    page = to_html(
        document.view,
        document.provenance(),
        advice=AdviceView(
            observed_at="2026-09-15T16:40:19+00:00",
            unadvised=(
                UnadvisedStep(
                    step_order=order,
                    component="keda",
                    reason="the release notes could not be read",
                ),
            ),
        ),
        forms=_forms(document),
    )
    row = _row_for(page, order)
    assert "No brief" in row
    assert "the release notes could not be read" in row
    assert row.count("<form") == 2
    assert "accepts running keda unsupported on this cluster" in row


def test_the_two_installed_version_sources_are_the_same_number(
    keda_inventory, keda_assessment
) -> None:
    """THE OTHER INPUT BOTH SIDES READ, and the claim that lets them read it from two documents.

    `unsupported_at_target` decides move-or-stay from the installed version. The page has the
    assessment open and the brief stage has the inventory open -- its role may read `plans/*` and
    `inventories/*` and nothing else -- so the two adapters must produce the same map, on the same
    key, for every component both documents name.

    THE CHAIN THAT MAKES IT TRUE is `inventory/resolve.py` -> `facts/gaps.py` -> `decide/engine.py`,
    each copying the version verbatim. This test is what would notice if one of them started
    normalising: the symptom otherwise is a row that offers a move the brief stage never requested,
    which renders as a perfectly plausible `No brief`.
    """
    from eks_addon_readiness.view.build import (
        installed_from_assessment,
        installed_from_inventory,
    )

    from_inventory = installed_from_inventory(keda_inventory)
    from_assessment = installed_from_assessment(keda_assessment)
    shared = set(from_inventory) & set(from_assessment)
    assert shared, "the two fixtures name no component in common, so this proves nothing"
    assert {k: from_inventory[k] for k in shared} == {k: from_assessment[k] for k in shared}
    # And the key is the one the plan carries verbatim, not a display name.
    assert "HelmRelease/keda/keda" in shared


def test_neither_the_page_nor_the_brief_stage_numbers_this_row_for_itself() -> None:
    """THE STRUCTURAL HALF: the arithmetic exists once, and the stage reads it rather than copies.

    The behavioural tests above compare the two answers. This one refuses the shape that makes
    disagreement possible in the first place, because two correct copies of `max(order) + 1` are
    one edit away from being one correct copy and one stale one -- and nothing about that edit
    looks like a bug in a diff.

    SCOPED TO THESE TWO MODULES. `order/recommend.py` numbers the plan's own optional steps by the
    same arithmetic and is a different rule about a different document; forbidding the expression
    package-wide would be forbidding Stage 5 from numbering its own steps.
    """
    import pathlib

    package = pathlib.Path(build_document.__module__.split(".")[0])
    root = pathlib.Path(__file__).resolve().parents[2] / package
    rules = (root / "view" / "build.py").read_text()
    stage = (root / "stages" / "upgrade_brief.py").read_text()

    numbering = re.compile(r"max\([^\n]*\.order[^\n]*\+ 1")
    assert len(numbering.findall(rules)) == 1, (
        "view/build.py must mint the unsupported-at-target step order in exactly one place; "
        f"found {len(numbering.findall(rules))}"
    )
    assert not numbering.findall(stage), (
        "stages/upgrade_brief.py computes a step order of its own. It must read "
        "view.build.unsupported_at_target instead, or a brief will one day attach to a row "
        "nobody drew"
    )
    assert "unsupported_at_target" in stage, (
        "the brief stage no longer reads the shared numbering rule"
    )


# ---------------------------------------------------------------------------
# The release carries an approved unsupported-at-target move (run6)
# ---------------------------------------------------------------------------


def test_an_approved_unsupported_move_is_released_and_not_refused(
    keda_plan, keda_inventory, store
) -> None:
    """Run6: approving keda's row beside real steps rejected the WHOLE release.

    The row's order is minted after the last plan step, so a release path that only knew
    `plan.steps` refused it as unknown and took the approved plan steps down with it. Now the
    same synthesis the brief stage uses turns it into a move from what is installed to the
    newest recorded release, and both the mail and the record carry it.
    """
    from eks_addon_readiness.stages import release_prep
    from eks_addon_readiness.stages.release_record import install_methods

    extra = release_prep.unsupported_moves(keda_plan, keda_inventory, store)
    keda_order = next(s.order for s in extra if s.component == "keda")

    changes = release_prep.approved_changes(keda_plan, [1, keda_order], extra=extra)
    assert changes == ("metrics-server 0.6.x -> 0.9.0", "keda 2.17.2 -> 2.20.2")

    steps = release_prep.approved_steps(
        keda_plan,
        [1, keda_order],
        install_methods=install_methods(keda_inventory),
        extra=extra,
    )
    keda = next(s for s in steps if s.component == "keda")
    assert (keda.order, keda.to_version, keda.install_method) == (keda_order, "2.20.2", "HELM")
    # Plan order is kept: the unsupported move is numbered after every real plan step.
    assert [s.order for s in steps] == sorted(s.order for s in steps)


def test_without_the_inventory_an_unsupported_order_is_still_refused(keda_plan) -> None:
    """No installed versions, no move: the old refusal stands, so the gate still fails closed."""
    from eks_addon_readiness.stages import release_prep

    unknown = max(s.order for s in keda_plan.steps) + 1
    with pytest.raises(ValueError, match="are not in the plan"):
        release_prep.approved_changes(keda_plan, [1, unknown])


def test_only_the_dead_end_unsupported_row_loses_its_control(document) -> None:
    """A row with a newer release keeps Approve; a row already on the newest is withheld.

    The dead-end row has nothing to release, so an Approve there could only mint an approval
    the release step has no move for. The move-shaped row is a real lane and keeps its button.
    """
    from types import SimpleNamespace

    from eks_addon_readiness.stages.upgrade_approval import _withheld_until_the_cluster_moves

    move_row = next(s for s in document.view.steps if s.unsupported_at_target)
    dead_end = SimpleNamespace(
        order=move_row.order + 1,
        kind=UNSUPPORTED_AT_TARGET_KIND,
        aws_managed=False,
        description=(
            "Unsupported at target: kyverno has no recorded version series supporting "
            "Kubernetes 1.36. Installed 1.19.1; newest recorded release 1.19.1 does NOT cover "
            "target. Approving records that this cluster accepts running kyverno unsupported "
            "at Kubernetes 1.36; it does not make the component reach the target."
        ),
    )
    view = SimpleNamespace(
        steps=(move_row, dead_end),
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
    )
    withheld = _withheld_until_the_cluster_moves(view, {})
    assert move_row.order not in withheld
    assert "nothing to upgrade" in withheld[dead_end.order]
