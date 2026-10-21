"""Rejecting is a real operation, and it is the weaker of the two on purpose.

WHY THIS EXISTS. Before this change the approval endpoint had exactly one write
path: approve. The only way to refuse a proposed fact was to do nothing -- which is
indistinguishable from not having looked. The candidate stayed PENDING_APPROVAL, so
it was offered again on the next report, and the reviewer who had already refused it
had no way to say so. A page offering an "approve" control and no counterpart is a
page that only knows one answer.

WHAT REJECT IS, PRECISELY:

    approve   writes a fact into the live store. Changes verdicts.
    reject    writes NO fact. Changes no verdict. Records who refused and why.

So it moves the human boundary nowhere: it is behind the same signature and the same
role allowlist, and the thing it cannot do is exactly the thing that mattered. A
component whose candidate is refused stays an honest gap, reported as unknown rather
than guessed at -- which is the same state it was in before anyone looked.

THE TWO COMPONENTS THAT MUST NOT BE APPROVED. `keda` and `cert-manager` are the
project's deliberate FACT_MISSING cases. Nothing in this file approves anything: it
tests the pure transition functions over a fixture candidate, with no AWS and no
network, so running the suite cannot convert a test case.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eks_addon_readiness.research.approval import ApprovalRefused, approve, decline
from eks_addon_readiness.research.receipt import AcceptanceDecision
from eks_addon_readiness.view.render.command import (
    REPLACE_COMMENT,
    REPLACE_REASON,
    decision_command,
)

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
_ARN = "arn:aws:sts::1:assumed-role/eks-addon-upgrades-approver/session"


@pytest.fixture
def pending():
    """A pending candidate, borrowed from the approval check suite's builder.

    Reused rather than rebuilt: a second fixture for the same shape is a second
    place for the shape to drift from the contract it is meant to exercise.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from approval_checks import built

    candidate, report = built()
    assert candidate.receipt.decision is AcceptanceDecision.PENDING_APPROVAL
    return candidate, report


# ---------------------------------------------------------------------------
# REJECT: the transition, and the four checks it shares with approve.
# ---------------------------------------------------------------------------


def test_declining_records_the_refusal_and_stores_no_fact(pending) -> None:
    candidate, report = pending
    declined = decline(
        candidate,
        approver=_ARN,
        reason="The cited matrix is a draft branch, not a release.",
        report_digest=report.digest(),
        submitted_candidate_digest=candidate.digest(),
        expected_report_digest=report.digest(),
        declined_at=_NOW,
    )
    assert declined.receipt.decision is AcceptanceDecision.REJECTED
    # No ApprovalRecord: the receipt contract permits that field only on APPROVED,
    # because an approval record on a refusal is a contradiction that serialises.
    assert declined.receipt.approval is None
    assert _ARN in declined.receipt.summary
    assert "draft branch" in declined.receipt.summary
    assert _NOW.isoformat() in declined.receipt.summary
    # The original is untouched. It is the record of what was proposed, and a
    # refusal that mutated it would destroy the evidence the refusal is about.
    assert candidate.receipt.decision is AcceptanceDecision.PENDING_APPROVAL


def test_a_refusal_must_carry_a_reason(pending) -> None:
    """Unlike an approval comment, which the report itself justifies.

    A refusal disagrees with the report it names, so a refusal with no stated
    grounds leaves the next reviewer exactly where this one started.
    """
    candidate, report = pending
    with pytest.raises(ApprovalRefused, match="must carry a reason"):
        decline(
            candidate,
            approver=_ARN,
            reason="   ",
            report_digest=report.digest(),
            submitted_candidate_digest=candidate.digest(),
            expected_report_digest=report.digest(),
            declined_at=_NOW,
        )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        pytest.param("approver", "  ", "accountable reviewer", id="no-principal"),
        pytest.param(
            "submitted_candidate_digest",
            "sha256:" + "0" * 64,
            "different candidate",
            id="stale-candidate",
        ),
        pytest.param(
            "report_digest",
            "sha256:" + "0" * 64,
            "does not match the report",
            id="wrong-report",
        ),
    ],
)
def test_a_refusal_is_checked_as_strictly_as_an_approval(
    pending, field: str, value: str, expected: str
) -> None:
    """A refusal is an audit record too, so it earns the same four checks.

    In particular the report digest must match: "I read this page and refused what
    it said" is only worth something if the page can be identified afterwards.
    """
    candidate, report = pending
    kwargs = {
        "approver": _ARN,
        "reason": "not a release",
        "report_digest": report.digest(),
        "submitted_candidate_digest": candidate.digest(),
        "expected_report_digest": report.digest(),
        "declined_at": _NOW,
    }
    kwargs[field] = value
    with pytest.raises(ApprovalRefused, match=expected):
        decline(candidate, **kwargs)


def test_an_already_decided_candidate_cannot_be_declined(pending) -> None:
    candidate, report = pending
    approved = approve(
        candidate,
        approver=_ARN,
        comment="checked",
        report_digest=report.digest(),
        submitted_candidate_digest=candidate.digest(),
        expected_report_digest=report.digest(),
        approved_at=_NOW,
    )
    with pytest.raises(ApprovalRefused, match="not PENDING_APPROVAL"):
        decline(
            approved,
            approver=_ARN,
            reason="changed my mind",
            report_digest=report.digest(),
            submitted_candidate_digest=approved.digest(),
            expected_report_digest=report.digest(),
            declined_at=_NOW,
        )


# ---------------------------------------------------------------------------
# ONE SPELLING of the signed request, shared by both pages.
# ---------------------------------------------------------------------------

_ARGS = {
    "endpoint": "https://abc.lambda-url.eu-west-1.on.aws/",
    "region": "eu-west-1",
    "component_id": "metrics-server",
    "version_series": "0.6.x",
    "candidate_uri": "s3://b/facts-candidates/decision=PENDING_APPROVAL/c.json",
    "candidate_digest": "sha256:" + "a" * 64,
    "report_digest": "sha256:" + "b" * 64,
}


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_the_command_signs_with_the_caller_s_own_credentials(action: str) -> None:
    command = decision_command(action=action, **_ARGS)
    assert "--aws-sigv4 'aws:amz:eu-west-1:lambda'" in command
    # A CREDENTIAL GUARD MUST PRECEDE THE REQUEST. With no credentials exported -- a
    # profile or an SSO session, the normal state of a laptop -- a bare `$VAR` expands
    # to empty, curl signs with an empty key, and the endpoint answers with exactly the
    # silent 403 this whole design exists to avoid. `${VAR:?}` makes the shell refuse
    # before curl runs and name the missing variable.
    guard, _, request = command.partition("curl ")
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert f"${{{var}:?}}" in guard, (
            f"{var} is not guarded before the request, so an unset credential signs "
            "an empty request and 403s instead of failing locally"
        )
        assert f"${var}" in request, f"{var} must still be used to sign"
    # No signature, no credential, no presigned query string. The page holds none of
    # these and must not appear to.
    for leak in ("X-Amz-Signature", "X-Amz-Credential", "X-Amz-Expires"):
        assert leak not in command


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_the_command_names_the_action_and_both_digests(action: str) -> None:
    command = decision_command(action=action, **_ARGS)
    assert f'"action":"{action}"' in command
    assert f'"candidate_digest":"{_ARGS["candidate_digest"]}"' in command
    assert f'"report_digest":"{_ARGS["report_digest"]}"' in command
    assert f'"candidate_uri":"{_ARGS["candidate_uri"]}"' in command
    # The two human-readable fields, so the endpoint can prove the fact NAMED on the
    # page is the fact being decided. Digests alone cannot: a page showing one
    # component's prose beside another's consistent triple passes every digest check.
    assert f'"component_id":"{_ARGS["component_id"]}"' in command
    assert f'"version_series":"{_ARGS["version_series"]}"' in command


def test_approve_asks_for_a_comment_and_reject_asks_for_a_reason() -> None:
    """Different words because they are different obligations.

    An approval's comment is optional to the endpoint; a refusal's reason is not.
    Both are placeholders the human must replace, so an unedited command is visibly
    unfinished rather than quietly acceptable.
    """
    approve_cmd = decision_command(action="approve", **_ARGS)
    reject_cmd = decision_command(action="reject", **_ARGS)
    assert f'"comment":"{REPLACE_COMMENT}"' in approve_cmd
    assert f'"reason":"{REPLACE_REASON}"' in reject_cmd
    assert REPLACE_REASON not in approve_cmd
    assert REPLACE_COMMENT not in reject_cmd


def test_both_pages_render_the_identical_command(pending) -> None:
    """THE POINT OF THE SHARED FUNCTION, asserted rather than assumed.

    The APPROVAL PAGE and the per-candidate evidence receipt both offer this control.
    Two hand-written copies of a signed request drift the moment either page
    changes, and a reviewer finding the two pages spelling the request differently
    has no way to tell which one the endpoint accepts.

    THE RUN REPORT USED TO BE ONE OF THE TWO PAGES, AND IS NOT ANY MORE. Approval now
    happens at the `Approval` stage, before `Present` renders, so the run report carries no
    control at all and `html._command` is gone with it. The invariant is unchanged -- one
    spelling of the request, shared -- but the pages that share it are the approval page and
    the receipt, and `decision_command` is the single source both call.
    """
    from eks_addon_readiness.research.render_html import render_approval_html
    from eks_addon_readiness.view import approvals_from_research

    _, report = pending
    receipt_html = render_approval_html(
        report,
        approve_endpoint=_ARGS["endpoint"],
        candidate_uri=_ARGS["candidate_uri"],
        region=_ARGS["region"],
    )
    controls = approvals_from_research(
        [
            {
                "component_id": report.component_id,
                "version_series": report.version_series,
                "candidate_uri": _ARGS["candidate_uri"],
                "candidate_digest": report.candidate_digest,
                "report_digest": report.digest(),
                "confidence": str(report.confidence),
                "change_direction": str(report.change_direction),
                "approval_headline": report.headline(),
                "pending_approval": True,
            }
        ],
        endpoint=_ARGS["endpoint"],
        region=_ARGS["region"],
    )
    for action in ("approve", "reject"):
        p0 = controls.pending[0]
        from_report = decision_command(
            endpoint=controls.endpoint or "",
            region=controls.region,
            action=action,
            component_id=p0.component,
            version_series=p0.version_series,
            candidate_uri=p0.candidate_uri,
            candidate_digest=p0.candidate_digest,
            report_digest=p0.report_digest,
        )
        # The receipt page escapes the same string into a <pre>; compare on the
        # escaped form, which is what a reader actually copies off either page.
        escaped = (
            from_report.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;")
        )
        assert escaped in receipt_html, (
            f"the {action} command on the evidence receipt is not the command the "
            "run report renders, so the two pages disagree about what the endpoint "
            "accepts"
        )


def test_the_placeholders_are_shoutingly_obvious() -> None:
    """A placeholder that reads like prose gets submitted unedited."""
    for placeholder in (REPLACE_COMMENT, REPLACE_REASON):
        assert re.fullmatch(r"REPLACE-WITH-[A-Z-]+", placeholder), placeholder
