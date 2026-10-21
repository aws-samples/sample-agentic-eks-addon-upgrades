"""Deterministic S3 object keys, one per stage document.

Every key carries the digests of the inputs that determined the document, so the
relationship between a Stage 1 snapshot and the Stage 5 plan derived from it is
visible in an object listing without opening a single file.

That also makes writes idempotent: re-running the same stage over the same inputs
overwrites the same key rather than accumulating near-duplicates, because the
inputs fully determine the output.

ONE KEY DEPARTS FROM THAT, and it says so where it lives. `sweep_key` carries the
sweep document's OWN digest, because a sweep is the one thing here that is not a pure
function of its inputs -- it reads the network. Keying it on inputs alone would let a
later sweep silently overwrite the record of an earlier, different answer. See the
reasoning in `sweep_key` itself.

The doctests are the contract. They pin the exact key shape.
"""

from __future__ import annotations


def _short(digest: str) -> str:
    return digest.removeprefix("sha256:")[:12]


def snapshot_key(cluster: str, digest: str, captured_at: str) -> str:
    """Stage 1 snapshot: partitioned by cluster, carrying the snapshot digest.

    >>> snapshot_key("prod-1", "sha256:" + "ab" * 32, "2026-09-01T12:00:00+00:00")
    'snapshots/cluster=prod-1/date=2026-09-01/abababababab.json'
    """
    return f"snapshots/cluster={cluster}/date={captured_at[:10]}/{_short(digest)}.json"


def inventory_key(
    cluster: str, snapshot_digest: str, ruleset_digest: str, captured_at: str
) -> str:
    """Stage 2 inventory report.

    Both digests appear, so the two questions a reader asks — *which cluster state
    was this?* and *which rules produced it?* — are answerable from the listing
    alone.

    >>> inventory_key("p1", "sha256:" + "a" * 64, "sha256:" + "b" * 64, "2026-09-01T00:00:00")
    'inventories/cluster=p1/date=2026-09-01/snap-aaaaaaaaaaaa.rules-bbbbbbbbbbbb.json'
    """
    return (
        f"inventories/cluster={cluster}/date={captured_at[:10]}/"
        f"snap-{_short(snapshot_digest)}.rules-{_short(ruleset_digest)}.json"
    )


def gaps_key(
    cluster: str,
    snapshot_digest: str,
    store_digest: str,
    ruleset_digest: str,
    captured_at: str,
) -> str:
    """Stage 3 gap ledger.

    Carries the store digest as well, because a ledger changes when the *knowledge*
    changes and not only when the cluster or the rules do. Without it, writing a new
    fact would silently overwrite the ledger that recorded the gap it filled,
    destroying the before-and-after a reviewer needs.

    >>> gaps_key("p1", "sha256:" + "a"*64, "sha256:" + "c"*64, "sha256:" + "b"*64, "2026-09-01T00:00:00")
    'gaps/cluster=p1/date=2026-09-01/snap-aaaaaaaaaaaa.store-cccccccccccc.rules-bbbbbbbbbbbb.json'
    """
    return (
        f"gaps/cluster={cluster}/date={captured_at[:10]}/"
        f"snap-{_short(snapshot_digest)}.store-{_short(store_digest)}"
        f".rules-{_short(ruleset_digest)}.json"
    )


def decisions_key(
    cluster: str,
    target_kubernetes_version: str,
    snapshot_digest: str,
    store_digest: str,
    ruleset_digest: str,
    captured_at: str,
) -> str:
    """Stage 4 readiness assessment.

    The target version is part of the key because it is an *input*: the same cluster
    state assessed against 1.36 and against 1.37 are two different answers, and keying
    only on the digests would have the second silently overwrite the first.

    >>> decisions_key("p1", "1.36", "sha256:" + "a"*64, "sha256:" + "c"*64, "sha256:" + "b"*64, "2026-09-01T00:00:00")
    'decisions/cluster=p1/target=1.36/date=2026-09-01/snap-aaaaaaaaaaaa.store-cccccccccccc.rules-bbbbbbbbbbbb.json'
    """
    return (
        f"decisions/cluster={cluster}/target={target_kubernetes_version}/"
        f"date={captured_at[:10]}/snap-{_short(snapshot_digest)}"
        f".store-{_short(store_digest)}.rules-{_short(ruleset_digest)}.json"
    )


def plan_key(
    cluster: str,
    target_kubernetes_version: str,
    snapshot_digest: str,
    store_digest: str,
    ruleset_digest: str,
    captured_at: str,
) -> str:
    """Stage 5 upgrade plan. Same shape as Stage 4's, under a distinct prefix.

    >>> plan_key("p1", "1.36", "sha256:" + "a"*64, "sha256:" + "c"*64, "sha256:" + "b"*64, "2026-09-01T00:00:00")
    'plans/cluster=p1/target=1.36/date=2026-09-01/snap-aaaaaaaaaaaa.store-cccccccccccc.rules-bbbbbbbbbbbb.json'
    """
    return (
        f"plans/cluster={cluster}/target={target_kubernetes_version}/"
        f"date={captured_at[:10]}/snap-{_short(snapshot_digest)}"
        f".store-{_short(store_digest)}.rules-{_short(ruleset_digest)}.json"
    )


def candidate_key(
    component_id: str,
    version_series: str,
    decision: str,
    candidate_digest: str,
    researched_at: str,
) -> str:
    """Stage 6 candidate fact, under a prefix naming what the gate decided.

    The decision is in the path, not only inside the object. Three reasons: an operator can
    list what is awaiting review without opening anything; an
    accepted and a held candidate for the same component cannot overwrite each other; and
    a prefix that is empty is a visible, checkable claim that nothing is waiting. A
    pending area that holds stale objects nothing surfaces -- because nothing can
    route there any more and nobody looks -- is the failure this prevents.

    Every candidate is written, including a rejected one. A rejected candidate is the
    record of a component that was researched and yielded no storable fact, which is
    exactly the evidence a reader needs when a report says "no fact".

    >>> candidate_key("c1", "2.17.x", "REJECTED", "sha256:" + "ab"*32, "2026-09-12T10:00:00")
    'facts-candidates/decision=REJECTED/component=c1/series=2.17.x/date=2026-09-12/abababababab.json'
    """
    return (
        f"facts-candidates/decision={decision}/component={component_id}/"
        f"series={version_series}/date={researched_at[:10]}/"
        f"{_short(candidate_digest)}.json"
    )


def report_key(
    cluster: str,
    target_kubernetes_version: str,
    snapshot_digest: str,
    store_digest: str,
    ruleset_digest: str,
    captured_at: str,
    suffix: str = "json",
) -> str:
    """The presentation stage's report. Same shape as the plan's, one prefix over.

    `suffix` selects the view: `json` is the document itself (canonical JSON, the
    bytes the digest covers), `html` and `md` are its two renderings. All three
    share one key stem, so a listing shows the three views of one report adjacent
    rather than scattered — and a reader who has the HTML can find the document
    that proves its numbers by changing the extension.

    The `rules-` slot carries this stage's own VALUE ruleset digest, as every other
    stage's key does, so a change to what the report says writes a new object.

    It deliberately does NOT carry the renderer digest. A renderer change produces
    the same report drawn differently, so it overwrites the same key -- which is
    the honest answer, because the key identifies a run and a set of numbers rather
    than a stylesheet. The previous bytes stay recoverable through bucket
    versioning, and the renderer digest is recorded inside the document, so `verify`
    can still say "only the rendering changed" rather than "the report changed".
    Observed live: a CSS-only edit moved `renderer_digest` and the document digest
    while leaving `ruleset_digest` and this key untouched.

    >>> report_key("p1", "1.36", "sha256:" + "a"*64, "sha256:" + "c"*64, "sha256:" + "b"*64, "2026-09-01T00:00:00")
    'reports/cluster=p1/target=1.36/date=2026-09-01/snap-aaaaaaaaaaaa.store-cccccccccccc.rules-bbbbbbbbbbbb.json'
    >>> report_key("p1", "1.36", "sha256:" + "a"*64, "sha256:" + "c"*64, "sha256:" + "b"*64, "2026-09-01T00:00:00", "html")
    'reports/cluster=p1/target=1.36/date=2026-09-01/snap-aaaaaaaaaaaa.store-cccccccccccc.rules-bbbbbbbbbbbb.html'
    """
    return (
        f"reports/cluster={cluster}/target={target_kubernetes_version}/"
        f"date={captured_at[:10]}/snap-{_short(snapshot_digest)}"
        f".store-{_short(store_digest)}.rules-{_short(ruleset_digest)}.{suffix}"
    )

def brief_key(
    cluster: str,
    target_kubernetes_version: str,
    plan_digest: str,
    ruleset_digest: str,
    brief_digest: str,
    observed_at: str,
) -> str:
    """The run's upgrade brief: what an operator should know before approving the moves.

    Its own prefix, so no stage's IAM grant reaches it and a listing of what has been briefed
    is readable on its own. It is not a stage document -- nothing downstream reads it -- so it
    sits beside the chain rather than in it.

    THE PLAN IS A PATH SEGMENT, for `grant_key`'s reason: "what advice was written against
    this plan" is an object listing rather than a scan, and the plan digest already covers the
    snapshot, the ledger, the assessment, the fact store and the ordering ruleset the steps
    came from. Naming those separately would claim a derivation this document does not have --
    the brief reads the plan's steps and upstream's release notes, and nothing else.

    THE DOCUMENT'S OWN DIGEST IS IN THE KEY, and this is the SECOND place this project's
    keying rule bends -- for `sweep_key`'s reason, not a new one. A brief is not a pure
    function of its inputs: it reads open issues, which change daily, so the same plan
    legitimately produces a different brief an hour later. Keying only on the inputs would
    have the second brief silently overwrite the first, destroying the before-and-after that
    is the only honest thing to say about a point-in-time observation. Two briefs that
    disagree are two objects; two that agree in every respect -- including `observed_at` --
    still collapse onto one.

    NO `store-` SEGMENT, and the omission is deliberate rather than an oversight. The fact
    store is read here only to resolve a component's repository, and a fact being re-approved
    does not change what a release's notes say. Carrying the store digest would write a new
    object for a change that cannot affect the content, which is the opposite of what every
    other key in this file is for. The digest IS recorded inside the document, so a reader can
    still see which knowledge resolved the repositories.

    >>> plan_d, rules_d = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    >>> brief_key("p1", "1.36", plan_d, rules_d, "sha256:" + "e" * 64,
    ...           "2026-09-14T09:00:00+00:00")
    'briefs/cluster=p1/target=1.36/plan=aaaaaaaaaaaa/date=2026-09-14/rules-bbbbbbbbbbbb.brief-eeeeeeeeeeee.json'
    """
    return (
        f"briefs/cluster={cluster}/target={target_kubernetes_version}/"
        f"plan={_short(plan_digest)}/date={observed_at[:10]}/"
        f"rules-{_short(ruleset_digest)}.brief-{_short(brief_digest)}.json"
    )


def step_decision_key(
    cluster: str,
    target_kubernetes_version: str,
    plan_digest: str,
    run_id: str,
    step_order: int,
) -> str:
    """One person's decision about one step of one cluster's upgrade.

    THE CLUSTER IS A PATH SEGMENT AND THAT IS THE POINT. An upgrade approval is per cluster,
    unlike a compatibility fact, which is fleet-wide: "0.9.x supports 1.36" is true everywhere,
    but "change this cluster" is true of exactly one. Keying this the way `facts` is keyed --
    component plus series -- would let one team's demo approval answer for everybody's
    production. Putting the cluster in the path means no listing can return a decision without
    saying which cluster it belongs to.

    THE PLAN IS A SEGMENT TOO, so "what was decided against this plan" is an object listing
    rather than a scan, and because the plan digest already covers the snapshot, the ledger,
    the assessment, the fact store and the ordering ruleset the steps came from.

    THE RUN IS A SEGMENT AND THE STEP IS THE LEAF, which is what makes a decision idempotent
    on the only path that writes one: a nonce is single-use, so a step cannot be decided twice
    in a run, and re-writing the same key with the same bytes is the same object. Two runs over
    the same plan are two folders, which is correct -- a person deciding again in a later run
    is deciding again, not amending.

    NO DIGEST IN THE LEAF, unlike `brief_key`. A decision is not an observation of a world that
    moves; it is one person's answer at one instant, and there is exactly one of them per step
    per run. A digest would only ever produce a second object for a retry that changed nothing.

    >>> step_decision_key("prod-1", "1.36", "sha256:" + "a" * 64, "run-7", 3)
    'upgrade-decisions/cluster=prod-1/target=1.36/plan=aaaaaaaaaaaa/run=run-7/step=3.json'
    """
    return (
        f"upgrade-decisions/cluster={cluster}/target={target_kubernetes_version}/"
        f"plan={_short(plan_digest)}/run={run_id}/step={step_order}.json"
    )


def run_decisions_key(
    cluster: str,
    target_kubernetes_version: str,
    plan_digest: str,
    run_id: str,
) -> str:
    """The summary of everything one paused run decided, and what it left undecided.

    Beside the per-step objects rather than instead of them. The steps are the record; this is
    the one thing they cannot say -- which steps were offered and never answered. A reader of
    six approvals cannot tell whether the seventh was rejected or never looked at, and the
    executor has to be able to tell an answer from a queue.

    Same folder as the steps it summarises, so one prefix read returns a run's whole decision
    history in one call.

    >>> run_decisions_key("prod-1", "1.36", "sha256:" + "a" * 64, "run-7")
    'upgrade-decisions/cluster=prod-1/target=1.36/plan=aaaaaaaaaaaa/run=run-7/decisions.json'
    """
    return (
        f"upgrade-decisions/cluster={cluster}/target={target_kubernetes_version}/"
        f"plan={_short(plan_digest)}/run={run_id}/decisions.json"
    )


def upgrade_recorded_key(cluster_name: str, run_id: str) -> str:
    """The page a browser lands on after a click is recorded.

    BESIDE THE APPROVAL PAGE, under the same prefix, so the stage needs no permission it does
    not already hold and the S3 notification on `upgrade-clicks/` cannot see it.

    >>> upgrade_recorded_key("prod-1", "run-7")
    'upgrade-approvals/cluster=prod-1/run=run-7/recorded.html'
    """
    return f"upgrade-approvals/cluster={cluster_name}/run={run_id}/recorded.html"


def readiness_report_key(cluster: str, run_id: str) -> str:
    """Where the read-only readiness report lives, for a deployment that executes no upgrade.

    THE SAME PREFIX AS THE APPROVAL PAGE, deliberately: the same function writes it, under the
    same grant, and it is the same page rendered without controls. A different file name, so a
    run under a deployment later switched to execute does not overwrite one with the other.

    >>> readiness_report_key("prod-1", "run-7")
    'upgrade-approvals/cluster=prod-1/run=run-7/readiness.html'
    """
    return f"upgrade-approvals/cluster={cluster}/run={run_id}/readiness.html"


def upgrade_approval_key(cluster: str, run_id: str) -> str:
    """Where the approval page for one paused upgrade lives.

    Keyed by run and not by digest, for `approval_key`'s reason: this object is a LINK SOMEONE
    IS GIVEN. A digest-keyed name would change when the page was re-rendered on a stage retry,
    leaving a person holding a URL to an object that no longer describes their run.

    ITS OWN PREFIX, not `approvals/`. That prefix holds fact-approval pages, and an S3 event
    notification on it drives `decision_watcher`; a second kind of page arriving there would be
    read by a watcher that knows nothing about steps.

    >>> upgrade_approval_key("prod-1", "run-7")
    'upgrade-approvals/cluster=prod-1/run=run-7/approval.html'
    """
    return f"upgrade-approvals/cluster={cluster}/run={run_id}/approval.html"


def release_key(cluster: str, run_id: str) -> str:
    """Where the RELEASE -- the AWS-authenticated authorisation -- is recorded.

    ONE OBJECT PER RUN, keyed by run rather than by digest, and that is `upgrade_approval_key`'s
    reason turned around. A release is not a pure function of its inputs: it records WHO
    authorised and WHEN, so two authorisations of byte-identical plans are two different facts
    and must be two different objects. The run is what tells them apart.

    THE CLUSTER IS A PATH SEGMENT AND THAT IS THE POINT, exactly as `step_decision_key` states
    it. Every authorisation ever granted for one cluster is one prefix listing, so an executor
    can ask "what has been authorised here" without opening anything, and a release for one
    cluster can never land on another's key.

    WHAT IS DELIBERATELY NOT IN THIS KEY: the task token and the click nonces. Both are
    capabilities, and an S3 key reaches CloudTrail, access logs and every listing -- so this
    project's convention keeps capabilities out of names. The automation execution id is not
    here either, but for a different reason: it belongs IN the document, where a reader granted
    the object can see it, rather than in the name, where it would make a run's authorisation
    unfindable to an executor that knows only the cluster and the run.

    >>> release_key("prod-1", "run-7")
    'releases/cluster=prod-1/run=run-7/release.json'
    >>> release_key("my-eks-cluster", "toast-191932")
    'releases/cluster=my-eks-cluster/run=toast-191932/release.json'
    """
    return f"releases/cluster={cluster}/run={run_id}/release.json"


def component_brief_key(
    component: str,
    from_version: str,
    to_version: str,
    ruleset_digest: str,
    brief_digest: str,
    observed_at: str,
) -> str:
    """ONE component's brief, addressed by the UPGRADE it is about and nothing else.

    THE COMPONENT AND THE VERSION PAIR ARE THE WHOLE ADDRESS, and that is what makes a brief
    reusable. A brief answers "what happens when this component goes from this version to that
    one", and the answer does not depend on which cluster asked or on what else the plan is moving.
    So any later run making the same move finds it by prefix and serves it without paying for the
    agent again. See `brief/reuse.py`.

    WHAT THIS KEY USED TO BE, AND WHY THAT WAS WRONG. It carried the cluster and the PLAN's digest.
    A plan digest moves whenever anything in the plan changes, so a plan that also moved
    cert-manager made metrics-server's brief unfindable and re-researched -- a cache that missed
    almost every time it mattered. No cluster appears here now either: a brief about a version hop
    is not about a cluster, and keying on one would re-research the same hop per cluster.

    THE RULESET DIGEST IS A PATH SEGMENT, not a field to compare. A brief produced under a
    different prompt or different rules answers a subtly different question, so it must not be
    served for this one -- and a segment makes those briefs INVISIBLE to the lookup rather than
    found-and-rejected.

    THE DATE AND THE CONTENT DIGEST ARE BOTH IN THE LEAF. The date is what `brief/reuse.py` reads
    to apply the seven-day window without opening an object. The content digest is there for
    `sweep_key`'s reason: a brief is not a pure function of its inputs -- it reads an issue tracker
    -- so two briefs of the same hop that DISAGREE must be two objects, and neither may erase the
    other.

    >>> component_brief_key("metrics-server", "0.6.4", "0.9.0", "sha256:" + "b" * 64,
    ...                     "sha256:" + "e" * 64, "2026-09-14T09:00:00+00:00")
    'briefs/components/component=metrics-server/from=0.6.4/to=0.9.0/rules-bbbbbbbbbbbb/date=2026-09-14.brief-eeeeeeeeeeee.json'
    """
    return (
        f"briefs/components/component={component}/"
        f"from={from_version}/to={to_version}/"
        f"rules-{_short(ruleset_digest)}/"
        f"date={observed_at[:10]}.brief-{_short(brief_digest)}.json"
    )


def component_brief_prefix(
    component: str, from_version: str, to_version: str, ruleset_digest: str
) -> str:
    """Where every stored brief for one upgrade lives. What the reuse lookup lists.

    Deliberately a separate function rather than string-slicing the key: the lookup and the write
    must agree on the prefix, and deriving one from the other by cutting at a delimiter is how they
    come to disagree the first time a segment is added.

    >>> component_brief_prefix("metrics-server", "0.6.4", "0.9.0", "sha256:" + "b" * 64)
    'briefs/components/component=metrics-server/from=0.6.4/to=0.9.0/rules-bbbbbbbbbbbb/'
    """
    return (
        f"briefs/components/component={component}/"
        f"from={from_version}/to={to_version}/"
        f"rules-{_short(ruleset_digest)}/"
    )


def dispatch_key(
    cluster: str,
    snapshot_digest: str,
    store_digest: str,
    ruleset_digest: str,
    captured_at: str,
    iteration: int = 1,
    max_iterations: int = 3,
    max_dispatch: int | None = None,
    withheld: int = 0,
) -> str:
    """Stage 7's dispatch document: which gaps to research, and why.

    Carries the store digest for the same reason the gap ledger does, and more
    sharply: the queue is a function of what the knowledge base already holds, so
    writing a fact must not overwrite the document that recorded the gap it filled.
    That before-and-after IS the fleet-economics evidence -- cluster one dispatches,
    cluster two dispatches nothing, and both documents survive to be compared.

    `iteration` is in the key because one run can dispatch more than once: research
    writes facts, the ledger is rebuilt, and round two asks a smaller question. Each
    round is its own document rather than an overwrite of the last.

    WHY THE LOOP BOUND IS IN THE KEY. It is recorded in the document, so it is covered by
    its `digest()` -- and it did not reach this key. An operator retuning the bound and
    re-running over the same snapshot and store therefore wrote DIFFERENT BYTES TO THE SAME
    KEY, silently replacing the very before-and-after the paragraph above claims survives.
    It is a genuine question parameter -- the bound decides whether a round asks at all or
    records `ITERATION_LIMIT_REACHED` -- so by this project's own rule, that every key
    carries the digests of the inputs that determined the document, it belongs here.

    THE CAP IS HERE FOR THE SAME REASON, and it was the last input missing from this key.
    `max_dispatch` TRUNCATES the request list -- `dispatch_queue` slices to it -- so the
    same ledger under a cap of 3 and a cap of 5 produces two different documents. It
    reached the document's body and its digest and did not reach this key, which is
    precisely the defect the loop bound above had. Found by the design review while this
    key was being rewritten, and fixed in the same pass rather than left for a later one:
    splitting one key's composition across two changes is how a digest gets broken.

    The segment appears ONLY when a cap was applied. That is deliberate rather than lazy:
    absence of the segment means absence of the input, which is a distinct and unambiguous
    encoding, and it keeps an uncapped key -- every key any deployment writes today,
    because nothing sets a cap -- free of a permanent `cap-all` that says nothing.

    WHAT USED TO BE HERE AND WHY IT IS GONE. This key carried a `fresh-<N>h` segment for
    `READINESS_FACT_MAX_AGE_HOURS`, the 24-hour expiry Stage 7 applied to a stored fact.
    That mechanism was removed: staleness in this system means THE UPSTREAM SOURCE MOVED,
    which the freshness sweep decides from outside the pipeline (see
    `research/freshness.py`). Stage 7 has no window to record, so the segment describes
    nothing and would mislead a reader into looking for a dial that no longer exists.

    A DEPLOYED CONSEQUENCE, STATED RATHER THAN DISCOVERED: this key changes shape once, so
    the first run after this lands writes to a new key instead of overwriting the last one.
    That is a one-time break in the listing's continuity, not a loss -- the old objects stay
    exactly where they are. NOTE THAT THE SHAPE CHANGED FOR TWO REASONS IN ONE CHANGE: the
    window segment was removed AND the cap segment was added. A reader who attributes the
    move to the freshness removal alone has half of it.

    >>> dispatch_key("p1", "sha256:" + "a"*64, "sha256:" + "c"*64, "sha256:" + "b"*64, "2026-09-01T00:00:00")
    'dispatch/cluster=p1/date=2026-09-01/snap-aaaaaaaaaaaa.store-cccccccccccc.rules-bbbbbbbbbbbb.max-3.round-1.json'
    >>> dispatch_key("p1", "sha256:" + "a"*64, "sha256:" + "c"*64, "sha256:" + "b"*64, "2026-09-01T00:00:00", iteration=2, max_iterations=5)
    'dispatch/cluster=p1/date=2026-09-01/snap-aaaaaaaaaaaa.store-cccccccccccc.rules-bbbbbbbbbbbb.max-5.round-2.json'
    >>> dispatch_key("p1", "sha256:" + "a"*64, "sha256:" + "c"*64, "sha256:" + "b"*64, "2026-09-01T00:00:00", max_dispatch=1)
    'dispatch/cluster=p1/date=2026-09-01/snap-aaaaaaaaaaaa.store-cccccccccccc.rules-bbbbbbbbbbbb.max-3.cap-1.round-1.json'
    >>> dispatch_key("p1", "sha256:" + "a"*64, "sha256:" + "c"*64, "sha256:" + "b"*64, "2026-09-01T00:00:00", withheld=3)
    'dispatch/cluster=p1/date=2026-09-01/snap-aaaaaaaaaaaa.store-cccccccccccc.rules-bbbbbbbbbbbb.max-3.withheld-3.round-1.json'
    """
    cap = "" if max_dispatch is None else f".cap-{max_dispatch}"
    # HOW MANY GAPS WERE WITHHELD because a candidate for them is already awaiting approval. An
    # INPUT that determined the document by the same argument the cap is: the same ledger produces
    # a different queue depending on what is already pending, and two different queues must not
    # land on one object. Present only when non-zero, for the reason the cap segment is: absence of
    # the segment means absence of the condition, which is unambiguous, and it keeps every key any
    # deployment wrote before this from acquiring a permanent `withheld-0`.
    held = "" if withheld <= 0 else f".withheld-{withheld}"
    return (
        f"dispatch/cluster={cluster}/date={captured_at[:10]}/"
        f"snap-{_short(snapshot_digest)}.store-{_short(store_digest)}"
        f".rules-{_short(ruleset_digest)}"
        f".max-{max_iterations}{cap}{held}"
        f".round-{iteration}.json"
    )


def sweep_key(
    mode: str,
    cluster: str | None,
    store_digest: str,
    ruleset_digest: str,
    sweep_digest: str,
    swept_at: str,
) -> str:
    """The freshness sweep's own document: what moved upstream, and what was marked.

    Its own prefix, so no stage's IAM grant reaches it and a listing of what the sweep has
    concluded is readable on its own.

    THE MODE IS IN THE PATH, for the reason `candidate_key` puts the acceptance decision
    there: an operator can list what one run swept without opening anything, and a
    fleet-wide sweep cannot overwrite a run-scoped one. A scheduled sweep is about no
    cluster, so it names none -- inventing a placeholder would make a fleet sweep look like
    a cluster's.

    THE DOCUMENT'S OWN DIGEST IS IN THE KEY, and this is the one place this project's
    keying rule bends -- deliberately, and in the safe direction. Every other key is a
    function of the document's INPUTS, because a stage document is a pure function of them:
    re-running writes the same bytes to the same key, which is what makes writes idempotent.
    A sweep is not a pure function of its inputs. It reads the network, so the same store and
    the same scope legitimately produce a different answer an hour later. Keying only on the
    inputs would have the second sweep silently overwrite the first, destroying the before
    and after that a drift record is entirely made of. Including the document's digest makes
    two sweeps that DISAGREE two objects, while two that agree in every respect -- including
    `swept_at` -- still collapse onto one.

    >>> sweep_key("pre-flight", "prod-1", "sha256:" + "c"*64, "sha256:" + "b"*64, "sha256:" + "d"*64, "2026-09-12T12:00:00+00:00")
    'sweeps/mode=pre-flight/cluster=prod-1/date=2026-09-12/store-cccccccccccc.rules-bbbbbbbbbbbb.sweep-dddddddddddd.json'
    >>> sweep_key("scheduled", None, "sha256:" + "c"*64, "sha256:" + "b"*64, "sha256:" + "d"*64, "2026-09-12T12:00:00+00:00")
    'sweeps/mode=scheduled/date=2026-09-12/store-cccccccccccc.rules-bbbbbbbbbbbb.sweep-dddddddddddd.json'
    """
    scope = f"cluster={cluster}/" if cluster else ""
    return (
        f"sweeps/mode={mode}/{scope}date={swept_at[:10]}/"
        f"store-{_short(store_digest)}.rules-{_short(ruleset_digest)}"
        f".sweep-{_short(sweep_digest)}.json"
    )
