"""Stage 6 through the Harness: invoke the model loop, then hand its output to the judge.

This is the PROXY, and it exists because Step Functions cannot call a Harness. Two facts
force it:

  * A harness-managed runtime REFUSES `InvokeAgentRuntime` -- "is managed by a harness and
    cannot be invoked directly. Use the InvokeHarness API".
  * Step Functions has no `invokeHarness` SDK integration. `aws-sdk:bedrockagentcore:
    invokeHarness` is not a recognised resource ARN.

So the pipeline keeps invoking a Lambda, and this is that Lambda. It holds no model client
and no judging code: it mints a session, calls `InvokeHarness`, pulls the extraction out of
the response stream, and invokes the judge.

WHY THE SESSION ID IS MINTED HERE. The fetch tool records every body under
`research-evidence/session=<id>/`, and the judge rebuilds the PageSet from that prefix. The
two only meet if the same id reaches both, so it is generated once here, put in the user
message for the model to echo onto each `fetch_source` call, and passed to the judge
directly. AgentCore requires at least 33 characters.

WHAT THIS HANDLER DELIBERATELY DOES NOT DO: derive a window, check a quote, decide an
acceptance, or write a fact. All of that is the judge's, in its own deployable.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ConnectionError as BotoConnectionError
from pydantic import ValidationError
from urllib3.exceptions import ProtocolError

from ..research.boundary import ResearchExtraction

# The Harness to invoke, and the judge to hand its output to. Both injected by Terraform:
# nothing here derives an ARN, so a rename cannot leave this pointing at a resource that
# used to exist.
HARNESS_ARN = os.environ.get("RESEARCH_HARNESS_ARN", "").strip()
JUDGE_FUNCTION = os.environ.get("JUDGE_FUNCTION_NAME", "").strip()

# AgentCore rejects a shorter session id with a 400. Padded rather than truncated, so a
# short component name cannot produce an invalid id.
MIN_SESSION_ID_LENGTH = 33


# TIMEOUTS ARE REQUIRED, NOT A NICETY, and this was found by a live run rather than by
# reading. botocore's default read timeout is 60 SECONDS. One component's research inside the
# Harness takes minutes -- it fetches several pages and runs a model loop over them -- so a
# default client aborts a call that is working, and the proxy reports it as
# `NO_EXTRACTION: ReadTimeoutError` even while the Harness is still producing a correct
# answer.
#
# Bounded by the Lambda's own 900s ceiling: waiting longer than the invocation can live
# achieves nothing. RETRIES ARE OFF on the harness call deliberately -- a retried
# `InvokeHarness` re-runs the whole model loop, so a slow-but-working call would be
# abandoned and charged twice.
_HARNESS_CONFIG = Config(read_timeout=890, connect_timeout=15, retries={"max_attempts": 0})

# The judge is deterministic and fast, but it is still a 120s Lambda doing S3 reads, so it
# gets more than the 60s default too. Retries stay off: it writes objects, and a retry after
# a response was actually produced would write a second candidate for one research run.
_JUDGE_CONFIG = Config(read_timeout=180, connect_timeout=15, retries={"max_attempts": 0})


# How many times the extraction is re-asked after a CONTRACT failure. It counts malformed
# objects, not sources read, so it stays small: a repeated schema failure is a real problem
# to surface rather than something to grind at. Matches the in-process path's default.
MAX_ATTEMPTS = max(1, int(os.environ.get("READINESS_RESEARCH_MAX_ATTEMPTS", "5")))

# A DROPPED CONNECTION IS NOT A SLOW CALL, and gets one more try where a timeout gets none.
# `retries.max_attempts=0` above is about not abandoning a working call. `ProtocolError:
# Response ended prematurely` is the opposite case: the stream closed before any answer
# arrived (the stream closed before any answer), so nothing is duplicated by asking again, and
# without the retry one dropped TCP connection costs a whole extra pipeline run for that
# component. Once, same session, same messages; a second drop is reported as before.
_DROPPED = (ProtocolError, BotoConnectionError)

# Sent back with the judge's own summary when a well-formed extraction was REJECTED. The
# three failures it names are the three observed; the fix for each is to quote less, not more.
_REASK_AFTER_JUDGE = (
    "Your previous reply was well-formed but the checker REJECTED it for the reason below. "
    "Fix ONLY what the reason names and reply again with the complete JSON object. Rules: "
    "every quote must be a verbatim span from a page you fetched IN THIS SESSION; if a page "
    "was not fetched, fetch it or drop the claim. Quote a release as the bare version tag "
    "exactly as the listing prints it (e.g. `v0.6.4`), never an install command or a "
    "sentence about it. Every release you name needs such a quote; if none can be found, "
    "omit the release. Do not invent a compatibility statement to satisfy the checker -- if "
    "none exists, keep no_compatibility_found true. The checker said: "
)

# Appended on a re-ask. It restates the one requirement the failed attempt violated and
# carries the validation error, so a near-miss recovers instead of becoming a gap. Naming
# the offending field is the point -- the observed failure was one shortened key.
_REASK = (
    "Your previous reply did not match the required output contract. Reply with ONLY one "
    "JSON object matching the schema in your instructions -- no prose, no code fence, and "
    "no field name that is not in the schema. Field names must be spelled exactly as the "
    "schema spells them; a shortened or guessed name is rejected outright. If you genuinely "
    'found no compatibility statement, still return the object with "found": true, '
    '"no_compatibility_found": true and a "not_found_reason". The validation error was: '
)


class HarnessError(RuntimeError):
    """The Harness could not be reached, or returned nothing usable."""


def _session_id(component_id: str, at: datetime) -> str:
    """A session id that is unique per invocation and legible in an S3 listing.

    Carries the component and the instant so an operator reading
    `research-evidence/session=.../` can tell what a prefix belongs to without opening it.
    """
    safe = re.sub(r"[^a-zA-Z0-9-]", "-", component_id)[:24]
    stamp = at.strftime("%Y%m%d-%H%M%S")
    base = f"research-{safe}-{stamp}-{uuid.uuid4().hex[:12]}"
    return base.ljust(MIN_SESSION_ID_LENGTH, "0")


def _chart_leads(event: dict[str, Any]) -> tuple[str | None, str | None]:
    """`(chart_name, chart_version)` for this event's Helm release, from the run's inventory.

    FROM THE `helm.sh/chart` LABEL, split by the scan's own `split_chart_label`, so the name and
    version are what Helm stamped on the installed objects -- not a guess from the component
    id. The inventory record is matched by its `HelmRelease/<namespace>/<release>` reference.

    FAILS OPEN TO "NO LEADS", never to a failed research call. No `inventory_uri`, an unreadable
    document, or no matching release all return `(None, None)`, and the task is exactly what it
    was before the leads existed. A lead is a hint for the agent; nothing is decided by it.
    """
    from ..report import InventoryReport
    from ..scan.helm_labels import split_chart_label
    from ..storage import read_document

    release = str(event.get("helm_release") or "").strip()
    uri = str(event.get("inventory_uri") or "").strip()
    if not (release and uri):
        return None, None
    try:
        inventory = read_document(uri, InventoryReport)
    except Exception as exc:
        print(f"chart leads skipped, the inventory could not be read: {type(exc).__name__}")
        return None, None
    for component in inventory.components:
        kind, _, rest = component.workload_ref.partition("/")
        if kind != "HelmRelease" or rest.rpartition("/")[2] != release:
            continue
        label = (component.labels or {}).get("helm.sh/chart")
        if label:
            name, version = split_chart_label(label)
            return name or component.identity, version
        return component.identity, None
    return None, None


def _prompt(event: dict[str, Any], session_id: str) -> str:
    """The user turn: the task inputs, what to return, and the session to record under.

    The session id is stated as an instruction rather than passed as a parameter because the
    model is the thing that puts it on each tool call -- the Gateway offers no way to inject
    a fixed argument, so the only path from here to the fetch tool runs through the prompt.

    A `target_kubernetes_version` in the event is what switches the assessment on. Without
    one there is nothing to assess against, so the model is asked to transcribe only and
    `extraction.assessment` stays absent rather than invented.
    """
    component = event["component_id"]
    series = event["version_series"]
    installed = event.get("installed_version")
    current = event.get("current_kubernetes_version")
    target = event.get("target_kubernetes_version")
    helm_release = event.get("helm_release")

    # The task inputs go in as LABELLED FIELDS the model copies, not as numbers buried in
    # a sentence: `judge.py` re-checks the assessment's echo of them, so they have to be
    # unambiguous. `as_of_date` is supplied because a model judging release dates against
    # its training cutoff calls a current release future-dated.
    lines = [
        f"session_id: {session_id}",
        f"as_of_date: {datetime.now(UTC).date().isoformat()}",
        f"component_id: {component}",
        f"version_series: {series}",
    ]
    if installed:
        lines.append(f"installed_version: {installed}")
    if current:
        lines.append(f"current_kubernetes_version: {current}")
    if target:
        lines.append(f"target_kubernetes_version: {target}")
    if helm_release:
        # THE ONE LINE THAT TURNS THE CHART SECTION ON. The prompt asks for `chart` and
        # `chart_versions` only when the task names a Helm release, because those values are
        # what the upgrade lane runs `helm` with -- and a managed add-on has no chart to run.
        lines.append(f"helm_release: {helm_release}")
        # THE LEADS: which chart, at which chart version, is installed. Read from the run's
        # inventory (the release's `helm.sh/chart` label), so the agent looks for THAT chart
        # instead of guessing which repository publishes it. Absent when unreadable.
        chart_name, chart_version = _chart_leads(event)
        if chart_name:
            lines.append(f"chart_name: {chart_name}")
        if chart_version:
            lines.append(
                f"chart_version: {chart_version}  (the installed CHART version; "
                f"the application version is {installed or 'unknown'})"
            )
    lines.append("")

    if target:
        lines += [
            f"The cluster runs Kubernetes {current or 'an unstated minor'} with "
            f"{component} {installed or series} installed and is being upgraded to "
            f"Kubernetes {target}.",
            "",
            "Return both the EVIDENCE and the ASSESSMENT the schema describes: the whole "
            "current compatibility matrix including series above the installed one, every "
            "release you can quote, whether the installed version is stated to work on "
            "the target, which version should run there, in what order to upgrade, and "
            "your proposed confidence with its rationale and the documents you used."
            + (
                " It is installed by Helm, so also return the chart repository: the URL on "
                "a `helm repo add` line, quoted from the page that shows it. If the "
                "project's install page has no such line, look in the project's own charts "
                "repository on GitHub before giving up; if you still find none, say so in "
                "not_found starting `chart repository:` and list the pages you tried."
                if helm_release
                else ""
            ),
        ]
    else:
        # No target: nothing to assess against, so ask for the transcription only and let
        # the assessment stay absent rather than invented.
        lines += [
            f"Research the Kubernetes compatibility of {component}, version series "
            f"{series}. Transcribe every compatibility statement and every release you "
            "can quote. No target Kubernetes version was supplied, so omit the "
            "assessment block.",
        ]

    lines += [
        "",
        f"Pass session_id={session_id} on every fetch_source call.",
        "Return the single JSON object the schema describes, and nothing else.",
    ]
    return "\n".join(lines)


def _invoke_once_retrying_a_dropped_connection(
    client: Any, session_id: str, messages: list[dict[str, Any]]
) -> str:
    """Invoke the Harness and read its whole stream; on a dropped connection, once more."""
    for drop in (False, True):
        try:
            response = client.invoke_harness(
                harnessArn=HARNESS_ARN,
                runtimeSessionId=session_id,
                messages=messages,
            )
            return _extract_text(response)
        except _DROPPED:
            if drop:
                raise
    raise AssertionError("unreachable")


def _extract_text(response: Any) -> str:
    """Concatenate the assistant text out of a converse-style event stream.

    Tolerant of shape on purpose: the stream carries tool-use and metadata events this
    handler has no interest in, and a new event type must not break the run. Only text
    deltas are collected.
    """
    chunks: list[str] = []
    for event in response.get("stream", []):
        if not isinstance(event, dict):
            continue
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            chunks.append(delta["text"])
        message = event.get("messageStart") or event.get("messageStop")
        if message:
            continue
        # Non-streaming shape, in case the API returns a whole message.
        for block in event.get("message", {}).get("content", []) or []:
            if isinstance(block, dict) and "text" in block:
                chunks.append(block["text"])
    return "".join(chunks)


def _parse_extraction(text: str) -> dict[str, Any]:
    """Pull the one JSON object out of the model's answer.

    The prompt asks for a bare object, but a model that wraps it in a fence or adds a
    sentence has still done the work -- so the outermost brace-balanced span is taken rather
    than failing the run over punctuation. If there is no object at all, that is a real
    failure and it raises.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)

    start = stripped.find("{")
    if start == -1:
        raise HarnessError(f"no JSON object in the Harness response: {text[:400]!r}")

    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(stripped[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(stripped[start : i + 1])

    raise HarnessError("the Harness response contains an unterminated JSON object")


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Research one component through the Harness, judge the result, return what happened.

    Required in `event`: `component_id`, `version_series`. Everything else is passed through
    to the judge, which uses `current_kubernetes_version` and `target_kubernetes_version` for
    the verdict delta and `bucket` for where to write.

    A failure here is returned, not raised: one component whose sources are unreadable must
    not discard the research that succeeded for the others, and the Map has no catch on this
    state.
    """
    component_id = str(event["component_id"]).strip()
    version_series = str(event["version_series"]).strip()
    started = datetime.now(UTC)
    session_id = _session_id(component_id, started)

    base = {
        **event,
        "stage": 6,
        "component_id": component_id,
        "version_series": version_series,
        "session_id": session_id,
        "path": "harness",
        # Stated so a caller reading only this result sees the invariant: a pending fact
        # drives nothing and the run did not wait for anyone.
        "blocks_pipeline": False,
    }

    if not HARNESS_ARN or not JUDGE_FUNCTION:
        return {
            **base,
            "decision": "NO_EXTRACTION",
            "confidence": "UNRESOLVED",
            "stored": False,
            "pending_approval": False,
            "reason": "RESEARCH_HARNESS_ARN or JUDGE_FUNCTION_NAME is unset",
        }

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"text": _prompt(event, session_id)}]}
    ]
    try:
        extraction = _run_harness(session_id, messages)
    except Exception as exc:
        return {
            **base,
            "decision": "NO_EXTRACTION",
            "confidence": "UNRESOLVED",
            "stored": False,
            "pending_approval": False,
            "reason": f"{type(exc).__name__}: {exc}",
        }

    judged = _run_judge(session_id, event, extraction)
    # ONE MORE ASK WHEN THE JUDGE REJECTS FOR A REASON THE AGENT CAN FIX. The schema re-ask
    # above covers a malformed object; this covers a well-formed one the checker refused --
    # a quote not on the page it cites, a release with no quote, a page cited that was never
    # fetched. Each is one slip in an otherwise sound extraction, and each cost a whole extra
    # pipeline run on 2026-09-27 (cert-manager 17:45, metrics-server 19:00, kyverno 20:47).
    # The judge's own reason goes back on the SAME session, where the pages are already
    # fetched, and the second answer is judged on exactly the same terms. Once: a second
    # rejection is the honest outcome, and the component stays a gap for the next run.
    if judged.get("decision") == "REJECTED" and judged.get("summary"):
        messages.append({"role": "assistant", "content": [{"text": json.dumps(extraction)}]})
        messages.append(
            {
                "role": "user",
                "content": [{"text": _REASK_AFTER_JUDGE + str(judged["summary"])}],
            }
        )
        try:
            second = _run_harness(session_id, messages)
        except Exception as exc:
            return {**base, **judged, "reask": f"failed: {type(exc).__name__}: {exc}"}
        rejudged = _run_judge(session_id, event, second)
        if rejudged.get("decision") != "REJECTED":
            return _with_chart(
                event, session_id, messages, second, {**base, **rejudged, "reask": "recovered"}
            )
        return {**base, **rejudged, "reask": "rejected again"}
    return _with_chart(event, session_id, messages, extraction, {**base, **judged})


def _with_chart(
    event: dict[str, Any],
    session_id: str,
    messages: list[dict[str, Any]],
    extraction: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """One more ask when an accepted Helm answer has no chart repository.

    WITHOUT A REPOSITORY THE UPGRADE LANE CANNOT INSTALL THE COMPONENT, and the commonest
    cause is that the agent stopped at an install page with no `helm repo add` line (live:
    argo-rollouts, whose chart is published from a separate `argo-helm` repository). The
    compatibility fact itself is fine, so this is not a rejection: the question goes back on
    the SAME session, naming the one missing value, and is judged on the same terms.

    THE SECOND ANSWER IS KEPT UNLESS THE JUDGE REJECTS IT. Every judge call writes a candidate,
    so the second answer is the newest one stored; returning it keeps this run's result and the
    newest candidate the same document. A rejected second answer returns the first, unchanged:
    the fact is never lost over its chart. Once, never more.
    """
    if not event.get("helm_release"):
        return result
    if not result.get("pending_approval") or result.get("chart_repository"):
        return result
    messages = [
        *messages,
        {"role": "assistant", "content": [{"text": json.dumps(extraction)}]},
        {"role": "user", "content": [{"text": _chart_reask(event)}]},
    ]
    try:
        second = _run_harness(session_id, messages)
    except Exception as exc:
        return {**result, "chart_reask": f"failed: {type(exc).__name__}: {exc}"}
    rejudged = _run_judge(session_id, event, second)
    if rejudged.get("decision") == "REJECTED":
        return {**result, "chart_reask": "rejected; first answer kept"}
    found = "found" if rejudged.get("chart_repository") else "still missing"
    return {**result, **rejudged, "chart_reask": found}


def _chart_reask(event: dict[str, Any]) -> str:
    """The targeted instruction for a Helm answer that came back with no `chart`."""
    chart = _chart_leads(event)[0] or event.get("helm_release")
    return (
        "Your previous reply was accepted, but it has no `chart`, and this component is "
        f"installed by Helm (release {event.get('helm_release')}, chart {chart}). Without "
        "the chart's location the upgrade cannot install it. Find it now: if the project's "
        "install page has no `helm repo add` line, fetch the README of the project's own "
        "charts repository on GitHub (for example `<org>/helm-charts`, `<org>/charts` or "
        "`<project>-helm`), which shows it. If the chart is published only as an OCI "
        "reference, return that `oci://` reference quoted from the page that shows it. Keep "
        "every other value of your previous reply exactly as it was, and reply with the "
        "complete JSON object. If you still find nothing, leave `chart` out and add a "
        "not_found entry starting `chart repository:` that lists every page you tried."
    )


def _run_harness(session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Invoke the Harness and return an extraction that VALIDATES, re-asking if it does not.

    THE PROXY VALIDATES BEFORE THE JUDGE DOES, and that is the whole point of this
    function's shape. The schema is advisory: Bedrock rejects both mechanisms that would
    enforce it (`output_config.format` and `tools[].strict` each return 400 on this
    endpoint), so it reaches the model as TEXT IN THE PROMPT and nothing makes the model
    obey it. `ResearchExtraction` catches a malformed object -- but catching it here, where
    the conversation is still open, is what lets it be fixed.

    Measured cost of not doing this: a live run returned eleven statements keyed
    `stated_maximum` instead of `stated_maximum_inclusive`. One shortened field name, and
    `extra="forbid"` rejected the entire extraction. The component came back JUDGE_FAILED
    with nothing stored, while the two components beside it -- same prompt, same model --
    got the key right. The in-process path has recovered from exactly this since it was
    written (`extract.py` `_REASK`/`MAX_ATTEMPTS`); moving the loop into the Harness dropped
    that, so this restores it.

    The re-ask carries the validation error itself, because "your object was malformed" is
    not actionable and "statements.0.stated_maximum: Extra inputs are not permitted" is.
    """
    client = boto3.client("bedrock-agentcore", config=_HARNESS_CONFIG)
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = _invoke_once_retrying_a_dropped_connection(client, session_id, messages)
        if not text.strip():
            raise HarnessError("the Harness returned no text")

        try:
            extraction = _parse_extraction(text)
            ResearchExtraction.model_validate(extraction)
        except (ValidationError, json.JSONDecodeError) as exc:
            if isinstance(exc, json.JSONDecodeError):
                # MALFORMED JSON IS THE SAME KIND OF FAILURE AS A MALFORMED OBJECT and gets
                # the same re-ask. Observed live: a chart entry quoted out of an `index.yaml`
                # carried an unescaped quote mark, `json.loads` failed at line 168, and the
                # component came back NO_EXTRACTION with the conversation still open and
                # nobody asked to fix one character.
                last_error = (
                    f"the reply is not valid JSON ({exc.msg} at line {exc.lineno} column "
                    f"{exc.colno}). Escape every quote mark and newline inside a string; "
                    "prefer the shortest span that contains the values."
                )
            else:
                # Report the first few errors rather than all of them: eleven copies of one
                # mistake is one mistake, and a wall of identical text buries the field name
                # that matters.
                errors = exc.errors()[:4]
                last_error = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors
                )
                if len(exc.errors()) > len(errors):
                    last_error += f" (and {len(exc.errors()) - len(errors)} more)"
            if attempt >= MAX_ATTEMPTS:
                raise HarnessError(
                    f"the extraction still did not match the contract after "
                    f"{MAX_ATTEMPTS} attempt(s): {last_error}"
                ) from exc
            # The model's own reply stays in the history, so it can see what it wrote.
            messages.append({"role": "assistant", "content": [{"text": text}]})
            messages.append({"role": "user", "content": [{"text": _REASK + last_error}]})
            continue

        return extraction

    raise HarnessError("the Harness produced no valid extraction")


def _run_judge(
    session_id: str, event: dict[str, Any], extraction: dict[str, Any]
) -> dict[str, Any]:
    """Hand the extraction to the judge and return its verdict.

    A synchronous invoke, and its FunctionError is surfaced rather than swallowed: a judge
    that raised means the extraction was not checked, which is not the same as a component
    with no derivable window and must not be reported as one.
    """
    payload = {
        "extraction": extraction,
        "session_id": session_id,
        "component_id": event["component_id"],
        "version_series": event["version_series"],
    }
    for key in (
        "bucket",
        "cluster_name",
        "installed_version",
        "helm_release",
        "current_kubernetes_version",
        "target_kubernetes_version",
    ):
        if event.get(key) is not None:
            payload[key] = event[key]

    response = boto3.client("lambda", config=_JUDGE_CONFIG).invoke(
        FunctionName=JUDGE_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = json.loads(response["Payload"].read() or b"{}")

    if response.get("FunctionError"):
        return {
            "decision": "JUDGE_FAILED",
            "confidence": "UNRESOLVED",
            "stored": False,
            "pending_approval": False,
            "reason": f"judge raised: {str(body)[:600]}",
        }
    return body
