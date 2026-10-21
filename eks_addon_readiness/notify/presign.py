"""Signing an approval link so it is authenticated BEFORE our code sees it.

WHY THIS EXISTS, and it replaced a design that an account guardrail can correctly refuse.

The first version put a one-time nonce in the query string of a Lambda function URL with
`AuthorizationType: NONE`. That is the shape AWS publishes as its own "Human Approval"
sample for Step Functions, and it works -- but a function URL open to the world is a real
finding regardless of what guards it downstream, and an organisation policy that denies
unauthenticated function-URL invocations outright is common. Under one, the endpoint answered
every request with

    HTTP 403 {"Message":"Forbidden. For troubleshooting Function URL authorization ..."}

while its own resource policy said `Allow` to `Principal: *`. An `Allow` plus a 403 is the
signature of a denial above the account, and it is not something to route around.

WHAT A PRESIGNED URL CHANGES. The function URL becomes `AWS_IAM`, and the link carries a
SigV4 signature in its query string instead of relying on being open. AWS verifies that
signature before the function is invoked at all. So:

  * The org policy is SATISFIED rather than circumvented -- the URL is authenticated, which
    is exactly what it asks for.
  * The signature is bound to ONE method, path and query string. Altering any part of the
    link -- a different nonce, a different run -- invalidates it. The nonce was already
    scoped to one action on one candidate; now the URL itself is too, cryptographically.
  * `X-Amz-Expires` is enforced by AWS, not by us. Set to the approval window, so the link
    and the paused execution die together. That also closes a gap the nonce alone had: a
    grant lives in the token store for 7 days, so a link could outlive the run that issued
    it by six days and still write a fact.

THE NONCE IS KEPT, NOT REPLACED. Two independent gates: the signature decides whether the
request reaches the function, and the grant decides what it may do and that it may do it
once. Dropping either would be a downgrade -- a signature alone is replayable until it
expires, and a nonce alone is what the guardrail refused.

WHAT THIS DOES NOT CLAIM. The signature is the approval stage's, because the stage is what
mints the link. So it attests that this pipeline issued the link, not that a particular human
clicked it. The recorded approver is still the mailbox the link was mailed to, and
`storage/tokens.py` says so where the approver is recorded. A signed link in the wrong
mailbox is still a usable link, which is why the capability goes only to email and never to
S3 -- see `notify/__init__.py`.
"""

from __future__ import annotations

import os
from typing import Any

# How long a link stays valid. Matches `approval_timeout_seconds` on the Step Functions
# wait, so a link cannot outlive the execution it resumes -- clicking an expired link and
# resuming a dead run are then the same answer instead of two different confusing ones.
#
# SigV4 query signing caps at 7 days; this is far below it, deliberately.
DEFAULT_EXPIRY_SECONDS = 7200


def link_expiry_seconds() -> int:
    raw = os.environ.get("APPROVAL_LINK_EXPIRY_SECONDS", "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        return DEFAULT_EXPIRY_SECONDS
    # Never longer than SigV4 permits; AWS would reject the signature at request time and
    # the failure would look like a broken link rather than a bad setting.
    return min(int(raw), 7 * 24 * 3600)


def presign_get(
    url: str, *, region: str | None = None, expires: int | None = None, session: Any = None
) -> str:
    """The same URL with a SigV4 signature in its query string, or unchanged if unsignable.

    UNSIGNABLE RETURNS THE PLAIN URL RATHER THAN RAISING, and that is a deliberate choice
    about which failure is worse. With no credentials -- a local render, a test -- raising
    would mean the approval stage could not build its email at all, so a run would fail on
    something that is only a presentation concern. Returning the plain URL produces a link
    that does not work, which is visible and recoverable, against a pipeline that does not
    run, which is neither.

    A deployed stage always has credentials from its execution role, so the fallback is not
    a path production takes.
    """
    try:
        import boto3
        from botocore.auth import SigV4QueryAuth
        from botocore.awsrequest import AWSRequest

        boto_session = session or boto3.Session()
        credentials = boto_session.get_credentials()
        if credentials is None:
            return url
        signing_region = (
            region or boto_session.region_name or os.environ.get("AWS_REGION") or "us-east-1"
        )
        request = AWSRequest(method="GET", url=url)
        SigV4QueryAuth(
            credentials.get_frozen_credentials(),
            "lambda",
            signing_region,
            expires=expires or link_expiry_seconds(),
        ).add_auth(request)
        return request.url
    except Exception:
        return url


def presign_s3_get(uri: str, *, expires: int | None = None, client: Any = None) -> str:
    """A long-lived link to one S3 object, or the console URL if it cannot be signed.

    THIS IS THE ONE PRESIGNED THING THAT ACTUALLY SURVIVES A MAILBOX, and the contrast with
    `presign_get` above is the whole reason both exist. A Lambda function URL caps SigV4
    freshness at five minutes and ignores `X-Amz-Expires`; S3 honours the expiry it is given.
    Same signing algorithm, completely different server-side policy.

    So the email carries THIS -- a link to the approval page -- and no longer carries per-row
    action links, which could not work. The page it opens holds the real buttons: presigned S3
    POST forms that a browser submits on click.

    CAPPED BY THE SIGNING SESSION regardless of what is asked for. A presigned URL cannot
    outlive the credentials behind it, and the approval stage signs with its Lambda execution
    role, which is an STS session. Stated because "valid for two hours" is only true while
    that session is.

    Falls back to the console URL rather than raising: an unsignable link is a worse page to
    land on than one that asks you to log in, but neither is worth failing a run over.
    """
    if not uri.startswith("s3://"):
        return uri
    bucket, _, key = uri[len("s3://") :].partition("/")
    region = os.environ.get("AWS_REGION", "us-east-1")
    console = (
        f"https://{region}.console.aws.amazon.com/s3/object/{bucket}"
        f"?region={region}&prefix={key}"
    )
    try:
        if client is None:
            import boto3

            client = boto3.client("s3")
        return client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                # Without this S3 serves the object with whatever content type it was stored
                # as; forcing text/html makes the browser RENDER the page instead of
                # downloading it, which is the difference between a working link and a file
                # in the Downloads folder.
                "ResponseContentType": "text/html; charset=utf-8",
            },
            ExpiresIn=expires or link_expiry_seconds(),
        )
    except Exception:
        return console
