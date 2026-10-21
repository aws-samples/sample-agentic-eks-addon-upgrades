"""The upgrade agent's instruction, and the evidence it is given.

    in    one sealed diagnosis bundle for one component
    out   one prompt, and the contract the answer must satisfy

**THIS AGENT READS THE CLUSTER ITSELF, THROUGH FOUR OF AWS'S OWN EKS MCP TOOLS.**
`list_k8s_resources`, `read_k8s_resource`, `get_pod_logs` and `get_k8s_events`, reached through an
AgentCore Gateway in front of the EKS MCP server. The harness calls them; nothing in this repository
executes a tool, holds a cluster credential, or relays a model-chosen name to an AWS API.

**READ-ONLY IS ENFORCED BY THE AUTHORIZER, NOT BY OUR CODE.** Two things the service checks: the
Gateway's role holds `eks-mcp:CallReadOnlyTool` and never `CallPrivilegedTool`, and its access entry
carries `AmazonEKSAdminViewPolicy`, which has no write verb.

**IT CAN READ SECRETS, AND IT COULD NOT BEFORE.** The entry carried `AmazonEKSViewPolicy`, and this
docstring proved the exclusion with a live probe -- a Secret list came back `secrets is forbidden`,
naming the Gateway's own session. That is no longer true. `view` has no `apiregistration.k8s.io`, so
the agent could not read the APIService that says whether an aggregated API like metrics-server
actually registered, and it said so in a verdict rather than guessing. AdminView closes that gap and
every neighbouring one -- CRDs, webhook configurations, storage classes, nodes -- and the price is
that Helm release contents in `kube-system`, and any application's Secrets, are now readable by this
agent. Read-only is intact; not-secret is not.

**THE MODEL CAN NAME A NAMESPACE AND A CLUSTER, AND THREE THINGS NARROW THAT.** `list_k8s_resources`
takes `cluster_name` and `namespace` as arguments, so the fence cannot be "there is nothing to name".
It is: the Gateway's IAM is scoped to ONE cluster, the target declares four tools out of the server's
twenty, and this prompt names one component and requires the verdict to echo it back. The user
accepted that trade explicitly.

**THE AGENT JUDGES WHETHER THE COMPONENT IS HEALTHY, AND THAT IS A CORRECTION TO HOW THIS STAGE
WAS FIRST BUILT.** Deterministic code used to decide HEALTHY or UNHEALTHY by pattern-matching log
lines, and the agent only triaged what that check had already rejected. It was the wrong division
of labour: "is this component working" is exactly what a model is good at and a regex is bad at,
and a pattern list is guaranteed to false-positive on some components and miss others -- the cost
of the first being a working component rolled back. `validate.py` now holds only a FLOOR of facts
the API server states outright, and this agent is the verdict.

**WHAT IT STILL DOES NOT DECIDE IS WHAT HAPPENS NEXT.** `TriageAction` has three members and no
fourth, so the only actions it can name are ones deterministic code already performs. A verdict
asking for anything else is refused at construction rather than interpreted.

**AND AN ABSENT VERDICT IS A REFERRAL, NOT A PASS.** If the Harness is unreachable, throttled, or
answers something that will not validate, then nothing judged this component -- so the floor
decides if it has something to say, and otherwise a person is told the upgrade was not validated.
Neither silently passing nor rolling back a probably-healthy component is honest there.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sys
from typing import Any

from .models import UpgradeTriage

# How much of each captured file reaches the model. A crash-looping container produces thousands
# of identical lines and the whole bundle is megabytes; what decides a triage is the first and
# last of a log, not its middle. The FULL bundle stays in S3 and the report cites it, so nothing
# is lost -- this is what fits in a prompt, not what was captured.
LOG_HEAD_LINES = 60
LOG_TAIL_LINES = 120
MAX_FILE_CHARS = 20_000

# Fields the PROXY owns, not the model. `checkpoint` and `elapsed_seconds` come from the build's
# clock and are stamped on after the answer is parsed; `schema_version` and `kind` are `Literal`
# constants with defaults. Showing any of them invites a model to supply a value that is then
# overwritten, and asking it to echo a constant is a way to get the constant wrong.
_PROXY_OWNED_FIELDS = ("checkpoint", "elapsed_seconds", "schema_version", "kind")

# What the model must supply. `observations` is the one addition over the generated `required`:
# `UpgradeTriage.every_verdict_must_say_what_it_read` refuses ANY verdict without it, so a schema
# that left it optional would permit exactly what the contract rejects -- the shown rule and the
# enforced rule disagreeing, which is the drift this function exists to remove.
_REQUIRED_TOP_LEVEL = ("component", "verdict", "proposed_action", "observations")


def triage_schema() -> dict[str, Any]:
    """The JSON Schema the agent is shown, GENERATED from `UpgradeTriage`.

    WHY GENERATED. The prompt used to carry a hand-written JSON example while the reply was
    validated against `UpgradeTriage`, which the prompt never saw -- two descriptions of one
    contract in two files with nothing holding them together. They agreed on the day they were
    written and nothing kept them agreeing: renaming a verdict or adding a field would leave the
    model shown the old shape and its correct-by-the-prompt answer refused by validation. This is
    the same fix `brief/prompt.py::extraction_schema` already applies, for the same reason.

    IT IS NOT MODEL-LEVEL ENFORCEMENT, and the difference is worth keeping straight.
    `research/extract.py` passes its schema as `output_config.format`, which constrains generation
    -- but that parameter and `tools[].strict` both return 400 on the Harness endpoint, as
    `brief_harness.py` and `research_harness.py` each record having tried. So this reaches the model
    as text in the system prompt. It removes a drift path; it does not make a malformed answer
    impossible, which is why `model_validate` and the one re-ask stay.

    The doctests are the guarantee rather than a demonstration:

    >>> s = triage_schema()
    >>> s["additionalProperties"]
    False
    >>> sorted(s["required"])
    ['component', 'observations', 'proposed_action', 'verdict']
    >>> s["properties"]["observations"]["minItems"]
    1
    >>> sorted(s["$defs"]["TriageVerdict"]["enum"])
    ['HEALTHY', 'REFER', 'SETTLING', 'UNHEALTHY']
    >>> sorted(s["$defs"]["TriageAction"]["enum"])
    ['NONE', 'REFER', 'ROLLBACK', 'WAIT']
    >>> any(f in s["properties"] for f in ("checkpoint", "elapsed_seconds", "kind"))
    False
    """
    schema = UpgradeTriage.model_json_schema()
    schema["additionalProperties"] = False
    schema.pop("title", None)

    properties = schema.get("properties", {})
    for field in _PROXY_OWNED_FIELDS:
        properties.pop(field, None)

    # NON-EMPTY, not merely present. `observations: []` would satisfy a bare `required` and is
    # exactly the answer the contract refuses -- a verdict that cites nothing it read.
    if "observations" in properties:
        properties["observations"]["minItems"] = 1

    schema["required"] = [f for f in _REQUIRED_TOP_LEVEL if f in properties]
    return schema


_SYSTEM_PROMPT_TEMPLATE = """\
You are the upgrade agent for an EKS add-on upgrade pipeline. One component has just been upgraded
on one cluster. Your job is to go and look at that cluster and say whether the component is
healthy.

YOU READ THE CLUSTER YOURSELF. You have tools that perform read-only Kubernetes reads, and the
credential behind them exists only for this one component's upgrade and is deleted when it ends.
Use them. Nothing is handed to you in advance except the situation below: if you want to know
whether the pods are up, read the pods; if you want to know why one is not, read its logs and the
namespace's events. An answer that read nothing is not an answer.

YOUR FOUR TOOLS, AND WHAT TO DO WITH THEM. They are AWS's own EKS reads:

  list_k8s_resources   every object of one kind. Needs cluster_name, kind, api_version; takes
                       namespace and label_selector. This is how you see pods, the Deployment or
                       DaemonSet, its ReplicaSets, the Service and its Endpoints.
  read_k8s_resource    one named object in full. Needs cluster_name, kind, api_version, name.
  get_pod_logs         one pod's log. Needs cluster_name, namespace, pod_name; takes container_name,
                       tail_lines, and previous.
  get_k8s_events       events for one object. Needs cluster_name, kind, name; takes namespace.

DO THE CHECKS A PLATFORM ENGINEER WOULD DO, ROUGHLY IN THIS ORDER. You are not required to run all
of them -- stop when you know the answer -- but do not answer without running any.

  1. THE PODS. list_k8s_resources kind=Pod with the component's label selector. Are the expected
     number there, are they Running, are they Ready, and what are their restart counts?
  2. THE ROLLOUT. list_k8s_resources kind=Deployment (or DaemonSet/StatefulSet) and its ReplicaSets.
     Did the new version actually roll out, or is it stuck with old and new side by side?
  3. THE LOGS, IF ANYTHING IS WRONG. get_pod_logs on the pod that looks unhappy. IF A CONTAINER HAS
     RESTARTED, READ THE PREVIOUS ONE: pass previous=true. The current log is the container that is
     running now; the PREVIOUS log is the one that died, and it is what says why.
  4. THE EVENTS. get_k8s_events on the pod or the workload. FailedScheduling, Unhealthy, BackOff and
     image pull failures appear here in plain words.
  5. IS IT ACTUALLY SERVING. Where the component fronts something -- a Service with Endpoints, an
     aggregated APIService -- read it. A pod that is Ready but has no endpoints is not working.

WHAT YOU CANNOT DO. You cannot read any Secret, anywhere; the API server refuses it and the refusal
names you. You cannot write, patch, delete, exec, attach or port-forward -- there is no tool for any
of those and no permission behind one. If the evidence you would need is behind one of those, say so
in your reasons and refer rather than guessing.

ONE COMPONENT, ON ONE CLUSTER. Pass the cluster name and namespace you were given below. The tools
would accept others; do not supply them.

YOU MAY SEE THINGS THAT ARE NOT YOURS, AND YOU MUST NOT REASON ABOUT THEM. Pod names are
generated, so your pod and log reads cover the whole namespace rather than only your component's
objects. On a cluster where several components share `kube-system` you will therefore be able to
read a neighbour's logs. Do not draw conclusions about a neighbour, do not report on one, and do
not recommend anything about one. The exception is causal and narrow: if a neighbour is the CAUSE of
your component's problem -- it holds a lock, it serves an API you need, it is consuming the node --
say so as a cause, cite it, and refer.

NOTHING YOU SAY IS EXECUTED. You run no commands. Deterministic code performs every change to the
cluster, including the rollback. What your answer decides is which branch that code takes, and
there are only the four below.

YOU ARE WORKING AGAINST A DEADLINE, AND YOU WILL BE ASKED MORE THAN ONCE. A rollout takes time to
settle, so you are asked at several checkpoints while the clock runs. Each time you are told how
long has elapsed and how long remains. If the component is still coming up, say SETTLING and you
will be asked again. When you are told this is the FINAL call, SETTLING is no longer available:
answer.

ANSWER WITH ONE JSON OBJECT AND NOTHING ELSE. It must validate against this schema, which is
generated from the contract your answer is checked against -- so what you are shown here and what
is enforced cannot differ:

__TRIAGE_SCHEMA__

`observations` is what you ACTUALLY READ, one entry per read, each naming the tool and what came
back. It may not be empty for any verdict, HEALTHY included: a verdict citing nothing is a verdict
that read nothing. `reasons` is why those observations lead to that verdict.

WHAT EACH VERDICT MEANS. Be strict about these; the difference is what a person's evening looks
like.

  HEALTHY with NONE
      The new version is running and doing its job. Say this when what you read supports it: the
      expected pods Ready, restarts settled rather than climbing, the workload's rollout complete,
      and -- where the component serves one -- its API actually available. Startup noise is not
      breakage: a leader election, a cache sync, a connection retried once. HEALTHY IS NOT THE
      QUIET ANSWER. It is the one that closes the lane and leaves a possibly-broken component
      running.

  SETTLING with WAIT
      Not yet, and nothing is wrong that time will not fix. A pod still pulling an image, a rollout
      part-way through, a readiness probe that has not passed its first check. Say this freely
      while the clock allows it -- it costs one more look and it is the honest answer to "is it up
      yet". You cannot say it on the final call.

  UNHEALTHY with ROLLBACK
      Broken, and returning it to the previous version fixes it. THIS TRIGGERS AN AUTOMATIC
      ROLLBACK: no person is asked, code does it as soon as you answer. The expected case for a bad
      version move -- an image that will not start, a flag the new version rejects, a configuration
      the new chart reads differently, a container looping.

  REFER with REFER
      A person is needed. This is the only verdict that reaches a human, so use it when it is true
      and not as a way of avoiding the other three. The cases that matter:
        - the cause is outside this component: a node under pressure or tainted, a quota, an
          admission webhook rejecting the write, a missing Secret named in an event, an IAM or IRSA
          failure
        - the previous version would be broken too, so a rollback returns the cluster to a state
          that was already failing
        - a read you needed was denied or empty, so the evidence is missing rather than absent
        - something is half-applied and a rollback could make it worse
        - it is genuinely ambiguous. AMBIGUITY IS A REAL ANSWER and a useful one. Say REFER and say
          what you could not settle.

WHAT THE CLUSTER ALREADY STATES AS FACT. Some things are not open to interpretation -- a container
in CrashLoopBackOff, an OOMKill, a pod in phase Failed, no pods at all where the manifest declares
a workload. Deterministic code reads those separately and lists them to you under FACTS. You may
explain them and you may judge what they mean for a rollback, but DO NOT CONTRADICT THEM: an answer
of HEALTHY over a stated CrashLoopBackOff is treated as a disagreement and sent to a person, which
wastes their time if you simply misread the list. An empty FACTS list means nothing is
unambiguously broken -- it does not mean the component is fine, and the judgement is still entirely
yours.

RULES YOU MUST FOLLOW.

  Read before you answer. Every observation names the tool you called and what it returned --
  "list_k8s_resources Pod: 1/1 Ready, 0 restarts" is an observation; "the deployment is healthy"
  is a restatement of the verdict.

  Quote what you read when the exact text matters. An error string is evidence; a summary of it is
  not.

  Do not recommend an action that is not one of the four above. There is no patch, no scale, no
  delete, no re-run, no "increase the memory limit". If what you would do is not in the list, your
  verdict is REFER and your reasons say what you would have done.

  If your observations would be empty, your verdict cannot be HEALTHY, SETTLING or UNHEALTHY. Say
  REFER and explain that you could not read what you needed.
"""


# THE SCHEMA IS SUBSTITUTED ONCE, AT IMPORT, so `SYSTEM_PROMPT` stays a module constant. Everything
# that reads it -- the ruleset digest, `app/readiness_upgrade/system-prompt.md`, the drift test
# comparing the two -- treats it as a string, and a function would make the digest depend on when it
# was called.
SYSTEM_PROMPT = _SYSTEM_PROMPT_TEMPLATE.replace(
    "__TRIAGE_SCHEMA__", json.dumps(triage_schema(), indent=2, sort_keys=True)
)

REASK = (
    "That object does not satisfy the contract. Answer again with one JSON object and nothing "
    "else, fixing exactly this: "
)


# THE FOUR TOOLS THE GATEWAY ACTUALLY EXPOSES, under the names the model actually sees.
#
# THE `eks-mcp___` PREFIX IS NOT DECORATION. An AgentCore Gateway namespaces a target's tools, so the
# server's `list_k8s_resources` reaches the model as `eks-mcp___list_k8s_resources`. A prompt that
# names the unprefixed tool tells the agent to call something that does not exist, and the failure is
# quiet: the model improvises a different tool or answers without reading.
#
# HELD TO THE GATEWAY BY `tests/test_the_prompt_names_the_tools_the_gateway_exposes.py`, which reads
# `terraform/upgrade/mcp_tools.json` -- the same file the target is built from -- so a tool added or
# removed there fails the suite rather than drifting silently past this list.
MCP_TOOLS: tuple[tuple[str, str], ...] = (
    (
        "eks-mcp___list_k8s_resources",
        "List resources of one kind in a namespace. Pods, Deployments, ReplicaSets, Endpoints. "
        "Takes cluster_name, kind, api_version, namespace, and an optional label selector.",
    ),
    (
        "eks-mcp___read_k8s_resource",
        "Read ONE named resource in full, including its status. This is what shows rollout "
        "progress, conditions, and a container's restart count and last state.",
    ),
    (
        "eks-mcp___get_pod_logs",
        "Read a pod's logs. Pass previous=true to read the log of a container that has already "
        "died -- that is what says WHY it died, and the current log will not.",
    ),
    (
        "eks-mcp___get_k8s_events",
        "Events for one resource. Pull failures, probe failures, scheduling failures and OOM "
        "kills appear here before they appear anywhere else.",
    ),
)


def user_prompt(
    *,
    component: str,
    cluster: str,
    namespace: str,
    release: str,
    install_method: str,
    installed: str,
    applied: str,
    summary: str,
    floor_facts: tuple[str, ...],
    elapsed_seconds: int,
    deadline_seconds: int,
    checkpoint: int,
    is_final: bool,
    tools: tuple[tuple[str, str], ...] = MCP_TOOLS,
) -> str:
    """One checkpoint's message: the situation, the clock, and what may be read.

    **IT CARRIES NO EVIDENCE, AND THAT IS THE WHOLE SHAPE OF B.** The previous design put a sealed
    diagnosis bundle in the prompt and asked the agent to judge it. The agent now reads the cluster
    itself, so what goes in the prompt is the situation and the tool surface -- and everything it
    learns, it learns by calling a tool. A prompt that pre-loaded the pods would make the tools
    optional and the reading unverifiable.

    **THE CLOCK IS STATED IN SECONDS AND SO IS THE REMAINDER**, because `SETTLING` is only a
    sensible answer when the agent knows there is time left to settle in. `is_final` removes it.

    **`floor_facts` IS THE ONE PIECE OF EVIDENCE THAT IS HANDED OVER**, because it is not a
    judgement: the API server already decided it. Handing it over rather than making the agent
    re-derive it from JSON is what lets the system prompt say "do not contradict these".

    **THE ATTEMPT IS STATED BEFORE ANYTHING ELSE**, because "0.6.4 to 0.9.0" changes how a log
    line reads -- an unrecognised flag is expected across three minor versions and surprising
    across one.
    """
    remaining = max(deadline_seconds - elapsed_seconds, 0)
    lines = [
        f"CHECKPOINT         {checkpoint}"
        + ("   THIS IS THE FINAL CALL" if is_final else ""),
        f"ELAPSED            {elapsed_seconds}s since the upgrade returned",
        f"REMAINING          {remaining}s before the deadline"
        + (
            "   SETTLING is NOT available on this call; answer HEALTHY, UNHEALTHY or REFER"
            if is_final
            else "   SETTLING is available: you will be asked again"
        ),
        "",
        f"CLUSTER            {cluster}",
        f"COMPONENT          {component}",
        f"NAMESPACE          {namespace}",
        f"INSTALLED VIA      {install_method}"
        + (f"   release {release}" if install_method == "HELM" else ""),
        f"WAS ON             {installed or 'unrecorded'}",
        f"NOW ON             {applied or 'unrecorded'}",
    ]
    if summary:
        lines.append(f"AUTHORISED AS      {summary}")

    lines += [
        "",
        "FACTS. What the cluster states outright, not open to interpretation:",
    ]
    lines += [f"  - {fact}" for fact in floor_facts] or [
        "  - none. Nothing the API server reports is unambiguously broken. That is not a statement "
        "that the component is fine; the judgement is yours."
    ]

    lines += ["", "WHAT YOU MAY READ. Call these; nothing is pre-loaded:"]
    lines += [f"  {name:<30} {what}" for name, what in tools]

    lines += [
        "",
        "Go and look, then answer with one JSON object as the contract specifies. Nothing else.",
    ]
    return "\n".join(lines)


def excerpt(body: str, *, head: int = LOG_HEAD_LINES, tail: int = LOG_TAIL_LINES) -> str:
    """Head and tail of a captured file, with the elision stated.

    THE TAIL IS LARGER THAN THE HEAD on purpose: a container that failed says why in its LAST
    lines, and a container that never started says why in its first. Both ends are kept and the
    middle is what goes.

    THE ELISION IS ANNOTATED IN THE TEXT rather than silently applied, so the agent can see that
    it is reading an excerpt. A model shown a truncated log with no marker will reason about it
    as a complete one, and "there are no further errors" is then a false statement it had no way
    to avoid.
    """
    lines = body.splitlines()
    if len(lines) <= head + tail:
        return body[:MAX_FILE_CHARS]
    dropped = len(lines) - head - tail
    kept = [
        *lines[:head],
        f"... [{dropped} line(s) elided by the pipeline, not by the component] ...",
        *lines[-tail:],
    ]
    return "\n".join(kept)[:MAX_FILE_CHARS]


def ruleset_digest() -> str:
    """The digest of this module's own source: what the agent was told, as a citable value.

    Same device the research and brief stages use. A triage is only interpretable against the
    instruction that produced it, and an instruction that changed between two runs without the
    digest moving would make two incomparable answers look like a disagreement.
    """
    return "sha256:" + hashlib.sha256(
        inspect.getsource(sys.modules[__name__]).encode("utf-8")
    ).hexdigest()


def contract_schema() -> dict[str, Any]:
    """The answer's shape, for the prompt. Advisory, and said to be.

    Bedrock refuses both mechanisms that would ENFORCE a schema on this endpoint, so this
    reaches the model as text and nothing makes it obey. `UpgradeTriage` is what actually
    refuses a malformed answer, and the proxy re-asks while the conversation is still open
    rather than losing the triage.
    """
    return {
        "component": "string, exactly the component you were given",
        "verdict": ["HEALTHY", "FIXABLE", "REFER"],
        "proposed_action": ["NONE", "ROLLBACK", "REFER"],
        "observations": ["string, each citing a bundle file by name"],
        "reasons": ["string"],
    }


def contract_text() -> str:
    """`contract_schema` as the text that goes in the prompt."""
    return json.dumps(contract_schema(), indent=2, sort_keys=True)
