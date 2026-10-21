"""The clickable approval endpoint: redeem a one-time link, then do what it authorises.

Behind a Lambda function URL with **`AuthorizationType: AWS_IAM`**, reached through a link
the approval stage PRESIGNS with SigV4 (`notify/presign.py`). AWS verifies that signature
before this function runs, and the signature is bound to one method, path and query string
and expires with the approval window. It started life as the AWS-published shape for human
approval in Step Functions -- the console's own "Human Approval" sample project uses an
unauthenticated endpoint and a token in the link -- and `notify/presign.py` records why that
open endpoint was replaced. A person still clicks once and the run continues with nothing to
paste.

**WHAT AUTHORISES THE ACTION, beyond the signature.** The signature proves this pipeline
issued the link, not who clicked it. What it may do is decided by a nonce, minted by the
approval stage, stored in the token store, and delivered ONLY by email. Four properties make
that worth relying on:

1. **It is scoped to one action on one candidate.** An `approve` grant cannot reject, and a
   grant for one component cannot touch another. The candidate URI and both digests are
   recorded IN the grant, so they are not parameters a caller supplies -- a clicker cannot
   redirect a valid link at a different fact.
2. **It works once.** `consume_grant` is a conditional delete, so redemption is atomic. A
   double-click, or a mail client that prefetches links -- several do -- cannot decide twice.
3. **It expires with the run.** The item carries a TTL, and the execution fails on its own
   approval timeout long before that.
4. **It is never written where the agent looks.** Not in the S3 page, not in a stage result,
   not in a log line. See `notify/__init__.py`, which is where that argument lives, and
   which lists the three ways to break it.

**THIS FUNCTION IS NOT A SECOND WRITER OF FACTS, deliberately.** It holds no DynamoDB write
on the fact table and no S3 write. Once a grant is redeemed it INVOKES the approve function,
which stays the only code that writes a fact -- so "one writer, behind one audit trail"
remains literally true, and the security review of that writer does not have to be repeated
here. What this function is, exactly: a bouncer that turns a nonce into a signed internal
invocation.

**WHY THE APPROVER IS NOT READ FROM THE REQUEST.** The only signed principal is the approval
stage that minted the link -- that is the trade. So the approver is the address the approval
stage mailed the link to,
recorded in the grant when it was minted. A clicker cannot influence it. That is a weaker
claim than a SigV4 principal and it is the honest one: the audit record says "whoever opened
this mailbox", which is what actually happened.

**GET, NOT POST, AND THAT IS A REAL COST.** A link in an email has to be a GET, and GET is
supposed to be safe. Some scanners and prefetchers will therefore redeem a link nobody
clicked, and the visible symptom is a fact approved by a robot. Two things bound it: the
grant is single-use, so a prefetch consumes the link rather than performing the action twice;
and `done` is what resumes the run, so a prefetched approve cannot on its own push the
pipeline forward. It is still the weakest point in this design and it is written here rather
than discovered.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import parse_qs

from botocore.config import Config

from ..storage.tokens import Grant, build_token_store

APPROVE_FUNCTION = os.environ.get("APPROVE_FUNCTION_NAME", "").strip()

# THE CALLER MUST OUTLAST THE CALLEE, and getting this wrong produced the one failure shape
# this pipeline is otherwise careful to avoid: a false negative on a real write.
#
# This function consumes the grant and THEN invokes `approve` synchronously, so `approve`'s
# whole execution happens inside this one's budget. Both Lambdas were 60s, and botocore's
# default read timeout is also 60s, so all three raced. If `approve` ran long, this function
# hit its ceiling first and rendered "That did not go through" -- while `approve` carried on
# and wrote the fact. The person saw a failure, the link was already spent, and the fact was
# silently stored.
#
# The nesting that fixes it, and the inequality is the whole point:
#
#     approve's own ceiling   60s   (a hard Lambda limit -- it cannot exceed this)
#   < this client's wait     150s   (so we ALWAYS see approve's answer, success or death)
#   < this function's ceiling 180s  (so there is time left to render a page about it)
#
# Because `approve` cannot outlive 60s, waiting 150 means the timeout branch below is now
# unreachable for a slow-but-working call. It stays for a genuinely unreachable function.
#
# Retries off: a retried invoke would run the fact write twice. `approve` refuses a
# non-pending candidate, so the second would 409 rather than double-write -- but paying for
# it and logging it as an error is worse than not doing it.
_INVOKE_CONFIG = Config(read_timeout=150, connect_timeout=10, retries={"max_attempts": 0})


def _page(title: str, body: str, *, hue: str = "#34a853") -> dict[str, Any]:
    """A small self-contained confirmation page. The person is in a browser, not a terminal.

    Deliberately plain HTML with inline style and no asset: this renders in whatever the
    mail client handed the URL to, including an in-app webview with no network beyond the
    one request it just made.
    """
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title></head>"
            "<body style=\"margin:0;padding:40px 24px;background:#0f1115;color:#eef1f6;"
            'font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
            '<div style="max-width:560px;margin:0 auto">'
            f'<div style="height:4px;background:{hue};border-radius:2px;margin-bottom:20px">'
            "</div>"
            f'<h1 style="font-size:20px;margin:0 0 12px">{title}</h1>'
            f'<div style="font-size:14px;color:#b9c0cc;line-height:1.6">{body}</div>'
            "</div></body></html>"
        ),
    }


def _params(event: dict[str, Any]) -> dict[str, str]:
    """The query string, from whichever shape the function URL used.

    Function URLs populate `queryStringParameters`; a direct invocation or a test may pass
    `rawQueryString`. Both are read rather than one, so the handler is exercisable without
    a synthetic API Gateway envelope.
    """
    direct = event.get("queryStringParameters")
    if isinstance(direct, dict) and direct:
        return {str(k): str(v) for k, v in direct.items()}
    raw = str(event.get("rawQueryString") or "")
    return {k: v[0] for k, v in parse_qs(raw).items() if v}


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Redeem `?r=<run_id>&t=<nonce>` and carry out exactly what that grant permits."""
    params = _params(event)
    run_id = params.get("r", "").strip()
    nonce = params.get("t", "").strip()

    if not run_id or not nonce:
        return _page(
            "Nothing to do",
            "This link is incomplete. Open the approval email again and use one of the "
            "buttons in it.",
            hue="#8f97a5",
        )

    store = build_token_store()
    grant = store.consume_grant(run_id, nonce)
    if grant is None:
        # ONE MESSAGE FOR THREE CAUSES, on purpose. Spent, wrong, and expired are
        # indistinguishable to the person and separating them would tell an unintended
        # holder of a link which of their guesses was closest.
        return _page(
            "This link has already been used",
            "Each approval link works exactly once. If you have already clicked it, your "
            "decision was recorded and nothing further is needed. If you believe it should "
            "still work, the run may have failed on its approval timeout -- the next run "
            "will offer the same proposals again.",
            hue="#ffd166",
        )

    if not APPROVE_FUNCTION:
        return _page(
            "Not configured",
            "This deployment has no approval function wired in, so the decision could not "
            "be carried out. Your link has been used up; a new run will issue fresh ones.",
            hue="#ea4335",
        )

    status, body = _perform(grant, run_id)
    return _render(grant, status, body)


def _perform(grant: Grant, run_id: str) -> tuple[int, dict[str, Any]]:
    """Invoke the approve function with what the grant authorises. Returns its answer.

    Every field comes off the GRANT and none off the request, which is what makes a
    redeemed link unable to act on anything but the fact it was minted for.
    """
    import boto3

    if grant.action == "done":
        payload: dict[str, Any] = {"action": "done", "run_id": run_id}
    else:
        payload = {
            "action": grant.action,
            "candidate_uri": grant.candidate_uri,
            "candidate_digest": grant.candidate_digest,
            "report_digest": grant.report_digest,
            "component_id": grant.component_id,
            "version_series": grant.version_series,
        }
        # The audit record needs a sentence, and a link cannot carry one -- so it says what
        # actually happened rather than leaving the field empty or, worse, keeping the
        # placeholder the command path refuses.
        note = f"Approved from the approval email for run {run_id}"
        payload["comment" if grant.action == "approve" else "reason"] = (
            note
            if grant.action == "approve"
            else f"Rejected from the approval email for run {run_id}"
        )

    response = boto3.client("lambda", config=_INVOKE_CONFIG).invoke(
        FunctionName=APPROVE_FUNCTION,
        InvocationType="RequestResponse",
        # `approver` is honoured by the approve handler ONLY on a direct invocation with no
        # requestContext -- an HTTP caller can never set it. See `approve._principal`.
        Payload=json.dumps({**payload, "approver": grant.approver}).encode(),
    )
    raw = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError"):
        return 500, {"error": f"the approval function raised: {str(raw)[:400]}"}
    status = int(raw.get("statusCode", 500))
    try:
        return status, json.loads(raw.get("body") or "{}")
    except json.JSONDecodeError:
        return status, {"error": str(raw.get("body"))[:400]}


def _render(grant: Grant, status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Turn the approve function's JSON into a page a person can read."""
    if status >= 400:
        return _page(
            "That did not go through",
            f"<p>{body.get('error', 'The decision could not be recorded.')}</p>"
            "<p>Your link has been used up. Nothing was changed, and a new run will issue "
            "fresh links for the same proposals.</p>",
            hue="#ea4335",
        )

    if grant.action == "done":
        changed = body.get("knowledge_changed")
        detail = (
            "Your approvals changed the fact store, so the run is rebuilding the ledger, "
            "the assessment and the plan with the approved facts in them before it reports."
            if changed
            else "Nothing was approved, so the run is going straight to its report."
        )
        left = body.get("left_undecided") or []
        tail = (
            f"<p>{len(left)} proposal(s) were left undecided. They stay pending and will be "
            "offered again on the next run.</p>"
            if left
            else ""
        )
        return _page(
            "The run is continuing",
            f"<p>{detail}</p>{tail}<p>You can close this page.</p>",
            hue="#f9ab00",
        )

    if grant.action == "approve":
        return _page(
            f"Approved: {grant.component_id} {grant.version_series}",
            "<p>The fact is stored and now answers from the live fact store.</p>"
            "<p><strong>The run has not resumed yet.</strong> Go back to the email and press "
            "<em>Done</em> when you have finished deciding.</p>",
        )

    return _page(
        f"Rejected: {grant.component_id} {grant.version_series}",
        "<p>No fact was stored. This component stays an open question, exactly as it was "
        "before anyone looked, and your refusal is recorded with the reason.</p>"
        "<p><strong>The run has not resumed yet.</strong> Go back to the email and press "
        "<em>Done</em> when you have finished deciding.</p>",
        hue="#ea4335",
    )
