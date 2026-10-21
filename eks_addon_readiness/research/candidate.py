"""The candidate fact: an extracted `ComponentKnowledge` bound to the receipt that judged it.

Stage 6's output unit. The `knowledge` is the exact contract Stages 3, 4 and 5 already
consume -- no new fact format -- so an APPROVED candidate drops into the store and starts
driving verdicts. A pending one drives nothing. The `receipt` travels with it, so the
confidence never appears without its justification.

Kept separate from `ComponentKnowledge` deliberately: the knowledge contract stays a pure
statement of fact with no trace of how it was obtained. The provenance of *this research
run* lives here, around the fact, not inside it.

A candidate may carry no knowledge at all. That is the REJECTED case, and it is a real
outcome rather than an error: the sources were read, transcribed and found to state no
supported window. Representing it as a candidate rather than an exception is what lets the
reason reach a report instead of a log line.
"""

from __future__ import annotations

import hashlib

from pydantic import Field, model_validator

from ..knowledge import ComponentKnowledge
from ..models import ContractModel
from .receipt import AcceptanceDecision, ResearchReceipt


class CandidateFact(ContractModel):
    """One researched fact and the receipt justifying its confidence and disposition."""

    # The question this fact answers, carried so a pending candidate is self-describing.
    component_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version_series: str = Field(pattern=r"^\d+\.\d+\.x$")

    # None only when the decision is REJECTED: no usable window was derivable.
    knowledge: ComponentKnowledge | None = None
    receipt: ResearchReceipt

    @model_validator(mode="after")
    def routed_candidates_carry_knowledge(self) -> CandidateFact:
        if self.knowledge is None and self.receipt.decision is not (
            AcceptanceDecision.REJECTED
        ):
            raise ValueError(
                f"a {self.receipt.decision} candidate must carry the knowledge it routes; "
                "only REJECTED has no fact"
            )
        return self

    def digest(self) -> str:
        """Content digest of the candidate, receipt included.

        The receipt is part of the digest: two facts with identical knowledge but
        different evidence are not the same artifact, and a reader diffing two runs should
        see the evidence change even when the conclusion did not.
        """
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    @property
    def approved(self) -> bool:
        """Whether a person approved this fact, which is the only route to the live store.

        There is no `accepted` property any more, and its absence is deliberate: the word
        implied a machine could do the accepting. Only `approval.approve` produces an
        APPROVED receipt, and only with an `ApprovalRecord` attached.
        """
        return self.receipt.decision is AcceptanceDecision.APPROVED

    @property
    def pending(self) -> bool:
        """Whether this fact is awaiting a person. It drives no verdict while it is."""
        return self.receipt.decision is AcceptanceDecision.PENDING_APPROVAL
