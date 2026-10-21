"""Stage 7 — the resolve loop's decision point.

    in    a gap ledger
    out   a digested dispatch document; `research_required` for the workflow to branch on

WHAT THIS HANDLER DOES AND DELIBERATELY DOES NOT DO
--------------------------------------------------
It decides **which gaps are legitimate to research** and publishes that decision as a
document. It does not research anything, does not write a fact, and does not re-run
decide or plan.

That division is what keeps the loop's guarantees intact in a deployed pipeline:

- **It cannot write a fact.** THIS role has no `PutItem` on the fact table. Promotion
  stays a separate, human-gated act.

  Exactly one role holds `dynamodb:PutItem` on the fact table: `-approve`, sid
  `WriteApprovedFact`, which only a signed human can reach.

  The guarantee this bullet is really making survives all of that, in the form that is
  true: the dispatch stage cannot write a fact because ITS OWN role grants no `PutItem`,
  not because no role does.
- **It cannot invoke another Lambda.** So the resume is not something this stage
  performs; it returns `research_required` and the state machine branches. The re-run
  is the *real* gaps, decide and plan functions over the enlarged store, which is what
  keeps the digest chain honest -- rather than this stage recomputing an assessment
  out of band and writing it to another stage's prefix.
- **Its package contains no model client.** It shares `stages.zip` with stages 2-5:
  pydantic and nothing else. So "the dispatch decision involved no model" is a
  property of the deployed artifact, not a claim about the code.

THE FLEET ECONOMICS, WHICH IS THE WHOLE POINT
--------------------------------------------
Two clusters running the same add-on. The first has no fact for it, so the ledger
reports `FACT_MISSING`, this stage queues one request, and research runs. The second
cluster's ledger finds the fact already there, so `researchable` is empty, the queue
is empty, and **zero research requests are dispatched -- therefore zero model calls.**

Nothing about that is a heuristic or a cache: `GapLedger.researchable` is a pure
function of the inventory, the snapshot and the fleet-wide fact store, and the store
is shared. The second cluster is free because the question was already answered, and
the dispatch document proves it was not asked again.

WHERE "IS THAT FACT STILL CURRENT?" IS ANSWERED, WHICH IS NOT HERE
----------------------------------------------------------------
This stage once applied a 24-hour expiry to a stored fact and re-queued anything older.
That mechanism is gone. In this system a fact stops answering when its SOURCE has moved,
which a sweep decides from outside the pipeline (`research/freshness.py`) and records as a
mark; the marked fact then returns None from the store, and Stage 3 reports its component
as researchable through the path it has always had for a component with no fact.

So this stage reads no clock, takes no reference instant and consults no fact store. It
sees a ledger and queues what the ledger already called researchable. Two invocations over
one ledger write byte-identical bytes to one key -- which the previous design achieved by
defaulting a reference instant carefully, and this one achieves by having no input that
could vary.
"""

from __future__ import annotations

import os
from typing import Any

from ..facts import GapLedger
from ..loop import dispatch_queue, ruleset_digest
from ..storage import build_uri, dispatch_key, read_document, write_document
from ..storage.candidates import PENDING_PREFIX as _PENDING_PREFIX
from ..storage.candidates import list_pending, pending_counts
from .settings import from_event

# How many rounds the loop may run. Bounded because the queue shrinks only when facts
# are written: a component whose research is repeatedly HELD would otherwise re-queue
# every round forever.
DEFAULT_MAX_ITERATIONS = 3


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# RE-EXPORTED, NOT DEFINED. The prefix now belongs to `storage/candidates.py` beside the rest
# of the listing rule; this name is kept so anything importing it from here still resolves.
PENDING_PREFIX = _PENDING_PREFIX


def _pending_candidates(bucket: str) -> dict[str, int]:
    """How many unanswered questions each component already has waiting for a person.

    THE I/O HALF OF THE PENDING SUPPRESSION, and it lives here rather than in
    `loop/resolve_loop.py` for the reason that module's own docstring gives about the fact store:
    a listing is I/O, and I/O inside the rule would make the dispatch document depend on the
    bucket's state at the instant it ran -- two runs citing the same digests and disagreeing. The
    rule takes the counts as an argument and stays pure.

    THE LISTING RULE MOVED TO `storage/candidates.py`, and that is the whole point of the move
    rather than tidying. `stages/approval_gate.py` now offers the same set this suppresses, and
    the two MUST agree: a candidate that suppresses research while being invisible to the page
    that could clear it leaves a component stuck for good. They agree by sharing one function.

    TWO THINGS THAT CHANGED WITH THE MOVE, both of which were defects here:

    1. **Counts are per SERIES, not per object.** Each candidate is written with an
       `.evidence.json`, an `.extraction.json` and a `.report.json` beside it under the same
       prefix, so the old count of `Contents` reported FOUR pending answers for one question --
       which is literally what `withheld_awaiting_approval: {"metrics-server": 4}` was saying.
    2. **A candidate older than the TTL no longer suppresses.** It ages out of the actionable
       set instead, so an abandoned answer stops blocking a real gap. Nothing is deleted.

    AN UNREADABLE PREFIX SUPPRESSES NOTHING, which is the safe direction: the cost is researching
    a component whose answer was already waiting, exactly the behaviour before this existed. The
    opposite default would silently stop researching real gaps because a list call failed.
    """
    return pending_counts(list_pending(bucket))


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Compute the dispatch queue for this run and store it as a document."""
    settings = from_event(event)
    ledger = read_document(event["ledger_uri"], GapLedger)

    iteration = int(event.get("resolve_iteration") or 1)
    document = dispatch_queue(
        ledger,
        iteration=iteration,
        max_iterations=int(
            event.get("max_resolve_iterations")
            or _float_env("READINESS_MAX_RESOLVE_ITERATIONS", DEFAULT_MAX_ITERATIONS)
        ),
        max_dispatch=(
            int(event["max_dispatch"]) if event.get("max_dispatch") else None
        ),
        # A component whose answer is already awaiting approval is not re-researched. Measured
        # before this existed: 100 pending candidates answering three questions, because research
        # writes a candidate and Stage 3 looks for a fact, so the gap never closed and the queue
        # re-dispatched it every run.
        pending_candidates=_pending_candidates(settings.bucket),
    )

    uri = build_uri(
        settings.bucket,
        dispatch_key(
            settings.cluster_name,
            document.snapshot_digest,
            document.store_digest,
            document.ruleset_digest,
            event["snapshot_captured_at"],
            iteration,
            # BOTH taken from the DOCUMENT, not from the payload that produced it, so the
            # key cannot describe a bound or a cap the document was not computed under.
            document.max_iterations,
            document.max_dispatch,
            # From the DOCUMENT, like the two above, so the key cannot claim a suppression the
            # document was not computed under.
            len(document.withheld),
        ),
    )
    write_document(uri, document)

    return {
        **event,
        "stage": 7,
        "dispatch_uri": uri,
        "dispatch_digest": document.digest(),
        "resolve_ruleset_digest": ruleset_digest(),
        # What the state machine branches on. A boolean rather than a count, so the
        # Choice state cannot drift from the document's own answer.
        "research_required": document.research_required,
        # What was withheld and why, on the payload so an operator sees it without opening the
        # document. A non-empty map here means somebody needs to open the approval surface: the
        # answers exist and are waiting for a decision.
        "withheld_awaiting_approval": {
            w.component_id: w.pending_candidates for w in document.withheld
        },
        # THE SECOND THING THE STATE MACHINE BRANCHES ON, and it exists because the map above
        # was a claim nothing acted on. `Research needed?` tested only `research_required`, so a
        # run whose every gap was withheld routed PAST the approval state -- the pending answers
        # suppressed research and were never offered, which leaves a component stuck for good
        # rather than merely unresearched. This is the edge that makes them reachable.
        #
        # A BOOLEAN FOR `research_required`'s REASON: Amazon States Language cannot test a map
        # for emptiness, and a count would let the Choice disagree with the document.
        #
        # Note it is deliberately independent of `research_required`. Both can be true at once
        # -- some gaps dispatched, others withheld -- and the machine then researches first and
        # reaches the approval afterwards, which is the order that lets one page carry both.
        "approval_pending": bool(document.withheld),
        # AN EMPTY RESULT SET, SO THE FIELD ALWAYS EXISTS, and this is not tidiness -- without
        # it the run FAILS. `Research`'s `ResultPath` is `$.research_results`, so that field is
        # created by the Map and exists only on a path that went through it. The `Approval`
        # state's Payload names `"research_results.$"`, and Step Functions fails a state whose
        # Payload names an absent path -- `States.Runtime: The JSONPath ... could not be found`.
        #
        # That never mattered while `Approval` was reachable only from `Research`. The moment a
        # withheld gap could route the Choice straight to `Approval`, every such run died one
        # state after `Plan` for a reason unrelated to what it was deciding.
        #
        # OVERWRITTEN BY THE MAP WHEN RESEARCH DOES RUN, because `ResultPath` replaces the whole
        # field -- so this is the value for exactly the case it is needed in, and invisible
        # otherwise. `[]` is also the honest statement: this run researched nothing, and the
        # gate's own fallback to stored candidates is what fills the page.
        #
        # Fixed here rather than in the Payload for the reason `upgrade_approval._payload`
        # records about `brief_uri`: a `.waitForTaskToken` Payload cannot express "this field
        # if present", so the only robust place is the stage that produces the payload.
        "research_results": [],
        "not_dispatched": (
            str(document.not_dispatched) if document.not_dispatched else None
        ),
        # The queue itself, so a research stage receives it without re-reading the
        # document, and so an operator watching the execution sees the questions.
        "research_requests": [r.model_dump(mode="json") for r in document.requests],
        # The round just decided, for a reader watching the execution.
        "resolve_round": iteration,
        # ALREADY INCREMENTED, on purpose. When the workflow loops back through
        # research and gap detection, the next resolve invocation reads this field
        # and is round N+1 -- so the loop needs no arithmetic in the state machine.
        # Amazon States Language has no clean way to increment one field of a payload
        # without enumerating the whole payload, and enumerating it would silently
        # drop any field a future stage adds. The document records the round it
        # actually decided; this field is where the *next* round starts.
        "resolve_iteration": iteration + 1,
        "dispatch_counts": document.counts(),
        # The store digest at decision time, carried for the resume comparison
        # `loop.resume_decision` performs.
        #
        # THE DEPLOYED MACHINE GOES TO `Present`, NOT BACK TO `Gaps`, and this comment used
        # to say the opposite. It read "the deployed state machine routes research back to
        # gap detection unconditionally", which names the wrong destination: the
        # unconditional route today is to the REPORT. `Knowledge changed?`'s first branch is
        # `IsPresent = false` on `$.store_digest_after_research`, nothing sets that field --
        # no handler returns it, and `Research` is a Map whose `ResultPath` is
        # `$.research_results`, so a per-item value cannot reach the payload root -- so that
        # branch matches on every execution and its `Next` is the report entry. `Default -> Gaps`
        # is unreachable. Observed on a test run.
        #
        # THIS FIELD IS REFERENCED BY THE DEPLOYED DEFINITION, and "nothing reads it" was
        # also too strong. The second branch's `StringEqualsPath` names
        # `$.store_digest_before_research` -- this value. Choices evaluate in order, so that
        # branch is never reached, which makes the wiring DEPLOYED AND DORMANT rather than
        # absent. The missing half is a producer for `store_digest_after_research`, not the
        # comparison.
        #
        # So: still not a protection now in force, and still worth carrying. Left on the
        # payload deliberately -- the value has to survive the loop-back to be usable, and
        # adding it later would mean changing three stages' payloads.
        "store_digest_before_research": document.store_digest,
    }
