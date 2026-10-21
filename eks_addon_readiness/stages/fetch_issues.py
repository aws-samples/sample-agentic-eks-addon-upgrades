"""The `search_issues` tool, behind the brief agent's Gateway. Records every result it serves.

**WHY THIS IS A TOOL I OWN RATHER THAN A CAPABILITY THE PLATFORM PROVIDES.** The same argument
`fetch_source.py` makes, applied to a different endpoint: the agent's quotes are only checkable
because the bytes were written down before it saw them. If issue search were something the
Harness did on the agent's behalf, an issue title in the brief would be an assertion nobody
could re-check. So the search stays a Lambda, and its first act is to record.

**IT RECORDS TWO DIFFERENT THINGS, AND BOTH ARE LOAD-BEARING.**

    one object per issue      keyed by the issue's own html_url and `issue-<number>`, holding
                              its title and body. This is what a quote is verified against --
                              `PageSet.get(url, revision)` is exact on both parts, so an issue
                              citation resolves to that issue's bytes and to nothing else. One
                              search call fans out into N citable objects, exactly as the
                              release listing does.
    one lookup receipt        `*.lookup.json`, naming the outcome, the query and the status. Not
                              for quoting -- for CHECKING THE AGENT. See below.

**THE RECEIPT IS WHY A RATE LIMIT CANNOT BECOME AN ALL-CLEAR.** With a managed Harness the
proxy does not see tool calls, so nothing downstream would otherwise know whether a search ever
ran. An agent that got three HTTP 403s and then wrote "no known issues" would produce the most
dangerous line this document can carry, and it would look identical to a genuinely clean
project. The receipts make that checkable on disk: `recorded_lookups` reads them back and
`brief/document.py::_cross_check_issues` downgrades the section when every attempt failed.

**THE TOOL OWNS THE QUERY SYNTAX.** The agent supplies intent -- a repository, optional terms,
optional labels, a comment floor -- and `brief/issues.py::build_query` assembles the qualifiers.
An agent writing raw GitHub search syntax earns a 422 for a stray colon and reads it as "no
issues", which is the failure the whole tri-state exists to prevent.

**A REFUSAL IS A SUCCESSFUL RETURN.** Like `fetch_source`, this never raises for a network
condition: the agent needs to be TOLD it could not look so it can report the section as
not-produced, and an exception would just become a dead component.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Any

from ..brief.issues import (
    DEFAULT_LIMIT,
    IssueLookup,
    IssueLookupOutcome,
    build_query,
    classify_response,
    not_looked,
    repository_slug,
    search_url,
)
from . import fetch_source

# Marks a lookup receipt. `fetch_source.load_recorded_pages` skips this suffix, so a receipt
# never enters the PageSet and can never be quoted from -- it is a record ABOUT a search, not a
# page that was read.
LOOKUP_SUFFIX = ".lookup.json"

_TIMEOUT_SECONDS = 20
_USER_AGENT = "eks-addon-readiness/2 (+upgrade brief; read-only)"


@lru_cache(maxsize=1)
def _token() -> str | None:
    """The GitHub token, from the environment or from Secrets Manager. None if there is none.

    TWO SOURCES, AND THE ORDER MATTERS. A plain `GITHUB_TOKEN` wins, because that is the
    convention `stages/freshness.py` already reads and it is what a local run exports. Failing
    that, `GITHUB_TOKEN_SECRET_ARN` is resolved from Secrets Manager, which is what
    `infra/terraform/modules/brief` supplies.

    WHY THE DEPLOYED PATH IS THE SECRET AND NOT THE VALUE. A token in an environment variable is
    readable by anyone holding `lambda:GetFunctionConfiguration`, which is a far wider set than
    `secretsmanager:GetSecretValue` on one secret. The token is a credential to a third party;
    keeping it out of the function's configuration keeps it out of every console view and every
    `describe` an operator runs.

    CACHED FOR THE LIFE OF THE CONTAINER, not per call. The agent may search several times for one
    component, and one Secrets Manager read per search would be latency and cost for a value that
    does not change within an invocation.

    A FAILURE TO RESOLVE IS NOT FATAL. It returns None, the search runs unauthenticated, and the
    tighter 10-per-minute limit applies -- which shows up honestly as `authenticated: false` on
    the lookup and, if the limit is hit, as a NOT_LOOKED section. Raising here would turn a
    misconfigured secret into a dead brief instead of a degraded one.
    """
    direct = os.environ.get("GITHUB_TOKEN", "").strip()
    if direct:
        return direct

    arn = os.environ.get("GITHUB_TOKEN_SECRET_ARN", "").strip()
    if not arn:
        return None
    try:
        import boto3

        # THE REGION COMES FROM THE ARN, not from the client's default. A secret is a regional
        # resource and nothing requires it to live where this function runs -- a default client
        # would look in the Lambda's own region and report the secret as absent, which this code
        # turns into "unauthenticated" rather than into an error. So the failure would be a
        # quieter brief with no explanation anywhere. Field 3 of an ARN is its region.
        parts = arn.split(":")
        region = parts[3] if len(parts) > 4 and parts[3] else None
        client = boto3.client("secretsmanager", region_name=region)
        secret = client.get_secret_value(SecretId=arn)
    except Exception:
        return None

    raw = (secret.get("SecretString") or "").strip()
    if not raw:
        return None
    # A secret may hold the bare token or a JSON object with it under a conventional key. Both
    # are accepted because both are what people actually store, and guessing wrong would leave a
    # correctly-configured secret looking absent.
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return raw
        for key in ("GITHUB_TOKEN", "github_token", "token"):
            value = str(parsed.get(key) or "").strip()
            if value:
                return value
        return None
    return raw


def lookup(
    repository: str,
    *,
    search_terms: str = "",
    labels: tuple[str, ...] = (),
    state: str = "open",
    min_comments: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> IssueLookup:
    """Run one issue search. Never raises; every failure is a `NOT_LOOKED` with a reason.

    The rate-limit headers are read and passed to `classify_response` because they are what
    separates "this repository has no matching issues" from "we were refused": a 403 with
    `x-ratelimit-remaining: 0` is a primary limit and a 403 with `retry-after` is a secondary
    one. Both must read as not-looked.
    """
    slug = repository_slug(repository)
    if slug is None:
        return not_looked(
            None,
            "",
            f"{repository!r} is not a GitHub repository, so there is no issue search to run. "
            "This component's issues are UNKNOWN, not absent.",
            authenticated=bool(_token()),
        )

    query = build_query(
        slug,
        search_terms=search_terms,
        labels=labels,
        state=state,
        min_comments=min_comments,
    )
    url = search_url(query, limit)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200) or 200
            remaining = response.headers.get("x-ratelimit-remaining")
            retry_after = response.headers.get("retry-after")
    except urllib.error.HTTPError as exc:
        # An HTTPError carries the status, the headers and the body -- exactly what the
        # rate-limit reading needs. Classified rather than reported as an error.
        return classify_response(
            slug,
            query,
            exc.code,
            exc.read().decode("utf-8", errors="replace") if exc.fp else "",
            authenticated=bool(token),
            rate_limit_remaining=exc.headers.get("x-ratelimit-remaining"),
            retry_after=exc.headers.get("retry-after"),
            limit=limit,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return not_looked(
            slug,
            query,
            f"the issue search could not be performed: {type(exc).__name__}: {exc}. This "
            "component's issues are UNKNOWN, not absent.",
            authenticated=bool(token),
        )

    return classify_response(
        slug,
        query,
        status,
        body,
        authenticated=bool(token),
        rate_limit_remaining=remaining,
        retry_after=retry_after,
        limit=limit,
    )


def record(session_id: str, result: IssueLookup) -> tuple[str, ...]:
    """Record one issue per object, plus the lookup receipt. Returns the issue digests.

    The per-issue objects go through `fetch_source.record_evidence`, so they land under the same
    prefix, in the same shape, with the same encryption as every other recorded body -- which is
    what lets `load_recorded_pages` rebuild one PageSet covering pages and issues alike.
    """
    digests: list[str] = []
    for issue in result.issues:
        digests.append(
            fetch_source.record_evidence(
                session_id,
                issue.url,
                f"issue-{issue.number}",
                200,
                issue.recorded_body(),
            )
        )
    _record_receipt(session_id, result)
    return tuple(digests)


def _record_receipt(session_id: str, result: IssueLookup) -> None:
    """Write the receipt that says whether this search actually ran.

    Keyed by a digest of the query and the outcome, so repeated identical lookups collapse onto
    one object while a lookup that answered differently is its own record.

    A receipt that cannot be written must not fail the tool -- the agent still needs its answer,
    and the consequence is bounded: `_cross_check_issues` receives None instead of False and
    changes nothing, which is the same as the behaviour before receipts existed.
    """
    payload = {
        "outcome": str(result.outcome),
        "repository": result.repository,
        "query": result.query,
        "reason": result.reason,
        "http_status": result.http_status,
        "authenticated": result.authenticated,
        "issues_returned": len(result.issues),
        "total_count": result.total_count,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    key = fetch_source.evidence_key(session_id, digest).replace(".json", LOOKUP_SUFFIX)
    try:
        import boto3

        boto3.client("s3").put_object(
            Bucket=os.environ["DOCUMENT_BUCKET"],
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
    except Exception:
        return


def recorded_lookups(session_id: str) -> tuple[dict[str, Any], ...]:
    """Every lookup receipt for one session. The input to the issue cross-check.

    Returns an empty tuple when nothing could be read, which the cross-check treats as "no
    evidence either way" rather than as "no search ran" -- an unreadable prefix must not
    downgrade a section the agent answered honestly.
    """
    import boto3

    bucket = os.environ.get("DOCUMENT_BUCKET", "").strip()
    if not bucket:
        return ()
    client = boto3.client("s3")
    prefix = f"{fetch_source.EVIDENCE_PREFIX}session={session_id}/"
    receipts: list[dict[str, Any]] = []
    token: str | None = None
    try:
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for obj in page.get("Contents") or []:
                if not obj["Key"].endswith(LOOKUP_SUFFIX):
                    continue
                raw = client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                receipts.append(json.loads(raw))
            token = page.get("NextContinuationToken")
            if not token:
                break
    except Exception:
        return ()
    return tuple(receipts)


def any_search_ran(session_id: str) -> bool | None:
    """Whether any issue search completed. None when there is no evidence either way.

    Three-valued deliberately, and the None is the important one: `False` is a claim that every
    attempt failed, which is only sayable when receipts were actually read. Returning False for
    an unreadable prefix would downgrade an honest section on the strength of an S3 error.

    >>> any_search_ran.__doc__ is not None
    True
    """
    receipts = recorded_lookups(session_id)
    if not receipts:
        return None
    return any(
        receipt.get("outcome") != str(IssueLookupOutcome.NOT_LOOKED) for receipt in receipts
    )


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Search one repository's issues, record what came back, and return it to the agent.

    Invoked by the Gateway as an MCP tool, so the input is the tool's arguments and the output is
    what the agent reads. Flat on purpose: a tool whose result has to be unwrapped is a tool
    used wrongly.

    THE RESULT LEADS WITH `looked`, and that is the field the tool description tells the agent
    to check first. An empty `issues` list with `looked: false` means the search did not happen,
    and the agent is instructed to report the section as not-produced rather than as clean.
    """
    repository = str(event.get("repository", "")).strip()
    session_id = str(event.get("session_id") or event.get("sessionId") or "unscoped").strip()
    if not repository:
        return {
            "looked": False,
            "outcome": str(IssueLookupOutcome.NOT_LOOKED),
            "reason": "no repository was supplied",
            "issues": [],
        }

    labels = tuple(
        part.strip()
        for part in str(event.get("labels", "")).split(",")
        if part.strip()
    )
    try:
        min_comments = max(0, int(event.get("min_comments") or 0))
    except (TypeError, ValueError):
        min_comments = 0

    result = lookup(
        repository,
        search_terms=str(event.get("search_terms", "")).strip(),
        labels=labels,
        state=str(event.get("state", "open")).strip() or "open",
        min_comments=min_comments,
    )
    record(session_id, result)

    return {
        # FIRST, because it is the field that must not be missed.
        "looked": result.looked,
        "outcome": str(result.outcome),
        "reason": result.reason,
        "repository": result.repository,
        "query": result.query,
        "http_status": result.http_status,
        "authenticated": result.authenticated,
        "total_matching": result.total_count,
        "truncated": result.truncated,
        "issues": [
            {
                "number": issue.number,
                "title": issue.title,
                "url": issue.url,
                "state": issue.state,
                "labels": list(issue.labels),
                "comments": issue.comments,
                "reactions": issue.reactions,
                "created_at": issue.created_at,
                "updated_at": issue.updated_at,
                "trail": issue.trail,
                "body": issue.body,
                # What to cite for a quote from this issue. Stated rather than left to be
                # inferred, because the quote check is exact on both parts.
                "cite_url": issue.url,
                "cite_revision": f"issue-{issue.number}",
            }
            for issue in result.issues
        ],
        "note": (
            "Every issue above is recorded. Quote a contiguous span of a title or body exactly, "
            "and cite it with cite_url and cite_revision; every quote is checked against these "
            "recorded bytes. If looked is false, do NOT report an absence of issues -- report "
            "the section as one that could not be produced, with this reason."
        ),
    }
