"""Turning a pending candidate into an approved fact. The only route into the live store.

Pure functions. The transport, the identity and the storage live in `stages/approve.py`;
what a valid approval *is* lives here, so it is testable with no AWS and no HTTP.

**Four things an approval must satisfy**, and each one closes a way an approval could be
faked or misapplied:

1. **The candidate must be pending.** Approving a rejected candidate would store a fact the
   gate refused; approving an already-approved one would overwrite an audit record.
2. **The candidate digest must match.** A submitted approval names the digest of what was
   shown. If the stored candidate has changed since, the approval is for a different fact
   and is refused rather than applied to the newer one -- which is how a re-research between
   render and submit would otherwise get approved unseen.
3. **The report digest must match the report that would be rendered.** This is the one that
   makes the audit trail worth having. Recording "approved by X at T" proves someone
   approved; recording the digest of the document they read proves *what* they approved.
4. **The approver must be a real signed principal.** Not a name in the request body. The
   handler takes it from the request's signed identity, so it cannot be typed in.

Nothing here can be reached from an unauthenticated path: `stages/approve.py` runs behind an
IAM-authenticated function URL whose resource policy allows exactly one principal, the
`${name_prefix}-approver` role. So "the agent can show a pending approval but cannot grant
one" is enforced by AWS, not by prompt wording.

That resource policy holds a single `Allow` and no `Deny`: every other principal is refused
by not being on a one-name allowlist, and the handler re-checks the resolved principal. See
`stages/approve.py`'s docstring, which sets out the controls and the gap that remains.
"""

from __future__ import annotations

from datetime import datetime

from ..view.render.command import REPLACE_COMMENT, REPLACE_REASON
from .candidate import CandidateFact
from .receipt import AcceptanceDecision, ApprovalRecord

# The placeholders the rendered command ships. Refused rather than stored: an
# unedited "REPLACE-WITH-WHAT-YOU-CHECKED" in an audit record is worse than an empty
# comment, because it reads like a considered answer and is not one. The strings come
# from the renderer that emits them, so the two cannot drift apart.
_PLACEHOLDERS = (REPLACE_COMMENT, REPLACE_REASON)


def _unedited(text: str) -> str | None:
    """The placeholder still present in `text`, or None.

    Substring rather than equality: a reader who types around the placeholder --
    "checked the matrix, REPLACE-WITH-WHAT-YOU-CHECKED" -- has still not removed it,
    and the record would still carry an instruction to its future reader.
    """
    return next((p for p in _PLACEHOLDERS if p in text), None)


class ApprovalRefused(RuntimeError):
    """An approval that must not be applied. Carries the reason, for the response body."""


def approve(
    candidate: CandidateFact,
    *,
    approver: str,
    comment: str,
    report_digest: str,
    submitted_candidate_digest: str,
    expected_report_digest: str,
    approved_at: datetime,
) -> CandidateFact:
    """Apply a human approval to a pending candidate, or refuse and say why.

    Returns a NEW candidate whose receipt is APPROVED and carries the `ApprovalRecord`. The
    original is not mutated -- contracts are frozen, and the pending artifact stays on disk
    as the record of what was proposed before anyone approved it.
    """
    if candidate.receipt.decision is not AcceptanceDecision.PENDING_APPROVAL:
        raise ApprovalRefused(
            f"candidate {candidate.component_id}/{candidate.version_series} is "
            f"{candidate.receipt.decision}, not PENDING_APPROVAL. Only a pending candidate "
            "can be approved: a rejected one has no usable fact, and an approved one "
            "already carries an audit record that must not be overwritten."
        )
    if candidate.knowledge is None:
        raise ApprovalRefused(
            "a pending candidate with no knowledge cannot be approved; there is no fact "
            "to store"
        )

    actual = candidate.digest()
    if submitted_candidate_digest != actual:
        raise ApprovalRefused(
            "the approval names a different candidate than the one stored. Submitted "
            f"{submitted_candidate_digest}, stored {actual}. The candidate was probably "
            "re-researched between the page being rendered and this approval being "
            "submitted, so approving would apply your decision to a fact you did not see."
        )

    if report_digest != expected_report_digest:
        raise ApprovalRefused(
            "the approval names a report digest that does not match the report this "
            f"candidate renders. Submitted {report_digest}, expected "
            f"{expected_report_digest}. The audit trail records what an approver was shown, "
            "so an approval whose report cannot be reproduced is refused."
        )

    if not approver.strip():
        raise ApprovalRefused(
            "no signed principal was resolved for this request, so there is no accountable "
            "approver to record"
        )

    unedited = _unedited(comment)
    if unedited is not None:
        # The comment was never inspected here at first, so a copy-pasted command stored
        # the renderer's own placeholder as the approver's account of what they checked.
        raise ApprovalRefused(
            f"the comment still contains {unedited}, so the command was submitted "
            "unedited. The comment is recorded on the fact as your account of what you "
            "checked; storing the placeholder would put an instruction in the audit "
            "record where a reason belongs."
        )

    record = ApprovalRecord(
        approver=approver.strip(),
        approved_at=approved_at,
        comment=comment.strip(),
        report_digest=report_digest,
        candidate_digest=actual,
    )
    receipt = candidate.receipt.model_copy(
        update={
            "decision": AcceptanceDecision.APPROVED,
            "approval": record,
            "summary": (
                candidate.receipt.summary
                + f" APPROVED by {record.approver} at {record.approved_at.isoformat()}."
            ),
        }
    )
    return candidate.model_copy(update={"receipt": receipt})


def decline(
    candidate: CandidateFact,
    *,
    approver: str,
    reason: str,
    report_digest: str,
    submitted_candidate_digest: str,
    expected_report_digest: str,
    declined_at: datetime,
) -> CandidateFact:
    """Record that a person refused a pending candidate. **Stores no fact.**

    The counterpart to `approve`, and deliberately the weaker of the two: approving
    writes into the live store, declining writes nothing there. A component whose
    candidate is declined stays exactly what it was before anyone looked -- an
    honest gap, reported as unknown rather than guessed at.

    WHY THIS EXISTS AT ALL, given that it changes no verdict. Without it the only
    way to refuse is to do nothing, and doing nothing is indistinguishable from not
    having looked. The candidate stays pending, so it is offered again on the next
    report, and the reviewer who already rejected it has no way to say so. The
    record is the product: who declined, when, why, and the digest of the report
    they read.

    THE SAME FOUR CHECKS AS `approve`, for the same reason. A declination is an
    audit record too, and one that names a candidate other than the one displayed is
    worth as little as an approval that does. In particular the report digest must
    match: "I read this page and refused what it said" is the claim, and it is only
    worth something if the page can be identified.

    WHAT IT DOES NOT DO. It does not carry an `ApprovalRecord` -- the receipt
    contract permits that field only on an APPROVED receipt, because an approval
    record on a refusal would be a contradiction that serialises. The account of who
    refused and why goes into the receipt's `summary`, which is where every other
    plain-language account of a decision already lives.
    """
    if candidate.receipt.decision is not AcceptanceDecision.PENDING_APPROVAL:
        raise ApprovalRefused(
            f"candidate {candidate.component_id}/{candidate.version_series} is "
            f"{candidate.receipt.decision}, not PENDING_APPROVAL. Only a pending "
            "candidate can be declined: a rejected one already records a refusal, "
            "and an approved one carries an audit record that must not be "
            "contradicted by a later refusal of the same object."
        )

    actual = candidate.digest()
    if submitted_candidate_digest != actual:
        raise ApprovalRefused(
            "the refusal names a different candidate than the one stored. "
            f"Submitted {submitted_candidate_digest}, stored {actual}. The "
            "candidate was probably re-researched between the page being rendered "
            "and this refusal being submitted, so recording it would attribute your "
            "decision to a fact you did not see."
        )

    if report_digest != expected_report_digest:
        raise ApprovalRefused(
            "the refusal names a report digest that does not match the report this "
            f"candidate renders. Submitted {report_digest}, expected "
            f"{expected_report_digest}. A refusal records what a reviewer was "
            "shown, so one whose report cannot be reproduced is refused."
        )

    if not approver.strip():
        raise ApprovalRefused(
            "no signed principal was resolved for this request, so there is no "
            "accountable reviewer to record"
        )
    if not reason.strip():
        # Required, unlike an approval comment. An approval is justified by the
        # report it cites; a refusal contradicts that report, and the next reader
        # needs to know on what grounds or the record teaches them nothing.
        raise ApprovalRefused(
            "a refusal must carry a reason. An approval is justified by the report "
            "it names; a refusal disagrees with that report, and a refusal with no "
            "stated grounds leaves the next reviewer exactly where this one started."
        )
    unedited = _unedited(reason)
    if unedited is not None:
        raise ApprovalRefused(
            f"the reason still contains {unedited}, so the command was submitted "
            "unedited. A refusal disagrees with the report it names, and the "
            "placeholder states no grounds for disagreeing."
        )

    receipt = candidate.receipt.model_copy(
        update={
            "decision": AcceptanceDecision.REJECTED,
            "summary": (
                candidate.receipt.summary
                + f" DECLINED by {approver.strip()} at {declined_at.isoformat()}"
                f" (report {report_digest}): {reason.strip()}"
            ),
        }
    )
    return candidate.model_copy(update={"receipt": receipt})


def is_pending(candidate: CandidateFact) -> bool:
    """Whether this candidate is awaiting a person.

    Used by the read-only pending listing: a caller can enumerate what is pending, which is
    exactly the capability that makes "show me what needs approval" answerable without
    granting anything.
    """
    return candidate.receipt.decision is AcceptanceDecision.PENDING_APPROVAL
