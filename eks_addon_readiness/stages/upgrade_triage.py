"""The upgrade agent's proxy: ask the Harness about one component, validate the answer.

    in    one component's situation and the lane's clock
    out   one `UpgradeTriage`, or a recorded absence

WHY A PROXY AT ALL, as in `research_harness.py` and `brief_harness.py`: a harness-managed runtime
refuses `InvokeAgentRuntime` -- "is managed by a harness and cannot be invoked directly. Use the
InvokeHarness API" -- and there is no `invokeHarness` SDK integration. So something invokes a
Lambda, and this is it.

**THE HARNESS DOES THE READING AND THIS PROXY DOES THE ASKING.** One `invoke_harness` per
checkpoint, with no `tools` parameter and no loop: the agent's four EKS MCP tools are declared on
the
Gateway, and the harness calls them itself under its own role, which holds
`AmazonEKSAdminViewPolicy`
on one cluster. So the model never holds a credential and this function never executes a tool -- the
fence is the Gateway's IAM and the target's declared tool list, not code here deciding what a
model-chosen name means.

**IT CARRIES NO EVIDENCE.** It hands over the situation, the clock and the facts the API server
already stated, and the agent goes and looks. Pre-loading the pods would make the tools optional and
the reading unverifiable.

**ONE CALL IS ONE CHECKPOINT, AND CODE OWNS THE CLOCK.** The build decides when to ask and passes
`elapsed_seconds`, `deadline_seconds` and whether this is the final call. Nothing here sleeps, polls
or waits. The alternative -- one invocation that reads repeatedly -- puts the wall clock inside the
model's turn, makes the deadline a Harness timeout rather than a number an operator set, and lets a
looping model burn the whole window with the lane's access entry open.

**`SETTLING` IS WITHDRAWN ON THE FINAL CALL AND THIS IS WHERE THAT IS ENFORCED.** The prompt says
so; a model that says it anyway is re-asked here, because a SETTLING at the deadline would leave the
lane with no answer at the one moment it must have one.

**AND AN ABSENT ANSWER IS A REFERRAL, NOT A PASS.** Every failure -- no ARN, an unreachable
Harness, a throttle, an answer that will not validate after the re-asks -- returns
`available: False` with the reason. The build then falls back to the FLOOR: if the cluster states
something unambiguously broken, it rolls back; if the floor is quiet, the component is REFERRED,
because nothing judged it. Silently passing an unvalidated upgrade and rolling back a
probably-healthy one are both dishonest, and this is the third option.

**IT WRITES NOTHING, MUTATES NOTHING, AND INVOKES NOTHING.** Its role holds `s3:GetObject` under one
prefix and the two invoke actions on one Harness ARN. No `lambda:InvokeFunction`, no EKS action, no
cluster ARN, no `s3:PutObject`: the triage travels back as the return value and the BUILD is what
records it, so a model's output cannot reach an object a reader trusts without passing through code
that validated it.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from ..upgrade_lane.models import TriageVerdict, UpgradeTriage
from ..upgrade_lane.naming import denotes_same_component
from ..upgrade_lane.prompt import REASK, ruleset_digest, user_prompt

Event = dict[str, Any]
Result = dict[str, Any]

HARNESS_ARN = os.environ.get("UPGRADE_HARNESS_ARN", "")

# NO `tools` PARAMETER, AND NO `allowedTools`. Both are omitted deliberately and both omissions are
# load-bearing, because each one, if sent, DISABLES the Gateway rather than adding to it:
#
#   * an invoke-time `tools` parameter REPLACES the tools configured on the harness resource.
# Measured
#     on this live harness with the same prompt: inputTokens 6615 with no parameter, 5557 with tools
#     passed -- the four EKS MCP tools were simply not offered to the model.
#
#   * `allowedTools` filters harness-resource tools, so naming anything there excludes the Gateway.
#     Measured: passing it alongside a tool SUPPRESSED that tool, `end_turn` with no call at all.
#
# SO THE HARNESS RUNS THE TOOL LOOP. One `invoke_harness` per checkpoint does the whole reading:
# measured at 85 seconds and twenty-two MCP reads on a healthy single-pod component, ending in text.
# Nothing here parses a tool call or executes one.

# ONE ATTEMPT, PLUS ONE RE-ASK, EACH IN ITS OWN SESSION. A re-ask is worth one shot -- the
# validation
# error names the field and a model usually fixes it -- and a third would keep the lane's access
# entry
# open while a model is asked the same thing again.
#
# A FRESH SESSION PER ATTEMPT, AND THE CORRECTION CARRIED IN THE PROMPT RATHER THAN IN THE HISTORY.
# THE HARNESS IS STATEFUL PER SESSION: it restores its own messages for a session id it has seen. So
# appending an assistant turn and re-sending the array duplicates the conversation, once from the
# payload and once from the restore. One user message on a session id nothing has used cannot
# collide
# with state that does not exist.
MAX_ATTEMPTS = 2

# THE WHOLE TIMEOUT CHAIN, AND THE ORDER MATTERS AS MUCH AS THE SIZES. Nested so that each limit is
# larger than the one it contains, and the HARNESS's own limit is the first to bind:
#
#     harness timeout_seconds   840   the service stops the model loop and says so
#     this read timeout         870   the client waits longer, so it never cuts off a live call
#     Lambda timeout            900   AWS's hard maximum; room left to return a refusal
#     health deadline           900   the build's window for one component
#     build timeout            3600   everything above, plus capture and a rollback
#
# WHY THE HARNESS MUST BIND FIRST. A socket timeout gives this proxy nothing to report but "the call
# did not come back", which the lane records as `unavailable` -- indistinguishable from the IAM,
# model and parsing failures that produced that same word for most of this stage's history. When the
# harness stops instead, the response says why.
#
# 180 WAS SMALLER THAN ONE READING. Measured against the live harness with the MCP read path: a
# HEALTHY verdict on a single-pod component took 158 seconds and twenty tool calls. An unhealthy one
# reads more, not less, and a big cluster reads slower -- so the old value would have timed out on
# exactly the cases the agent exists for, and reported it as the agent being unavailable.
READ_TIMEOUT_SECONDS = 870
CONNECT_TIMEOUT_SECONDS = 10

class TriageUnavailable(Exception):
    """The agent could not be asked, or could not answer. Never raised out of `handler`."""


def _events(response: dict[str, Any]) -> list[dict[str, Any]]:
    """The response's events as a LIST, because `response["stream"]` can only be read once.

    THIS EXISTS BECAUSE READING IT TWICE LOOKS EXACTLY LIKE A MODEL THAT SAID NOTHING. botocore
    returns an EventStream, which is a single-pass generator: the first consumer drains it and every
    later one sees an empty sequence. Two passes over `response["stream"]` therefore report
    `the Harness returned no text` for a response that contained text, every time -- so the stream
    is
    materialised once, here, and every reader takes the list.
    """
    return [event for event in response.get("stream", []) if isinstance(event, dict)]


def _final_turn_text(events: list[dict[str, Any]]) -> str:
    """The text of the LAST assistant turn that has any, not every turn concatenated.

    THE DIFFERENCE IS THE WHOLE ANSWER, because the harness runs the tool loop. A stream is no
    longer one turn: it is a dozen, each `messageStart` .. `messageStop`, and the model narrates
    between its tool calls -- "All events are Normal", "The old ReplicaSet has 0 desired replicas".
    Concatenating all of it, which is what `brief_harness._extract_text` does and what this used to
    do, hands `_parse_object` a paragraph of commentary with the verdict buried in it. Measured on a
    live invocation: 761 characters of prose from a reading that had gone perfectly.

    IT IS RIGHT FOR THE OTHER TWO AGENTS AND WRONG FOR THIS ONE. Research and brief reach a Gateway
    too, but they are asked for prose and their callers want all of it. This one is asked for a bare
    object, so only the turn that ends the conversation can hold it.

    THE LAST TURN WITH TEXT, NOT SIMPLY THE LAST TURN. A stream can end with a `messageStop`
    carrying
    no content -- `max_iterations_exceeded` does exactly that -- and taking that literally would
    report "no text" for a model that answered one turn earlier.
    """
    turns: list[list[str]] = [[]]
    for event in events:
        if "messageStart" in event:
            turns.append([])
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            turns[-1].append(delta["text"])
        for block in event.get("message", {}).get("content", []) or []:
            if isinstance(block, dict) and "text" in block:
                turns[-1].append(block["text"])

    for chunks in reversed(turns):
        joined = "".join(chunks)
        if joined.strip():
            return joined
    return ""


def _parse_object(text: str) -> dict[str, Any]:
    """The outermost brace-balanced span of `text`, as JSON.

    Lifted from `brief_harness._parse_object` and for its reason: the prompt asks for a bare object,
    and a model that wraps it in a fence or adds a sentence has still done the work. Failing over
    punctuation would cost a triage that was correct.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```\s*$", "", stripped)

    start = stripped.find("{")
    if start == -1:
        raise TriageUnavailable(f"no JSON object in the Harness response: {text[:300]!r}")

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
    raise TriageUnavailable("the Harness response contains an unterminated JSON object")


def _validation_summary(exc: Any) -> str:
    errors = exc.errors()[:4]
    summary = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors
    )
    if len(exc.errors()) > len(errors):
        summary += f" (and {len(exc.errors()) - len(errors)} more)"
    return summary


def run_agent(
    *,
    session_id: str,
    prompt: str,
    component: str,
    aliases: Sequence[str] = (),
    is_final: bool = False,
) -> UpgradeTriage:
    """Invoke the Harness and return a triage that VALIDATES, re-asking once if it does not.

    THE PROXY VALIDATES BEFORE ANYTHING ELSE DOES. `UpgradeTriage` refuses a verdict with no
    observations and refuses HEALTHY that asks for an action, and `TriageAction` has three members
    so
    an invented action cannot be constructed at all. None of that is enforceable at the API -- the
    schema reaches the model as text -- so catching it HERE, while the conversation is open, is what
    turns a malformed answer into a fixed one rather than into a lost triage.

    THE COMPONENT IS CHECKED AGAINST THE ONE WE ASKED ABOUT, and a mismatch is a refusal rather than
    a correction. The agent is shown one component's bundle and told to echo its name; an answer
    naming a different component is evidence that something is wrong with the ask, and silently
    rewriting the field would file that answer under the right component.

    THE CHECK IS NOT A STRING COMPARISON, AND IT WAS, AT A COST OF TWENTY-FOUR MINUTES ON AN
    UNTOUCHED ADD-ON. An EKS add-on name is not the workload identity a reader reports: the lane is
    keyed on `vpc-cni` because that is what `eks update-addon` takes, and the agent that went and
    looked answered `amazon-vpc-cni (aws-node DaemonSet, kube-system)`. Both denote the same
    component and `!=` said otherwise, four checkpoints in a row, so the window ran to its deadline
    with no verdict and rolled back. `naming.denotes_same_component` reconciles the decoration and
    still refuses a genuinely different component -- see that module for why the rule is positional
    rather than a containment test, and why no component is named in it.
    """
    import boto3
    from botocore.config import Config
    from pydantic import ValidationError

    if not HARNESS_ARN:
        raise TriageUnavailable(
            "UPGRADE_HARNESS_ARN is unset, so there is no agent to ask. The lane falls back to its "
            "deterministic default, which is to roll the component back."
        )

    client = boto3.client(
        "bedrock-agentcore",
        config=Config(
            read_timeout=READ_TIMEOUT_SECONDS,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            # RETRIES OFF. `InvokeHarness` runs a whole model loop, so a retry re-runs it -- and a
            # slow-but-working call would be abandoned and paid for twice while the lane's access
            # entry stays open.
            retries={"max_attempts": 0},
        ),
    )
    # WHAT THIS ATTEMPT ASKS. The first attempt is the prompt as built. A re-ask appends the model's
    # own answer and the validation error to a NEW user message rather than extending a
    # conversation,
    # for the session-state reason recorded at the top of this file.
    ask = prompt

    for attempt in range(1, MAX_ATTEMPTS + 1):
        # ONE CALL DOES THE WHOLE READING. The Gateway tools are called BY the harness, so there is
        # no
        # loop here: `invoke_harness` returns once the model has finished reading and answered. The
        # stream carries the harness's own tool calls as events, which this proxy IGNORES -- they
        # are
        # a record of what it read, not work for this function to perform.
        response = client.invoke_harness(
            harnessArn=HARNESS_ARN,
            # A SESSION ID PER ATTEMPT, never reused. See the header: the harness restores messages
            # for a session it has seen, and a second ask on the same id is how the old loop got a
            # bare `messageStop` with no content.
            runtimeSessionId=f"{session_id}-a{attempt}",
            messages=[{"role": "user", "content": [{"text": ask}]}],
        )
        events = _events(response)

        text = _final_turn_text(events)
        if not text.strip():
            # NAMES THE STOP REASON, because "no text" alone cost this stage four runs to diagnose.
            # `max_iterations_exceeded` means the model spent its budget reading and never answered,
            # which is a different fault from a model that answered nothing -- and the two were
            # indistinguishable in the lane's report until this said which it was.
            stops = [
                event["messageStop"].get("stopReason", "")
                for event in events
                if "messageStop" in event
            ]
            raise TriageUnavailable(
                "the Harness returned no text"
                + (f" (stop reasons: {', '.join(s for s in stops if s)})" if stops else "")
            )

        candidate = _parse_object(text)
        try:
            triage = UpgradeTriage.model_validate(candidate)
        except ValidationError as exc:
            summary = _validation_summary(exc)
            if attempt >= MAX_ATTEMPTS:
                raise TriageUnavailable(
                    f"the triage still did not match the contract after {MAX_ATTEMPTS} "
                    f"attempt(s): {summary}"
                ) from exc
            # THE CORRECTION GOES INTO THE NEXT ASK, not into a conversation. It carries the
            # original prompt so the re-ask still says which component, which clock and which
            # cluster -- a bare "fix this field" on a fresh session would ask the model to re-read a
            # cluster it was no longer told the name of.
            ask = f"{prompt}\n\nYour previous answer was:\n{text}\n\n{REASK}{summary}"
            continue

        if is_final and triage.verdict is TriageVerdict.SETTLING:
            # THE DEADLINE'S ENFORCEMENT, and it is here rather than left to the prompt. A SETTLING
            # at the deadline is not an answer -- the window is over and the lane must decide -- so
            # the model is told that and asked once more. If it says it again the caller treats the
            # checkpoint as unavailable and the floor decides, which is the same fallback as a
            # throttle.
            if attempt >= MAX_ATTEMPTS:
                raise TriageUnavailable(
                    "the agent answered SETTLING on the final call, twice. The window is over, so "
                    "there is no later checkpoint to defer to and this is not an answer."
                )
            ask = (
                f"{prompt}\n\nYour previous answer was:\n{text}\n\n"
                "This IS the final call: the deadline has passed and there will be no further "
                "checkpoint. SETTLING is not available. Answer HEALTHY, UNHEALTHY or REFER, "
                "on what you read."
            )
            continue

        if not denotes_same_component(
            triage.component, expected=component, aliases=aliases
        ):
            raise TriageUnavailable(
                f"the triage names component {triage.component!r}, which does not denote "
                f"{component!r}"
                + (f" or {list(aliases)!r}" if aliases else "")
                + ". It was shown one component's bundle; an answer about another one is not a "
                "triage of this lane."
            )
        return triage

    raise TriageUnavailable("the Harness produced no valid triage")


def handler(event: Event, _context: Any = None) -> Result:
    """Ask the agent about one component at one checkpoint. NEVER raises.

    RETURNS `available` AS A FIELD RATHER THAN RAISING, because the caller is a shell in a CodeBuild
    phase about to decide whether to roll a component back. An exception reaches it as a non-zero
    exit from `aws lambda invoke`, indistinguishable from a network problem, and the shell would
    have to guess. A field it can read says which it was.
    """
    component = str(event.get("component") or "").strip()
    run_id = str(event.get("run_id") or "").strip()
    started = datetime.now(UTC)
    checkpoint = int(event.get("checkpoint") or 1)
    elapsed = int(event.get("elapsed_seconds") or 0)
    deadline = int(event.get("deadline_seconds") or 0)
    is_final = bool(event.get("is_final"))

    if not (component and run_id and deadline):
        return {
            "available": False,
            "reason": (
                "the checkpoint call carried no component, run id or deadline, so there is nothing "
                f"to ask about. Got component={component!r}, run_id={run_id!r}, "
                f"deadline_seconds={deadline!r}."
            ),
            "ruleset_digest": ruleset_digest(),
        }

    try:
        prompt = user_prompt(
            component=component,
            cluster=str(event.get("cluster") or ""),
            namespace=str(event.get("namespace") or ""),
            release=str(event.get("release") or component),
            install_method=str(event.get("install_method") or "HELM"),
            installed=str(event.get("installed") or ""),
            applied=str(event.get("applied") or ""),
            summary=str(event.get("summary") or ""),
            # WHAT THE CLUSTER STATES OUTRIGHT, from `validate.floor_for`, handed over by the build.
            # Usually empty: the floor only speaks when the API server has already decided.
            floor_facts=tuple(str(fact) for fact in (event.get("floor_facts") or ())),
            elapsed_seconds=elapsed,
            deadline_seconds=deadline,
            checkpoint=checkpoint,
            is_final=is_final,
        )
        # ONE SESSION PER CHECKPOINT, not one per lane. Carrying the conversation across checkpoints
        # would let a model that said SETTLING at 45s anchor on that reading at 600s instead of
        # looking again -- and looking again is the entire point of a second checkpoint.
        session_id = f"upgrade-{run_id}-{component}-cp{checkpoint}-{uuid.uuid4().hex[:8]}"
        # THE OTHER NAMES THIS LANE ALREADY HOLDS FOR ITS OWN COMPONENT. The release name is on the
        # payload for every lane -- equal to the component for a managed add-on, the Helm release
        # otherwise -- so the identity check gets it for free and no component-specific string
        # enters the code. A caller that later carries the add-on's pod selector can add it here.
        triage = run_agent(
            session_id=session_id,
            prompt=prompt,
            component=component,
            aliases=tuple(
                name for name in (str(event.get("release") or ""),) if name
            ),
            is_final=is_final,
        )
    # BROAD, AND THE DOCSTRING IS THE REASON: this handler never raises.
    except Exception as problem:
        return {
            "available": False,
            "reason": f"{type(problem).__name__}: {problem}",
            "ruleset_digest": ruleset_digest(),
            "asked_at": started.isoformat(),
            "checkpoint": checkpoint,
        }

    # The checkpoint and the clock are stamped onto the document HERE rather than trusted to the
    # model: it is told them, but what gets recorded is what the code knows.
    stamped = triage.model_copy(
        update={"checkpoint": checkpoint, "elapsed_seconds": elapsed}
    )
    return {
        "available": True,
        "triage": json.loads(stamped.model_dump_json()),
        # THE VERDICT AS TWO FLAT STRINGS, so the buildspec can branch on them without a JSON parser
        # in the middle of a shell block. The full document is above, for the report.
        "verdict": stamped.verdict.value,
        "proposed_action": stamped.proposed_action.value,
        "checkpoint": checkpoint,
        "elapsed_seconds": elapsed,
        "ruleset_digest": ruleset_digest(),
        "asked_at": started.isoformat(),
    }


# The values the buildspec compares against, exported so the shell and the contract cannot drift.
HEALTHY = TriageVerdict.HEALTHY.value
SETTLING = TriageVerdict.SETTLING.value
UNHEALTHY = TriageVerdict.UNHEALTHY.value
REFER = TriageVerdict.REFER.value
