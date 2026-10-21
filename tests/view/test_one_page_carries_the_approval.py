"""The run report is ONE page, and the approval lives on it.

WHAT WAS WRONG. The run's output was scattered across three artifacts a reader had
to reassemble: the presentation document's HTML, and a separate per-candidate
`.report.html` for each researched fact. The decision a human is actually asked to
make -- approve this fact, or do not -- was on the per-candidate page, which is not
the page anyone opens to look at the cluster. So the single most important control
in the system was on the artifact least likely to be read.

The fix is to carry the pending approvals onto the run report as a CARRIED INPUT,
the same way `Provenance` is carried: recorded on the document so `verify` can
reproduce the bytes, and never derived by a renderer.

THE TWO PROPERTIES THESE TESTS EXIST TO PIN, because both are load-bearing and
neither is visible by reading the HTML:

1. **The page cannot approve.** No pre-filled comment, no pre-selected control, no
   form that submits on load. Approving is the human's act, expressed by running a
   command signed with the human's own credentials.
2. **The page contains no unsigned POST to an AWS_IAM endpoint.** A browser form
   posts without SigV4, so an `AWS_IAM` function URL refuses it at the authorizer
   with a bare 403. A control that silently 403s is worse than no control, and the
   per-candidate page shipped exactly that.
"""

from __future__ import annotations

import re

import pytest

from eks_addon_readiness.view import approvals_from_research, build_document
from eks_addon_readiness.view.model import (
    ApprovalControls,
    PendingApproval,
    PresentationDocument,
)
from eks_addon_readiness.view.render import to_html, to_markdown
from eks_addon_readiness.view.verify import verify_document

# The two components the reference run leaves UNRESOLVED with FACT_MISSING, and the
# two the research stage therefore proposes facts for. They are deliberate test
# cases: NEITHER may be approved by anything but a person, so a test that asserted
# an approval had happened would be asserting the defect.
_KEDA = PendingApproval(
    component="keda",
    version_series="2.17.x",
    candidate_uri="s3://bucket/facts-candidates/decision=PENDING_APPROVAL/keda/c.json",
    candidate_digest="sha256:" + "1" * 64,
    report_digest="sha256:" + "2" * 64,
    confidence="MEDIUM",
    change_direction="NEW",
    headline="Approving records that keda 2.17.x supports Kubernetes 1.35 to 1.37.",
)
_CERT_MANAGER = PendingApproval(
    component="cert-manager",
    version_series="1.13.x",
    candidate_uri="s3://bucket/facts-candidates/decision=PENDING_APPROVAL/cm/c.json",
    candidate_digest="sha256:" + "3" * 64,
    report_digest="sha256:" + "4" * 64,
    confidence="LOW",
    change_direction="NEW",
    headline="Approving records that cert-manager 1.13.x supports Kubernetes 1.35.",
)

_ENDPOINT = "https://abc123.lambda-url.us-east-1.on.aws/"


def _research_result(pending: PendingApproval, **overrides: object) -> dict:
    """One research-stage result, in the shape the Step Functions Map returns.

    Built from the real result shape rather than from an `ApprovalControls` literal,
    so these tests exercise the production path -- including the sort, which is what
    keeps the rendered bytes independent of Map result order.
    """
    entry = {
        "component_id": pending.component,
        "version_series": pending.version_series,
        "candidate_uri": pending.candidate_uri,
        "candidate_digest": pending.candidate_digest,
        "report_digest": pending.report_digest,
        "confidence": pending.confidence,
        "change_direction": pending.change_direction,
        "approval_headline": pending.headline,
        "pending_approval": True,
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def approvals() -> ApprovalControls:
    # Deliberately handed to the builder in the WRONG order: keda before
    # cert-manager. A Map returns results in whatever order its iterations
    # completed, and the report's bytes must not depend on that.
    return approvals_from_research(
        [_research_result(_KEDA), _research_result(_CERT_MANAGER)],
        endpoint=_ENDPOINT,
        region="us-east-1",
    )


def test_the_builder_sorts_and_drops_what_cannot_be_acted_on() -> None:
    """Only a pending candidate with both digests gets a control.

    A rejected candidate has no fact to store, and one with no report digest could
    only produce an approval the endpoint refuses -- so neither is something a
    person can act on, and rendering a control for either would be rendering a
    control that cannot work.
    """
    controls = approvals_from_research(
        [
            _research_result(_KEDA),
            _research_result(_CERT_MANAGER, pending_approval=False),
            _research_result(_KEDA, component_id="other", report_digest=""),
            "not a dict",
        ],
        endpoint=_ENDPOINT,
        region="us-east-1",
    )
    assert tuple(p.component for p in controls.pending) == ("keda",)


def test_no_research_results_at_all_is_an_empty_surface() -> None:
    """A run where research never went. Not an error, and not a blank section."""
    controls = approvals_from_research(None, endpoint=_ENDPOINT, region="us-east-1")
    assert controls.pending == ()


@pytest.fixture
def document(assessment, plan, inventory, ledger, approvals):
    doc, html, markdown = build_document(
        assessment, plan, inventory, ledger, None, approvals
    )
    return doc, html, markdown


# ---------------------------------------------------------------------------
# ONE PAGE: the approval is on the run report, not on a separate artifact.
# ---------------------------------------------------------------------------


def test_no_credential_or_signature_is_baked_into_the_page(document) -> None:
    """A presigned approve URL would hand the capability to every page reader.

    The project's load-bearing claim is that a reader of the page can SHOW a
    pending approval and cannot GRANT one. A presigned URL inside the report is a
    bearer token for approval, and the agent reads the report by construction.
    """
    _, html, _ = document
    for leak in ("X-Amz-Signature", "X-Amz-Credential", "AWS_SECRET_ACCESS_KEY="):
        assert leak not in html, f"{leak} must never appear in a rendered report"


def test_rendering_is_byte_identical_across_two_renders(document) -> None:
    doc, html, markdown = document
    again_html = to_html(doc.view, doc.provenance())
    again_markdown = to_markdown(doc.view, doc.provenance(), doc.approvals)
    assert again_html == html
    assert again_markdown == markdown


def test_the_approvals_block_is_recorded_on_the_document(document) -> None:
    """Recorded, so `verify` can reproduce the bytes rather than guess at them."""
    doc, _, _ = document
    assert isinstance(doc, PresentationDocument)
    assert doc.approvals.endpoint == _ENDPOINT
    assert tuple(p.component for p in doc.approvals.pending) == (
        "cert-manager",
        "keda",
    ), "pending approvals are sorted by component, so the bytes cannot depend on Map order"


def test_verify_reproduces_a_report_that_carries_approvals(
    document, assessment, plan, inventory, ledger
) -> None:
    doc, _, _ = document
    outcome = verify_document(doc, assessment, plan, inventory, ledger, None)
    assert outcome.reproduced, outcome.detail
    assert outcome.html_reproduced and outcome.markdown_reproduced


# ---------------------------------------------------------------------------
# THE LOOK: dark canvas, orange accent.
# ---------------------------------------------------------------------------


_GROUP_HEADINGS = {
    "MANAGED_ADDON": "EKS-managed add-ons",
    "HELM_RELEASE": "CNCF add-ons: Helm-installed",
    "OTHER_WORKLOAD": "CNCF add-ons: other workloads",
}


# `class` may now carry `card-await` too, so the attribute is matched loosely and the
# `open` flag is read from wherever it sits in the tag. Pinning the literal `class="card"`
# made this regex silently match nothing the moment a second class was added -- and a
# regex that matches nothing turns every assertion built on it into a vacuous pass.
_SECTION = re.compile(
    r'<details class="card[^"]*"(?P<open> open)?>'
    r'<summary><span class="sum-title">(?P<title>[^<]*)'
)


def _open_sections(html: str) -> dict[str, bool]:
    """Every top-level disclosure card, mapped to whether it renders expanded.

    Parsed with one regex over the whole document rather than by splitting on the
    heading text: the `open` attribute and the heading are separated by two more
    tags, so a "what comes immediately before the heading" test is looking at the
    wrong bytes and passes or fails for reasons unrelated to the attribute.
    """
    found = {m.group("title"): bool(m.group("open")) for m in _SECTION.finditer(html)}
    assert found, "the section regex matched nothing, so every check on it is vacuous"
    return found


def test_every_card_starts_closed(document) -> None:
    """The report opens as a summary, not as every table at once.

    IT USED TO OPEN THE GROUPS THAT NEEDED ATTENTION, which was the right rule while the
    report was also the decision surface -- a control sitting in a collapsed group would have
    been invisible. Approval moved upstream, so nothing on this page needs acting on and
    there is nothing to single out; opening four wide tables of transcribed evidence at once
    just buries the tiles a reader actually starts from.

    Asserted because `open` is one word and reappearing by accident is exactly how a
    deliberate layout decision gets undone.
    """
    doc = document[0] if isinstance(document, tuple) else document
    html = to_html(doc.view, doc.provenance())
    assert 'class="card" open' not in html, (
        "a card on the run report starts expanded. Every section is collapsed by design now "
        "that the page carries no controls -- see _group_block."
    )
    assert 'class="card"' in html, "no cards rendered at all, which is a different fault"


def test_with_nothing_to_act_on_the_first_group_still_opens() -> None:
    """The fallback, so an all-compatible report does not render entirely closed.

    Exercised on the predicate rather than through a second full document: the rule
    being pinned is "actionable means a verdict other than COMPATIBLE", and a report
    where that set is empty falls back to the first non-empty group.
    """
    from eks_addon_readiness.view.render.html import _needs_action

    class _Row:
        def __init__(self, verdict: str) -> None:
            self.verdict = verdict

    assert not _needs_action([_Row("COMPATIBLE"), _Row("COMPATIBLE")])
    assert _needs_action([_Row("COMPATIBLE"), _Row("UNRESOLVED")])
    assert _needs_action([_Row("REQUIRES_COMPONENT_UPGRADE")])
    assert _needs_action([_Row("INCOMPATIBLE")])
    # An empty group renders nothing at all, so it is never the one that opens.
    assert not _needs_action([])


def test_the_page_is_dark_with_an_orange_accent(document) -> None:
    """The report is dark with orange. The audience sees this on a projector."""
    _, html, _ = document
    root = html.split(":root {", 1)[1].split("}", 1)[0]
    assert "color-scheme: dark" in root, "the default theme must be dark, not light"
    assert "--accent:#f9ab00" in root.replace(" ", ""), (
        "the accent must be the established orange"
    )


def test_nothing_can_flip_the_page_to_light_behind_the_reader_s_back(document) -> None:
    """THE ASSERTION THE FIRST SCREENSHOT EARNED.

    The first version of this renderer set dark on `:root` and then carried a
    `@media (prefers-color-scheme: light)` branch. The default was dark and the
    effective theme was not: headless Chrome reports light, so the very first
    screenshot came out light -- and so would a presenter's laptop nobody had
    switched over, which is the one machine the deck exists for.

    A test that only reads the `:root` block cannot see that, because the defect is
    in a LATER block overriding it. So this one asserts on the override: light is
    reachable only by an explicit `data-theme` stamp, never by a media query.
    """
    _, html, _ = document
    # The MEDIA QUERY, not the word: the stylesheet's own comment names
    # `prefers-color-scheme` to explain why there is no branch on it, and a bare
    # substring test would trip on the explanation rather than on the defect.
    assert "@media (prefers-color-scheme" not in html, (
        "a prefers-color-scheme branch hands the theme choice to the reading "
        "machine, and the machines this page is read on report light"
    )
    assert ':root[data-theme="light"]' in html, (
        "light must still be reachable, by an explicit host stamp"
    )
    assert '<meta name="color-scheme" content="dark">' in html, (
        "the color-scheme meta must name dark alone; advertising 'light dark' "
        "invites the browser to paint its own controls and scrollbars light "
        "against a canvas that is not"
    )


# ---------------------------------------------------------------------------
# THE DECISION LIVES IN THE COMPONENT'S OWN ROW, not in a section of its own.
# ---------------------------------------------------------------------------


def test_the_bottom_approvals_section_is_gone(document) -> None:
    _, html, _ = document
    assert 'id="approvals"' not in html
    assert "Awaiting your approval</span>" not in html
    # And the two headings whose sections speak for themselves.
    for heading in ("<h2>What to do</h2>", "<h2>What we could not say</h2>",
                    "<h2>What needs a person</h2>"):
        assert heading not in html


def test_the_run_report_carries_no_approval_control(document) -> None:
    """THE INVERSE OF WHAT THIS FILE USED TO ASSERT, and the inversion IS the design change.

    The report carried an Approve and a Reject control for every fact research proposed,
    because approval happened AFTER the report was written. The `Approval` stage now pauses
    the run BEFORE `Present`, so by the time this renders every decision is made: an approved
    fact reads out of the fact store like any other, and a refused or undecided one is an
    honest UNRESOLVED. A control here would be furniture.

    Asserted rather than assumed because a HALF-reverted change is the dangerous shape -- a
    lingering button, or a Decision column of em-dashes, tells a reader they are being asked
    for something when nothing is waiting on them.
    """
    doc = document[0] if isinstance(document, tuple) else document
    html = to_html(doc.view, doc.provenance())
    # MARKUP PATTERNS, NOT BARE CLASS NAMES. `_CSS` is shared with the approval page, which
    # still needs the button and proposal styles, so the stylesheet legitimately DEFINES
    # `.cmdrow` and `.btn-wrap` while this page emits neither. Asserting on the bare name
    # would fail on the shared stylesheet and force deleting styles the other page depends
    # on -- so each probe below is the shape the markup would actually take.
    for gone, why in (
        ("<th>Decision</th>", "a Decision column whose every cell would be empty"),
        ('class="cmdrow', "a full-width row holding a signed approve/reject command"),
        ("aws-sigv4", "a signed curl command for a decision"),
        ('class="btn-wrap', "an approve/reject control"),
        ('class="await-dot"', "an awaiting-approval marker beside a component name"),
        ('class="await-pill"', "an awaiting-approval count on a group bar"),
        (">Awaiting approval<", "an awaiting-approval tile"),
    ):
        assert gone not in html, (
            f"the run report still contains {gone!r} -- {why}. Nothing on this page awaits a "
            "decision, so it must not display one."
        )


def test_the_report_still_shows_what_the_fact_store_knows(document) -> None:
    """Removing the controls must not remove the DATA, which would be the worse regression.

    These columns are the point of the page and they are fed from the fact store through
    `ComponentRow`. The `PendingApproval` fallbacks they briefly carried existed only because
    a fact was not stored yet when the report rendered; with approval upstream the store is
    the single source, and these headers are the evidence it is still being read.
    """
    doc = document[0] if isinstance(document, tuple) else document
    html = to_html(doc.view, doc.provenance())
    for header in ("<th>Component</th>", "<th>Installed</th>", "<th>Status</th>",
                   "<th>Confidence</th>", "<th>Recommended</th>"):
        assert header in html, f"the report lost {header}, which is data rather than control"
