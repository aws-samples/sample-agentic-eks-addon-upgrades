"""The acceptance gate: does a fact become a proposal for a person, or nothing at all.

**No fact is ever accepted automatically.** That is the project's decision and it is what
this module implements. `decide` routes a candidate to `PENDING_APPROVAL` or to
`REJECTED`, and there is no third option -- `AcceptanceDecision` has no `AUTO_ACCEPT`
member for it to return. A fact reaches the live store only through `approval.py`, only
carrying an `ApprovalRecord`, and only because a person signed a request.

**What confidence is for now.** It is still computed from the same checkable signals, and it
is still the most useful single thing to tell an approver -- but it decides nothing on its
own. The same is true of the change direction. Both are displayed prominently in the
approval report, because "this widens what is permitted and rests on a single prose source"
is exactly what a person needs in order to decide, and neither is a threshold any more.

**Why the automatic path was removed rather than tightened.** The failure was not a
threshold set too low. A gate function that takes confidence, contradictions and direction,
uses none of them, and returns accept unconditionally -- with nothing else in the system
objecting -- lets a fact that fails its quote check and scores LOW be written to the live
store and returned INCOMPATIBLE for a working component. A correct threshold would be one
edit away from that same state at any time. Removing the automatic route entirely means the
failure mode has nowhere to reappear: there is no constant to flip and no member of the enum
to return.

**The asymmetry survives as advice.** A fact that *widens* what is permitted causes a bad
upgrade if it is wrong; one that *narrows* costs only unnecessary work. That difference is
real, and it is what `advisory_note` renders at the top of the report so the approver reads
it before the numbers. It is no longer a bar a machine clears on their behalf.

Pure functions of the receipt inputs and the prior fact, so every routing decision is
testable with no model, no store and no network.
"""

from __future__ import annotations

from ..knowledge import ComponentKnowledge, VersionRange
from ..models import Confidence
from .receipt import AcceptanceDecision, ChangeDirection, Contradiction


def classify_direction(
    spec: str,
    candidate: ComponentKnowledge,
    prior: ComponentKnowledge | None,
) -> ChangeDirection:
    """Whether the candidate widens or narrows the window for `spec`, versus the prior fact.

    Widening means the new window permits Kubernetes versions the old one did not: a lower
    floor, or a higher or removed ceiling. No prior, or no rule for the spec in either, is
    NEW.
    """
    if prior is None:
        return ChangeDirection.NEW

    new_rule = _rule_for(candidate, spec)
    old_rule = _rule_for(prior, spec)
    if new_rule is None or old_rule is None:
        return ChangeDirection.NEW

    if _widens(new_rule.kubernetes_versions, old_rule.kubernetes_versions):
        return ChangeDirection.WIDENING
    if _widens(old_rule.kubernetes_versions, new_rule.kubernetes_versions):
        return ChangeDirection.NARROWING
    # Identical window: not a change in what is permitted. Classified as narrowing -- the
    # safe side -- so re-asserting an unchanged fact never clears the widening bar by
    # accident.
    return ChangeDirection.NARROWING


def _rule_for(knowledge: ComponentKnowledge, spec: str):
    return next(
        (
            r
            for r in knowledge.compatibility_rules
            if r.component_version_series == spec
        ),
        None,
    )


def _to_tuple(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def _widens(a: VersionRange, b: VersionRange) -> bool:
    """Whether window `a` permits at least one Kubernetes version outside window `b`.

    >>> _widens(VersionRange(minimum_inclusive="1.24"), VersionRange(minimum_inclusive="1.25"))
    True
    >>> _widens(VersionRange(minimum_inclusive="1.25", maximum_exclusive="1.30"),
    ...         VersionRange(minimum_inclusive="1.25"))
    False
    >>> _widens(VersionRange(minimum_inclusive="1.25"),
    ...         VersionRange(minimum_inclusive="1.25", maximum_exclusive="1.30"))
    True
    """
    if _to_tuple(a.minimum_inclusive) < _to_tuple(b.minimum_inclusive):
        return True
    if b.maximum_exclusive is None:
        return False  # b is already open-ended; a cannot exceed it above
    if a.maximum_exclusive is None:
        return True  # a is open-ended, b is not
    return _to_tuple(a.maximum_exclusive) > _to_tuple(b.maximum_exclusive)


def decide(
    confidence: Confidence,
    contradictions: tuple[Contradiction, ...],
    direction: ChangeDirection,
) -> AcceptanceDecision:
    """Route a candidate: propose it to a person, or store nothing.

    Two outcomes, and no third is expressible -- `AcceptanceDecision` has no `AUTO_ACCEPT`
    member. Every argument is still read, because each one changes what the approval report
    tells the approver even where it no longer changes the route.

    >>> decide(Confidence.HIGH, (), ChangeDirection.WIDENING)
    <AcceptanceDecision.PENDING_APPROVAL: 'PENDING_APPROVAL'>
    >>> decide(Confidence.MEDIUM, (), ChangeDirection.NARROWING)
    <AcceptanceDecision.PENDING_APPROVAL: 'PENDING_APPROVAL'>
    >>> decide(Confidence.LOW, (), ChangeDirection.NARROWING)
    <AcceptanceDecision.PENDING_APPROVAL: 'PENDING_APPROVAL'>
    >>> decide(Confidence.UNRESOLVED, (), ChangeDirection.NEW)
    <AcceptanceDecision.REJECTED: 'REJECTED'>

    A HIGH-confidence fact and a LOW-confidence one take the same route, and that identity
    is the point: the difference between them is information for the approver, not a licence
    for the tool.

    >>> decide(Confidence.HIGH, (), ChangeDirection.NEW) == decide(
    ...     Confidence.LOW, (), ChangeDirection.NEW
    ... )
    True
    """
    # No usable window at all, and no disagreement to adjudicate. Not a fact: rejected, and
    # the component stays an honest researchable gap. This is the ONLY case in which nothing
    # is proposed, because it is the only case with nothing to propose.
    if confidence is Confidence.UNRESOLVED and not contradictions:
        return AcceptanceDecision.REJECTED

    # Everything else -- HIGH, MEDIUM, LOW, and an UNRESOLVED fact whose sources actively
    # contradict each other -- becomes a proposal a person decides on. A contradiction is
    # shown with both sources quoted rather than resolved here.
    return AcceptanceDecision.PENDING_APPROVAL


def advisory_note(
    confidence: Confidence,
    contradictions: tuple[Contradiction, ...],
    direction: ChangeDirection,
) -> str:
    """The one sentence an approval report leads with. Advice, never a gate.

    This is where the widen/narrow asymmetry lives now. It is stated as a consequence -- what
    goes wrong if this fact is wrong -- rather than as a rating, because a rating invites the
    reader to defer to it and a consequence invites them to think.

    >>> advisory_note(Confidence.HIGH, (), ChangeDirection.WIDENING)[:31]
    'This WIDENS what is permitted, '
    >>> advisory_note(Confidence.HIGH, (), ChangeDirection.NARROWING)[:32]
    'This NARROWS what is permitted, '
    >>> advisory_note(Confidence.HIGH, (), ChangeDirection.NEW)[:22]
    'This is the FIRST fact'
    """
    if contradictions:
        lead = (
            f"Sources DISAGREE on {len(contradictions)} version bound(s). Both are quoted "
            "below; deciding which is right is the judgement being asked for."
        )
    elif direction is ChangeDirection.WIDENING:
        lead = (
            "This WIDENS what is permitted, so if it is wrong an operator upgrades onto a "
            "Kubernetes version nothing supports. That is the expensive direction to be "
            "wrong in."
        )
    elif direction is ChangeDirection.NARROWING:
        lead = (
            "This NARROWS what is permitted, so if it is wrong the cost is an unnecessary "
            "component upgrade rather than a broken cluster. That is the cheap direction "
            "to be wrong in."
        )
    else:
        lead = (
            "This is the FIRST fact for this component, so there is no previous window to "
            "compare it against."
        )

    strength = {
        Confidence.HIGH: (
            "Evidence is strong: every stored value traces to a quote verified against the "
            "bytes of the page it cites, from the component's own table or a structured "
            "file, or corroborated by a second independent source."
        ),
        Confidence.MEDIUM: (
            "Evidence is a single first-party PROSE source. Every stored value traces to a "
            "verified quote, but prose is where a tested window and a supported one read "
            "alike, so it is worth reading the quote yourself."
        ),
        Confidence.LOW: (
            "Evidence is WEAK: at least one stored value does not trace to a verified "
            "quote, or sources disagree. Read the quotes before approving."
        ),
        Confidence.UNRESOLVED: (
            "There is no coherent window here. This should not have been proposed; treat "
            "it as a defect report rather than as a fact."
        ),
    }[confidence]
    return f"{lead} {strength}"


def summarize(
    confidence: Confidence,
    contradictions: tuple[Contradiction, ...],
    direction: ChangeDirection,
    decision: AcceptanceDecision,
) -> str:
    """A plain-language receipt summary. Derived from the inputs, not model narration.

    States what happened and what a reader should do about it. A pending or rejected fact
    says so and says why, because the alternative is a component quietly missing from the
    report with no explanation.
    """
    if decision is AcceptanceDecision.REJECTED:
        return (
            f"Confidence {confidence}: no usable compatibility window could be derived "
            "from what the sources state. Nothing was stored and the component remains a "
            "researchable gap -- absence of a fact is not evidence of incompatibility."
        )
    if decision is AcceptanceDecision.APPROVED:
        return (
            f"Confidence {confidence} ({direction.value.lower()} change). APPROVED by a "
            "person and written to the live store; the receipt names who approved it and "
            "the digest of the report they were shown."
        )
    caveat = ""
    if contradictions:
        caveat = f" {len(contradictions)} source contradiction(s), both quoted."
    elif confidence in (Confidence.LOW, Confidence.UNRESOLVED):
        caveat = " Evidence is weak; read the quotes."
    return (
        f"Confidence {confidence} ({direction.value.lower()} change). PENDING APPROVAL: it "
        "drives no verdict, and the component stays a typed gap until a person approves "
        f"it.{caveat} No fact is ever accepted automatically."
    )
