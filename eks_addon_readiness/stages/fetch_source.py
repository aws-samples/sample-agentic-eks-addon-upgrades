"""The source-fetch tool, behind the research Gateway. Records every byte it serves.

**This module is what keeps hole #4 closed after the move to Harness.** It is worth being
explicit about why, because the naive conversion loses it.

The defect this prevents: a judging layer that cannot compare a quote against the page it
cites, because the only HTTP client lives inside the model's tool loop and retains nothing.
A quote spliced from two commits of one README then passes its check. The fix is that every
fetched body is
recorded and the judging layer checks every quote against those recorded bytes.

Moving the model loop to a managed Harness would have broken that, if fetching had become
something the platform does on the agent's behalf and the bodies never reached my code. So
fetching stays a tool **I own**: a Lambda behind the Gateway. It fetches, writes the body and
its digest to object storage, and returns the body to the model along with the digest. The
judging layer then reads the recorded bodies back by digest.

That is not merely as good as the in-process `PageSet`; it is better. The evidence is
persisted out-of-band before the model ever sees it, so it survives the process, and the
receipt's `evidence_digest` cites bytes a third party can fetch and re-hash. In-process
recording could only ever be trusted by the process doing the recording.

Scoping, size caps and https-only come from `research/sources/web_docs.py` unchanged -- the
`SourceReader` port exists exactly so the fetching rules are host-independent.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from ..research.sources.web_docs import HttpSourceReader, SourceScope

# Where recorded bodies land. One object per (url, revision), keyed by the digest of the
# body, so the same page fetched twice in a session is one object and a quote check can
# address it by content rather than by name.
EVIDENCE_PREFIX = "research-evidence/"


def _bucket() -> str:
    bucket = os.environ.get("DOCUMENT_BUCKET", "").strip()
    if not bucket:
        raise RuntimeError(
            "DOCUMENT_BUCKET is unset, so a fetched body cannot be recorded. Refusing to "
            "serve a page whose bytes nothing would retain: an unrecorded body is a quote "
            "nothing can verify."
        )
    return bucket


def _s3():
    import boto3

    return boto3.client("s3")


def evidence_key(session_id: str, body_digest: str) -> str:
    """Where one recorded body lives.

    Partitioned by session so one research run's evidence is one prefix the judging layer can
    list, and named by content digest so the key itself is a checkable claim about the bytes.

    >>> evidence_key("s-1", "sha256:" + "ab" * 32)
    'research-evidence/session=s-1/abababababababab.json'
    """
    short = body_digest.removeprefix("sha256:")[:16]
    return f"{EVIDENCE_PREFIX}session={session_id}/{short}.json"


def record_evidence(
    session_id: str, url: str, revision: str, status: int, body: str
) -> str:
    """Write one fetched body to durable storage and return its content digest.

    EXTRACTED FROM `handler` SO A SECOND CAPABILITY CAN RECORD THE SAME WAY, not to shorten
    it. The upgrade brief (`stages/upgrade_brief.py`) fetches release notes and an issue
    search payload, and both must land in `research-evidence/` before anything reads them --
    that ordering is the entire reason a quote can be checked against bytes the reader could
    not influence. Reimplementing the write beside it would have produced a second key shape,
    a second encoding, and a second thing to get the encryption wrong in.

    Callers, all of them:

        `handler` below                     one pinned page, served to the model
        `stages/upgrade_brief.py`           one recorded body per release, and the issue
                                            search payload

    Returns the digest rather than the key, because the digest is what a citation carries and
    `evidence_key` derives the key from it. `load_recorded_pages` reads every object under the
    session prefix back, so anything recorded here is verifiable by anything that verifies
    quotes.
    """
    body_digest = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
    _s3().put_object(
        Bucket=_bucket(),
        Key=evidence_key(session_id, body_digest),
        Body=json.dumps(
            {
                "url": url,
                "revision": revision,
                "status": status,
                "digest": body_digest,
                "body": body,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    return body_digest


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Fetch one pinned source, record it, and return it to the model.

    Invoked by the Gateway as an MCP tool, so the input is the tool's arguments and the
    output is what the model reads. Both shapes are flat on purpose: a tool whose result the
    model has to unwrap is a tool it uses wrongly.

    A refusal is a successful return with `refused: true`, not an error. The model needs to
    be told it cannot read that host so it looks where the component actually publishes, and
    the refusal is recorded alongside the fetches so provenance stays answerable afterwards.
    """
    url = str(event.get("url", "")).strip()
    revision = str(event.get("revision", "")).strip()
    session_id = str(event.get("session_id") or event.get("sessionId") or "unscoped").strip()
    repository = str(event.get("canonical_repository", "")).strip() or None
    documentation = tuple(
        str(d) for d in (event.get("official_documentation") or ()) if str(d).strip()
    )

    if not url:
        return {"refused": True, "reason": "no url was supplied"}

    scope = SourceScope(
        canonical_repository=repository, official_documentation=documentation
    )
    # A GitHub repository in the request widens the scope to the hosts that serve it, which is
    # how a first fetch for a new component becomes possible without permitting arbitrary
    # hosts. `permit_repository` accepts only a GitHub repository URL.
    if repository:
        scope.permit_repository(repository)

    result = HttpSourceReader(scope).fetch(url, revision)

    if result.refused:
        _record_refusal(session_id, result)
        return {
            "refused": True,
            "url": result.url,
            "revision": result.revision,
            "reason": result.reason,
        }
    if not result.ok:
        return {
            "refused": False,
            "error": True,
            "url": result.url,
            "revision": result.revision,
            "reason": result.reason or f"HTTP {result.status}",
        }

    body_digest = record_evidence(
        session_id, result.url, result.revision, result.status, result.body
    )

    return {
        "refused": False,
        "url": result.url,
        "revision": result.revision,
        "status": result.status,
        # Returned to the model so it can cite the exact bytes it read. The judging layer
        # reads the same object back and checks every quote against it.
        "body_digest": body_digest,
        "characters": len(result.body),
        "body": result.body,
        "note": (
            "These bytes are recorded. Quote a contiguous span of this text exactly; every "
            "quote is checked against this recording byte-for-byte."
        ),
    }


def _record_refusal(session_id: str, result) -> None:
    """Record that a fetch was refused, so provenance is answerable after the fact.

    Without retained refusals, the diagnosis must leave open whether a third-party page was
    fetched or invented. This closes that question for good.
    """
    digest = "sha256:" + hashlib.sha256(
        f"refused:{result.url}:{result.revision}".encode()
    ).hexdigest()
    try:
        _s3().put_object(
            Bucket=_bucket(),
            Key=evidence_key(session_id, digest).replace(".json", ".refused.json"),
            Body=json.dumps(
                {
                    "url": result.url,
                    "revision": result.revision,
                    "refused": True,
                    "reason": result.reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
    except Exception:
        # A refusal that could not be recorded must not fail the fetch tool: the model still
        # needs to be told it cannot read that host. The refusal is in the tool result either
        # way; only the durable copy is lost.
        return


def load_recorded_pages(session_id: str) -> Any:
    """Rebuild the `PageSet` for one session from the recorded bodies.

    This is the other half of the hole-#4 fix under Harness: the judging layer gets its
    evidence from the recording rather than from process memory, so it checks quotes against
    bytes that were persisted before the model saw them.
    """
    from ..research.pages import FetchedPage, PageSet

    pages = PageSet()
    client = _s3()
    token: str | None = None
    prefix = f"{EVIDENCE_PREFIX}session={session_id}/"
    while True:
        kwargs: dict[str, Any] = {"Bucket": _bucket(), "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            # `.refused.json` records a fetch that was declined; `.lookup.json` records the
            # OUTCOME of an issue search rather than a page that was read. Neither is a body
            # anything may quote from, so neither enters the PageSet -- a receipt in here would
            # let a quote "verify" against a description of a search.
            if obj["Key"].endswith((".refused.json", ".lookup.json")):
                continue
            raw = client.get_object(Bucket=_bucket(), Key=obj["Key"])["Body"].read()
            record = json.loads(raw)
            pages.record(
                FetchedPage(
                    url=record["url"],
                    revision=record["revision"],
                    body=record["body"],
                    status=record.get("status", 200),
                )
            )
        token = page.get("NextContinuationToken")
        if not token:
            break
    return pages
