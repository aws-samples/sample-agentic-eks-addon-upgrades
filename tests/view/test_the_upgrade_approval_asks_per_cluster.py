"""The upgrade approval page, and the decision a click on it records.

WHAT THESE TESTS PIN, because none of it is visible by reading the HTML:

1. **An approval is per cluster.** The decision names the cluster in its key and in its
   body, and `RunDecisions` refuses to be assembled from decisions naming a different one.
   This is the property that stops one team's demo approval authorising everybody's
   production, and it is a key rather than a check precisely so it cannot be forgotten.
2. **The page without advice is byte-identical to the report it was.** Extending `to_html`
   must not change what the read-only report renders, because that is what the fact
   approval page's stylesheet and cell helpers are shared with.
3. **A cluster hop is never offered.** It is the one step in the plan this system does not
   perform, and a control for it would be a button that cannot work.
4. **No nonce reaches the page's own bytes anywhere except inside a form's hidden fields.**
   A nonce in visible text, or in the stage's return value, is a capability written where
   the evidence lives -- see `notify/__init__.py`.
5. **A missing brief renders and says so.** A brief cannot gate an approval, so absence has
   to be a sentence rather than an exception.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

from eks_addon_readiness.models import ClusterReference
from eks_addon_readiness.storage.decisions import (
    parse_upgrade_click_key,
    upgrade_click_key,
)
from eks_addon_readiness.storage.keys import (
    run_decisions_key,
    step_decision_key,
    upgrade_approval_key,
)
from eks_addon_readiness.storage.step_tokens import PausedUpgrade, StepGrant
from eks_addon_readiness.upgrade import (
    Authority,
    Decision,
    RunDecisions,
    StepDecision,
)
from eks_addon_readiness.view import build_document
from eks_addon_readiness.view.model import (
    AdviceCoverage,
    AdviceFinding,
    AdvicePanel,
    AdvicePath,
    AdviceView,
    StepAdvice,
    UnadvisedStep,
)
from eks_addon_readiness.view.render.html import to_html

PLAN_D = "sha256:" + "a" * 64
PAGE_D = "sha256:" + "b" * 64
OTHER_D = "sha256:" + "c" * 64


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


@pytest.fixture
def document(assessment, plan, inventory, ledger):
    class _EmptyStore:
        def find(self, _identity):
            return None

        def digest(self):
            return "UNAVAILABLE"

        def load_errors(self):
            return ()

    doc, _html, _markdown = build_document(
        inventory=inventory,
        ledger=ledger,
        assessment=assessment,
        plan=plan,
        store=_EmptyStore(),
    )
    return doc


def _advice(document, *, panels_state: str = "REPORTED") -> AdviceView:
    """Advice for every component-move step the plan carries.

    A MOVE, WHICH IS NARROWER THAN "AUTHORISABLE" AND USED TO BE THE SAME SET. The upgrade
    approval also offers a row per component that cannot reach the target at all, minted from
    the plan's `unsequenceable` array rather than from a plan step -- and the brief stage walks
    `plan.steps`, so it never sees one and can never produce advice for one. Advising it here
    would build a fixture the pipeline cannot produce, and would then assert the page draws a
    brief for a row whose whole point is that there is no move to brief.
    """
    orders = [
        s.order
        for s in document.view.steps
        if s.kind != "CLUSTER_UPGRADE" and not s.unsupported_at_target
    ]
    finding = AdviceFinding(
        title="Ten klog logging flags are removed",
        detail="A values file still passing any of them will fail to start.",
        concern="flags or args",
        stated=True,
        release="v0.7.0",
        action="Remove them from args before upgrading.",
        action_phase="BEFORE_UPGRADE",
    )
    panels = tuple(
        AdvicePanel(
            section=section,
            state=panels_state,
            reason="a reason",
            findings=(finding,) if panels_state == "REPORTED" else (),
        )
        for section in (
            "BREAKING_CHANGES",
            "DEPRECATIONS",
            "RELEASE_NOTES",
            "KNOWN_ISSUES",
            "NICE_TO_HAVES",
        )
    )
    return AdviceView(
        observed_at="2026-09-14T16:40:19+00:00",
        steps=tuple(
            StepAdvice(
                step_order=order,
                component="metrics-server",
                chart_version="3.14.0",
                coverage=AdviceCoverage(
                    releases_on_path=6,
                    releases_read=("0.7.0", "0.9.0"),
                    minors_entered=("0.7", "0.8", "0.9"),
                    minors_unread=(),
                ),
                path=AdvicePath(must_install_in_order=(), detail="nothing on the way"),
                panels=panels,
                observed_at="2026-09-14T16:40:19+00:00",
                reuse_note="researched fresh",
            )
            for order in orders
        ),
    )


def _forms(document) -> dict[int, dict[str, dict]]:
    return {
        s.order: {
            action: {
                "url": "https://bucket.s3.amazonaws.com/",
                "fields": {
                    "key": upgrade_click_key("run-7", f"NONCE{s.order}{action[0]}"),
                    "policy": "POLICYBYTES",
                },
            }
            for action in ("approve", "reject")
        }
        for s in document.view.steps
        if s.kind != "CLUSTER_UPGRADE"
    }


def test_the_report_without_advice_is_unchanged(document) -> None:
    """The extension must be invisible when nothing asks for it.

    `render/approval.py` shares this module's stylesheet and cell helpers, so a page that
    grew rules or markup it does not use would change the fact approval page's bytes for no
    reason. Asserted on the report rather than on the stylesheet, because that is the
    artifact anyone would notice.
    """
    page = to_html(document.view, document.provenance())
    assert "<form" not in page
    assert "<button" not in page
    assert 'table class="seq"' not in page
    assert "table.seq" not in page, "the advice stylesheet leaked into a plain report"
    assert 'class="chip' not in page


def test_advice_turns_the_sequence_card_into_the_decision_surface(document) -> None:
    page = to_html(
        document.view, document.provenance(), advice=_advice(document), forms=_forms(document)
    )
    assert 'table class="seq"' in page
    # One brief row and one Details toggle per briefed step.
    briefed = len(_advice(document).steps)
    assert page.count('tr class="briefrow"') == briefed
    assert page.count('class="detbtn"') == briefed
    # Two forms per step the page was given controls for, and no more. COUNTED AGAINST THE
    # FORMS AND NOT AGAINST `briefed`, which were the same number only for as long as every
    # authorisable row was a plan move. An unsupported-at-target row is authorisable and
    # unbriefable, so tying the control count to the brief count would assert that a row with
    # no brief has no buttons -- the exact defect this whole surface exists to fix.
    assert page.count("<form") == 2 * len(_forms(document))
    assert len(_forms(document)) > briefed, (
        "this fixture no longer carries an authorisable row that gets no brief, so the "
        "assertion above no longer distinguishes the two counts"
    )


def test_a_cluster_hop_is_never_offered_a_control(document) -> None:
    """The one step this system does not perform must not carry a button, and must say why.

    A control for it would be a button that cannot work, which is the silently-failing
    control this project refuses everywhere. The cell says so instead of being blank, because
    a blank cell reads as an oversight.

    THE ABSENCE IS ASSERTED STRUCTURALLY, on the hop's own row rather than on the page, which
    is the half of this test's intent the wording never carried: the page is full of forms for
    the steps that ARE authorisable, so "no form on the page" was never assertable and "the
    cell reads X" only ever caught the label changing.

    The wording moved once, deliberately. "Not authorisable / a kind this system does not
    perform" was accurate and oblique -- it named a property of the software instead of
    answering the reader's question, which is who advances the cluster if this does not. The
    replacement says that the reader does it themselves and that nothing here will.
    """
    hops = [s for s in document.view.steps if s.kind == "CLUSTER_UPGRADE"]
    if not hops:
        pytest.skip("this plan has no cluster hop to assert about")
    page = to_html(
        document.view, document.provenance(), advice=_advice(document), forms=_forms(document)
    )
    assert "You perform this one" in page
    assert "This automation upgrades add-ons only" in page
    assert "nothing here to approve and nothing here will do it" in page

    for hop in hops:
        row = re.search(
            rf'<tr class="step" id="s{hop.order}">.*?</tr>', page, re.DOTALL
        )
        assert row is not None, f"the hop's own row is missing from the page (step {hop.order})"
        assert "<form" not in row[0], "a cluster hop was offered a control that cannot work"
        assert "<button" not in row[0], "a cluster hop was offered a control that cannot work"


def test_only_a_form_field_may_carry_a_nonce(document) -> None:
    """A nonce belongs in a hidden field the policy signs, and nowhere else on the page.

    `notify/__init__.py` names the three ways this breaks -- rendering a nonce into an S3
    object, returning one in a stage result, logging one. This asserts the first: every
    occurrence of a nonce in the page is inside a `value="..."` of a hidden input.
    """
    page = to_html(
        document.view, document.provenance(), advice=_advice(document), forms=_forms(document)
    )
    for match in re.finditer(r"NONCE\d+[ar]", page):
        window = page[max(0, match.start() - 220) : match.start()]
        assert 'type="hidden"' in window, (
            f"a nonce appears outside a hidden field at offset {match.start()}"
        )


def test_a_step_with_no_brief_still_renders_and_says_so(document) -> None:
    """A brief cannot gate an approval, so its absence is a sentence.

    The step is still offered its controls -- the decision is about the cluster, and the
    fact behind the move is unaffected by whether anybody read the release notes.
    """
    orders = [s.order for s in document.view.steps if s.kind != "CLUSTER_UPGRADE"]
    if not orders:
        pytest.skip("this plan has no component move")
    advice = AdviceView(
        observed_at="2026-09-14T16:40:19+00:00",
        unadvised=(
            UnadvisedStep(
                step_order=orders[0],
                component="metrics-server",
                reason="the release notes could not be read",
            ),
        ),
    )
    page = to_html(
        document.view, document.provenance(), advice=advice, forms=_forms(document)
    )
    assert "No brief" in page
    assert "the release notes could not be read" in page
    # The controls are still there: absence of advice is not absence of a decision.
    assert page.count("<form") == 2 * len(orders)


def test_five_not_looked_panels_do_not_read_as_five_all_clears(document) -> None:
    """Nothing produced at all has its own shape.

    Five NOT_LOOKED panels say "we know nothing here". Five empty panels would say "we
    checked and it is clean", which is the one thing this document must never imply.
    """
    page = to_html(
        document.view,
        document.provenance(),
        advice=_advice(document, panels_state="NOT_LOOKED"),
        forms=_forms(document),
    )
    assert "No section of this brief could be produced" in page
    assert "unknown, not absent" in page


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def _decision(**overrides) -> StepDecision:
    fields = {
        "cluster": ClusterReference(name="prod-1", region="us-east-1"),
        "target_kubernetes_version": "1.36",
        "plan_digest": PLAN_D,
        "presented_page_digest": PAGE_D,
        "run_id": "run-7",
        "step_order": 3,
        "step_kind": "COMPONENT_MOVE",
        "component": "metrics-server",
        "installed": "0.6.x",
        "recommended": "0.9.0",
        "at_kubernetes": "1.35",
        "decision": Decision.APPROVED,
        "approver": "someone@example.test",
        "decided_at": datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
    }
    return StepDecision(**{**fields, **overrides})


def test_the_decision_names_the_cluster_in_its_key_and_its_body() -> None:
    """Per cluster, structurally.

    A compatibility fact is keyed on component and version series because it is true
    everywhere. This is the opposite claim, so the cluster is a path segment: no listing can
    return a decision without saying which cluster it belongs to.
    """
    key = step_decision_key("prod-1", "1.36", PLAN_D, "run-7", 3)
    assert key.startswith("upgrade-decisions/cluster=prod-1/")
    assert "/plan=" in key and key.endswith("/run=run-7/step=3.json")
    assert _decision().cluster.name == "prod-1"
    # The summary sits beside the steps it summarises, so one prefix read returns both.
    assert run_decisions_key("prod-1", "1.36", PLAN_D, "run-7").rsplit("/", 1)[0] == (
        key.rsplit("/", 1)[0]
    )


def test_a_summary_refuses_a_decision_from_another_cluster() -> None:
    """The one error this document must not be able to record."""
    with pytest.raises(ValueError, match="per cluster"):
        RunDecisions(
            cluster=ClusterReference(name="prod-1", region="us-east-1"),
            target_kubernetes_version="1.36",
            plan_digest=PLAN_D,
            presented_page_digest=PAGE_D,
            run_id="run-7",
            decisions=(
                _decision(cluster=ClusterReference(name="demo", region="us-east-1")),
            ),
            finished_at=datetime(2026, 9, 15, 10, 5, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"run_id": "run-OTHER"}, "not 'run-7'"),
        ({"plan_digest": OTHER_D}, "different plan"),
    ],
)
def test_a_summary_refuses_a_decision_from_another_run_or_plan(
    overrides: dict, expected: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(expected)):
        RunDecisions(
            cluster=ClusterReference(name="prod-1", region="us-east-1"),
            target_kubernetes_version="1.36",
            plan_digest=PLAN_D,
            presented_page_digest=PAGE_D,
            run_id="run-7",
            decisions=(_decision(**overrides),),
            finished_at=datetime(2026, 9, 15, 10, 5, tzinfo=UTC),
        )


def test_undecided_is_distinguishable_from_rejected() -> None:
    """The whole reason the run summary exists.

    A reader of the per-step objects sees approvals and rejections. Only `offered` says which
    steps were shown and never answered -- and the executor has to tell an answer from a
    queue, because undecided is offered again next run.
    """
    summary = RunDecisions(
        cluster=ClusterReference(name="prod-1", region="us-east-1"),
        target_kubernetes_version="1.36",
        plan_digest=PLAN_D,
        presented_page_digest=PAGE_D,
        run_id="run-7",
        decisions=(_decision(step_order=1), _decision(step_order=2, decision=Decision.REJECTED)),
        offered=(3, 5),
        finished_at=datetime(2026, 9, 15, 10, 5, tzinfo=UTC),
    )
    assert summary.approved == (1,)
    assert summary.rejected == (2,)
    assert summary.offered == (3, 5)
    assert "2 left undecided" in summary.summary()


def test_the_authority_recorded_is_the_weaker_of_the_two() -> None:
    """A click carries no AWS principal, and the record must not imply one.

    `AWS_SIGV4` is absent from the enum rather than merely unused, so a future writer cannot
    record the stronger authority for a click that never carried it.

    THIS TEST USED TO ASSERT THE ENUM HELD EXACTLY ONE MEMBER, and that assertion was a proxy
    for two different claims: that a click records the weak authority, and that `AWS_SIGV4` does
    not exist. The proxy held only while there was nothing else legitimate to record. The
    release gate added `AWS_IAM_SSM_APPROVE` -- an authority AWS really does establish, because
    `aws:approve` refuses a signal from a principal outside its `Approvers` list -- so the
    one-member count broke while both real claims stayed true.

    Both are asserted directly now, and the count is not. A member being added is not the risk;
    a member being added AND WRITTEN ONTO A CLICK is, and the first assertion is what covers it.
    """
    assert _decision().approver_authority is Authority.EMAIL_LINK_TOKEN
    assert not hasattr(Authority, "AWS_SIGV4"), (
        "Authority gained AWS_SIGV4. The presigned forms on this page are signed by the "
        "STAGE's role and not by the person clicking, so no path here resolves a SigV4 "
        "principal for an approval -- the member could only ever be recorded wrongly."
    )


def test_a_naive_decision_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        _decision(decided_at=datetime(2026, 9, 15, 10, 0))


# ---------------------------------------------------------------------------
# The click path
# ---------------------------------------------------------------------------


def test_an_upgrade_click_key_is_not_a_fact_decision_key() -> None:
    """The property that keeps the two watchers apart.

    An upgrade click landing on `approval-decisions/` would be handed to code that redeems a
    fact grant and invokes the fact writer.
    """
    assert parse_upgrade_click_key(upgrade_click_key("run-7", "abc")) == ("run-7", "abc")
    assert parse_upgrade_click_key("approval-decisions/run=run-7/nonce=abc") is None
    assert upgrade_click_key("run-7", "abc").startswith("upgrade-clicks/")


def test_the_page_and_the_fact_page_do_not_share_a_prefix() -> None:
    """Two kinds of page under one prefix would be read by one S3 notification."""
    assert upgrade_approval_key("prod-1", "run-7").startswith("upgrade-approvals/")


def test_a_paused_upgrade_knows_what_it_has_not_been_told() -> None:
    paused = PausedUpgrade(
        run_id="run-7",
        task_token="TOKEN",
        page_uri="s3://b/k",
        cluster="prod-1",
        offered=(1, 2, 3),
        decisions={"2": "approve"},
        grants={"n": StepGrant(action="approve", approver="a@b.c", step_order=1)},
    )
    assert paused.undecided == (1, 3)
