"""The brief agent's proxy: invoke ITS Harness for one component, verify, write the partial.

This is the PROXY, and it exists for the reason `research_harness.py` does. Two facts force it:

  * A harness-managed runtime REFUSES `InvokeAgentRuntime` -- "is managed by a harness and
    cannot be invoked directly. Use the InvokeHarness API".
  * Step Functions has no `invokeHarness` SDK integration.

So the pipeline invokes a Lambda, and this is that Lambda. **It is a different Harness from the
research one**, with its own ARN, its own prompt and its own two-plus-one tools -- see
`brief/prompt.py` for why sharing the research agent would erode the discipline every stored
fact depends on.

WHY THE SESSION ID IS MINTED HERE. Both tools record under `research-evidence/session=<id>/`,
and the verification below rebuilds the evidence from that prefix. The two only meet if the same
id reaches both, so it is generated once here and stated in the prompt for the agent to echo onto
every tool call.

ONE SESSION PER COMPONENT, not per run. Shared evidence would let `PageSet.any_contains` find
one component's quote in another component's release notes and report WRONG_PAGE where ABSENT is
the truth.

WHAT THIS HANDLER DOES BEYOND CALLING THE AGENT, and why it is not the agent's job:

    validates      the returned object against `BriefExtraction`, re-asking on failure while
                   the conversation is still open. The schema reaches the model as TEXT -- both
                   API-level mechanisms return 400 on this Bedrock endpoint -- so nothing makes
                   it obey, and catching a malformed object HERE is what lets it be fixed.
    verifies       every quote against the bytes recorded before the agent read them.
    cross-checks   whether any issue search actually ran, from the recorded receipts, so a
                   rate-limited query cannot be reported as "no known issues".
    writes         one `ComponentBrief` partial, so the run document is assembled from
                   per-component objects rather than one Lambda holding every component's work.

IT NEVER FAILS. Every failure -- no ARN, an unreachable Harness, an object that will not
validate after the re-asks -- returns a partial whose five sections read NOT_LOOKED with the
reason. The brief is advisory, and a run that failed because a model was throttled would be the
blocking behaviour this whole design refuses.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from ..brief.boundary import BriefExtraction
from ..brief.document import (
    ComponentBrief,
    component_brief,
    unbriefable_reason,
)
from ..brief.prompt import REASK, user_prompt
from ..brief.ruleset import ruleset_digest
from ..brief.walk import ReleaseWalk, plan_walk, releases_from_listing
from ..order.plan import PlanStep
from ..research.pages import PageSet
from ..research.sources.web_docs import HttpSourceReader, SourceScope
from ..storage import build_uri, write_document
from ..storage.keys import component_brief_key
from . import fetch_issues, fetch_source

# The Harness to invoke. Injected by Terraform: nothing here derives an ARN, so a rename cannot
# leave this pointing at a resource that used to exist. DELIBERATELY NOT `RESEARCH_HARNESS_ARN`
# -- a shared variable name is how two agents end up being one agent by accident.
HARNESS_ARN = os.environ.get("BRIEF_HARNESS_ARN", "").strip()

# AgentCore rejects a shorter session id with a 400. Padded rather than truncated, so a short
# component name cannot produce an invalid id.
MIN_SESSION_ID_LENGTH = 33

# How many times the object is re-asked after a CONTRACT failure. Counts malformed objects, not
# sources read, so it stays small: a repeated schema failure is a real problem to surface rather
# than something to grind at. Matches the research proxy's default, for the same measured reason
# -- one shortened field name there cost a whole component its fact.
MAX_ATTEMPTS = max(1, int(os.environ.get("READINESS_BRIEF_MAX_ATTEMPTS", "4")))

# One page of the release listing. 100 is GitHub's maximum, and one page is deliberate: a second
# is another core call for a component whose fortieth-most-recent release is almost never on the
# hop. A release on the walk that is missing from the page shows up as an unread minor.
RELEASES_PER_PAGE = 100

# Read timeout on the Harness call. TIMEOUTS ARE REQUIRED, NOT A NICETY: botocore's default read
# timeout is 60 SECONDS, and one component's brief inside the Harness takes minutes -- it fetches
# several release-note pages, runs an issue search and reasons over all of it. A default client
# aborts a call that is working. Bounded by the Lambda's own 900s ceiling. RETRIES ARE OFF: a
# retried `InvokeHarness` re-runs the whole loop, so a slow-but-working call would be abandoned
# and charged twice.
READ_TIMEOUT_SECONDS = 890
CONNECT_TIMEOUT_SECONDS = 15


class BriefHarnessError(RuntimeError):
    """The Harness could not be reached, or returned nothing usable."""


def _session_id(component_id: str, at: datetime) -> str:
    """The evidence prefix for one component's brief, legible in an S3 listing."""
    safe = re.sub(r"[^a-zA-Z0-9-]", "-", component_id)[:24]
    base = f"brief-{safe}-{at.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}"
    return base.ljust(MIN_SESSION_ID_LENGTH, "0")


# ---------------------------------------------------------------------------
# THE WALK, WHICH IS COVERAGE THE AGENT OWES RATHER THAN A PAGE LIST
# ---------------------------------------------------------------------------


def _walk_for(
    installed: str, recommended: str, known_releases: tuple[str, ...], repository: str | None
) -> tuple[ReleaseWalk | None, str]:
    """Plan the hop, widening the known releases with a listing when one can be read.

    The listing is read HERE rather than left to the agent because the walk has to exist before
    the prompt does -- the prompt states which minors must be accounted for, and that obligation
    is what stops the agent reading only the target release's notes. The agent then fetches
    whatever pages it needs, including this listing again if it wants the bodies.

    The fact store's recorded releases are a floor, not a census: a component researched three
    months ago has released since, and the recommended version may not appear in the fact at all.
    """
    available = set(known_releases)
    note = ""
    if repository:
        payload, why_not = _fetch_listing(repository)
        if payload is None:
            note = why_not
        else:
            listed, parse_error = releases_from_listing(payload)
            if parse_error is not None:
                note = f"{parse_error} ({len(payload)} characters returned)"
            else:
                available |= {release.version for release in listed}
    return plan_walk(installed, recommended, tuple(available)), note


def _fetch_listing(repository: str) -> tuple[str | None, str]:
    """The release listing, through the scoped reader. Returns `(payload, why_not)`.

    Through `HttpSourceReader` rather than a bare urlopen, so it inherits the https-only rule,
    the size cap and the host scoping the research stage already enforces. NOT recorded here:
    this copy exists only to enumerate version numbers for the walk, nothing is quoted from it,
    and recording a body that no citation can reference would put an unquotable object in the
    evidence prefix.
    """
    from ..brief.issues import repository_slug

    slug = repository_slug(repository)
    if slug is None:
        return None, f"{repository!r} is not a GitHub repository, so no release listing was read"

    scope = SourceScope(canonical_repository=repository)
    scope.permit_repository(repository)
    url = f"https://api.github.com/repos/{slug}/releases?per_page={RELEASES_PER_PAGE}"
    result = HttpSourceReader(scope).fetch(url, revision="")
    if not result.ok:
        return None, (
            f"the release listing at {url} could not be read: "
            f"{result.reason or f'HTTP {result.status}'}"
        )
    return result.body, ""


# ---------------------------------------------------------------------------
# THE HARNESS CALL
# ---------------------------------------------------------------------------


def _extract_text(response: dict[str, Any]) -> str:
    """Concatenate the assistant text out of a converse-style event stream.

    Tolerant of shape on purpose: the stream carries tool-use and metadata events this handler
    has no interest in, and a new event type must not break the run.
    """
    chunks: list[str] = []
    for event in response.get("stream", []):
        if not isinstance(event, dict):
            continue
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            chunks.append(delta["text"])
        for block in event.get("message", {}).get("content", []) or []:
            if isinstance(block, dict) and "text" in block:
                chunks.append(block["text"])
    return "".join(chunks)


def _parse_object(text: str) -> dict[str, Any]:
    """Pull the one JSON object out of the agent's answer.

    The prompt asks for a bare object, but a model that wraps it in a fence or adds a sentence
    has still done the work -- so the outermost brace-balanced span is taken rather than failing
    over punctuation. No object at all is a real failure and raises.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)

    start = stripped.find("{")
    if start == -1:
        raise BriefHarnessError(f"no JSON object in the Harness response: {text[:400]!r}")

    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(stripped[start:], start):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return json.loads(stripped[start : index + 1])

    raise BriefHarnessError("the Harness response contains an unterminated JSON object")


def _validation_summary(exc: Any) -> str:
    """The first few validation errors, named by field.

    A wall of identical messages buries the field name that matters, and the field name is the
    only actionable part of a re-ask.
    """
    errors = exc.errors()[:4]
    summary = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors
    )
    if len(exc.errors()) > len(errors):
        summary += f" (and {len(exc.errors()) - len(errors)} more)"
    return summary


def run_agent(session_id: str, prompt: str) -> BriefExtraction:
    """Invoke the Harness and return an extraction that VALIDATES, re-asking if it does not.

    THE PROXY VALIDATES BEFORE ANYTHING ELSE DOES, and that is this function's whole shape. The
    schema is advisory: Bedrock rejects both mechanisms that would enforce it
    (`output_config.format` and `tools[].strict` each return 400 on this endpoint), so it reaches
    the model as text in the prompt and nothing makes the model obey it. `BriefExtraction`
    catches a malformed object -- but catching it here, while the conversation is still open, is
    what lets it be fixed instead of costing the component its brief.

    The re-ask carries the validation error itself, because "your object was malformed" is not
    actionable and "empty_sections: DEPRECATIONS came back empty and is not in empty_sections"
    is.
    """
    import boto3
    from botocore.config import Config
    from pydantic import ValidationError

    client = boto3.client(
        "bedrock-agentcore",
        config=Config(
            read_timeout=READ_TIMEOUT_SECONDS,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            retries={"max_attempts": 0},
        ),
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": prompt}]}]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = client.invoke_harness(
            harnessArn=HARNESS_ARN, runtimeSessionId=session_id, messages=messages
        )
        text = _extract_text(response)
        if not text.strip():
            raise BriefHarnessError("the Harness returned no text")

        candidate = _parse_object(text)
        try:
            return BriefExtraction.model_validate(candidate)
        except ValidationError as exc:
            summary = _validation_summary(exc)
            if attempt >= MAX_ATTEMPTS:
                raise BriefHarnessError(
                    f"the brief still did not match the contract after {MAX_ATTEMPTS} "
                    f"attempt(s): {summary}"
                ) from exc
            # The model's own reply stays in the history, so it can see what it wrote.
            messages.append({"role": "assistant", "content": [{"text": text}]})
            messages.append({"role": "user", "content": [{"text": REASK + summary}]})

    raise BriefHarnessError("the Harness produced no valid brief")


# ---------------------------------------------------------------------------
# ONE COMPONENT
# ---------------------------------------------------------------------------


def brief_one(
    step: PlanStep,
    *,
    installed_version: str,
    repository: str | None,
    documentation: tuple[str, ...],
    chart_name: str | None,
    known_releases: tuple[str, ...],
    target_kubernetes: str | None,
    now: datetime,
    agent: Any = None,
    event_reuse_note: str | None = None,
) -> tuple[ComponentBrief, str]:
    """One component's brief. Returns `(brief, failure_reason)`; the reason is empty on success.

    `agent` is injectable so the whole flow -- walk, prompt, validation, verification, the issue
    cross-check, the document -- is exercisable with no Harness and no network. It is called as
    `agent(session_id, prompt)` and must return a `BriefExtraction` or raise.
    """
    session_id = _session_id(str(step.component), now)
    recommended = str(step.recommended_version or step.to_series)
    walk, listing_note = _walk_for(installed_version, recommended, known_releases, repository)

    prompt = user_prompt(
        session_id,
        str(step.component),
        installed_version,
        recommended,
        at_kubernetes=step.at_kubernetes,
        target_kubernetes=target_kubernetes,
        repository=repository,
        documentation=documentation,
        chart_name=chart_name,
        releases_on_walk=walk.hops if walk else (),
        minors_entered=walk.minors_entered if walk else (),
        as_of_date=now.date().isoformat(),
    )

    invoke = agent or run_agent
    extraction: BriefExtraction | None = None
    reason = ""
    try:
        if agent is None and not HARNESS_ARN:
            raise BriefHarnessError("BRIEF_HARNESS_ARN is unset")
        extraction = invoke(session_id, prompt)
    except Exception as exc:
        # BROAD ON PURPOSE. The failures worth surviving are every way a Harness call can go
        # wrong -- a throttle, a timeout, an object that will not validate after the re-asks, an
        # SDK that is not installed -- and naming them would import the SDK into this module
        # while still missing the next one. The consequence is bounded and stated: five
        # NOT_LOOKED sections carrying this exception's type and message, and the run continues.
        reason = (
            f"the brief agent produced nothing usable for this step: "
            f"{type(exc).__name__}: {exc}. What these releases change, and what to watch for, "
            "is UNKNOWN rather than empty."
        )
        if listing_note:
            reason += f" (Release listing: {listing_note}.)"

    # READ THE RECORDED BYTES BACK. The quote check is only worth running against bytes that were
    # persisted before anything read them; a PageSet held in memory can only be trusted by the
    # process holding it -- and here the process that fetched was the Harness, not this one.
    pages = PageSet()
    searches_ran: bool | None = None
    if extraction is not None:
        try:
            pages = fetch_source.load_recorded_pages(session_id)
            searches_ran = fetch_issues.any_search_ran(session_id)
        except Exception as exc:
            # Evidence that cannot be read means every quote reports NOT_FETCHED, which is the
            # honest verdict and exactly what an unverifiable brief should look like. It does not
            # discard the agent's work: a reader still sees the bullets, marked unverified.
            reason = (
                f"the brief was produced but its recorded evidence could not be read back "
                f"({type(exc).__name__}: {exc}), so no quote in it is verified."
            )

    return (
        component_brief(
            step,
            walk=walk,
            extraction=extraction,
            pages=pages,
            session_id=session_id,
            observed_at=now.isoformat(),
            reuse_note=str(event_reuse_note or "researched fresh"),
            failure_reason=reason,
            issue_searches_ran=searches_ran,
        ),
        reason,
    )


# ---------------------------------------------------------------------------
# THE HANDLER
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Brief ONE component and write its partial. One invocation per plan step.

    ONE COMPONENT PER INVOCATION, so the Map fans out and a slow or unreadable project delays
    nothing else -- the same reason Stage 6 is invoked once per component rather than once per
    run, and the reason a 900-second Lambda can cover a plan with a dozen moves.

    Required in `event`: `step` (a `PlanStep` as JSON), `plan_digest`, `cluster_name`, and
    `bucket` or DOCUMENT_BUCKET. Optional: `installed_version`, `repository`, `documentation`,
    `chart_name`, `known_releases`, `target_kubernetes_version`.

    A failure is RETURNED, not raised: the Map has no catch on this state, and one component
    whose sources are unreadable must not discard the briefs that succeeded beside it.
    """
    now = datetime.now(UTC)
    step = PlanStep.model_validate(event["step"])

    unbriefable = unbriefable_reason(step)
    if unbriefable is not None:
        return {
            "stage": "brief-component",
            "step_order": step.order,
            "component": step.component,
            "briefed": False,
            "reason": unbriefable,
            "blocks_pipeline": False,
        }

    brief, reason = brief_one(
        step,
        installed_version=str(event.get("installed_version") or step.from_series or ""),
        repository=(str(event["repository"]).strip() if event.get("repository") else None),
        documentation=tuple(str(d) for d in (event.get("documentation") or ())),
        chart_name=(str(event["chart_name"]) if event.get("chart_name") else None),
        known_releases=tuple(str(v) for v in (event.get("known_releases") or ())),
        target_kubernetes=(
            str(event["target_kubernetes_version"])
            if event.get("target_kubernetes_version")
            else None
        ),
        now=now,
        agent=None,
        event_reuse_note=(
            str(event["reuse_note"]) if event.get("reuse_note") else None
        ),
    )

    # A BRIEF THAT PRODUCED NOTHING IS NOT STORED, and this is a correctness rule rather than a
    # tidiness one. Stored briefs are served again for a week (`brief/reuse.py`), so caching an
    # all-NOT_LOOKED brief would have the next six days of runs serve that FAILURE instead of
    # retrying -- the cache would poison itself with a transient throttle or a bad IAM grant and
    # keep answering "unknown" long after the cause was fixed. Observed live: an InvokeHarness 403
    # produced exactly such a brief and stored it.
    #
    # It is still RETURNED for this run, so the report says what happened. Only the reusable copy
    # is withheld.
    if brief.all_sections_unavailable:
        uri, not_written = None, (
            "this brief produced no section at all, so it was NOT stored for reuse: a cached "
            "failure would be served for the next week instead of retried. It is still on the "
            "payload for this run, so the report explains the absence."
        )
    else:
        uri, not_written = _store(brief, event, now)

    return {
        "stage": "brief-component",
        "step_order": brief.step_order,
        "component": brief.component,
        # True when a brief object exists, whatever its sections say. Absence of a brief and a
        # brief full of NOT_LOOKED are different things and a caller reads them differently.
        "briefed": True,
        "sections_reported": sum(1 for s in brief.sections if s.items),
        "sections_not_looked": sum(
            1 for s in brief.sections if s.state.value == "NOT_LOOKED"
        ),
        "all_sections_unavailable": brief.all_sections_unavailable,
        "unverified_quotes": brief.unverified,
        "actions": len(brief.actions),
        "minors_unread": list(brief.coverage.minors_unread if brief.coverage else ()),
        "evidence_session": brief.evidence_session,
        # NAMED `brief_uri`, which is what the assembler reads. Not `partial_uri`: the object is a
        # complete brief for one upgrade, stored so any later run making the same move can serve it,
        # and calling it a partial invited the reading that it is a fragment of this run.
        "brief_uri": uri,
        "blocks_pipeline": False,
        **({"reason": reason} if reason else {}),
        **({} if uri else {"brief_not_stored": not_written}),
    }


def _store(
    brief: ComponentBrief, event: dict[str, Any], now: datetime
) -> tuple[str | None, str | None]:
    """Store one component's brief under the UPGRADE it describes. Returns `(uri, why_not)`.

    KEYED ON THE COMPONENT AND THE VERSION PAIR, not on this run. That is what makes the object
    findable by any later run making the same move -- see `storage/keys.py::component_brief_key` and
    `brief/reuse.py`. It carries no cluster and no plan digest, because a brief about a version hop
    is about neither.

    NON-FATAL, for `stages/freshness.py::_publish`'s reason applied end to end: this document is
    advisory, so "the brief could not be stored" must never become "this cluster gets no upgrade
    approval". The consequence of a failed write is bounded and stated -- the brief is still
    returned to the assembler on the payload, so the run document still carries it; only the reuse
    copy is lost, and the next run researches it again.
    """
    bucket = str(event.get("bucket") or os.environ.get("DOCUMENT_BUCKET", "")).strip()
    if not bucket:
        return None, (
            "no bucket was supplied and DOCUMENT_BUCKET is unset, so this component's brief was "
            "produced but not stored and cannot be reused"
        )
    uri = build_uri(
        bucket,
        component_brief_key(
            brief.component,
            str(brief.from_series or "unknown"),
            str(brief.recommended_version or brief.to_series),
            ruleset_digest(),
            brief.digest_of_self(),
            now.isoformat(),
        ),
    )
    try:
        return write_document(uri, brief), None
    except Exception as exc:
        return None, (
            f"the brief could not be stored at {uri}: {type(exc).__name__}: {exc}. It is still on "
            "the payload for this run; only the reusable copy is lost."
        )
