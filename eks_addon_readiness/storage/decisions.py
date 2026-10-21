"""A decision dropped into S3 by a browser form, and the key that says what it was.

WHY A FORM POST TO S3 AND NOT A CALL TO OUR OWN ENDPOINT. Every attempt to let a person
click something that reaches a Lambda directly has failed on a constraint outside this
project's control:

  * `NONE` auth on a function URL is world-accessible. A security scanner raised it, correctly, and
  an
    org guardrail refuses the requests anyway -- 403 while the resource policy said Allow.
  * `AWS_IAM` with a SigV4-presigned link satisfies the guardrail and is unusable: Lambda
    function URLs cap signature freshness at FIVE MINUTES and ignore `X-Amz-Expires`.
    Measured -- 200 at t+0s, `403 Signature expired ... (- 5 min.)` at t+330s -- and the
    clock starts when the mail is SENT, so delivery latency eats the window.

S3 IS A DIFFERENT MECHANISM AND DOES NOT SHARE THAT CAP. A presigned POST carries its
credential in the FORM BODY rather than the URL, and S3 honours the policy's own `Expiration`.
So a browser can submit an authenticated write hours after the page was built, with no
JavaScript and no credential of its own.

WHAT THE BROWSER IS ALLOWED TO DO, and it is deliberately almost nothing: write one small
object, at one exact key, into one prefix. The policy pins the key exactly, so a submitted
form cannot be redirected at another candidate, another run, or anywhere else in the bucket.
It cannot read, cannot list, cannot overwrite anything outside that key.

WHAT MAKES THE DECISION REAL. Nothing about the S3 object authorises anything -- it is a
doorbell, not a key. The object's ARRIVAL triggers `decision_watcher`, which redeems the
one-time grant named in the key and only then invokes `approve`. So the same single-use,
one-action-one-candidate scoping that has always guarded a decision still guards it, and the
browser never holds a credential that could approve a fact.

WHY THE KEY CARRIES THE NONCE RATHER THAN THE BODY. The presigned policy pins the key, so a
nonce in the key is a value the submitter cannot alter. A nonce in the body could be edited
before submission, and the policy has no way to constrain arbitrary content -- so the
tamper-proof field is the one the policy signs.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

# Where a browser may write, and nowhere else. A separate prefix from `approvals/` because
# that one holds pages the pipeline WRITES and a person reads; this one holds objects a
# person writes and the pipeline reads. Different direction, different blast radius,
# different IAM.
DECISION_PREFIX = "approval-decisions"

# Where an UPGRADE decision lands, and deliberately not the prefix above. Two reasons, and
# the second is the one that would bite:
#
#   * An S3 event notification on `approval-decisions/` invokes `decision_watcher`, which
#     redeems a fact grant and invokes `approve`. An upgrade click arriving there would be
#     handed to code that knows about candidates and version series and nothing about steps.
#   * The two are answering different questions. A fact decision is fleet-wide; an upgrade
#     decision is about ONE CLUSTER. Sharing a prefix would make "what has been decided" a
#     query that cannot distinguish them.
UPGRADE_CLICK_PREFIX = "upgrade-clicks"

# How long a form stays submittable. Matches the approval wait, so the page and the run
# expire together -- a form that outlives its execution would write a decision object nobody
# is waiting on.
#
# CAPPED BY THE SIGNING SESSION IN PRACTICE, whatever this says. A presigned form cannot
# outlive the credentials that signed it, and the approval stage signs with its Lambda
# execution role, which is an STS session. Stated here because "expires in 2 hours" is only
# true while the session behind it lives.
DEFAULT_FORM_TTL_SECONDS = 7200


@dataclass(frozen=True)
class DecisionForm:
    """One presigned POST: where it submits to, and the hidden fields that authorise it."""

    url: str
    fields: dict[str, str]
    key: str


def decision_key(run_id: str, nonce: str) -> str:
    """The one key a given form may write.

    Both values are in the PATH rather than the body because the presigned policy pins the
    key exactly, which makes them tamper-proof: a submitter can change neither without
    invalidating the signature.
    """
    return f"{DECISION_PREFIX}/run={run_id}/nonce={nonce}"


def parse_decision_key(key: str) -> tuple[str, str] | None:
    """`(run_id, nonce)` from a written object's key, or None if it is not one of ours.

    Returns None rather than raising for anything unexpected, because this runs from an S3
    event notification: an object arriving under a prefix we did not expect is a
    configuration question, not a reason to fail an invocation and retry it forever.
    """
    match = re.fullmatch(rf"{DECISION_PREFIX}/run=([^/]+)/nonce=([^/]+)", key)
    if not match:
        return None
    return match.group(1), match.group(2)


def build_decision_form(
    bucket: str,
    run_id: str,
    nonce: str,
    *,
    redirect_url: str = "",
    ttl_seconds: int | None = None,
    client: Any = None,
) -> DecisionForm | None:
    """A presigned POST that lets a browser write exactly one decision object.

    Returns None when it cannot be signed -- no credentials in a local render, for instance.
    The page then falls back to the signed-command controls rather than showing a button that
    cannot work, which is the same judgement the rest of this project makes: a control that
    silently fails is worse than an honest absence.

    `success_action_redirect` is what turns S3's bare `204 No Content` into a page a person
    can read. Without it a click lands on a blank response and looks broken even though the
    decision was recorded.
    """
    return _presigned_post(
        bucket,
        decision_key(run_id, nonce),
        redirect_url=redirect_url,
        ttl_seconds=ttl_seconds,
        client=client,
    )


def upgrade_click_key(run_id: str, nonce: str) -> str:
    """The one key a given upgrade form may write.

    >>> upgrade_click_key("run-7", "abc")
    'upgrade-clicks/run=run-7/nonce=abc'
    """
    return f"{UPGRADE_CLICK_PREFIX}/run={run_id}/nonce={nonce}"


def parse_upgrade_click_key(key: str) -> tuple[str, str] | None:
    """`(run_id, nonce)` from a written upgrade click, or None if it is not one of ours.

    None rather than raising, for `parse_decision_key`'s reason: this runs from an S3 event
    notification, and an object under an unexpected prefix is a configuration question rather
    than a reason to fail an invocation and retry it forever.

    >>> parse_upgrade_click_key("upgrade-clicks/run=run-7/nonce=abc")
    ('run-7', 'abc')
    >>> parse_upgrade_click_key("approval-decisions/run=run-7/nonce=abc") is None
    True
    """
    match = re.fullmatch(rf"{UPGRADE_CLICK_PREFIX}/run=([^/]+)/nonce=([^/]+)", key)
    if not match:
        return None
    return match.group(1), match.group(2)


def build_upgrade_click_form(
    bucket: str,
    run_id: str,
    nonce: str,
    *,
    redirect_url: str = "",
    ttl_seconds: int | None = None,
    client: Any = None,
) -> DecisionForm | None:
    """A presigned POST that lets a browser record exactly one upgrade decision.

    Identical mechanics to `build_decision_form` and a different prefix, which is the whole
    difference: the policy pins one key under `upgrade-clicks/`, so the form cannot be
    redirected at a fact decision, at another step, or anywhere else in the bucket.
    """
    return _presigned_post(
        bucket,
        upgrade_click_key(run_id, nonce),
        redirect_url=redirect_url,
        ttl_seconds=ttl_seconds,
        client=client,
    )


def _presigned_post(
    bucket: str,
    key: str,
    *,
    redirect_url: str,
    ttl_seconds: int | None,
    client: Any,
) -> DecisionForm | None:
    """One presigned POST pinned to one key.

    EXTRACTED SO THE SECOND FORM KIND CANNOT DRIFT FROM THE FIRST. The bounded
    content-length and the pinned key are what keep this a doorbell rather than an upload
    endpoint, and a second copy of them written from the docstring above is where that would
    quietly stop being true.
    """
    if client is None:
        try:
            import boto3

            client = boto3.client("s3")
        except Exception:
            return None

    fields: dict[str, Any] = {}
    conditions: list[Any] = [
        # The object is a few bytes. Bounding it is not about storage: an unbounded content
        # length on a presigned POST is an upload endpoint, and this is a doorbell.
        ["content-length-range", 1, 256],
    ]
    if redirect_url:
        fields["success_action_redirect"] = redirect_url
        conditions.append({"success_action_redirect": redirect_url})

    try:
        presigned = client.generate_presigned_post(
            Bucket=bucket,
            Key=key,
            Fields=dict(fields),
            Conditions=conditions,
            ExpiresIn=ttl_seconds or form_ttl_seconds(),
        )
    except Exception:
        return None

    return DecisionForm(
        url=presigned["url"], fields=dict(presigned["fields"]), key=key
    )


def form_ttl_seconds() -> int:
    raw = os.environ.get("APPROVAL_FORM_TTL_SECONDS", "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return DEFAULT_FORM_TTL_SECONDS
    # S3 caps a presigned POST at 7 days; asking for more produces a policy S3 rejects at
    # submission time, which would look like a broken button rather than a bad setting.
    return min(int(raw), 7 * 24 * 3600)
