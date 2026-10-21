"""The freshness sweep, as a handler. A state in the machine; not a stage in the chain.

WHERE IT SITS, AND WHY THAT EXACT PLACE. The sweep runs **between `Inventory` and
`Gaps`**. Marks have to land before the fact store is read, or a run decides against
knowledge the sweep was about to withdraw; and the research loop-back's target is `Gaps`,
so putting the sweep ahead of it means the network read happens once per run rather than
once per research round. (That loop-back is unreachable on the deployed definition today --
see the store-digest branch in `workflow.tf` -- so the once-per-run property is currently the
only one in force.
The placement is still the right one for when it is wired.)

WHY IT IS NOT INSIDE A STAGE, which is the constraint the whole design turns on. Stage 3 is
a pure function of already-fetched evidence plus the fact store, and Stage 7 is a pure
function of the ledger. A network read inside either would make that stage's document depend
on GitHub's state at the instant it ran: two runs would cite the same digests and disagree,
and replay would be gone while still appearing guaranteed. So the sweep is its own function,
with its own role, in its own Terraform component -- and no stage imports it, which
`tests/test_the_sweep_publishes_its_own_document.py` asserts over the import graph.

WHAT IT WRITES. Two things, and they are different in kind:

    marks       into the marks table. What the STAGES read: a marked fact returns None
                from `find()`, so Stage 3 reports its component as researchable through
                the path it already had for a component with no fact.
    a document  into `sweeps/`, digested and keyed like every other document. What a
                PERSON reads: which fact was withheld, the repository that was asked, the
                release it reported and when that release was published, the instant the
                fact itself was read, and when the comparison was made.

A mark is also RETIRED here, which is the other half of the same write. A staleness mark
raised against a revision a human has since replaced withholds nothing, and the sweep stamps
it retired so it stops being read as in force. The stamp is a put at the mark's own
coordinates -- the row is overwritten, not deleted, so the role needs no
`dynamodb:DeleteItem`. It is housekeeping and it is deliberately not load-bearing: a spent
mark suppresses nothing whether or not the stamp ever lands, so a refused retirement is
reported on the payload and the run continues (`_save_marks`).

The document is why this handler is more than a mark writer. "Why is this fact stale" is
what someone asks of a report, and a prose `reason` on a DynamoDB item is not where they can
ask it. It is also the one document in this system whose content is honestly a function of
the instant it was produced -- which is not an exception to the reproducibility rule but the
reason this work lives outside every stage.

**Two cadences, one mechanism.**

    scheduled    no scope in the event -- visits every stored fact
    pre-flight   `inventory_uri` (or an explicit `components` list) -- visits only the
                 components this run is about

The pre-flight form is what the state machine uses, and it scopes itself by READING THE
STAGE 2 INVENTORY rather than being handed a list: the run's components are already recorded
there, the read is pure, and the document can then cite the inventory digest that scoped it.

**Revocation shares the handler**, because it is the same act -- a fact stops answering and
a mark records why -- differing only in whether a comparison or a human decided. Passing
`revoke` names the component and requires a reason and an actor. It publishes no document:
a revocation is a claim about a fact's correctness, not a report on the state of upstream,
and its own return value already records the whole act.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from ..models import ClusterReference
from ..report import InventoryReport
from ..research.freshness import (
    GitHubUpstreamResolver,
    StaticUpstreamResolver,
    SweepDocument,
    SweepMode,
    SweepResult,
    revoke_mark,
    ruleset_digest,
    sweep,
    sweep_document,
)
from ..research.marks import MarkLedger
from ..storage import (
    build_mark_store,
    build_raw_fact_store,
    build_uri,
    read_document,
    store_entries,
    sweep_key,
    write_document,
)


class _Scope:
    """What this sweep is about: which components, for which run, on whose evidence."""

    def __init__(
        self,
        components: tuple[str, ...] | None,
        cluster: ClusterReference | None = None,
        inventory_digest: str | None = None,
    ) -> None:
        self.components = components
        self.cluster = cluster
        self.inventory_digest = inventory_digest

    @property
    def mode(self) -> SweepMode:
        return SweepMode.SCHEDULED if self.components is None else SweepMode.PRE_FLIGHT


def _scope(event: dict[str, Any]) -> _Scope:
    """Resolve the sweep's scope from the payload.

    `inventory_uri` wins: it is what the state machine supplies, and reading the Stage 2
    document means the scope is the run's own recorded components rather than a list
    somebody maintained by hand. An explicit `components` list still works, for a targeted
    sweep by a human or a schedule.

    AN EMPTY SCOPE MEANS NOTHING, NOT EVERYTHING, and the difference is one `or` away from
    being a live defect: an inventory in which no component could be identified must sweep
    zero facts, because this run asked about zero. Falling back to a whole-store sweep would
    resolve every fact's upstream on behalf of a run that named none of them -- the opposite
    of what the pre-flight form is for.
    """
    uri = str(event.get("inventory_uri") or "").strip()
    if uri:
        report = read_document(uri, InventoryReport)
        identities = tuple(
            sorted({c.identity.strip() for c in report.components if c.identity})
        )
        return _Scope(identities, report.cluster, report.digest())
    if event.get("components") is not None:
        return _Scope(tuple(str(c).strip() for c in event["components"]))
    return _Scope(None)


def _publish(
    document: SweepDocument, event: dict[str, Any], cluster_name: str | None
) -> tuple[str | None, str | None]:
    """Store the sweep document. Returns `(uri, why_not)` -- exactly one of them is None.

    THE ONE THING THIS FUNCTION MUST NOT DO IS FAIL THE RUN, and that is a line drawn
    through the whole handler rather than a local defensive habit:

        the sweep FAILS on anything that compromises the MARKS
        the sweep DOES NOT FAIL on anything that only compromises the REPORT of them

    Marks are the mechanism -- they are what makes a superseded fact stop answering, so a
    sweep that finds a release and cannot record it would let the run answer from knowledge it
    knows is stale, which is a wrong answer rather than a missing one. That still raises.

    This document is the report. By the time it is written the marks are already in force,
    so a failure here costs a reader an explanation and costs the run nothing. Raising would
    turn "the report of the sweep could not be stored" into "this cluster gets no readiness
    report at all", which inverts the design's own rule that a stale fact is withheld and
    never awaited.

    IT IS ALSO A FAILURE THAT CAN HAPPEN. `infra/terraform/modules/platform/sweep.tf` owns this
    function's role, and a deployment missing its `s3:PutObject` grant on `sweeps/*` makes every
    write here AccessDenied. Under a raising version every run would then fail at the Sweep
    state, in a pipeline that worked the day before.

    Not swallowed either -- the reason names the exception and reaches the payload, so this
    is loud and non-blocking rather than quiet.
    """
    bucket = str(event.get("bucket") or os.environ.get("DOCUMENT_BUCKET", "")).strip()
    if not bucket:
        return None, (
            "no bucket was supplied on the payload and DOCUMENT_BUCKET is unset, so the "
            "sweep document was computed and digested but not stored. The marks were "
            "written; the report of them was not."
        )
    uri = build_uri(
        bucket,
        sweep_key(
            str(document.mode),
            cluster_name,
            document.store_digest,
            document.ruleset_digest,
            document.digest(),
            document.swept_at,
        ),
    )
    try:
        return write_document(uri, document), None
    except Exception as exc:
        # BROAD ON PURPOSE, and this is the one place in this package where that is the
        # right call. The failures worth surviving here are botocore's
        # `ClientError`/`BotoCoreError` (AccessDenied, NoSuchBucket, a throttle), and
        # naming those types would import boto3 into a module that otherwise does not need
        # it -- while still missing whatever the next transport raises. The consequence of
        # over-catching is bounded: the marks are already saved, and the exception's type
        # and message are reported rather than discarded.
        return None, (
            f"the sweep document could not be stored at {uri}: "
            f"{type(exc).__name__}: {exc}. The marks WERE written and the stages will see "
            "them, so the sweep took effect and only its report is missing. If this is "
            "AccessDenied, check the s3:PutObject grant on sweeps/* in "
            "infra/terraform/modules/platform/sweep.tf."
        )


def _save_marks(
    result: SweepResult, existing: MarkLedger, marks: Any
) -> tuple[SweepResult, str | None]:
    """Write the sweep's marks. Returns `(what actually landed, why the rest did not)`.

    THE SAME LINE `_publish` DRAWS, ONE STEP EARLIER, and it is the line that keeps the
    loop-breaker independent of the housekeeping:

        a NEW MARK is the mechanism        -- a failure to write it still raises
        a RETIREMENT is housekeeping       -- a failure to write it is reported, not raised

    A new mark that cannot be written would let the run answer from knowledge the sweep knows
    is stale: a wrong answer rather than a missing one, so it must fail the run. A retirement
    is only a tidy-up of a mark that is ALREADY withholding nothing -- it names a revision the
    store no longer holds, and `MarkLedger.suppresses` declines to honour it whether or not
    this write ever succeeds. Failing the run over it would make the tidy-up load-bearing and
    trade a working pipeline for a neat table.

    On a retirement-only failure the result is rebuilt WITHOUT the retirements, so the
    document that follows cites the ledger that is actually in the table. A document claiming
    a retirement no row carries would be exactly the overclaim the digest chain exists to
    prevent.
    """
    if not (result.new_marks or result.retired_marks):
        return result, None
    try:
        marks.save(result.ledger)
    except Exception as exc:
        if result.new_marks:
            raise
        return (
            SweepResult(
                result.visited,
                result.new_marks,
                existing,
                result.unresolved,
                result.visits,
                (),
            ),
            (
                f"{len(result.retired_marks)} spent mark(s) could not be retired: "
                f"{type(exc).__name__}: {exc}. They name a revision the store no longer "
                "holds, so they already withhold nothing and no fact is affected; the next "
                "sweep retries the stamp."
            ),
        )
    return result, None


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Mark facts whose component has released, publish the sweep's document, or revoke a fact.

    Staleness is AN UPSTREAM RELEASE, not elapsed time: a fact is marked when its component's
    GitHub repository published a release after the fact was read. Optional in `event`:

        inventory_uri     the Stage 2 document -- the pre-flight form the state machine
                          uses. Scopes the sweep to this run's components.
        components        list of identities -- an explicit pre-flight scope. Absent
                          together with `inventory_uri` means sweep every stored fact.
        bucket            where to publish the sweep document. Falls back to
                          DOCUMENT_BUCKET; with neither, no document is written and the
                          result says so.
        pinned_releases   {"owner/repo": {"tag": ..., "published_at": ...}} freezing what
                          upstream reports, so a past sweep can be reproduced exactly.
                          Absent means read GitHub live.
        revoke       {"component_id": ..., "reason": ..., "actor": ...,
                      "consumed_by": [...]} -- revoke instead of sweeping.

    Returns the payload it was given plus its own fields, because this handler is a state
    BETWEEN two stages: a state that returned only its own result would strip
    `snapshot_uri` and `inventory_uri` off the payload and Stage 3 would fail for want of
    its inputs.
    """
    now = datetime.now(UTC)
    marks = build_mark_store()
    existing = marks.load()

    revoke = event.get("revoke")
    if revoke:
        return _revoke(revoke, existing, marks, now, event)

    # The RAW store: the sweep has to see a fact in order to reason about it, and the
    # decorated store would already be withholding the ones it has marked.
    #
    # `store_entries` rather than a getattr with an empty default. The default store is the
    # digest-frozen `FileFactStore`, which has no public `entries()` and cannot be given
    # one, so a tolerant lookup silently swept zero facts and reported success -- caught by
    # a fixture, and exactly the failure this codebase keeps naming. It now raises instead.
    store = build_raw_fact_store()
    facts = store_entries(store)

    scope = _scope(event)

    # Staleness is decided by an upstream RELEASE, so the sweep needs something that can
    # answer "what is the newest release in this repository?". A `pinned_releases` map in the
    # event freezes that answer, which is how a caller reproduces a past sweep exactly;
    # otherwise the GitHub resolver reads it live. Either way no model is involved.
    # THE TOKEN COMES FROM THE SAME RESOLVER THE BRIEF'S TOOL USES, not from a second copy
    # of the lookup. This read `os.environ["GITHUB_TOKEN"]` and nothing else, so a token
    # held in Secrets Manager -- which is the only place a deployed function should carry a
    # third-party credential -- was invisible to the sweep, and the sweep ran
    # unauthenticated against GitHub's tighter unauthenticated rate limit.
    #
    # `fetch_issues._token()` already resolves both forms in the documented order: a plain
    # `GITHUB_TOKEN` first, because that is what a local run exports, then
    # `GITHUB_TOKEN_SECRET_ARN` from Secrets Manager. Its docstring names *this* module as
    # the reason the plain variable wins, so it was written to be the shared one. A private
    # name across a module boundary is the lesser evil: re-implementing the Secrets Manager
    # read here is the thing that silently drifts, and the two would then disagree about
    # which sources count and how a JSON-wrapped secret is unwrapped.
    from .fetch_issues import _token as _github_token

    resolver = (
        StaticUpstreamResolver(dict(event["pinned_releases"]))
        if event.get("pinned_releases")
        else GitHubUpstreamResolver(token=_github_token())
    )

    result = sweep(facts, existing, now, resolver=resolver, only=scope.components)

    # MARKS FIRST, DOCUMENT SECOND. The document reports the marks that are in force, so
    # they have to be in force for it to be true. A document written first would describe a
    # state that a failed write then never produced.
    result, not_retired = _save_marks(result, existing, marks)

    document = sweep_document(
        result,
        mode=scope.mode,
        swept_at=now,
        store_digest=store.digest(),
        marks_before=existing,
        cluster=scope.cluster,
        scope=scope.components or (),
        inventory_digest=scope.inventory_digest,
    )
    uri, not_published = _publish(
        document, event, scope.cluster.name if scope.cluster is not None else None
    )

    return {
        **event,
        "stage": "sweep",
        "mode": str(document.mode),
        "swept_at": document.swept_at,
        # No time window appears here. Staleness is an upstream release, and reporting a TTL
        # would describe a mechanism this sweep does not use.
        "staleness": document.staleness,
        "visited": list(result.visited),
        # `expired` for the caller's shape, which predates the mechanism. Nothing about a
        # clock decided it: these are the components whose repository released after the
        # fact was read.
        "expired": list(result.expired),
        # Components whose SPENT mark was retired: it named a revision the store no longer
        # holds, so it was withholding nothing. Reported beside `expired` because "this
        # component stopped being researchable" is as much an event as "it started".
        "retired": list(result.retired),
        "marks_in_force": len(result.ledger),
        "store_digest_moves": document.store_digest_moves,
        "upstream_unresolved": list(result.unresolved),
        "reasons": {m.component_id: m.reason for m in result.new_marks},
        "retirement_reasons": {
            m.component_id: m.retirement.reason
            for m in result.retired_marks
            if m.retirement is not None
        },
        "summary": result.summary(),
        # The document, in the shape every other state uses: a URI and a digest on the
        # payload, so the execution's final output carries this one too.
        "sweep_uri": uri,
        "sweep_digest": document.digest(),
        "sweep_ruleset_digest": ruleset_digest(),
        "sweep_counts": document.counts(),
        # Present exactly when `sweep_uri` is null, naming why. The pair is the same
        # discipline `DispatchDocument` applies to an empty queue: an absent document and
        # an unexplained absent document are different things, and only one of them is
        # something an operator can act on.
        **({} if uri else {"sweep_not_published": not_published}),
        # Present exactly when a retirement was attempted and refused. Loud rather than
        # swallowed, and non-fatal: the marks it names were already withholding nothing.
        **({"marks_not_retired": not_retired} if not_retired else {}),
    }


def _revoke(
    spec: dict[str, Any],
    existing: MarkLedger,
    marks: Any,
    now: datetime,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Revoke one fact. Requires a reason and an actor, and keeps the fact.

    The fact is not deleted. A deletion leaves nothing to explain what a past assessment
    rested on, so the mark is the record: it names who revoked, why, the revision revoked,
    and every document computed from it that is therefore due recomputation.
    """
    component_id = str(spec["component_id"]).strip()
    reason = str(spec.get("reason", "")).strip()
    actor = str(spec.get("actor", "")).strip()
    if not reason or not actor:
        raise ValueError(
            "a revocation requires both a reason and an actor: a fact that quietly stops "
            "answering with no accountable author is the failure the audit trail exists "
            "to prevent"
        )

    store = build_raw_fact_store()
    fact = store.find(component_id)
    mark = revoke_mark(
        component_id,
        reason,
        now,
        actor=actor,
        fact=fact,
        consumed_by=tuple(str(c) for c in spec.get("consumed_by", ())),
    )
    ledger = MarkLedger(marks=(*existing.marks, mark))
    marks.save(ledger)

    return {
        "mode": "revoke",
        "revoked_at": now.isoformat(),
        "component_id": component_id,
        "actor": actor,
        "reason": reason,
        "fact_revision": mark.fact_revision,
        "fact_was_present": fact is not None,
        "consumed_by": list(mark.consumed_by),
        "marks_in_force": len(ledger),
        # Stated rather than left implicit: the next run past this mark reports the
        # component as a researchable gap and re-researches it under the current logic.
        "effect": (
            f"{component_id} stops answering immediately. Stage 3 reports it as "
            "researchable, Stage 4 reports UNRESOLVED rather than a verdict from the "
            "revoked fact, and the fact itself is retained for audit."
        ),
        **({"note": event["note"]} if event.get("note") else {}),
    }
