"""Stage 7 — close the loop from a knowledge gap to a resolved plan.

    in    a gap ledger
    out   a digested dispatch document: exactly which gaps to research, and why

NAMED `resolve_loop`, NOT `resolve`, AND THE NAME IS THE POINT
----------------------------------------------------------
Two modules called `resolve` would collide in meaning: `inventory/resolve.py` resolves a
*version*; this one resolves a *knowledge gap*. They share no symbols and have disjoint
responsibilities, so a "de-duplicate the two resolve modules" migration step could delete
live Stage 7 behaviour. Stage 2's module cannot be touched (it is digest-frozen), so this
one carries the distinguishing name.

THE NON-NEGOTIABLE PROPERTY
--------------------------
**Gap detection, dispatch and resume are all deterministic.** Making
Stage 7 an agent that decides to research was considered and rejected explicitly:
*"if the assessment pipeline were an agent that decides
to invoke research, a model would re-enter the verdict path."* Every function here is
a pure function of documents. The research it dispatches remains fully agentic; the
decision to dispatch never is.

Three structural consequences, in decreasing order of obviousness:

1. `dispatch_queue` is a pure function of the gap ledger. No model is consulted, and
   there is no seam where one could be.
2. The queue is published as a **digested document**, so what was asked is evidence
   rather than a log line. Two runs over the same ledger and store produce
   byte-identical dispatch documents.
3. `resume_decision` computes whether to re-run from **the fact store's digest
   changing**, not from what research reported -- so a research stage cannot talk it
   into re-running over knowledge that was not written.

   **NOT YET WIRED, AND SAYING SO IS THE POINT.** The deployed state machine does not
   call it -- but not for the reason this paragraph used to give. It said "`6 Research`
   routes to `3 Gaps` unconditionally", which is wrong twice over: the states carry no
   ordinals any more, and the unconditional route is to `Present`, not to `Gaps`.

   What is deployed: `Research` goes to a `Knowledge changed?` Choice whose store-digest
   comparison IS in the definition and never fires. Its first branch, `IsPresent = false`
   on `$.store_digest_after_research`, matches on every execution -- nothing sets that
   field -- and routes to `Present`. The `StringEqualsPath` branch that would apply this
   comparison is therefore never reached, and `Default -> Gaps` is unreachable.

   Which corrects the consequence too, in the run's favour: the deployment does NOT
   re-dispatch. A research pass that writes nothing ends the run at the report rather
   than causing rounds 2 and 3, so the "bounded waste" this paragraph warned about is
   not what the deployment does either. Still not the guarantee this function provides:
   the comparison is dormant, waiting on a producer for `store_digest_after_research`.

   So this remains the function the workflow MAY use, not a protection the deployment
   currently has.

THERE IS NO CLOCK HERE, AND THERE IS NO WINDOW EITHER
----------------------------------------------------
This module once carried a 24-hour expiry: a fact older than the window re-joined the
queue, and the reference instant it was measured against was an argument recorded in
the document. Both are gone, and the reason is the project's settled definition of
staleness -- **a fact goes stale when its SOURCE has moved, not when a clock has**.
`research/freshness.py` records that decision and the evidence for it: with every fact
human-approved, a time expiry means re-approving every fact every day forever, and it
makes the same snapshot produce different verdicts on different days, which is what the
digest chain exists to prevent.

WHY THAT LEAVES NOTHING FOR THIS MODULE TO DO ABOUT STALENESS. Drift is decided by a
sweep that runs beside the pipeline and writes a mark. A marked fact's `find()` returns
None, so the gap ledger reports its component through the FACT_MISSING path it has always
had for a component with no fact at all, and this module dispatches it because the ledger
called it researchable. No age is consulted because none needs to be: by the time a
ledger reaches here, the withholding has already happened.

WHAT THE REMOVAL BUYS, beyond deleting a rejected mechanism: `dispatch_queue` now takes
one argument. It cannot read a clock, it cannot read the fact store, and two invocations
over one ledger are byte-identical by construction rather than by care -- the earlier
version had a `datetime.now()` defect in exactly this spot, caught by re-invoking the
deployed function and comparing digests, and the fix was a default rather than the
removal of the input.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from ..facts import STORE_UNAVAILABLE, GapLedger
from ..models import ClusterReference, ContractModel
from .dispatch import DispatchReason, ResearchOutcome, ResearchRequest
from .ruleset import ruleset_digest

# Dispatch order is NOT recomputed here. `GapLedger.researchable` already sorts by
# assessment priority then workload reference, and re-sorting would create a second
# place for the order to be decided -- which is the class of defect the presentation
# stage exists to remove. A capped dispatch therefore takes the ledger's own
# highest-priority gaps first, by construction.


class NotDispatchedReason(StrEnum):
    """Why the loop declined to dispatch anything, when it declined.

    Each is a configuration or evidence condition, never a judgement about the
    cluster. Distinguishing them matters: "no gaps" and "the store is unreadable"
    look identical in a queue length of zero and mean opposite things.
    """

    # There is nothing to ask. The good case.
    NOTHING_TO_RESEARCH = "NOTHING_TO_RESEARCH"
    # Every researchable gap already has a candidate waiting for a human decision. Distinct from
    # NOTHING_TO_RESEARCH because the follow-up is the opposite: that one means the knowledge is
    # complete, this one means somebody needs to open the approval surface.
    ALL_AWAITING_APPROVAL = "ALL_AWAITING_APPROVAL"
    # The fact store could not be read, so EVERY component looks researchable. That
    # is a configuration fault, not a cluster full of gaps, and dispatching into it
    # would research the entire fleet against a store that cannot record the answer.
    STORE_UNREADABLE = "STORE_UNREADABLE"
    # The loop has already run its allowed number of rounds. A component that stays
    # HELD would otherwise re-queue forever.
    ITERATION_LIMIT_REACHED = "ITERATION_LIMIT_REACHED"


class WithheldRequest(ContractModel):
    """A researchable gap that was NOT dispatched because an answer is already awaiting review.

    RECORDED RATHER THAN FILTERED, because a queue that shrank silently would be
    indistinguishable from a ledger that had fewer gaps. A reader asking "why did keda not get
    researched this run" gets the answer here, with the count of candidates already waiting.
    """

    component_id: str = Field(min_length=1)
    version_series: str = Field(min_length=1)
    pending_candidates: int = Field(ge=1)
    reason: str = Field(min_length=1)


class DispatchDocument(ContractModel):
    """The Stage 7 output document: what to research, why, and what it rests on.

    Clock-free, and now completely so: there is no run identifier, no wall-clock read
    inside a rule, and no reference instant supplied from outside either. Two runs over
    one ledger produce byte-identical documents at one key.

    Cites the ledger and store digests it was derived from plus its own ruleset, so
    a reader can tell whether a changed queue came from the cluster, from the
    knowledge, or from the rules.
    """

    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["7"] = "7"
    cluster: ClusterReference

    ledger_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    snapshot_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    store_digest: str = Field(min_length=1)
    ruleset_digest: str = Field(min_length=1)

    # Which round of the loop produced this, from 1. Bounded, so a component that
    # never earns acceptance cannot spin the loop forever.
    iteration: int = Field(ge=1)
    max_iterations: int = Field(ge=1)

    # The cap on this round's queue, when one was supplied. Recorded because it is an
    # INPUT that determined the document: it truncates the request list, so the same
    # ledger under two caps is two different queues. Null means uncapped, which is every
    # deployment today -- nothing sets it, and it is still recorded rather than assumed,
    # because a document that omits an input a reader can vary is a document they cannot
    # reproduce. `dispatch_key` carries it for the same reason.
    max_dispatch: int | None = Field(default=None, ge=1)

    requests: tuple[ResearchRequest, ...] = ()
    # Gaps deliberately left un-researched because a candidate for them is already pending a
    # human decision. AN INPUT THAT DETERMINED THE DOCUMENT, so `dispatch_key` carries a segment
    # for it, exactly as it does for `max_dispatch`: the same ledger with and without pending
    # candidates is two different queues, and two different queues must not land on one object.
    withheld: tuple[WithheldRequest, ...] = ()
    # Present exactly when `requests` is empty, naming which of the three reasons
    # applies. A zero-length queue with no reason would leave "no gaps" and "the
    # store is unreadable" indistinguishable.
    not_dispatched: NotDispatchedReason | None = None

    @model_validator(mode="after")
    def an_empty_queue_says_why(self) -> DispatchDocument:
        """A zero-length queue must name its reason, and a non-empty one must not.

        The same discipline `ReportView` applies to its totals, for the same reason:
        the document is evidence, so an inconsistent one must not be constructible.
        Without this, "nothing to research" and "the fact store is unreadable" are
        both a queue of length zero -- and they mean opposite things. One is the fleet
        working; the other is a configuration fault that makes every component look
        researchable.
        """
        if not self.requests and self.not_dispatched is None:
            raise ValueError(
                "an empty dispatch queue must record why it is empty. Zero requests "
                "with no reason leaves 'nothing to research' indistinguishable from "
                "'the fact store could not be read', which are opposite conditions."
            )
        if self.requests and self.not_dispatched is not None:
            raise ValueError(
                f"{len(self.requests)} requests were queued, so not_dispatched must "
                f"be absent, not {self.not_dispatched!r}."
            )
        return self

    @model_validator(mode="after")
    def the_queue_respects_its_own_cap(self) -> DispatchDocument:
        """A document may not record a cap it exceeded.

        Cheap to check and the only thing that keeps the recorded cap meaningful: a queue
        longer than the cap it cites would describe a truncation that did not happen, and
        the key derived from it would then distinguish documents by a number neither of
        them obeyed.
        """
        if self.max_dispatch is not None and len(self.requests) > self.max_dispatch:
            raise ValueError(
                f"{len(self.requests)} requests were queued under a cap of "
                f"{self.max_dispatch}. The cap is recorded as an input, so a document "
                "cannot both cite it and exceed it."
            )
        return self

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    @property
    def research_required(self) -> bool:
        """Whether anything is worth asking. The state machine branches on this."""
        return bool(self.requests)

    def counts(self) -> dict[str, int]:
        by_reason: dict[str, int] = {}
        for request in self.requests:
            key = str(request.reason)
            by_reason[key] = by_reason.get(key, 0) + 1
        return {
            "requests": len(self.requests),
            **{f"reason_{k.lower()}": v for k, v in sorted(by_reason.items())},
        }


def _missing_requests(
    ledger: GapLedger,
) -> list[tuple[str, str, str, str | None, str | None]]:
    """`(component_id, series, priority, installed_version, helm_release)` per researchable gap.

    Drawn straight from `ledger.researchable`, which is `FactStatus.FACT_MISSING`
    and already priority-sorted -- a pure function of the report, the snapshot and
    the store. Nothing is added to it here; a gap the ledger did not call
    researchable is not researchable, and `NOT_ASKABLE` never appears in it.
    The installed version rides along for the assessment; it decides nothing here.
    The Helm release name rides along for the chart section of the research prompt.
    """
    return [
        (
            entry.identity,
            entry.version_series,
            entry.assessment_priority,
            entry.version,
            helm_release_of(entry.workload_ref),
        )
        for entry in ledger.researchable
        if entry.identity and entry.version_series
    ]


def helm_release_of(workload_ref: str) -> str | None:
    """The release name out of a `HelmRelease/<namespace>/<release>` workload ref, else None.

    Stage 2 folds every object Helm stamped with one release into a single record whose
    `workload_ref` has this shape, so the prefix IS the install method: no other record
    carries it. A `Deployment/...` or `EksAddon/...` ref is not a Helm release and gets None.

    >>> helm_release_of("HelmRelease/kube-system/metrics-server")
    'metrics-server'
    >>> helm_release_of("Deployment/kube-system/coredns") is None
    True
    """
    kind, _, rest = workload_ref.partition("/")
    if kind != "HelmRelease":
        return None
    _, _, release = rest.partition("/")
    return release or None


def dispatch_queue(
    ledger: GapLedger,
    *,
    iteration: int = 1,
    max_iterations: int = 3,
    max_dispatch: int | None = None,
    pending_candidates: dict[str, int] | None = None,
) -> DispatchDocument:
    """Decide exactly which gaps to research. A pure function of the ledger.

    No model participates and there is no seam where one could. Order is the ledger's
    own priority order, so a capped dispatch takes the most important questions first
    and two runs over the same ledger produce the same queue.

    THE FACT STORE IS NOT AN ARGUMENT, and its absence is the point. It used to be one,
    solely so a stored fact's `refreshed_at` could be aged against a window. With
    staleness decided by upstream drift outside the pipeline, the ledger is the whole
    input: it already records which components the store answered, and a fact the sweep
    marked is already absent from it. Passing the store would let a reader believe this
    stage consults knowledge it does not consult.

    `max_dispatch` caps the queue, and is recorded in the document and carried into the
    key -- because it is an input that changes the document's bytes, and every such input
    must reach the key or two different queues land on one object.

    `pending_candidates` MAPS A COMPONENT TO HOW MANY OF ITS CANDIDATES ARE ALREADY AWAITING
    APPROVAL, and a component in it is withheld rather than dispatched. THIS IS THE FIX FOR A
    MEASURED, EXPENSIVE LOOP: research writes a CANDIDATE, never a fact, and Stage 3 looks for a
    fact -- so a component whose answer is sitting in `facts-candidates/decision=PENDING_APPROVAL/`
    stays a researchable gap and is re-dispatched every single run, forever, until a human
    approves it. Counted on the live bucket before this existed: 40 pending candidates for one
    component, 36 for another, 24 for a third. One hundred multi-minute model calls answering three
    questions, and the 40 were 40 DISTINCT digests -- forty different answers, so whoever finally
    opened the approval surface was choosing arbitrarily among them.

    IT IS A SET PASSED IN, NOT A LOOKUP DONE HERE, and that is the same line this function's
    docstring already draws about the fact store. Listing an S3 prefix is I/O, and I/O inside this
    rule would make the dispatch document depend on the bucket's state at the instant it ran --
    two runs citing the same digests and disagreeing. The handler does the listing; this stays a
    pure function of the ledger and the arguments it is given.
    """
    base = {
        "cluster": ledger.cluster,
        "ledger_digest": ledger.digest(),
        "snapshot_digest": ledger.snapshot_digest,
        "store_digest": ledger.store_digest,
        "ruleset_digest": ruleset_digest(),
        "iteration": iteration,
        "max_iterations": max_iterations,
        "max_dispatch": max_dispatch,
    }

    # Guard 1: an unreadable store makes EVERY component look researchable. Refuse
    # to dispatch into that, because it is a configuration fault presenting as a
    # fleet-wide knowledge gap -- and researching the whole fleet against a store
    # that cannot record the answer is the worst possible response to it.
    if ledger.store_digest == STORE_UNAVAILABLE:
        return DispatchDocument(
            **base, not_dispatched=NotDispatchedReason.STORE_UNREADABLE
        )

    # Guard 2: bound the loop. The queue shrinks only when facts are written, so a
    # component that never earns acceptance would re-queue every round.
    if iteration > max_iterations:
        return DispatchDocument(
            **base, not_dispatched=NotDispatchedReason.ITERATION_LIMIT_REACHED
        )

    # De-duplicated by component while order is preserved. One component can appear in
    # the ledger under more than one workload, and its absence would show up as the same
    # question asked twice.
    ordered: list[tuple[str, str, str, str | None, str | None]] = []
    seen: set[str] = set()
    for component, series, priority, installed, helm_release in _missing_requests(ledger):
        if component in seen:
            continue
        seen.add(component)
        ordered.append((component, series, priority, installed, helm_release))

    # WITHHELD BEFORE THE CAP, deliberately. Filtering after `max_dispatch` would let components
    # whose answers are already waiting consume queue slots and push genuinely unanswered ones out
    # of a capped round -- the cap would then be spent on questions nobody needs asked again.
    waiting = {k: v for k, v in (pending_candidates or {}).items() if v > 0}
    withheld = tuple(
        WithheldRequest(
            component_id=component,
            version_series=series,
            pending_candidates=waiting[component],
            reason=(
                f"{waiting[component]} candidate(s) for this component are already awaiting a "
                "human decision, so re-researching it would add another answer to a queue nobody "
                "has read. Approve or reject one of them and the next run will see a fact."
            ),
        )
        for component, series, _, _, _ in ordered
        if component in waiting
    )
    ordered = [row for row in ordered if row[0] not in waiting]

    requests = tuple(
        ResearchRequest(
            component_id=component,
            version_series=series,
            # One reason, because there is one way to become researchable: no source
            # holds a fact for this series. A fact the sweep withheld arrives here
            # exactly like a fact that never existed -- deliberately, since that is
            # what makes Stage 3 a pure read. WHY it was withheld is answered by the
            # sweep's own document, which names the revisions that decided it.
            reason=DispatchReason.FACT_MISSING,
            priority=priority,
            installed_version=installed,
            helm_release=helm_release,
            ledger_digest=ledger.digest(),
        )
        for component, series, priority, installed, helm_release in ordered
    )
    if max_dispatch is not None:
        requests = requests[:max_dispatch]

    return DispatchDocument(
        **base,
        requests=requests,
        withheld=withheld,
        not_dispatched=(
            None
            if requests
            # An empty queue WITH withheld gaps is not "nothing to research" -- it is "somebody
            # needs to open the approval surface", and the two call for opposite follow-up.
            else NotDispatchedReason.ALL_AWAITING_APPROVAL
            if withheld
            else NotDispatchedReason.NOTHING_TO_RESEARCH
        ),
    )


class ResumeDecision(ContractModel):
    """Whether to re-run the assessment, and the evidence for that answer.

    THE INTENDED DETAIL: `resume` is driven by `store_digest_before !=
    store_digest_after`, not by what research reported. A research stage that returned
    ACCEPTED for everything while writing nothing could not make the loop re-run, and one
    that wrote a fact while reporting FAILED could not stop it. The store is the evidence;
    the outcomes are the narrative.

    **THE DEPLOYED WORKFLOW DOES NOT CALL THIS FUNCTION YET.** It is computed by nothing
    in `stages/`, and no state machine state invokes it -- `stages/resolve.py` puts
    `store_digest_before_research` on the payload and nothing reads it. The property above
    is asserted by `test_the_loop_refuses_to_resume_on_knowledge_that_was_not_written`,
    which calls this function directly; it is a property OF THIS FUNCTION and not yet a
    property of the pipeline. The pipeline's side of it is the `Knowledge changed?` Choice
    in `infra/terraform/modules/platform/workflow.tf`.
    """

    resume: bool
    store_digest_before: str = Field(min_length=1)
    store_digest_after: str = Field(min_length=1)
    dispatched: int = Field(ge=0)
    accepted_reported: int = Field(ge=0)
    held_reported: int = Field(ge=0)
    failed_reported: int = Field(ge=0)
    # Components that were queued and are still gaps afterwards, so the resolved
    # plan is honest about its limits rather than silently short.
    unresolved_after: tuple[str, ...] = ()
    detail: str = Field(min_length=1)


def resume_decision(
    document: DispatchDocument,
    outcomes: tuple[ResearchOutcome, ...],
    store_digest_after: str,
) -> ResumeDecision:
    """Decide whether the enlarged store warrants re-running decide and plan."""
    before = document.store_digest
    changed = before != store_digest_after and store_digest_after != STORE_UNAVAILABLE
    by = {"ACCEPTED": 0, "HELD": 0, "FAILED": 0}
    for outcome in outcomes:
        by[outcome.disposition] = by.get(outcome.disposition, 0) + 1
    still_gaps = tuple(
        dict.fromkeys(
            o.component_id for o in outcomes if o.disposition != "ACCEPTED"
        )
    )

    if not document.requests:
        detail = (
            "Nothing was dispatched, so there is nothing new to assess. "
            f"Reason: {document.not_dispatched}."
        )
    elif changed:
        detail = (
            "The fact store changed, so decide and plan are re-run over the enlarged "
            "knowledge and every downstream digest moves to reflect it."
        )
    elif store_digest_after == STORE_UNAVAILABLE:
        detail = (
            "The fact store became unreadable after dispatch. Not resuming: a "
            "re-run against an unreadable store would turn every verdict UNRESOLVED "
            "and read as though the cluster had regressed."
        )
    else:
        detail = (
            "No fact was written -- every result was held or failed -- so the store "
            "is byte-identical and a re-run would reproduce the same assessment. "
            "The gaps stay gaps, reported as gaps."
        )

    return ResumeDecision(
        resume=changed and bool(document.requests),
        store_digest_before=before,
        store_digest_after=store_digest_after,
        dispatched=len(document.requests),
        accepted_reported=by["ACCEPTED"],
        held_reported=by["HELD"],
        failed_reported=by["FAILED"],
        unresolved_after=still_gaps,
        detail=detail,
    )
