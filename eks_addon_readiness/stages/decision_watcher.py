"""An object arrived under `approval-decisions/`, so a person clicked something.

Invoked by an S3 event notification, which is the whole point: the browser wrote a small
object with a presigned form, and the ARRIVAL of that object is what reaches code that holds
credentials. No public endpoint exists anywhere in this path -- the bucket is private, the
form is signed by the approval stage, and nothing on the internet can invoke anything.

WHY THIS EXISTS AT ALL. Two earlier designs let a click reach a Lambda directly and both
died on constraints outside this project:

  * `NONE` auth on a function URL is world-accessible. A security scanner raised it and an org
    guardrail refused the requests anyway.
  * `AWS_IAM` with a presigned link satisfies the guardrail and is unusable, because Lambda
    function URLs cap signature freshness at five minutes and ignore `X-Amz-Expires`.

An S3 presigned POST has neither problem: the credential is in the form body, S3 honours the
policy's own expiry, and the endpoint being written to is a private bucket.

WHAT THIS HANDLER TRUSTS, WHICH IS ALMOST NOTHING. The object's arrival proves only that
somebody held a valid presigned form. Everything that decides what actually happens comes
from the GRANT named in the key: the action, the candidate, both digests and the approver.
The object's body is never read. So a person who somehow submitted a form for a key they were
not given still gets nothing -- the grant lookup fails and this returns without acting.

IT IS NOT A WRITER OF FACTS. It redeems the grant and invokes `approve`, which remains the
only code that writes one. Same arrangement as `approve_link`, and for the same reason: the
security argument for that writer should not have to be made twice.

FAILURES ARE SWALLOWED DELIBERATELY, which is unusual in this codebase and worth stating.
An S3 event notification RETRIES on error, and every operation here is either idempotent
already or must not be repeated -- a retried `SendTaskSuccess` fails on a spent token, and a
retried approve re-runs the only fact write. A grant is single-use, so the second attempt
finds nothing and does nothing; raising would just produce a retry storm and a misleading
error metric. What went wrong is logged instead.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

from ..storage.decisions import parse_decision_key
from ..storage.tokens import Grant, build_token_store

APPROVE_FUNCTION = os.environ.get("APPROVE_FUNCTION_NAME", "").strip()


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Act on every decision object in this notification. Returns what it did, for the log."""
    outcomes: list[dict[str, Any]] = []
    for record in event.get("Records") or []:
        key = (
            ((record.get("s3") or {}).get("object") or {}).get("key")
            or ""
        )
        # S3 event keys are URL-encoded: `run=a+b` and `%3D` both appear for keys containing
        # spaces or reserved characters. Decoding is not optional -- an undecoded key simply
        # fails to parse and the click is silently dropped.
        decoded = urllib.parse.unquote_plus(key)
        outcomes.append(_act(decoded))
    return {"stage": "decision-watcher", "decisions": outcomes}


def _act(key: str) -> dict[str, Any]:
    parsed = parse_decision_key(key)
    if parsed is None:
        return {"key": key, "acted": False, "reason": "not a decision key"}
    run_id, nonce = parsed

    store = build_token_store()
    grant = store.consume_grant(run_id, nonce)
    if grant is None:
        # Spent, unknown, or the run is gone. All three are the same answer to a click: the
        # decision either already happened or was never authorised.
        return {
            "key": key,
            "run_id": run_id,
            "acted": False,
            "reason": "no unspent grant for this nonce",
        }

    if not APPROVE_FUNCTION:
        return {
            "key": key,
            "run_id": run_id,
            "acted": False,
            "reason": "APPROVE_FUNCTION_NAME is unset",
        }

    status, body = _invoke_approve(grant, run_id)
    return {
        "key": key,
        "run_id": run_id,
        "action": grant.action,
        "component_id": grant.component_id,
        "acted": status == 200,
        "status": status,
        "detail": body.get("error") or body.get("effect") or "",
    }


def _invoke_approve(grant: Grant, run_id: str) -> tuple[int, dict[str, Any]]:
    """Hand the grant's contents to the one writer of facts.

    Every field comes off the GRANT, which the approval stage minted and stored, and none off
    the S3 object. That is what makes a decision unable to name a candidate it was not issued
    for.
    """
    import boto3
    from botocore.config import Config

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
        note = f"Decided from the approval page for run {run_id}"
        payload["comment" if grant.action == "approve" else "reason"] = note

    # Longer than `approve`'s own 60s ceiling, so we always see its answer rather than
    # abandoning a call that is still writing a fact. Retries off: this is the fact-writing
    # path and a retry runs it twice. Same reasoning as `approve_link._INVOKE_CONFIG`.
    config = Config(read_timeout=150, connect_timeout=10, retries={"max_attempts": 0})
    response = boto3.client("lambda", config=config).invoke(
        FunctionName=APPROVE_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps({**payload, "approver": grant.approver}).encode(),
    )
    raw = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError"):
        return 500, {"error": f"approve raised: {str(raw)[:300]}"}
    try:
        return int(raw.get("statusCode", 500)), json.loads(raw.get("body") or "{}")
    except json.JSONDecodeError:
        return int(raw.get("statusCode", 500)), {"error": str(raw.get("body"))[:300]}
