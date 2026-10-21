"""The pipeline, in execution order. One Lambda per stage; Step Functions sequences them.

Each stage is a pure function of documents. It reads what the stage before it wrote,
from S3, and writes its own document back to S3 -- so an arrow below is a real object
with a real digest, not a call.

The rows are in the order they run. The number is the STAGE's name in this project's
vocabulary, which the handler names below and `lambdas.tf` still use; it is NOT a
position, and it is NOT what the state is called in the console -- see the transition
table further down for those. `7 resolve` runs third.

    1  scan       cluster name, region, read-only role  ->  snapshot
    2  inventory  snapshot                              ->  inventory report
       sweep      the fact store, and each fact's own upstream source
                                                        ->  freshness marks + a document
    3  gaps       snapshot, inventory, fact store       ->  gap ledger
    7  resolve    the gap ledger, and nothing else      ->  a dispatch queue
    4  decide     ledger, fact store, target version    ->  readiness assessment
    5  plan       assessment, fact store, target        ->  upgrade plan
    6  research   one queued component                  ->  a fact PROPOSED for approval

RESOLVE READS THE LEDGER AND NOTHING ELSE, which is why it can run third. It takes
`event["ledger_uri"]` and `event["snapshot_captured_at"]` off the payload -- no
assessment, no plan, no digest of either -- so its inputs are complete the moment `gaps`
returns. This table used to give it "ledger, assessment", which was false in the way that
matters most: after the reorder there IS no assessment when resolve runs, because decide
has not run yet. `stages/resolve.py`'s own header says `in  a gap ledger`, and
`infra/terraform/modules/platform/workflow.tf` says it at the `Gaps` successor.

Read 7 before 4, because that is the order they run in and the reason is the thesis:
Stage 7 decides DETERMINISTICALLY which gaps are worth asking about, and only then is a
model invoked, once per queued component. Nothing reaches a model that deterministic
code did not choose to send.

The deployed state machine, by transition. State names are the ones in the console:

    Scan                  ->  Inventory
    Inventory             ->  Sweep
    Sweep                 ->  Gaps
    Gaps                  ->  Resolve
    Resolve               ->  Decide
    Decide                ->  Plan
    Plan                  ->  Research needed?
    Research needed?      ->  Research              if research_required
                          ->  Approval survey       if approval_pending
                          ->  Anything to upgrade?  otherwise
    Research              ->  Approval survey       count before pausing, never pause to count
    Approval survey       ->  Anything to approve?
                          ->  Approval              on any error; never skip an uncounted pause
    Anything to approve?  ->  Approval              if the survey counted anything to offer
                          ->  Knowledge changed?    if it counted zero
    Approval              ->  Knowledge changed?    waits for a person
    Knowledge changed?    ->  Gaps                  if the store digest moved  (the loop)
                          ->  Anything to upgrade?  if it is absent or unmoved
    Anything to upgrade?  ->  Brief plan            and nowhere else; see below
    Brief plan            ->  Any to research?
                          ->  Upgrade approval      on any error  (the brief is advisory)
                          ->  Readiness report      on error instead, if execution is disabled
    Any to research?      ->  Brief research        if brief_research_count > 0
                          ->  Brief assemble        otherwise, everything was cached
    Brief research        ->  Brief assemble        including on error, so partial work survives
    Brief assemble        ->  Upgrade approval      on success and on error alike
                          ->  Readiness report      instead, when upgrade_stage_enabled is false
    Readiness report      ->  Done                  same page, no control, mailed; nothing to decide
    Upgrade approval      ->  Anything approved?    waits for a person, per cluster
    Anything approved?    ->  Release approvers     if any step was approved
                          ->  Done                  if none were; never page a person
    Release approvers     ->  Release gate          the cluster's admins, and what moves
                          ->  Release rejected      if nobody may authorise, or a read failed
    Release gate          ->  Release waiting       started the SSM approval automation
                          ->  Release rejected      on any error starting it
    Release waiting       ->  Release polled        60s
    Release polled        ->  Release decided?
                          ->  Release rejected      on any error reading the status
    Release decided?      ->  Release approved      if the automation succeeded
                          ->  Release rejected      if it failed, expired or was cancelled
                          ->  Release waiting       while it is still waiting  (the poll loop)
    Release approved      ->  Release assembled
    Release assembled     ->  Release recorded      the moves, the principal, the instant
    Release recorded      ->  Upgrade lanes         when upgrade_stage_enabled
                          ->  Done                  otherwise, which is the stage's default
    Upgrade lanes         ->  Upgrade lane
                          ->  Upgrade report        on any error; nothing opened, still report
    Upgrade lane          ->  Upgrade report        a Map, MaxConcurrency 1, one per component
                          ->  Upgrade report        on any error, because the report IS the point
    Upgrade report        ->  Anything referred?
                          ->  Anything referred?    on error
    Anything referred?    ->  Done                  only when the report says nobody
                          ->  Upgrade referred      on anything else, unreadable counts included

THE BRIEF CANNOT FAIL A RUN, which is why three of its edges above lead to the approval on
error. Its handlers turn every expected failure into a recorded absence and return normally;
what they cannot report on is a failure to invoke them at all, so the catches carry that case
to the approval with the absence recorded rather than failing the execution. A brief cannot
gate an approval, and routing the catches here is where that is enforced rather than trusted.

`Approval survey` EXISTS BECAUSE THE FIRST PAUSE USED TO BE UNCONDITIONAL. `Approval` is
`.waitForTaskToken` and the execution stops the moment that state is ENTERED, not when its
handler returns -- so a handler that found nothing to offer could not undo the pause, and must
not be given the permission to (`stage_approval` holds no `SendTaskSuccess`, deliberately). On
one test run the stage reported `awaiting_approval` 0, mailed
somebody, and held the run until a person pressed Done on a page with no rows. The survey is the
SAME Lambda invoked without a task token and with `survey_only` set: it computes the offered set
through the same expression and returns the count, so `Anything to approve?` can take the
decision before any token exists. Only a counted ZERO skips the wait -- an unreadable count, or
a survey that errored, pauses -- because a wasted page is visible and self-correcting while a
skipped one strands a candidate nothing reports.

`Upgrade approval` IS THE SECOND STATE THAT STOPS, and it asks a different question from the
first. `Approval` asks whether a claim about a component is true -- a fact, true of every
cluster, so one answer serves the fleet. This asks whether we may change THIS cluster, so the
same component may be approved here and refused next door. Its decisions are keyed on the
cluster and never reach the fact store.

It took the report state's place rather than following it: `Present` rendered a read-only
report and was terminal, and this renders the same report WITH a control per step. Two
rendering states would have produced two pages of one run.

THE RELEASE GATE IS THE THIRD STATE THAT STOPS, AND THE FIRST ONE WHERE AWS AUTHENTICATED THE
ANSWER. Both waits above are answered by clicking a single-use presigned form, which proves
that whoever held the capability opened the mailbox it was sent to -- not which human, and
certainly not which AWS principal. `StepDecision.approver_authority` records `EMAIL_LINK_TOKEN`
and says exactly that. `Release gate` closes the gap: it starts an SSM Automation runbook whose
`aws:approve` step refuses a signal from any principal outside a committed `Approvers` list, so
reaching `Done` means a named IAM principal stood behind the anonymous page clicks. INTENT,
THEN AUTHORISATION -- and neither is an execution grant.

It adds NO LAMBDA. The runbook, two IAM roles and the approver list are declarations in
`infra/terraform/modules/platform/release_gate.tf`; the five states around it start the automation
and watch
it. Read that file's header for why the task token is never handed to SSM: a real token is 664
characters, an SSM document parameter caps at 512, so the obvious `.waitForTaskToken` shape
would be refused on every run -- and passing no token also keeps a capability out of CloudTrail
and leaves the automation's role holding `ses:SendEmail` and nothing else.

`Anything approved?` DEFAULTS TO `Done`, WHICH IS THE OPPOSITE CHOICE FROM `Anything to
upgrade?` ABOVE, and the asymmetry is the point rather than an inconsistency. That Choice
proceeds on an unknown count because proceeding costs one cheap advisory invocation. This one
wakes a person up to authorise a cluster change, and `Done` authorises nothing -- so an empty
approved list and an unreadable payload both route to the outcome that grants no authority.

`Done` IS NO LONGER THE ONLY TERMINAL STATE, AND THIS PARAGRAPH USED TO SAY IT WAS. It is
still the terminal Pass the definition already emitted whenever the report was absent, now
emitted unconditionally and reached from `Release approved` or from an empty approved list.
`Release rejected` is the second terminal state and it is a `Fail`: a rejection, an expiry, or
an error starting the automation FAILS the execution. A run that quietly reached `Done` without
an authorisation would be indistinguishable from one that was authorised, and every per-step
decision is already durable in S3 -- so failing loses the release and never the answers.

`Anything to upgrade?` has ONE outgoing edge because all three of its branches -- two
conditions on `$.plan_counts` and the Default -- name `Brief plan`. The conditions fire on
a known count; the Default is reached when the count is UNKNOWN, and one cheap invocation
that reads the plan document beats silently skipping a plan that did move something.

THE UPGRADE STAGE HANGS OFF `Release recorded`, NOT `Release approved`, AND THE MOVE IS AN
IMPROVEMENT RATHER THAN AN ACCOMMODATION. It used to follow `Release approved`, which is a Pass
that reshapes the automation's outputs. `Release recorded` is the state that makes the release
DURABLE -- so the lane planner now runs after the authorisation it acts on exists as an object,
not merely as a field on a payload. A run that could not record its authorisation therefore never
reaches a lane, which is the fail-closed direction and was previously only true by accident.

THE UPGRADE STAGE IS THE ONLY GATED THING IN THIS TABLE THAT CHANGES A CLUSTER, and the five
states from `Upgrade lanes` down are behind `upgrade_stage_enabled`, which DEFAULTS TO FALSE.
Every other gate in this file withholds an ARN because a stage is deployed separately; this one
withholds a stage because an operator watching the pipeline behave must be able to tell a stage's
absence from its silence. With it false, `Release recorded` still names `Done`.

ITS MUTATIONS ARE NOT LAMBDAS AND THERE IS NO MODEL ON THE PATH. `Upgrade lane` is a Map of one
component at a time, and inside each iteration `Lane open` creates an EKS access entry through an
AWS SDK integration, `Lane upgrade` runs `helm upgrade` or `eks update-addon` in CodeBuild through
`startBuild.sync`, and `Lane cleanup` deletes the access entry. `Lane cleanup` is reached by BOTH
edges of both states before it and is the only route to the iteration's single end, so no path
exists that leaves the just-in-time grant standing. The four iterator states are not states of
this machine and are deliberately absent from the table above, exactly as `Research one component`
and `Brief one component` / `Brief failed` are.

`Anything referred?` DEFAULTS TO THE FAIL, WHICH IS THE OPPOSITE OF `Anything approved?`, and the
asymmetry is again the point rather than an inconsistency. That Choice defaults to `Done` because
`Done` authorises nothing. By the time this one runs A CLUSTER HAS ALREADY BEEN CHANGED, so `Done`
is the reading that claims everything worked -- and it is reached only on a positive test that the
report counted nobody. The report is written and durable before the Choice evaluates, so failing
loses the green tick and never the answers.

THIRTY STATES, `StartAt` `Scan`, and NO ORDINALS IN ANY NAME. This sentence said ELEVEN,
and it had been wrong since the brief's five states landed -- which is the fourth drift in the
sequence the paragraph below lists, and the one the test could not catch, because the test
parses the TABLE's edges and never read this count. It is stated here as a number a reader can
check against the table above rather than as a claim nothing verifies. The states were once
`1 Scan` ... `7 Resolve`; the digits were dropped because they had stopped meaning
anything readable -- `7 Resolve` ran third, `Sweep` and `Present` never had a digit at
all, and `3 Gaps -> 7 Resolve -> 4 Decide` needed to be explained before it could be
read. A name in this table can be pasted into the console and found; a name with a digit
cannot, because no such state exists.
`tests/test_the_pipeline_table_matches_the_deployed_machine.py`
holds this table to a `describe-state-machine` read, because it has drifted three times
-- once per graph change -- and every drift was silent.

The loop re-enters at GAP DETECTION rather than patching a document, which is what keeps
the digest chain intact across it: nothing rewrites a stage document, the stages are re-run
and their digests move to reflect the new knowledge.

In practice the loop branch does not fire today, and that is correct rather than broken.
Stage 6 writes a candidate, never a fact; `approve_fact` is the only writer and a human
calls it. So the store cannot grow WITHIN one execution, and the loop closes ACROSS
executions -- a person approves, the next run picks the fact up. Both no-move branches
therefore go to the report. See `workflow.tf`, which measured the alternative: routing an
absent digest back to `Gaps` ran the maximum four rounds every time, nine model calls
where three were needed, each round re-deriving byte-identical documents.

WHY THERE ARE MORE THAN FIVE. The original brief was stages 1-5. This is a deliberate
superset: 6 research, 7 resolve, the presentation stage and the supporting handlers below
came afterwards. Stages 1-5 still stand alone, and that is the DEFAULT rather than a
teardown -- each of the three is a separate Terraform component, and
`infra/terraform/modules/platform`
takes each one's ARN as a variable that defaults to null. Apply platform by itself and
`Resolve`, `Research` and both Choice states are not emitted at all. The report state is
no longer among them: it is not gated any more, it is gone.

WHAT IS LEFT IS SIX STATES, NOT FIVE, AND THIS PARAGRAPH USED TO SAY FIVE. `Sweep` is not
gated: `sweep.tf` creates its Lambda, role and policy in this same module and sets
`after_inventory = "Sweep"` unconditionally, so there is no ARN to withhold and no way to
leave it out. A defaults-only apply therefore yields `Scan -> Inventory -> Sweep -> Gaps
-> Decide -> Plan`. The five document stages of the brief are all present and unchanged;
the freshness sweep rides along with them, and calling that "exactly the five-stage
pipeline" understated what the minimum deployment contains.

Every handler here is one line of real work. All the plumbing -- resolving parameters,
building object keys, reading and validating documents, choosing a fact store -- lives
behind `eks_addon_readiness.stages`. If you are looking for what this system does, it is
the list above. If you are looking for how, follow one of them down.

WHERE THE MODEL IS, AND IS NOT. Models run in exactly two places, both behind a proxy: the
research Harness (`stage_research_harness`, in `entrypoints/research_harness.py`) and the brief and
upgrade Harnesses. None of them writes a fact -- research proposes one. Stage 4 issues every
verdict, and it does so with rules over a digest-cited ledger, with no cluster access and no
model. `approve_fact` is the only writer of facts, the only role IAM permits to write one,
and only a signed human can reach it.
"""

from typing import Any

from eks_addon_readiness.stages import (
    approval_gate,
    approve,
    approve_link,
    decide,
    decision_watcher,
    fetch_source,
    freshness,
    gaps,
    inventory,
    judge_extraction,
    plan,
    release_prep,
    release_record,
    resolve,
    scan,
    upgrade_approval,
    upgrade_decision_watcher,
)

Event = dict[str, Any]
Result = dict[str, Any]


# ---------------------------------------------------------------------------
# THE PIPELINE. These eight run inside one Step Functions execution, in the order
# they appear here.
# ---------------------------------------------------------------------------


def stage_scan(event: Event, context: Any = None) -> Result:
    """Read the cluster. Writes a sanitized snapshot; returns its URI and digest."""
    return scan.handler(event, context)


def stage_inventory(event: Event, context: Any = None) -> Result:
    """Identify every component and resolve its true version from the snapshot."""
    return inventory.handler(event, context)


def stage_sweep(event: Event, context: Any = None) -> Result:
    """Mark facts whose upstream source has moved, or revoke a named one.

    A STAGE, and the `Sweep` state runs it between `Inventory` and `Gaps` -- so it happens
    before anything opens the fact store, which is what makes a run fresh at its own moment
    without any stage doing I/O of its own. Staleness means the upstream source moved, not
    that a clock advanced.

    It writes marks into a SEPARATE table rather than touching the fact store, so `Gaps`
    reports a stale fact as researchable while staying a pure read -- a network call inside
    `Gaps` would make the ledger depend on the instant it ran and destroy replay.

    Sweeps everything by default; pass `components` for the pre-flight form that covers just
    an imminent run. No model, and its role grants no Bedrock.
    """
    return freshness.handler(event, context)


def stage_gaps(event: Event, context: Any = None) -> Result:
    """Ledger what is known and what is missing, typing every unknown."""
    return gaps.handler(event, context)


def stage_resolve(event: Event, context: Any = None) -> Result:
    """Decide which knowledge gaps are worth researching. No model participates.

    Runs BEFORE Stage 6 and is what makes the model edge deterministic: it publishes a
    dispatch queue as a document, so what was asked and why is inspectable evidence
    rather than a log line.
    """
    return resolve.handler(event, context)


def stage_decide(event: Event, context: Any = None) -> Result:
    """Issue a verdict per component per version hop, citing its evidence."""
    return decide.handler(event, context)


def stage_plan(event: Event, context: Any = None) -> Result:
    """Order the moves into an interleaved sequence, each marked reversible or not."""
    return plan.handler(event, context)


def stage_approval(event: Event, context: Any = None) -> Result:
    """Pause the run on a page of proposals and wait -- or, in survey mode, only count.

    TWO STATES SHARE THIS ENTRY POINT, and this docstring used to name only one of them.
    `Approval survey` invokes it with `survey_only` on the payload and does NOT stop: it
    computes the offered set and returns the counts so `Anything to approve?` can decide
    whether entering the wait asks anybody anything. `Approval` invokes it with a task
    token and stops. One function because the count and the page must come from one
    expression; see `stages/approval_gate.py::survey`.

    IN THE WAITING MODE its state is `.waitForTaskToken`, so this handler renders the page,
    writes down where to resume, and returns -- the execution then sits still until the
    approval endpoint calls `SendTaskSuccess`, or until the state's own timeout fails the
    run. It is not "the only stage that stops", which this docstring also used to claim:
    `Upgrade approval` is the second and `Release gate` the third, as the module docstring
    above says.

    It cannot resume itself in either mode: no `states:SendTaskSuccess` on its role,
    deliberately. The human's signature on the approval endpoint is what continues the
    pipeline.
    """
    return approval_gate.handler(event, context)


def stage_upgrade_approval(event: Event, context: Any = None) -> Result:
    """Pause the run on the report, with the brief and a control per step, and wait.

    THE SECOND STAGE THAT STOPS, and it asks a different question from the first.
    `stage_approval` asks whether a claim about a component is true -- a fact, true of every
    cluster, so one answer serves the fleet. This asks whether we may change THIS cluster.
    The same component may be approved here and refused next door, so the decision it records
    is keyed on the cluster and never reaches the fact store.

    IT TOOK THE REPORT STATE'S PLACE rather than following it. `stage_present` rendered a
    read-only report and was terminal; this renders the same report WITH controls. Two
    rendering states would have produced two pages of one run and left a person deciding from
    whichever they opened.

    It cannot resume itself either: no `states:SendTaskSuccess` on its role. The decision a
    person makes travels down the click path, and `upgrade_decision_watcher` is what resumes
    the execution.

    THREE JOBS BEHIND ONE HANDLER, DISPATCHED ON AN EXPLICIT KEY, and all three belong to the
    same question asked at different moments:

        (no key)          render the report, mint the capabilities, mail the link, wait.
        release_prep      who may authorise this cluster's release, and what exactly moves.
        release_record    the authorisation, as the document that outlives the execution.

    They are served by THIS function because the deployment adds no Lambda for any of them, and
    this is the one function whose role may already read `plans/` -- which the last two both need,
    since the plan is the only thing that knows what step 4 moves. None of them can write a
    release: `releases/` is written by the state machine's own role.

    THE DISCRIMINATOR IS A KEY THE OTHER PAYLOADS CANNOT HAVE, not a heuristic about what is
    present. `upgrade_approval.handler` is handed `{"task_token", "run", ...}`, and guessing
    from the absence of a token would mean a malformed approval payload silently resolving an
    approver list instead of failing.
    """
    if "release_prep" in event:
        return release_prep.handler(event, context)
    if "release_record" in event:
        return release_record.handler(event, context)
    if "readiness_report" in event:
        # The deployment executes no upgrade: the same page, no controls, a mail, no pause.
        return upgrade_approval.readiness_report(event, context)
    return upgrade_approval.handler(event, context)


def upgrade_decision(event: Event, context: Any = None) -> Result:
    """An upgrade click landed in S3. Record the decision; on Done, resume the run.

    NOT A STAGE. Invoked by an S3 event notification on `upgrade-clicks/`, so it runs when a
    person acts rather than when the pipeline reaches a step.

    SEPARATE FROM `approval_decision` and not a branch of it. That handler redeems a fact
    grant and invokes `approve`, the only writer of facts; this redeems a step grant and
    writes a decision about one cluster. One handler holding both would have to ask which
    kind of thing it had, and the failure mode of getting that wrong is a fact written from
    an upgrade click.

    THIS is where `states:SendTaskSuccess` lives for the upgrade wait, for the same reason
    `approve` holds it for the fact wait: the human's decision is what continues the pipeline.
    """
    return upgrade_decision_watcher.handler(event, context)


# ---------------------------------------------------------------------------
# NOT IN THE PIPELINE. Each of these is invoked on its own -- by a human, or by the
# research Harness through its Gateway. None is a stage, and no Step Functions execution
# calls any of them.
#
# The freshness sweep used to sit here, invoked on a schedule. It is a stage now (`Sweep`,
# between `Inventory` and `Gaps`) and has moved above this line with the others; the
# schedule was removed, because staleness means the upstream source moved rather than that
# six hours passed.
# ---------------------------------------------------------------------------


def approve_fact(event: Event, context: Any = None) -> Result:
    """Approve one proposed fact, or list what is pending. The ONLY writer of facts.

    Invoked by a HUMAN. Behind a Lambda function URL with AWS_IAM auth: the approver is
    the principal AWS resolved from the request signature, never a name in the body, and
    the function's resource policy allows exactly one principal, the `-approver` role --
    so the agent can show a pending approval and cannot grant one.

    That resource policy holds one Allow and no Deny. `stages/approve.py`'s docstring sets
    out the three controls that do the refusing, and the one gap left.
    """
    return approve.handler(event, context)


def approve_from_link(event: Event, context: Any = None) -> Result:
    """Redeem a one-time approval link from the email, then do what it authorises.

    Invoked by a HUMAN CLICKING A LINK. Behind a function URL with NO auth, which is the
    AWS-published shape for human approval in Step Functions -- authorisation is the
    single-use nonce, which is scoped to one action on one candidate, atomically consumed,
    and delivered only by email so it never lands anywhere a page reader can see it.

    NOT a writer of facts. It invokes `approve_fact`, which stays the only one.
    """
    return approve_link.handler(event, context)


def approve_from_page(event: Event, context: Any = None) -> Result:
    """Act on a decision a person clicked on the approval page. Writes no fact itself.

    Invoked by an S3 EVENT NOTIFICATION, not by a request. The browser submitted a presigned
    POST that wrote one small object; its arrival is what reaches code holding credentials.
    No public endpoint exists in this path at all -- the bucket is private and the form was
    signed by the approval stage.

    This is the third attempt at a clickable approval and the one that works. A `NONE` auth
    function URL was world-accessible and refused by an org guardrail; a SigV4-presigned link
    on an `AWS_IAM` URL is capped at five minutes because Lambda function URLs ignore
    `X-Amz-Expires`. S3 presigned POSTs have neither problem.

    It redeems the single-use grant named in the object's key and invokes `approve_fact`,
    which stays the only writer of facts.
    """
    return decision_watcher.handler(event, context)


def stage_judge(event: Event, context: Any = None) -> Result:
    """Judge one extraction against the bytes recorded for its session. No model runs here.

    Invoked by the research Harness. The deterministic half of Stage 6, deployed apart
    from the Harness that produced the extraction -- the thesis as two artifacts: the
    Harness reads and cannot judge, this judges and has no model client in its package.
    Every quote is checked against bodies the fetch tool recorded before the model saw
    them.

    Named `stage_judge` because it belongs to Stage 6's boundary, not because it is a
    stage of the pipeline.
    """
    return judge_extraction.handler(event, context)


def tool_fetch_source(event: Event, context: Any = None) -> Result:
    """Fetch one pinned source page and record its bytes before the model reads them.

    Invoked by the research Harness through its Gateway -- its only tool. Fetching stays
    a tool this project owns rather than a managed capability, because the
    quote-versus-source check depends on the fetched body reaching the judging layer, and
    a body the platform fetched on the agent's behalf would not.
    """
    return fetch_source.handler(event, context)
