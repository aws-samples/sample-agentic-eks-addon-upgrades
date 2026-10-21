"""The fact approval page shows its table expanded and its reasoning one click away.

WHAT WENT WRONG, TWICE. First the page whose only purpose is to be decided on kept its entire
content behind a disclosure: the "Proposed compatibility facts" table sat in a
`<details class="card">` with no `open`, and a reviewer arriving from the mail saw a summary
bar and had to click to see any proposal at all. So the card was opened -- and so were each
row's rationale and caveats. Then the research got good: three rows with five-to-eight-sentence
rationales and caveat lists pushed the second and third rows below the fold, and the table
became a page of prose scrolled past to find the next Approve.

THE SETTLED SHAPE, pinned here: the card is OPEN (the table is the page), and each row's
`prop-why` and `prop-notes` are CLOSED under a summary that names what is inside and how much
("why this confidence", "5 caveat(s)"). The `<details>` elements are all still there, so
nothing lost its ability to expand or collapse.

`view/render/html.py` is the SHARED renderer: this page imports its stylesheet and
five of its cell helpers, and the run report is built from it too. It is NOT modified by this
change, and `test_the_run_reports_own_disclosures_are_untouched` is what makes that a checked
claim rather than an assertion about intent.
"""

from __future__ import annotations

import re

import pytest

from eks_addon_readiness.view.model import ApprovalControls, PendingApproval
from eks_addon_readiness.view.render.approval import to_approval_html

_DIGEST = "sha256:" + "ab" * 32
_REPORT_DIGEST = "sha256:" + "cd" * 32


def _pending(**overrides) -> PendingApproval:
    """One proposal carrying the fields the collapsed blocks are built from.

    The values are the keda row this run actually produced -- MEDIUM, 38 of 38 values
    verified, the recommendation 2.20.0 against an installed 2.17.2 -- so the rendered page
    this file asserts on is the shape a reviewer really met.
    """
    fields = {
        "component": "keda",
        "version_series": "2.17.x",
        "candidate_uri": "s3://bucket/facts-candidates/keda.json",
        "candidate_digest": _DIGEST,
        "report_digest": _REPORT_DIGEST,
        "confidence": "MEDIUM",
        "change_direction": "WIDENS",
        "headline": "keda 2.17.x must move to 2.20.x to reach Kubernetes 1.36.",
        "values_verified": 38,
        "values_total": 38,
        "installed_version": "2.17.2",
        "recommended_version": "2.20.0",
        "recommended_series": "2.20.x",
        "action": "UPGRADE",
        "machine_confidence": "HIGH",
        "proposed_confidence": "MEDIUM",
        "confidence_rationale": (
            "every stored value traces to a quote verified against the page it cites, but "
            "the target window rests on install prose rather than the compatibility table"
        ),
        "caveats": ("the 2.20 window is stated once as a range and once as a floor",),
        "not_found": ("a published release list for the 2.20 series",),
    }
    fields.update(overrides)
    return PendingApproval(**fields)


def _page(*pending: PendingApproval) -> str:
    return to_approval_html(
        ApprovalControls(pending=pending),
        run_id="local",
        cluster="my-eks-cluster",
        target_kubernetes_version="1.36",
        rendered_at="2026-09-20T09:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# THE DEFECT: the proposal a reviewer must weigh is behind a click.
# ---------------------------------------------------------------------------


def test_the_proposals_table_is_expanded_on_arrival() -> None:
    page = _page(_pending())
    assert '<details class="card" open>' in page, (
        "the page exists only to be decided on, so its proposals must be visible without a "
        "click; a closed card shows a reviewer nothing has arrived"
    )
    assert '<details class="card">' not in page


@pytest.mark.parametrize(
    "css_class",
    [
        pytest.param("prop-why", id="why-this-confidence"),
        pytest.param("prop-notes", id="caveats-and-gaps"),
    ],
)
def test_each_rows_reasoning_is_one_click_away(css_class: str) -> None:
    """The two blocks holding what no column can carry are present, named, and closed."""
    page = _page(_pending())
    assert f'<details class="{css_class}">' in page
    assert f'<details class="{css_class}" open>' not in page


def test_the_rationale_and_the_caveats_are_in_the_bytes() -> None:
    """Closed is not removed: the text is in the page, one click from the row."""
    page = _page(_pending())
    assert "rests on install prose rather than the compatibility table" in page
    assert "stated once as a range and once as a floor" in page
    assert "not found: a published release list for the 2.20 series" in page


# ---------------------------------------------------------------------------
# THE DISCLOSURE SURVIVES: expanded is a default, not a removal.
# ---------------------------------------------------------------------------


def test_every_block_is_still_a_details_a_reader_can_toggle() -> None:
    page = _page(_pending())
    assert page.count('<details class="card" open>') == 1
    for css_class in ("prop-why", "prop-notes"):
        assert page.count(f'<details class="{css_class}">') == 1
    assert page.count("</details>") >= 3


def test_a_row_with_no_rationale_and_no_caveats_emits_no_empty_disclosure() -> None:
    """A row with nothing to disclose gets no disclosure, open or closed.

    Asserted on the ELEMENT rather than the class name: `prop-why` and `prop-notes` also
    appear in the stylesheet, so a bare substring check would count the rule that styles the
    block as the block.
    """
    page = _page(_pending(confidence_rationale=None, caveats=(), not_found=()))
    assert '<details class="prop-why"' not in page
    assert '<details class="prop-notes"' not in page
    assert '<details class="card" open>' in page


def test_the_signed_command_stays_behind_its_own_disclosure() -> None:
    """The Approve / Reject fallback hides a long signed command, not proposal content.

    Expanding it would put two multi-line `aws` invocations between every pair of rows, which
    is the opposite of making the proposal readable. Untouched on purpose.
    """
    page = _page(_pending())
    assert '<details class="btn-wrap wrap-approve">' in page
    assert '<details class="btn-wrap wrap-approve" open>' not in page


# ---------------------------------------------------------------------------
# THE SHARED RENDERER IS NOT TOUCHED.
# ---------------------------------------------------------------------------


def test_the_run_reports_own_disclosures_are_untouched() -> None:
    """`view/render/html.py` is shared with the run report and keeps its closed defaults.

    Read off the module's source rather than off a rendered report, because the point is that
    the file itself did not move: every `<details>` literal it emits is checked, so a later
    change that opens one on the run report fails here instead of surprising a reader of that
    page.
    """
    from eks_addon_readiness.view.render import html

    source = html.__loader__.get_source(html.__name__)
    literals = re.findall(r"<details[^>]*>", source)
    opened = [literal for literal in literals if " open>" in literal]
    assert opened == ['<details class="card" id="sequence" open>'], (
        "the only disclosure the run report opens by default is its own sequence card; "
        f"this change must not add another. Found: {opened}"
    )
