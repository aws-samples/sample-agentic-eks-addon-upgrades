"""The approval endpoint: the only path a fact takes into the live store.

Runs behind a Lambda **function URL with `AWS_IAM` auth**, and the function's resource
policy is an **allowlist of exactly one principal**: the `${name_prefix}-approver` role, and
nothing else. That is the whole security argument, and it is worth being precise about why
this shape was chosen:

**The project's load-bearing claim is that anything able to read the approval page -- a
person, a script or an agent -- can SHOW a pending approval but cannot GRANT one.** An open
endpoint destroys that claim, because anything holding the URL could approve -- including
every reader of the page, which holds every URL rendered onto it. An endpoint that checks a
token in the request body is no better: the reader would have to be given the token in order
to display the page.

So authorisation is moved off the request entirely and onto the caller's signature. AWS
resolves the principal from a SigV4 signature before this code runs, and the approver is read
from the resolved identity in the request context, never from the body, so it cannot be typed
in.

**WHAT REFUSES EVERY OTHER CALLER, STATED AS DEPLOYED IAM.** Three controls:

1. **The resource policy is an allowlist of one.** Its single statement Allows
   `lambda:InvokeFunctionUrl` to `${name_prefix}-approver` alone, conditioned on
   `lambda:FunctionUrlAuthType = AWS_IAM`. It contains no `Deny`: every other principal in
   the account is refused by the ABSENCE of an Allow -- an implicit deny. Implicit is weaker
   than explicit in exactly one way that matters: a policy attached later could grant it,
   and nothing would override that grant.
2. **The approver role can name roles that may never assume it.** When
   `denied_approver_role_names` in `infra/terraform/modules/research` is non-empty, the
   `${name_prefix}-approver` trust policy carries a second statement, sid
   `AgentToolRolesMayNeverApprove`, Effect `Deny` on `sts:AssumeRole` naming those roles. It
   is empty by default; list there the role of any agent you add that reads approval pages.

3. **And this handler refuses a principal that is not on the approver allowlist.**
   `_is_approver` matches the resolved principal's role name against
   `APPROVER_ROLE_NAMES`, which `infra/terraform/modules/research/approval.tf` sets to
   `${name_prefix}-approver` on the deployed function, and returns 403 with the
   principal named otherwise. So even a principal that somehow acquired invoke on this URL
   does not get to approve. One caveat that keeps this honest: configured EMPTY it returns
   True, which is the local and test configuration. It is a real control in the deployment
   and not one in a bare checkout.

The gap, stated so it is not mistaken for covered: a role not named in control 2 is kept off
this endpoint by control 1, an implicit deny, plus control 3, which is code in this file
rather than IAM. A policy attached to such a role later could grant
`lambda:InvokeFunctionUrl`, and only control 3 would then stand between it and an approval.
Closing that needs an explicit `Deny` on the role itself, or an SCP.

**Three operations, and only one of them writes a fact.**

    GET  ?pending=1              list what is awaiting approval. Read-only.
    POST {"action":"approve"}    approve one candidate. Writes the fact.
    POST {"action":"reject"}     refuse one candidate. Writes NO fact.

`action` defaults to `approve` so that every client written before it existed keeps working;
the default is the pre-existing behaviour rather than a new one, which is the only default
that cannot silently change what an old caller does.

The listing exists so a reader has something safe to call: "show me what needs
approval" is answerable without granting anything. It reads the candidate prefix and returns
pending candidates and their report links.

**WHY REJECT LIVES HERE RATHER THAN NOWHERE.** Without it the only way to refuse is to do
nothing, which is indistinguishable from not having looked: the candidate stays pending and
is offered again on the next report, so a reviewer who already refused it cannot say so. It
is strictly the weaker operation -- it stores no fact, changes no verdict, and cannot make
the pipeline assert anything -- and it is behind the same signature and the same allowlist,
so it moves the human boundary nowhere. It writes a NEW object under the `decision=REJECTED`
prefix; the pending object is left exactly as it was, because it is the record of what was
proposed.

**The audit trail is the point of the write path.** An approval stores who approved (their
principal ARN), when, their comment, and the digest of the report they were shown. The last
one is what makes the record evidence rather than a timestamp: it proves *what* was
approved, which in a regulated environment is a different question from *that* something was.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from ..research.acceptance import advisory_note
from ..research.approval import ApprovalRefused, approve, decline, is_pending
from ..research.candidate import CandidateFact
from ..research.report import ApprovalReport, build_report
from ..storage import (
    build_fact_store,
    build_fact_writer,
    build_uri,
    candidate_key,
    read_document,
    write_document,
)
from ..storage.facts import FactRejected
from ..storage.tokens import build_token_store

# The two operations a POST may name. Listed rather than accepted freely so a typo in a
# hand-edited command is refused with the choices named, instead of falling through to
# whichever branch happens to be last.
#
# `done` is the odd one out and deliberately so: it decides NOTHING about any candidate. It
# says "I have finished deciding", which is what lets the paused execution continue. It is
# listed here because it arrives on the same signed POST and must pass the same allowlist --
# resuming a pipeline is not a lesser act than approving one fact.
ACTIONS = ("approve", "reject", "done")

CANDIDATE_PREFIX = "facts-candidates/"
PENDING_PREFIX = f"{CANDIDATE_PREFIX}decision=PENDING_APPROVAL/"

# Role names permitted to approve, injected by Terraform. Defence in depth on top of the
# IAM controls, and the layer that covers one residual: for a Lambda function URL in the
# same account an identity-based Allow is sufficient on its own, so a role granted
# `lambda:InvokeFunctionUrl` on this function directly would reach this code despite the
# resource policy naming only the approver role. `aws_lambda_permission` has no `effect`
# argument, so Terraform cannot write the Deny that would close that in IAM. This check
# closes it here instead.
APPROVER_ROLE_NAMES = tuple(
    name.strip()
    for name in os.environ.get("APPROVER_ROLE_NAMES", "").split(",")
    if name.strip()
)


def _is_approver(principal: str) -> bool:
    """Whether this principal is one the deployment permits to approve.

    Matches on the role NAME inside an assumed-role ARN, because the session name differs
    per assumption while the role is what was authorised. Configured empty means unchecked,
    which is the local and test configuration -- a deployed one always sets it.

    >>> _check = lambda arn, names: any(f"/{n}/" in arn or arn.endswith(f"/{n}") for n in names)
    >>> _check("arn:aws:sts::1:assumed-role/approver/session", ("approver",))
    True
    >>> _check("arn:aws:sts::1:assumed-role/tool-query-facts/s", ("approver",))
    False
    """
    if not APPROVER_ROLE_NAMES:
        return True
    # AN EMAIL APPROVER IS NOT A ROLE, so the role-name match cannot apply to it. It
    # arrives only on a direct invocation from `-approve-link`, which had to hold
    # `lambda:InvokeFunction` on this function AND redeem a single-use grant -- two
    # controls, neither of them this list. Matching an address against role names would
    # refuse every emailed approval while looking like a security check.
    if "@" in principal:
        return True
    return any(
        f"/{name}/" in principal or principal.endswith(f"/{name}")
        for name in APPROVER_ROLE_NAMES
    )


def _bucket() -> str:
    bucket = os.environ.get("DOCUMENT_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError("DOCUMENT_BUCKET is unset; there is no candidate store to read")
    return bucket


def _s3():
    import boto3

    return boto3.client("s3")


def _principal(event: dict[str, Any]) -> str:
    """The caller's identity, taken from the request context AWS populated.

    Read from the signed request context and never from the body. A body field would be a
    name anyone could type; this is the principal AWS resolved from the SigV4 signature
    before this function ran, so it is the one thing in the request that cannot be forged
    without the corresponding credentials.
    """
    ctx = event.get("requestContext") or {}
    if ctx:
        identity = (ctx.get("authorizer") or {}).get("iam") or {}
        for key in ("userArn", "callerId", "principalOrgId"):
            value = identity.get(key)
            if value:
                return str(value)
        # Function URLs also surface it here for some auth shapes.
        legacy = (ctx.get("identity") or {}).get("userArn")
        return str(legacy or "")

    # NO requestContext AT ALL MEANS THIS IS NOT AN HTTP REQUEST -- it is a direct
    # `lambda:Invoke`, and the only principal holding that on this function is the
    # `-approve-link` role. So an `approver` field is honoured here and NOWHERE ELSE.
    #
    # WHY THAT IS NOT A HOLE. The guard is the presence of `requestContext`, which AWS
    # populates for every function-URL request and which a caller cannot suppress: a
    # request arriving over HTTP always has it, so the branch above always wins and an
    # HTTP body's `approver` is never read. The field is reachable only by a caller who
    # already had IAM permission to invoke this function directly, and that caller had to
    # redeem a single-use grant to get here.
    #
    # WHAT THE RECORDED VALUE MEANS, stated plainly because it is weaker than a SigV4
    # principal: it is the address the approval stage mailed the link to. It attests that
    # somebody opened that mailbox, not which human it was. That is the honest reading of a
    # one-time link, and `approve_link.py`'s docstring makes the same point.
    return str(event.get("approver") or "")


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Read the request body, JSON or form-encoded, with a real parser either way.

    The HTML form posts `application/x-www-form-urlencoded`; a CLI or script posts JSON.
    Both are parsed properly rather than pattern-matched, because escaping differs between
    clients and a substring search over a serialised body eventually finds the wrong thing.
    """
    # A DIRECT INVOCATION CARRIES THE FIELDS AT THE TOP LEVEL, not under `body`: there is
    # no HTTP envelope to unwrap. Detected by the absence of `requestContext`, the same
    # discriminator `_principal` uses, so the two cannot disagree about which kind of call
    # this is.
    if not event.get("requestContext") and "body" not in event:
        return {k: v for k, v in event.items() if k != "approver"}

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("request body parsed to a non-object")
        return parsed
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(raw).items() if v}


def _response(status: int, body: Any, content_type: str = "application/json") -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": content_type},
        "body": body if isinstance(body, str) else json.dumps(body, indent=2, default=str),
    }


def _list_pending() -> list[dict[str, Any]]:
    """Every candidate awaiting approval, with the links a person needs.

    Listed from the prefix the decision is encoded into, so "what is awaiting approval" is
    an object listing rather than a scan of every candidate ever produced. That is why
    `candidate_key` puts the decision in the path.
    """
    bucket = _bucket()
    client = _s3()
    pending: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": PENDING_PREFIX}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            # One candidate produces several objects under the same stem; the candidate
            # itself is the one with no extra suffix.
            if not key.endswith(".json") or key.endswith(
                (".evidence.json", ".report.json")
            ):
                continue
            pending.append(
                {
                    "candidate_uri": f"s3://{bucket}/{key}",
                    "report_uri": f"s3://{bucket}/{key[:-5]}.report.json",
                    "report_html_uri": f"s3://{bucket}/{key[:-5]}.report.html",
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )
        token = page.get("NextContinuationToken")
        if not token:
            break
    return sorted(pending, key=lambda p: p["last_modified"], reverse=True)


def _stored_assessment(candidate_uri: str) -> Any:
    """The model's assessment for this candidate, read back from its extraction object.

    WITHOUT THIS EVERY APPROVAL IS REFUSED, and that is not a hypothetical -- it is the bug
    this function was added to fix. Stage 6 builds the approval report with
    `assessment=extraction.assessment`; `_rebuild_report` below did not pass an assessment at
    all, so the rebuilt report differed from the one a person was shown and the digest check
    refused the approval with a mismatch. Every approval, for every component, since the
    assessment was added to the report. Nothing detected it because no approval had ever got
    as far as the digest comparison.

    Read from `<base>.extraction.json` rather than reconstructed from the stored report's flat
    assessment fields. The report flattens and stringifies them for display; rebuilding an
    `Assessment` out of that would be a lossy inverse of a rendering step, and any drift
    between it and the real object reappears as exactly the same digest mismatch it is meant
    to fix.

    Returns None when there is no extraction object -- a candidate from the older
    transcribe-only prompt has none, and the report renders and digests without an assessment.
    That is why this cannot simply raise on absence.
    """
    if not candidate_uri.endswith(".json"):
        return None
    try:
        from ..research.boundary import ResearchExtraction

        extraction = read_document(
            candidate_uri[:-5] + ".extraction.json", ResearchExtraction
        )
    except Exception:
        return None
    return extraction.assessment


def _rebuild_report(
    candidate: CandidateFact, stored: ApprovalReport, assessment: Any = None
) -> ApprovalReport:
    """Recompute the report for this candidate, to check the submitted digest against it.

    Recomputed rather than trusted from S3. If the stored report object were simply hashed,
    an approval would prove only that the file on S3 matched -- and that file is writable by
    the research role. Rebuilding from the candidate and the current store state means the
    digest an approval carries has to match a report this code can derive, which is a
    stronger claim.

    The inputs the report needs that are not on the candidate -- the quoted claims, the
    bases, the hops -- are read back off the stored report. Those are presentation inputs
    that do not affect what is being approved, and re-deriving them would need the
    extraction, which the candidate does not carry by design.

    `assessment` IS THE EXCEPTION TO THAT, and omitting it was a real defect rather than a
    simplification: Stage 6 builds the report WITH the model's assessment, so a rebuild
    without one is a different report, and the digest check refused every approval as a
    mismatch. It comes from the extraction object via `_stored_assessment` because the
    stored report only holds a flattened, stringified copy for display.
    """
    return build_report(
        candidate,
        current=build_fact_store().find(candidate.component_id),
        quoted_claims=stored.claims,
        proposed_bases={row.version_spec: row.basis for row in stored.proposed_windows},
        researched_at=stored.researched_at,
        installed_version=stored.installed_version,
        cluster=stored.cluster,
        hops=tuple(d.hop for d in stored.verdict_deltas),
        advisory=advisory_note(
            candidate.receipt.confidence,
            candidate.receipt.contradictions,
            candidate.receipt.change_direction,
        ),
        assessment=assessment,
    )


def _reject(
    body: dict[str, Any],
    candidate: CandidateFact,
    approver: str,
    expected_report_digest: str,
) -> dict[str, Any]:
    """Record a person's refusal of a pending candidate. Writes no fact.

    The write goes to the candidate area under `decision=REJECTED`, never to the fact
    store, and never over the pending object. Two consequences worth being explicit
    about, because both are the point rather than a limitation:

    **Nothing about the cluster changes.** The component stays a gap and the next run
    reports it as UNRESOLVED with a typed FACT_MISSING reason, exactly as it did
    before anyone looked. A refusal is a statement about the *evidence*, not a
    verdict about the component.

    **The pending object survives.** It is the record of what was proposed, and a
    refusal that erased it would destroy the evidence the refusal is about. It also
    means the candidate remains under the PENDING_APPROVAL prefix that `_list_pending`
    reads, so a refused candidate is still listed there until the pending object is
    aged out -- the declination is durable and attributable, but it does not yet
    suppress the listing. Called out here rather than left to be discovered.
    """
    declined_at = datetime.now(UTC)
    try:
        declined = decline(
            candidate,
            approver=approver,
            reason=str(body.get("reason", "")),
            report_digest=str(body.get("report_digest", "")),
            submitted_candidate_digest=str(
                body.get("candidate_digest", candidate.digest())
            ),
            expected_report_digest=expected_report_digest,
            declined_at=declined_at,
        )
    except ApprovalRefused as exc:
        # 409, not 500: the request was well-formed and the state was wrong.
        return _response(409, {"error": str(exc)})

    location = build_uri(
        _bucket(),
        candidate_key(
            declined.component_id,
            declined.version_series,
            str(declined.receipt.decision),
            declined.digest(),
            declined_at.isoformat(),
        ),
    )
    try:
        write_document(location, declined)
    except Exception as exc:
        # THIS PATH IS REACHABLE TODAY. Recording a refusal is the first write this
        # function has ever made, and the deployed approve role grants only
        # `s3:GetObject` and `s3:ListBucket` on the document bucket -- so an
        # AccessDenied here is the expected outcome until that policy adds
        # `s3:PutObject` on `facts-candidates/decision=REJECTED/*`. Answered with the
        # remedy named rather than as a bare 502, because a 502 would send a reviewer
        # looking for a bug in their request.
        return _response(
            500,
            {
                "error": (
                    "the refusal could not be recorded: "
                    f"{type(exc).__name__}: {exc}. Nothing was stored, and no fact was "
                    "approved. If this is AccessDenied, this function's role needs "
                    "s3:PutObject on facts-candidates/decision=REJECTED/* -- recording "
                    "a refusal is a write, and the role was previously read-only on "
                    "the document bucket."
                ),
                "recorded": False,
            },
        )
    # A REJECTION COUNTS AS A DECISION, which is the whole reason this endpoint has a
    # reject action at all: without it the only way to refuse is to do nothing, which is
    # indistinguishable from not having looked. The paused run records it as settled, so
    # the person is not offered it again before they press Done.
    _note_decision(candidate, "reject")

    return _response(
        200,
        {
            "approved": False,
            "rejected": True,
            "component_id": declined.component_id,
            "version_series": declined.version_series,
            "stored_at": location,
            "reviewer": approver,
            "declined_at": declined_at.isoformat(),
            "reason": str(body.get("reason", "")).strip(),
            "report_digest": str(body.get("report_digest", "")),
            "candidate_digest": candidate.digest(),
            "effect": (
                f"No fact was stored. {declined.component_id} stays an open question, "
                "and the next run reports it as UNRESOLVED with a typed gap exactly "
                "as before. The refusal itself is recorded, with your principal and "
                "the digest of the report you read."
            ),
        },
    )


def _note_decision(candidate: CandidateFact, decision: str) -> None:
    """Record that this candidate has been settled, against whichever run is waiting on it.

    BEST-EFFORT ON PURPOSE, and this is the one place in this file where swallowing a
    failure is right. By the time this is called the decision is already durable -- the
    fact is written, or the refusal is stored. This table is scaffolding that tells the
    approval page which rows have been dealt with; failing an approval that has ALREADY
    written a fact, because a display convenience could not be updated, would report a
    false negative about a real write.
    """
    try:
        store = build_token_store()
        paused = store.find_run_for_candidate(candidate.digest())
        if paused is not None:
            store.record_decision(paused.run_id, candidate.digest(), decision)
    except Exception:
        return


def _done(body: dict[str, Any], approver: str) -> dict[str, Any]:
    """Resume a paused execution, because the person says they have finished deciding.

    THE ONE PLACE A PAUSED RUN CAN BE RESUMED, and the reason it is here rather than in
    the approval stage is the same reason approval is here at all: the person's signature
    is what authorises it. The approval stage renders the page and holds NO
    `SendTaskSuccess` permission, so nothing that runs unattended can decide that a human
    is finished.

    THE TASK TOKEN IS NEVER IN THE REQUEST. It is read from the token store by run id.
    That matters: the token is the capability to resume the pipeline, and putting it in a
    command printed onto a page in S3 would hand that capability to every reader of the
    page -- including any agent that renders report links.
    The run id is not a capability; it is in the console.

    WHAT IT SENDS BACK, and why it is one field. The approval state's `ResultPath` nests
    this output under `$.approval`, so the run payload survives untouched and the
    `Knowledge changed?` Choice sees both `$.approval.store_digest_after_research` and the
    `$.store_digest_before_research` that `Resolve` put there. Sending the whole payload
    instead would mean storing it somewhere for this function to read back, for no gain.

    THIS IS WHERE THE DORMANT LOOP FINALLY FIRES. `Knowledge changed?` has been deployed
    for a long time and has never once taken its `Gaps` branch, because nothing between
    `Research` and that Choice could write a fact -- research writes candidates and this
    function is the only writer of facts. Now the wait sits in between, so an approval
    moves the store digest inside the same execution, and the ledger, assessment and plan
    are rebuilt with the approved fact in them.

    DECIDING NOTHING IS A VALID OUTCOME. A person may open the page, read all three
    proposals and approve none. `Done` still resumes: an undecided proposal stays pending
    and is offered again next run, which is the same outcome as never having looked. The
    response says how many were left, so the answer is not silent about it.
    """
    run_id = str(body.get("run_id", "")).strip()
    if not run_id:
        return _response(
            400,
            {
                "error": (
                    "run_id is required to resume a paused run. It is the execution name, "
                    "shown on the approval page and in the Step Functions console."
                )
            },
        )

    store = build_token_store()
    paused = store.get(run_id)
    if paused is None:
        return _response(
            404,
            {
                "error": (
                    f"no paused run {run_id!r} is waiting. Either it was already resumed, "
                    "or it failed on its approval timeout, or the id is wrong. Nothing was "
                    "done."
                )
            },
        )

    # The digest AFTER whatever this person decided. Computed here rather than accumulated
    # as decisions arrive, because it is the store's own answer about its own contents --
    # deriving it from a count of approvals would be a second, driftable source of truth
    # for whether knowledge actually changed.
    store_digest_after = build_fact_store().digest()

    try:
        _sfn().send_task_success(
            taskToken=paused.task_token,
            output=json.dumps({"store_digest_after_research": store_digest_after}),
        )
    except Exception as exc:
        # NAMED, not swallowed. A TaskTimedOut here means the execution already failed on
        # its approval timeout, and telling the person "resumed" when their decisions are
        # going nowhere would be the worst possible answer: they would believe a report was
        # coming.
        return _response(
            409,
            {
                "error": (
                    f"the run could not be resumed: {type(exc).__name__}: {exc}. Any fact "
                    "you approved IS stored and stays approved -- this failure is about "
                    "the paused execution, not about your decisions. If this is "
                    "TaskTimedOut, the run failed on its approval timeout and a new run "
                    "will pick the approved facts up."
                ),
                "resumed": False,
            },
        )

    store.release(run_id)
    decided = paused.decisions
    return _response(
        200,
        {
            "resumed": True,
            "run_id": run_id,
            "reviewer": approver,
            "decided": decided,
            "left_undecided": list(paused.undecided),
            "store_digest_before_research": paused.store_digest_before,
            "store_digest_after_research": store_digest_after,
            "knowledge_changed": store_digest_after != paused.store_digest_before,
            "effect": (
                "The run continues. If your approvals changed the fact store, it rebuilds "
                "the ledger, the assessment and the plan with the approved facts in them "
                "before it reports; if nothing changed, it goes straight to the report. "
                "Anything you left undecided stays pending and is offered again next run."
            ),
        },
    )


def _sfn():
    import boto3

    return boto3.client("stepfunctions")


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Approve or refuse one candidate, resume a paused run, or list what is pending.

    Behind IAM auth; no anonymous path, and no path on which this code chooses. Both
    write paths require a signed principal that the deployment's allowlist permits,
    and both record the digest of the report the person was shown.
    """
    method = (
        (event.get("requestContext") or {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or "POST"
    ).upper()

    if method == "GET":
        return _response(
            200,
            {
                "pending": _list_pending(),
                # Says WHICH refusal, rather than "the agent is denied". Both are true, but
                # only one of them is checkable by the reader: they can read this function's
                # resource policy and see the single Allow. See this module's docstring on
                # why the vaguer wording was worth replacing.
                "note": (
                    "Read-only. Approving requires a POST signed with your own credentials, "
                    "and this function's resource policy allows only the approver role, so "
                    "no other principal can call it."
                ),
            },
        )

    approver = _principal(event)
    if not approver:
        # Should be unreachable behind AWS_IAM auth. Refused rather than defaulted, because
        # a default would be an anonymous approval with a plausible-looking record.
        return _response(
            403,
            {
                "error": (
                    "no signed principal was resolved for this request. This endpoint "
                    "requires AWS_IAM auth; there is no anonymous approval path."
                )
            },
        )

    if not _is_approver(approver):
        # Reached only if something was granted invoke on this function outside the intended
        # path. Refused here with the principal named, so the attempt is in the log.
        return _response(
            403,
            {
                "error": (
                    f"{approver} is not permitted to approve a fact. Approval requires "
                    f"assuming one of: {', '.join(APPROVER_ROLE_NAMES)}."
                )
            },
        )

    try:
        body = _parse_body(event)
    except ValueError as exc:
        return _response(400, {"error": f"request body could not be parsed: {exc}"})

    # `done` is dispatched BEFORE the candidate is loaded, because it names no candidate.
    # Falling through to the `candidate_uri` requirement below would refuse it with a
    # message about a field it is not supposed to carry.
    if str(body.get("action", "")).strip().lower() == "done":
        return _done(body, approver)

    candidate_uri = str(body.get("candidate_uri", "")).strip()
    if not candidate_uri:
        return _response(
            400,
            {
                "error": (
                    "candidate_uri is required. Call GET on this endpoint to list what is "
                    "pending approval."
                )
            },
        )

    candidate = read_document(candidate_uri, CandidateFact)

    # THE FACT NAMED MUST BE THE FACT LOADED. The URI and the two digests are what
    # authorise a decision, and none of them is human-readable -- so a page whose prose
    # said "keda" beside another component's digests would let a reader decide something
    # they never read about, and every digest check would still pass. The command
    # therefore also carries the component and series the page displayed, and they are
    # checked against the candidate at that URI. Optional, so a client written before
    # these fields existed still works; checked whenever supplied.
    for field, shown, actual in (
        ("component_id", str(body.get("component_id", "")).strip(), candidate.component_id),
        (
            "version_series",
            str(body.get("version_series", "")).strip(),
            candidate.version_series,
        ),
    ):
        if shown and shown != actual:
            return _response(
                409,
                {
                    "error": (
                        f"the request names {field} {shown!r} but the candidate at "
                        f"{candidate_uri} is {actual!r}. The page you read and the fact "
                        "this would decide are not the same thing, so nothing was done."
                    )
                },
            )

    if not is_pending(candidate):
        return _response(
            409,
            {
                "error": (
                    f"{candidate.component_id} is {candidate.receipt.decision}, not "
                    "PENDING_APPROVAL"
                )
            },
        )

    action = str(body.get("action", "approve")).strip().lower() or "approve"
    if action not in ACTIONS:
        return _response(
            400,
            {
                "error": (
                    f"unknown action {action!r}. This endpoint accepts "
                    f"{', '.join(ACTIONS)}; approve stores the fact, reject stores "
                    "none and records why."
                )
            },
        )

    report_uri = candidate_uri[:-5] + ".report.json" if candidate_uri.endswith(".json") else ""
    stored_report = read_document(report_uri, ApprovalReport)
    expected = _rebuild_report(
        candidate, stored_report, _stored_assessment(candidate_uri)
    )

    if action == "reject":
        return _reject(body, candidate, approver, expected.digest())

    try:
        approved = approve(
            candidate,
            approver=approver,
            comment=str(body.get("comment", "")),
            report_digest=str(body.get("report_digest", "")),
            submitted_candidate_digest=str(
                body.get("candidate_digest", candidate.digest())
            ),
            expected_report_digest=expected.digest(),
            approved_at=datetime.now(UTC),
        )
    except ApprovalRefused as exc:
        # 409, not 500: the request was well-formed and the state was wrong.
        return _response(409, {"error": str(exc)})

    try:
        location = build_fact_writer().write_approved(approved)
    except FactRejected as exc:
        return _response(500, {"error": str(exc)})

    # Note it against the paused run, if there is one. A no-op otherwise -- a candidate can
    # legitimately be approved from the standing queue with no run waiting on it, and that
    # must not fail an approval that has already written a fact.
    _note_decision(candidate, "approve")

    record = approved.receipt.approval
    return _response(
        200,
        {
            "approved": True,
            "component_id": approved.component_id,
            "version_series": approved.version_series,
            "stored_at": location,
            "approver": record.approver,
            "approved_at": record.approved_at.isoformat(),
            "comment": record.comment,
            "report_digest": record.report_digest,
            "candidate_digest": record.candidate_digest,
            "effect": (
                f"{approved.component_id} now answers from the live fact store. The next "
                "run reports it as FACT_AVAILABLE instead of a typed gap."
            ),
        },
    )
