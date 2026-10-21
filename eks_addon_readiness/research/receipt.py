"""The research receipt: why a fact was rated as it was, and where it was routed.

Carried alongside the fact everywhere it goes -- into the store, into the report. A reader
never has to trust the confidence; they read the signals that produced it and the quotes
those signals rest on.

Two invariants are structural here, enforced by validators rather than requested in a
prompt:

- **Confidence is never a field a model fills in.** It is derived from signals that only
  fire when code could verify them. There is no `model_confidence` input anywhere.
- **A decision must match its evidence.** The validator covers the whole acceptance
  policy, not just the `UNRESOLVED` case, so an accepted fact whose confidence and
  contradictions do not permit acceptance cannot be constructed at all, let alone stored.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from ..models import Citation, Confidence, ContractModel
from .authority import SourceAuthority


class SignalName(StrEnum):
    """The checkable signals confidence is computed from.

    Each is a question code answers about evidence code holds. None of them is a signal
    that fires because the model said so.
    """

    # Every quote appears in the bytes of the page it cites.
    QUOTE_MATCHES_SOURCE = "QUOTE_MATCHES_SOURCE"
    # Every value destined for the store appears in a verified quote.
    VALUES_TRACE_TO_QUOTES = "VALUES_TRACE_TO_QUOTES"
    # Two independent sources state the same typed value.
    CORROBORATION = "CORROBORATION"
    # The source is the component's own, and the quote came from a machine-readable file
    # or a table in the fetched bytes. Determined in `authority.py`, never asserted.
    FIRST_PARTY_SOURCE = "FIRST_PARTY_SOURCE"
    # A support window could be derived from what the sources actually said.
    SUPPORT_WINDOW_DERIVED = "SUPPORT_WINDOW_DERIVED"
    # Structurally coherent: floor below ceiling, versions well-formed, contract valid.
    SHAPE_SANITY = "SHAPE_SANITY"
    # -- Assessment signals. The first is about the evidence; the other three re-check the
    # model's assessment against evidence code already holds, and appear only when an
    # assessment was returned.
    # Concrete releases were named, each with a quote, so a version can be discussed.
    RELEASES_PRESENT = "RELEASES_PRESENT"
    # The recommended version appears among the releases the model itself returned.
    RECOMMENDATION_IS_A_SEEN_RELEASE = "RECOMMENDATION_IS_A_SEEN_RELEASE"
    # A derived window for the recommended series contains the target minor.
    TARGET_COVERED_BY_EVIDENCE = "TARGET_COVERED_BY_EVIDENCE"
    # The N-2 arithmetic in the assessment's derivation recomputes exactly, and its
    # policy index points at a SUPPORT_POLICY statement.
    POLICY_DERIVATION_RECOMPUTES = "POLICY_DERIVATION_RECOMPUTES"


class SignalResult(ContractModel):
    """One signal, whether it fired, and the evidence for that determination.

    `passed=True` without the evidence it claims is a contradiction in terms for the
    quote-based signals. The validator refuses it.
    """

    signal: SignalName
    passed: bool
    detail: str = Field(min_length=1)
    citations: tuple[Citation, ...] = ()
    quotes: tuple[str, ...] = ()
    # For FIRST_PARTY_SOURCE: how each cited source was classified, so a reader can see
    # that authority was derived and from what.
    authorities: tuple[SourceAuthority, ...] = ()

    @model_validator(mode="after")
    def evidence_signals_carry_their_evidence(self) -> SignalResult:
        if (
            self.signal is SignalName.QUOTE_MATCHES_SOURCE
            and self.passed
            and not self.quotes
        ):
            raise ValueError(
                "QUOTE_MATCHES_SOURCE cannot pass without the quotes it matched"
            )
        if (
            self.signal is SignalName.CORROBORATION
            and self.passed
            and len(self.citations) < 2
        ):
            raise ValueError(
                "CORROBORATION cannot pass with fewer than two independent citations"
            )
        if (
            self.signal is SignalName.FIRST_PARTY_SOURCE
            and self.passed
            and not self.authorities
        ):
            raise ValueError(
                "FIRST_PARTY_SOURCE cannot pass without the derived authority that "
                "justifies it"
            )
        return self


class Contradiction(ContractModel):
    """Two sources making incompatible typed claims about the same fact.

    Its own type rather than a failed signal, because a contradiction routes a fact
    regardless of how much else corroborates.
    """

    field: str = Field(min_length=1)
    left_value: str = Field(min_length=1)
    right_value: str = Field(min_length=1)
    left_citation: Citation
    right_citation: Citation


class AcceptanceDecision(StrEnum):
    """Where the acceptance policy routed the candidate. **There is no automatic accept.**

    `PENDING_APPROVAL`
        A usable fact was derived. It is written to the pending area with its receipt and
        an approval report, drives no verdict, and waits for a person. This is the only
        route a fact can take toward the live store.

    `REJECTED`
        Not approvable: no usable window could be derived, or a quote failed its check.
        NOT hidden — everything researched is presented. The
        candidate, its receipt and its report document are all still written with the
        reason; the word only means there is no fact here a person could approve, and
        the component stays an honest gap.

    `APPROVED`
        A person approved a pending candidate. Only `approval.py` produces this, only from
        a `PENDING_APPROVAL` candidate, and only with an `ApprovalRecord` attached.

    **`AUTO_ACCEPT` is deliberately absent from this enum.** Not defaulted off, not gated
    behind a flag -- absent, so no code path can select it and no stored artifact can claim
    it. Confidence and change direction are still computed and are shown to the approver as
    the strongest available summary of the evidence, but they no longer decide anything on
    their own. A `decide()` that returns accept unconditionally, with nothing else in the
    system objecting, is a subtler failure than a wrong threshold -- so the safest place for
    "a machine may accept a fact" is nowhere.
    """

    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ChangeDirection(StrEnum):
    """Whether a fact widens or narrows what is permitted, versus any existing fact.

    **Advisory, not a gate.** It used to drive an asymmetric automatic bar -- widening
    needs HIGH, narrowing may pass at MEDIUM. Every fact is now human-approved, so the
    asymmetry is no longer a threshold; it is the sentence the approval report leads with,
    because it is still the most useful thing to tell an approver. A WIDENING fact permits
    Kubernetes versions the previous fact did not, so being wrong means an operator
    upgrades onto a version nothing supports. A NARROWING one costs an unnecessary upgrade.
    The asymmetry in consequence is real; what changed is that a machine no longer acts on
    it alone.

    NEW is the first fact for a spec, with no prior to compare.
    """

    WIDENING = "WIDENING"
    NARROWING = "NARROWING"
    NEW = "NEW"


class ApprovalRecord(ContractModel):
    """Who approved a fact, when, with what comment, and what they were shown.

    `report_digest` is the load-bearing field and the reason this is a contract rather than
    a log line. It is the digest of the approval report the person actually read. Recording
    only "approved by X at T" proves that someone approved; recording the digest of the
    document in front of them proves **what** they approved -- the claim, the quote, the
    signals, and the verdicts the approval would change. In a regulated environment those
    are different questions, and only the second one is worth anything after the fact.

    `approver` is an AWS principal ARN, not a display name. It comes from the request's
    signed identity rather than from the request body, so it cannot be typed in.
    """

    approver: str = Field(min_length=1)
    approved_at: datetime
    comment: str = ""
    # Digest of the ApprovalReport document rendered for this candidate.
    report_digest: str = Field(min_length=1)
    # Digest of the candidate approved, so an approval cannot be replayed against a
    # different candidate that happens to share a component id.
    candidate_digest: str = Field(min_length=1)


class ResearchReceipt(ContractModel):
    """Why a fact was rated as it was -- computed, cited, and re-checkable."""

    confidence: Confidence
    signals: tuple[SignalResult, ...] = Field(min_length=1)
    contradictions: tuple[Contradiction, ...] = ()
    change_direction: ChangeDirection
    decision: AcceptanceDecision
    # Plain-language summary derived from the signals, never model narration.
    summary: str = Field(min_length=1)
    # Digest of the exact page bytes judged, so a later reader can tell whether a
    # re-research saw the same sources.
    evidence_digest: str = Field(min_length=1)
    # (verified, total) stored values. Reported because silence about it is what lets
    # a fact reach HIGH on one quote while storing 8 values.
    values_verified: int = 0
    values_total: int = 0
    # Tested windows found in the sources, rendered with their qualifiers. These DO derive
    # windows (basis TESTED); this list is how the receipt keeps the project's
    # own wording — "tested", plus any disclaimer — visible beside the derived numbers.
    tested_windows: tuple[str, ...] = ()
    # -- The confidence arithmetic, shown so a reader can re-run it by hand.
    # What the machine computed from the signals alone, before any cap.
    machine_confidence: Confidence | None = None
    # What the model proposed in its assessment. A cap on the final level, never a floor:
    # `confidence` above is min(machine, tier cap, this). None when the prompt is
    # transcription-only.
    proposed_confidence: Confidence | None = None
    # The evidence tier the assessment claimed, which carries its own cap (signals.py).
    evidence_tier: str | None = None
    # Present only on an APPROVED receipt: who approved, when, and the digest of the
    # report they were shown. Absent everywhere else, and the validator enforces both
    # directions.
    approval: ApprovalRecord | None = None

    @model_validator(mode="after")
    def decision_matches_evidence(self) -> ResearchReceipt:
        """The acceptance policy, re-asserted as a contract invariant.

        Deliberate duplication of `acceptance.decide` and `approval.approve`. Those decide;
        this refuses to *represent* a decision they could not have made. A gate that lives
        in one function can be removed by editing that function alone, with nothing else in
        the system objecting -- enough for a LOW, quote-check-failing fact to be serialised,
        stored, and drive verdicts. Stating the invariant on the contract means the next
        such edit cannot serialise.

        Two invariants now, and the first is the whole scope of the approval change:

        **APPROVED requires an approval record.** A fact cannot claim to have been approved
        without naming who approved it and the digest of what they read. So an approval
        cannot be forged by writing a status.

        **UNRESOLVED never reaches the store.** A fact with no usable window and no
        contradiction has nothing for a person to adjudicate either; it is REJECTED.
        """
        if self.decision is AcceptanceDecision.APPROVED and self.approval is None:
            raise ValueError(
                "an APPROVED receipt must carry the ApprovalRecord naming who approved it "
                "and the digest of the report they were shown; a fact cannot be approved "
                "by setting a status"
            )
        if self.decision is not AcceptanceDecision.APPROVED and self.approval is not None:
            raise ValueError(
                f"a {self.decision} receipt carries an ApprovalRecord, which means an "
                "approval was recorded without the decision moving to APPROVED"
            )
        if (
            self.decision is AcceptanceDecision.PENDING_APPROVAL
            and self.confidence is Confidence.UNRESOLVED
            and not self.contradictions
        ):
            raise ValueError(
                "an UNRESOLVED fact with no contradiction has no usable window and "
                "nothing for a person to adjudicate; it is REJECTED, not PENDING_APPROVAL"
            )
        return self
