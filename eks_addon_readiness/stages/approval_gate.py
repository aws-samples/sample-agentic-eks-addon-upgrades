"""The Approval stage: pause the run, show a person what needs deciding, and wait.

    in    the research stage's per-item results, PLUS the candidates still awaiting a
          decision from an earlier run
    out   nothing, until a person says they are done

IT HAS A SECOND MODE THAT DOES NOT WAIT, AND THAT MODE IS WHY THE PAUSE IS NOW HONEST. See
`survey` below: the same function, the same offered set, no token and no mail, returning only
the counts so the state machine can decide whether entering the wait asks anybody anything.

WHERE IT SITS, AND WHY THAT IS THE WHOLE POINT.

    Research -> Approval survey -> Anything to approve? -> APPROVAL (waits)
                                                       -> Knowledge changed?  (nothing to ask)

The `Knowledge changed?` Choice has existed on the deployed definition for a long time
and has NEVER fired. Its default branch loops back to `Gaps`, so an enlarged fact store
rebuilds the ledger, the assessment and the plan -- exactly the behaviour the design
wanted. It never fired because nothing between `Research` and that Choice could write a
fact: research writes a CANDIDATE, and `approve.py` is the only writer of facts. So within
one execution the store could not change, and the loop only ever closed ACROSS executions:
a person approved, and the NEXT run picked the fact up.

This stage is what closes it inside one execution. Approval is the only thing that can move
the store digest, so putting the wait here -- before the Choice, not after the report --
means the run that proposed a fact is the run that reports on it.

WHY A TASK TOKEN AND NOT A POLLING LOOP. `.waitForTaskToken` costs nothing while it waits
and resumes in one call. The alternatives are worse in ways that matter: a Wait/Choice
poll loop pays for a state transition per tick and puts an upper bound on patience that has
nothing to do with the person, and an Activity would need a worker to hold open.

WHAT THIS HANDLER DOES NOT DO. It does not decide, does not approve, does not judge, and
does not write a fact -- it renders what research proposed and records where to resume. It
holds no `SendTaskSuccess` permission and could not resume its own run if it tried; that
permission is the approval endpoint's, because the person's decision is what resumes it.

WHAT HAPPENS IF NOBODY EVER DECIDES. The state carries a `TimeoutSeconds` and the execution
FAILS when it expires. Failing is deliberate: a run that quietly gave up and reported on
unapproved research would produce a report indistinguishable from one nobody was asked
about. A failed execution says plainly that a decision was needed and never came.

WHY THE PAGE IS NOT BUILT FROM `research_results` ALONE, WHICH IS THE OTHER HALF OF A TRAP.
`stages/resolve.py` WITHHOLDS a component whose answer is already awaiting a decision, so it
is not researched twice -- correct on its own. This stage read only this execution's Map
output, so the very candidate that suppressed the research was invisible to the only surface
that could clear it:

    resolve says   "skip it, an answer is waiting"
    this stage     "I see nothing to ask about"
    the operator   is never asked, and the fact never arrives

A component in that state is stuck FOREVER and nothing reports it. Observed live on
metrics-server: a double-start produced two executions, one was stopped, its
research Lambda kept running past the abort and wrote a candidate, and that candidate then
blocked metrics-server in every later run while appearing on no page. So the offered set is
this run's results MERGED WITH the actionable pending candidates in the bucket, read through
the same `storage/candidates.py` rule `resolve` suppresses on -- one function answering both
questions is what keeps the suppressor and the offerer from disagreeing again.

THE I/O IS HERE AND THE RULE STAYS PURE. `view/build.py::approvals_from_research` takes a
list and reads nothing, exactly as `loop/resolve_loop.py` takes counts and lists nothing:
I/O inside the rule would make the rendered page depend on the bucket's state at the instant
it ran, so two renders of one run could differ for a reason no reader could diagnose. This
stage lists, reads and reconstructs; the rule still only decides.

THIS RUN'S OWN RESULTS WIN. `approvals_from_research` dedupes on `(component, series)` and
keeps the FIRST entry it sees, so the merge concatenates the Map output first and the
reconstructed stored entries after it. Inverting that order would show a person a stale
answer while a fresher one sat unoffered beside it.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from ..notify import build_approval_email, send_approval_email
from ..notify.email import approver_address
from ..notify.presign import presign_s3_get
from ..research.candidate import CandidateFact
from ..research.report import ApprovalReport, chart_summary
from ..storage import build_fact_store, build_uri, read_document, write_text
from ..storage.candidates import list_pending, newest_per_series
from ..storage.decisions import build_decision_form
from ..storage.tokens import Grant, PausedRun, build_token_store, new_nonce
from ..view import ApprovalControls, approvals_from_research
from ..view.render.approval import to_approval_html
from .settings import from_event

# The IAM-signed approval endpoint, injected by Terraform. Rendered into the S3 page as a
# `curl` command -- the break-glass path for someone who would rather not use the email.
# THIS STAGE IS NOT GRANTED PERMISSION TO CALL IT and must not be.
APPROVE_ENDPOINT = os.environ.get("APPROVE_ENDPOINT", "").strip()


# The payload field that selects the non-waiting mode. A PAYLOAD FIELD AND NOT AN ENVIRONMENT
# VARIABLE, because it varies per invocation and not per deployment -- the same deployed function
# is surveyed once and then, on the same run, entered as the wait. `stages/settings.py` states
# that split as the rule for this package: the environment says what the deployment is, the
# payload says what this call is about.
SURVEY_ONLY = "survey_only"


def approval_key(cluster: str, run_id: str) -> str:
    """Where the page for one paused run lives.

    Keyed by run and not by digest, unlike every other document this pipeline writes. The
    reason is that this object is a LINK SOMEONE IS GIVEN: a person is told "open this and
    decide", and a digest-keyed name would change when the page was re-rendered on a stage
    retry, leaving them holding a URL to an object that no longer describes their run. The
    digests that matter are per row, inside the page.
    """
    return f"approvals/cluster={cluster}/run={run_id}/approval.html"


def _as_text(value: Any) -> str | None:
    """Enum or None -> plain string or None, because a research result carries JSON.

    The same conversion `judge_extraction.py` applies on the way out. Doing it here rather
    than letting `str()` run over a None keeps "not stated" distinguishable from the literal
    string `"None"`, which `view/build.py::_text` would otherwise faithfully display.
    """
    return None if value is None else str(value)


def entry_from_stored_candidate(bucket: str, key: str) -> dict[str, Any]:
    """One research-result-shaped entry, REBUILT from the candidate an earlier run stored.

    Rebuilt from the two documents themselves -- the candidate and the `.report.json` written
    beside it -- rather than assembled from the listing. The listing knows the component and
    the series and nothing else, and the two fields that make a control WORK are digests:
    `candidate_digest`, which the approval endpoint checks against the object it loads, and
    `report_digest`, which it checks against a report it re-derives. A row carrying anything
    else in those two fields is a button that fails at the last step, after a person has
    already decided.

    `report_digest` IS THE FIELD THAT DECIDES WHETHER THIS WORKS AT ALL. Rule 2 of
    `approvals_from_research` skips any entry without one, so getting it wrong does not
    render a broken control -- it renders NO control, and the candidate stays exactly as
    stranded as it was before this function existed. It is `ApprovalReport.digest()` over the
    sidecar document, which is the same expression `judge_extraction.py` reports on the wire
    and the same one `render_html.py` signs its commands with.

    RECOMPUTED FROM THE PARSED DOCUMENT, not hashed over the fetched bytes. `write_document`
    stores canonical JSON, so the two agree for anything this pipeline wrote -- and hashing
    the bytes would make the digest depend on how S3 handed them back rather than on what the
    document says.

    THE REPORT MUST BELONG TO THIS CANDIDATE, and that is checked rather than assumed. The
    two objects share a key stem, so a mismatch means a corrupted pair; offering it would put
    one candidate's prose beside another's digests, which is the failure `approve.py` guards
    against on the way in by comparing the component and series it was shown.

    WHAT IS DELIBERATELY LEFT OUT. `report_html_uri` -- Stage 6 stopped writing a per-
    candidate HTML view, so there is no such object for either a stored candidate or a fresh
    one, and pointing a reader at a key that does not exist is worse than showing no link.
    Everything the stored documents genuinely do not state stays None, because
    `view/build.py::_text` shows "not stated" differently from a value and inventing one
    would tell an approver something no evidence supports.
    """
    candidate_uri = build_uri(bucket, key)
    candidate = read_document(candidate_uri, CandidateFact)
    if not candidate.pending:
        # THE PREFIX IS NOT THE AUTHORITY ON THE DECISION -- the object is. A candidate is
        # listed under `decision=PENDING_APPROVAL/` by the key it was written with, and an
        # approval writes a NEW object rather than moving that one, so the prefix alone
        # cannot be trusted to still describe the receipt inside.
        raise ValueError(
            f"the candidate at {candidate_uri} is {candidate.receipt.decision}, not "
            "PENDING_APPROVAL, so there is nothing here for a person to act on"
        )

    report = read_document(candidate_uri[:-5] + ".report.json", ApprovalReport)
    if report.candidate_digest != candidate.digest():
        raise ValueError(
            f"the report beside {candidate_uri} was built for candidate "
            f"{report.candidate_digest} but the candidate hashes to {candidate.digest()}. "
            "Offering that pair would show one candidate's evidence beside another's "
            "digests, so nothing is offered for it."
        )

    receipt = candidate.receipt
    return {
        # The flag rule 1 gates on, set from the receipt read above rather than from the
        # prefix the object was listed under.
        "pending_approval": True,
        "component_id": candidate.component_id,
        "version_series": candidate.version_series,
        "candidate_uri": candidate_uri,
        "candidate_digest": candidate.digest(),
        "report_digest": report.digest(),
        # -- The receipt: the confidence and its arithmetic, which is what a reader weighs.
        "confidence": str(receipt.confidence),
        "change_direction": str(receipt.change_direction),
        "summary": receipt.summary,
        "values_verified": receipt.values_verified,
        "values_total": receipt.values_total,
        "machine_confidence": _as_text(receipt.machine_confidence),
        "proposed_confidence": _as_text(receipt.proposed_confidence),
        "evidence_tier": receipt.evidence_tier,
        # COMPUTED BY THE REPORT DOCUMENT, exactly as Stage 6 computes it, because
        # `headline()` is a pure method over fields we have just read. Deriving the sentence
        # here instead would be a second implementation of it, free to disagree.
        "approval_headline": report.headline(),
        # -- The assessment, as the approver reads it. Held by the report rather than the
        # candidate: Stage 6 keeps the fact contract free of any trace of how it was
        # obtained, so the recommendation lives on the report beside the fact.
        "installed_version": report.installed_version,
        "installed_version_status": report.installed_version_status,
        "recommended_version": report.recommended_version,
        "recommended_series": report.recommended_series,
        "action": report.action,
        "upgrade_ordering": report.upgrade_ordering,
        "covers_current_kubernetes": report.covers_current_kubernetes,
        "covers_target_kubernetes": report.covers_target_kubernetes,
        "confidence_rationale": report.confidence_rationale,
        "cited_sources": [row.model_dump(mode="json") for row in report.cited_sources],
        "caveats": list(report.caveats),
        "not_found": list(report.not_found),
        # The sibling key Stage 6 writes the model's own answer to, unconditionally and
        # beside the two objects just read. Named rather than fetched: it is a link a reader
        # follows, not a value this page displays, so paying for a third GET per row to prove
        # it exists would buy nothing a 404 in the console does not already say.
        "extraction_uri": candidate_uri[:-5] + ".extraction.json",
        # THE CHART, AS THE CANDIDATE'S FACT CARRIES IT -- the same key Stage 6 writes, so a
        # candidate re-offered from an earlier run shows "chart from X" exactly as a fresh one
        # does. It was missing here, so a reused proposal never showed its chart repository.
        **chart_summary(candidate.knowledge),
    }


def entries_from_earlier_runs(
    bucket: str, *, now: datetime | None = None, client: Any = None
) -> tuple[tuple[dict[str, Any], ...], int]:
    """Every actionable pending candidate as a research result, and how many were unreadable.

    Newest per `(component, series)`: two runs answering one question wrote two candidates,
    and offering both asks a person to decide the same thing twice.
    `approvals_from_research` would drop the second anyway, keeping whichever it saw first --
    so choosing the newest here makes the choice deliberate rather than a side effect of
    listing order.

    A REJECTED CANDIDATE IS NEVER OFFERED, and there is no filter for it here on purpose.
    `list_pending` reads `facts-candidates/decision=PENDING_APPROVAL/` and a refusal writes a
    NEW object under `decision=REJECTED/`, so a rejected series is excluded by the prefix that
    was never asked for. A filter would imply the listing could return one, and the next
    reader would go looking for the case it handles.

    A CANDIDATE WHOSE COMPONENT ALREADY HAS A STORED FACT IS NOT OFFERED, and this is not a
    nicety -- without it the page renders controls that CANNOT WORK. The approval endpoint does
    not hash the stored report; `approve.py::_rebuild_report` re-derives one with
    `current=build_fact_store().find(component_id)` and compares digests. So once a fact exists
    the rebuild differs from the sidecar this entry's `report_digest` was taken from, and the
    endpoint refuses an approval the page had already offered. Measured against the live bucket
    with the store populated:

        cert-manager    fact in store: yes  ->  endpoint would accept: NO
        keda            fact in store: yes  ->  endpoint would accept: NO
        metrics-server  fact in store: no   ->  endpoint would accept: yes

    FILTERING IS THE REPAIR RATHER THAN MAKING THE DIGESTS AGREE, because a stored fact means
    the question is ANSWERED. The gap is closed, `Resolve` no longer withholds the component,
    and there is nothing left to decide -- the candidate is a leftover of the run that produced
    it, not a pending question. Offering it would ask somebody to re-approve what they already
    approved, with a button that fails.

    CHECKED AGAINST THE STORE, NOT AGAINST `decision=` IN THE KEY, because those answer
    different questions. The prefix records what the JUDGE decided about this candidate; the
    store records whether a fact now exists. An approval writes a NEW object and never moves
    this one, so a candidate written before its approval sits under `PENDING_APPROVAL/` forever.

    AN UNREADABLE STORE WITHHOLDS NOTHING, matching `list_pending`'s direction: the cost is a
    control that may be refused at the endpoint, and the opposite default silently suppresses
    the only surface that can clear a stranded component -- the exact bug this exists to fix.

    FAILURE IS DEGRADATION, NEVER AN EXCEPTION, and the reason is structural rather than
    stylistic: this handler runs behind `.waitForTaskToken`, so a raise leaves the execution
    paused holding a token nobody can redeem and no page to redeem it from -- strictly worse
    than the stranding this whole function exists to fix. A candidate that cannot be read or
    reconstructed is logged, counted and skipped, and the rest of the page is still offered.
    """
    offered: list[dict[str, Any]] = []
    unreadable = 0
    for row in newest_per_series(list_pending(bucket, now=now, client=client)):
        if _fact_is_already_stored(row.component):
            print(
                f"pending candidate {row.key} is not offered: a fact for "
                f"{row.component!r} is already stored, so the question it asks is answered"
            )
            continue
        try:
            offered.append(entry_from_stored_candidate(bucket, row.key))
        except Exception as exc:
            unreadable += 1
            print(
                f"pending candidate {row.key} could not be offered and was skipped: "
                f"{type(exc).__name__}: {exc}"
            )
    return tuple(offered), unreadable


def _fact_is_already_stored(component: str) -> bool:
    """Whether the store already holds a fact for this component.

    ONE `find` PER OFFERED COMPONENT, which is the store's whole read API and at most a handful
    of calls -- the same lookup `approve.py::_rebuild_report` will make, so this asks exactly
    the question that decides whether the control could succeed.

    NOT NARROWED TO THE SERIES. `FactStore.find` takes a component id and returns one entry
    whose rules span every series it knows, so a candidate for `0.6.x` and a stored fact reached
    via `0.9.x` are the same question answered. Narrowing would offer the leftover again.

    FALSE ON ANY FAILURE, so an unreachable store withholds nothing. See the caller.
    """
    try:
        return build_fact_store().find(component) is not None
    except Exception as exc:
        print(f"the fact store could not be read for {component!r}, so nothing is withheld: {exc}")
        return False


def offered_approvals(
    research_results: object,
    stored_entries: tuple[dict[str, Any], ...],
    *,
    endpoint: str | None,
    region: str,
) -> tuple[ApprovalControls, int, int]:
    """The controls to render, and how many of them came from this run and from earlier ones.

    THE ORDER OF THE CONCATENATION IS THE WHOLE RULE. `approvals_from_research` keeps the
    FIRST entry it sees for a `(component, series)` pair, so this run's Map output goes first
    and the reconstructed stored entries after it. A collision means two runs answered one
    question; the fresher answer read the more recent releases, and showing the older one
    while holding the newer would be the worst of both.

    THE TWO COUNTS ARE DERIVED FROM A SECOND EVALUATION OF THE SAME PURE RULE, not from a set
    of digests. A row cannot say which entry built it, and the digests do not settle it
    either: this run's own candidate is in the bucket by the time this stage runs, so the
    fresh entry and the stored one for it carry the SAME `candidate_digest`. Asking the rule
    what this run alone offers answers it exactly, because adding entries after the first pass
    can never remove a key the first pass already claimed.

    They exist because an operator looking at a page with one control and an empty
    `research_results` had no way to tell whether the run had researched something or was
    surfacing an answer somebody left waiting days ago -- which is the state metrics-server
    was in, unreported, for every run it blocked.
    """
    from_this_run = list(research_results) if isinstance(research_results, list) else []
    controls = approvals_from_research(
        [*from_this_run, *stored_entries], endpoint=endpoint, region=region
    )
    this_run_only = approvals_from_research(
        from_this_run, endpoint=endpoint, region=region
    )
    claimed = {(p.component, p.version_series) for p in this_run_only.pending}
    from_this_run_count = sum(
        1 for p in controls.pending if (p.component, p.version_series) in claimed
    )
    return controls, from_this_run_count, len(controls.pending) - from_this_run_count


def _offered(
    bucket: str, research_results: object
) -> tuple[ApprovalControls, int, int, int]:
    """The controls a page would carry, both provenance counts, and the unreadable count.

    THE SURVEY AND THE WAIT BOTH CALL THIS, AND THAT IS THE ENTIRE REASON IT IS A FUNCTION.
    The pre-check decides whether the pause is entered; the pause decides what a person is
    shown. If those two computed the offered set by two expressions, a run could skip a pause
    that had something to ask, or pause on a page the pre-check had already found empty -- and
    the second of those is the defect the pre-check exists to remove, reintroduced one state
    later. One function answering both questions is the same argument `entries_from_earlier_runs`
    makes about `resolve` and this stage: the suppressor and the offerer must not be able to
    disagree, so they read through one rule.

    THEY CAN STILL DISAGREE ACROSS TIME, AND THAT IS BOUNDED RATHER THAN FIXED. The two calls
    are seconds to minutes apart over the same bucket and the same store, so a concurrent
    execution writing a candidate, or a person approving one, can move the answer between them.
    Both directions are survivable and both are strictly better than the unconditional pause:

        survey > 0, wait sees 0   pause on an empty page -- today's defect, now reachable only
                                  inside that window instead of on every withheld run
        survey = 0, wait skipped  the newcomer stays PENDING_APPROVAL and the NEXT run's survey
                                  offers it, because the survey runs on every round

    Neither strands anything, because nothing here deletes a candidate and the survey is not a
    decision about the candidate -- only about whether THIS execution stops.
    """
    stored_entries, unreadable = entries_from_earlier_runs(bucket)
    controls, from_this_run, from_earlier_runs = offered_approvals(
        research_results,
        stored_entries,
        endpoint=APPROVE_ENDPOINT,
        region=os.environ.get("AWS_REGION", "us-east-1"),
    )
    return controls, from_this_run, from_earlier_runs, unreadable


def survey(event: dict[str, Any]) -> dict[str, Any]:
    """Count what a page WOULD offer. Mint nothing, write nothing, store nothing, mail nothing.

    WHY THIS EXISTS. `Approval` is `.waitForTaskToken`, and every edge into it entered the wait
    unconditionally: the token was stored whether or not anything was pending and the mail went
    whether or not the page had a row. Observed on a test run --
    `awaiting_approval` 0, `offered_from_this_run` 0,
    `offered_from_earlier_runs` 0, `approval_email_sent` true -- which paged a person, paused the
    execution on a task token, and held it there until somebody pressed Done on a page with no
    rows. All three pending candidates in the bucket belonged to components that already have a
    stored fact, so `_fact_is_already_stored` withheld every one of them, correctly.

    THE DECISION HAS TO BE TAKEN BEFORE THE WAIT IS ENTERED, WHICH IS WHY THIS IS A SEPARATE
    INVOCATION AND NOT A BRANCH INSIDE THE PAUSE. This stage holds no `states:SendTaskSuccess`
    and must not be given it (`infra/terraform/modules/research/approval_stage.tf`): a stage able to
    resume
    its own pause is not a pause. So a handler that discovered the empty set after the token was
    minted could do nothing about it -- the execution is already stopped by then, because Step
    Functions hands out the token on entering the state and not on the handler returning.

    WHAT IT DELIBERATELY DOES NOT DO, each one a way a "harmless" pre-check could do damage:

      mint no grant       a nonce minted here and not rendered anywhere is a live capability
                          against the token store with nobody holding it
      write no page       the page is keyed by run, so a survey writing one would leave an
                          approver's URL pointing at a page built before their decisions existed
      store no token      there is no token to store; a row paused on an empty string would be
                          a run the resume path believes it can complete
      send no mail        the whole point is to stop paging a person about nothing

    IT IS NOT A SECOND SOURCE OF TRUTH. The offered set comes from `_offered`, which is the
    expression the wait uses -- see its docstring for what the two can and cannot disagree
    about.

    `pending_candidates_skipped` IS REPORTED AND DOES NOT FORCE THE PAUSE, and the code is what
    settles that rather than a preference. A candidate that could not be read has no
    `report_digest`, so `approvals_from_research` rule 2 drops it and it renders NO control --
    pausing for it would produce exactly the zero-row page this change exists to stop, and put
    a person in front of a problem no button on that page can act on. So it is counted here, it
    is named per key in the log, and it is visible in the execution history; what it needs is an
    operator reading a count, not an execution held open.

    A RETURN VALUE FROM THIS ONE IS NOT DISCARDED, unlike the wait's. The pre-check is a plain
    `lambda:invoke` whose result lands under its own `ResultPath`, and the Choice after it reads
    `awaiting_approval` from there -- so this dict IS a contract, and `awaiting_approval` is the
    field the graph depends on.
    """
    settings = from_event(event)
    controls, from_this_run, from_earlier_runs, unreadable = _offered(
        settings.bucket, event.get("research_results")
    )
    return {
        "stage": "approval_survey",
        "run_id": _run_id(event),
        # THE FIELD THE CHOICE BRANCHES ON. Named identically to the wait's own count on
        # purpose: an operator comparing the two states in one execution history is comparing
        # the same quantity, and a survey that said `offered` while the pause said
        # `awaiting_approval` would read as two different measurements of one thing.
        "awaiting_approval": len(controls.pending),
        "offered_from_this_run": from_this_run,
        "offered_from_earlier_runs": from_earlier_runs,
        "pending_candidates_skipped": unreadable,
        "surveyed_at": datetime.now(UTC).isoformat(),
        # Stated so the history distinguishes this state from the one that DOES stop. The wait
        # returns `paused: True` from the same module, and a reader scanning for it must not
        # find an unqualified truthy value on the state that deliberately does not stop.
        "paused": False,
    }


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Survey when asked to, otherwise pause: one function, because the offered set is one rule.

    `SURVEY_ONLY` on the payload selects the non-waiting mode. Dispatching here rather than
    from a second Lambda handler is the same decision the release gate made about its own
    stages: the pre-check must compute the offered set with the code that renders it, and a
    second function is a second deployment artifact free to drift from this one. It also needs
    no new Lambda resource, no new IAM role and no new Terraform variable -- the pre-check
    invokes this same function ARN with the flag set, and every permission it uses (list and
    read the pending prefix, scan the fact table) this role already holds.

    THE FLAG IS READ STRICTLY, `is True` RATHER THAN TRUTHY, AND THE DIRECTION MATTERS. A
    mangled payload must fall through to the PAUSE, not to the survey: pausing a run nobody
    needed to answer wastes a person's attention, and surveying a run that was meant to pause
    walks straight past the only surface that can clear a stranded candidate. Step Functions
    puts a real JSON `true` there from the state's `Parameters`, so nothing legitimate is lost
    by refusing the string `"true"`.
    """
    if event.get(SURVEY_ONLY) is True:
        return survey(event)
    return _pause_and_ask(event)


def _pause_and_ask(event: dict[str, Any]) -> dict[str, Any]:
    """Render the pending proposals, store the task token, and let the execution wait.

    Required in `event`: `task_token` (Step Functions puts it there via the state's
    `Parameters`), plus the run payload -- `bucket`, `cluster_name`,
    `target_kubernetes_version`, and `research_results` from the Map.

    `research_results` MAY BE EMPTY AND THE PAGE STILL HAVE ROWS. `Research needed?` routes a
    run whose every gap was withheld BECAUSE an answer was already waiting through the
    pre-check and into this state -- so the offered set is read from the bucket as well as from
    the payload, and a grant is minted for each of those candidates exactly as for a fresh
    one. That is what makes the buttons on a stranded candidate work rather than merely
    display.

    A RETURN VALUE FROM THIS HANDLER IS DISCARDED, because the state is
    `.waitForTaskToken`: the execution's next input is whatever the eventual
    `SendTaskSuccess` carries, not what this returns. The dict below is for the log and
    for a human invoking the function directly -- it is not the stage's output contract.
    """
    settings = from_event(event)
    task_token = str(event.get("task_token") or "").strip()
    run_id = _run_id(event)

    # LISTED AND READ BEFORE THE PAGE IS BUILT, because the rule that builds it reads nothing.
    # The bucket holds every candidate still awaiting a decision, including the ones that
    # suppressed this run's research and would otherwise be offered by nobody.
    #
    # THROUGH `_offered`, WHICH IS ALSO WHAT THE PRE-CHECK CALLS. The pre-check decides whether
    # this state is entered at all, so the two must compute one offered set -- see that
    # function's docstring for what they can and cannot disagree about.
    controls, from_this_run, from_earlier_runs, unreadable = _offered(
        settings.bucket, event.get("research_results")
    )

    # MINTED FIRST, because the page embeds forms built from these nonces and the token row
    # must be written before any of them can be redeemed. Order: mint, build forms, render,
    # STORE, then send. A form that reaches a person before its grant is persisted is a dead
    # button, and the failure it produces reads as "already used" -- which sends someone
    # looking for a decision they never made.
    approver = approver_address()
    grants: dict[str, Grant] = {}
    nonces: dict[str, dict[str, str]] = {}
    for candidate in controls.pending:
        per_candidate: dict[str, str] = {}
        for action in ("approve", "reject"):
            nonce = new_nonce()
            grants[nonce] = Grant(
                action=action,
                approver=approver,
                candidate_uri=candidate.candidate_uri,
                candidate_digest=candidate.candidate_digest,
                report_digest=candidate.report_digest,
                component_id=candidate.component,
                version_series=candidate.version_series,
            )
            per_candidate[action] = nonce
        nonces[candidate.candidate_digest] = per_candidate
    done_nonce = new_nonce()
    grants[done_nonce] = Grant(action="done", approver=approver)

    rendered_at = datetime.now(UTC).isoformat()
    # THE FORMS ARE BUILT BEFORE THE PAGE, because the page embeds them. Each is a presigned
    # S3 POST scoped to one key carrying one nonce, so a click writes one small object and
    # `decision_watcher` turns that arrival into an approval. The browser holds no credential
    # and the bucket is private -- which is what makes this a real button after two designs
    # that could not be.
    forms = {
        digest: {
            action: {"url": form.url, "fields": form.fields}
            for action, nonce in per_action.items()
            if (form := build_decision_form(settings.bucket, run_id, nonce)) is not None
        }
        for digest, per_action in nonces.items()
    }
    forms = {digest: acts for digest, acts in forms.items() if acts}
    done_built = build_decision_form(settings.bucket, run_id, done_nonce)
    done_form = (
        {"url": done_built.url, "fields": done_built.fields} if done_built else None
    )

    page = to_approval_html(
        controls,
        run_id=run_id,
        cluster=settings.cluster_name,
        target_kubernetes_version=settings.target_kubernetes_version,
        rendered_at=rendered_at,
        forms=forms,
        done_form=done_form,
    )
    page_uri = write_text(
        build_uri(settings.bucket, approval_key(settings.cluster_name, run_id)),
        page,
        "text/html; charset=utf-8",
    )

    # THE TOKEN IS STORED EVEN WHEN NOTHING IS PENDING, and that is still right even though
    # this state is no longer entered on an empty set. The execution is paused the moment the
    # state is entered -- Step Functions hands out the token then, not when this returns -- so
    # a run that gets here with nothing to offer still needs something able to resume it, and
    # resuming it here is impossible by design: this function holds no SendTaskSuccess
    # permission. That reaches a person rather than deadlocking, which is why the write stays.
    #
    # WHAT CHANGED IS HOW OFTEN THAT HAPPENS, NOT WHETHER IT IS HANDLED. `Anything to approve?`
    # now only routes here on a counted non-zero offered set, or when the pre-check could not
    # produce a count at all -- so an empty page here means the bucket moved in the seconds
    # between the two invocations (see `_offered`), not that every withheld run pages somebody.
    store = build_token_store()
    store.pause(
        PausedRun(
            run_id=run_id,
            task_token=task_token,
            report_uri=page_uri,
            cluster=settings.cluster_name,
            queued=tuple(p.candidate_digest for p in controls.pending),
            store_digest_before=str(event.get("store_digest_before_research") or ""),
            grants=grants,
        )
    )

    # SENT UNCONDITIONALLY, not gated on the link endpoint any more. The mail carries no
    # per-row action link, so the link endpoint has nothing to do with whether it can be
    # built -- gating on it meant a deployment that had dropped that variable silently sent
    # nobody anything while the run paused waiting for a person who was never told.
    #
    # A FAILURE HERE MUST NOT FAIL THE RUN. The page is written and the token is stored by the
    # time this runs, so the approval is already actionable; losing the notification is a
    # degradation, not a reason to fail an execution that a person could still complete from
    # the console or the CLI. That is exactly what happened when this raised a TypeError: the
    # run FAILED holding five valid grants and a perfectly good page.
    message_id = ""
    try:
        message_id = send_approval_email(
            build_approval_email(
                controls.pending,
                run_id=run_id,
                cluster=settings.cluster_name,
                target_kubernetes_version=settings.target_kubernetes_version,
                page_url=presign_s3_get(page_uri),
            )
        )
    except Exception as exc:
        email_error = f"{type(exc).__name__}: {exc}"
        print(f"approval email could not be sent: {email_error}")
    else:
        email_error = ""

    return {
        "stage": "approval",
        "run_id": run_id,
        "approval_html_uri": page_uri,
        "awaiting_approval": len(controls.pending),
        # ADDITIVE, and they exist because the total above cannot answer the first question an
        # operator asks about a paused run: did this run find something, or is it showing me an
        # answer somebody left waiting? A page with one control beside an empty
        # `research_results` used to be indistinguishable from a page with one control because
        # research produced one -- and the first of those is the stranded-candidate case that
        # went unreported on metrics-server for every run it blocked.
        #
        # `offered_from_earlier_runs` is the number of controls that exist ONLY because this
        # stage now reads the bucket. Non-zero on a run whose research was withheld is the
        # normal, healthy shape of the fix working.
        "offered_from_this_run": from_this_run,
        "offered_from_earlier_runs": from_earlier_runs,
        # Stated so a candidate that could not be read is visible in the execution history
        # rather than only in a log line. Non-zero means a pending candidate is still
        # unreachable, which is the exact condition this stage exists to end, so it must not be
        # inferable only from a control that is missing.
        "pending_candidates_skipped": unreadable,
        # WHETHER the mail went, never WHAT WAS IN IT. A nonce in a stage result would be a
        # nonce in the execution history, which anything holding
        # `states:GetExecutionHistory` can read. That would hand any such reader a working
        # approval and defeat the entire reason
        # the links go by email.
        "approval_email_sent": bool(message_id),
        # Stated so a run that paused with nobody notified is visible in the execution
        # history rather than only in a log line.
        "approval_email_error": email_error,
        "approval_email_message_id": message_id,
        "candidates": [
            {
                "component_id": p.component,
                "version_series": p.version_series,
                "candidate_digest": p.candidate_digest,
            }
            for p in controls.pending
        ],
        "rendered_at": rendered_at,
        # Stated so a reader of the log knows the run really is stopped here, rather than
        # inferring it from the absence of a later stage.
        "paused": True,
    }


def _run_id(event: dict[str, Any]) -> str:
    """This execution's identifier, as the resume path will look it up.

    Step Functions supplies it through the state's `Parameters` from
    `$$.Execution.Name`, which is unique per execution and is what an operator sees in
    the console -- so the id in the page's URL, the id in the token store and the id in
    the console are one value.

    Falls back to the payload's own `run_id` and then to the snapshot instant, so the
    handler is invocable outside a state machine for a local render. It never invents a
    random id: a resume path keyed on something the caller cannot name again would be
    unusable.
    """
    for key in ("execution_name", "run_id"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    return str(event.get("snapshot_captured_at") or "local").strip() or "local"
