"""The upgrade agent reads a sealed bundle and answers. It can reach nothing else, and cannot act.

WHAT THIS CATCHES. This is the only model in the upgrade stage, and the stage's whole claim is that
deterministic code performs every mutation. Three ways that claim could quietly stop being true:

  THE HARNESS GAINS A TOOL. It has none today, which is stronger than read-only tools: with no tool
  there is no `component` argument a model could point at a neighbour, so the hard lane fence needs
  no
  enforcement on the agent's side at all. A Gateway grant would be the first half of undoing that.

  THE PROXY GAINS A WRITE. The verdict travels back as a return value and the BUILD records it, so a
  model's output cannot reach an object a later reader trusts without passing through code that
  validated it against `UpgradeTriage`. An `s3:PutObject` would open exactly that path.

  THE AGENT'S VERDICT STARTS DECIDING THE ROLLBACK. It does not: the deterministic default for an
  unhealthy component is ROLLBACK and the agent can only redirect it to REFER. A model that is
  throttled, unreachable or answering badly must not be able to leave a broken component running.

And one more that is about honesty rather than reach: the DEPLOYED prompt must be the REVIEWED
prompt.
`infra/terraform/modules/upgrade` reads `agents/readiness_upgrade/system-prompt.md` with `file()`, and the text
lives in
`upgrade_lane/prompt.py` beside the contract it describes. A stale file deploys an instruction
nobody
read.

Deliberately no AWS call: the Terraform and the committed prompt are the sources.
"""

from __future__ import annotations

import re
from pathlib import Path

from eks_addon_readiness.upgrade_lane.models import TriageAction, TriageVerdict, UpgradeTriage
from eks_addon_readiness.upgrade_lane.prompt import SYSTEM_PROMPT

ROOT = Path(__file__).resolve().parent.parent
HARNESS_TF = ROOT / "infra/terraform/modules/upgrade/harness.tf"
TRIAGE_TF = ROOT / "infra/terraform/modules/upgrade/triage.tf"
PROMPT_FILE = ROOT / "agents/readiness_upgrade/system-prompt.md"
BUILDSPEC = ROOT / "infra/terraform/modules/platform/upgrade_buildspec.yml"

# What the Harness role may do. Reaching a model and writing its own telemetry, and nothing else.
EXPECTED_HARNESS_ACTIONS = {
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock-agentcore:GetWorkloadAccessToken",
    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    # THE READ PATH. Four EKS MCP tools behind one Gateway, which the harness calls itself -- so
    # without this grant the tool block would be advertised to the model and fail on use. Scoped to
    # ONE gateway ARN, never `gateway/*`: the research and brief agents have their own in this
    # account, and the reason three agents exist is that their reach must not be interchangeable.
    "bedrock-agentcore:InvokeGateway",
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
    "logs:DescribeLogStreams",
}

# What the proxy may do. Read one prefix, ask one Harness, log.
# `lambda:InvokeFunction` IS ABSENT AND ITS ABSENCE IS THE POINT. The proxy used to hold it, scoped
# to the one function that carried the cluster credential, because an `inline_function` tool comes
# BACK to the caller: the model named a tool and this proxy performed it. The agent's tools are
# Gateway tools now and the harness calls them itself, so the proxy relays no model-chosen string to
# any AWS API -- which removes a whole class of grant rather than narrowing one.
EXPECTED_TRIAGE_ACTIONS = {
    "logs:CreateLogStream",
    "logs:PutLogEvents",
    "s3:GetObject",
    # ONE API CALL, TWO IAM ACTIONS, established by two failed runs rather than by reading. The
    # proxy calls the `InvokeHarness` API -- a harness-managed runtime refuses `InvokeAgentRuntime`
    # AS AN API -- and IAM requires BOTH names on the harness ARN to authorise that one call:
    #
    #   granted InvokeHarness only      -> denied on bedrock-agentcore:InvokeAgentRuntime
    #   granted InvokeAgentRuntime only -> denied on bedrock-agentcore:InvokeHarness
    #
    # Each cost a run: every checkpoint returned `available: false`, the deadline expired with no
    # verdict, and an upgrade that had landed cleanly was rolled back. Neither name is redundant and
    # neither widens reach beyond this one call on this one resource.
    "bedrock-agentcore:InvokeHarness",
    "bedrock-agentcore:InvokeAgentRuntime",
}


def _without_comments(text: str) -> str:
    """`text` with every `#` comment line removed.

    LOAD-BEARING: both files end their policies with `DELIBERATELY ABSENT:` blocks that name every
    action this test forbids. Scanning the raw text would fail on a policy that is exactly right.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _policy_body(path: Path, name: str) -> str:
    """The body of one `data "aws_iam_policy_document" "<name>"` block, comments stripped.

    NAMED RATHER THAN SCANNING THE WHOLE FILE, and the first version of this test did scan the whole
    file and was wrong twice over. A file like this carries three kinds of `actions` list and only
    one
    of them is a grant the subject holds:

      the PERMISSION policy      what this role may do. The thing under test.
      the TRUST policy           `sts:AssumeRole`, which is what the SERVICE may do to assume the
                                 role -- reporting it as a permission the agent holds is simply
                                 false.
      `aws_lambda_permission`    `lambda:InvokeFunction`, which is what the BUILD may do to this
                                 function. Reading it as a grant the agent holds inverts the
                                 direction
                                 of the permission entirely.

    So the block is addressed by name, and a rename fails this test with a message saying so rather
    than silently widening what it reads.
    """
    text = path.read_text()
    match = re.search(
        rf'data "aws_iam_policy_document" "{re.escape(name)}" \{{(.*?)\n\}}', text, re.S
    )
    assert match, (
        f'could not find data "aws_iam_policy_document" "{name}" in {path.name}. If it was '
        "renamed, "
        "fix this test rather than deleting it."
    )
    return _without_comments(match.group(1))


def _actions(text: str) -> set[str]:
    found: set[str] = set()
    for block in re.findall(r"actions\s*=\s*\[(.*?)\]", text, re.S):
        found |= set(re.findall(r'"([a-z0-9-]+:[A-Za-z0-9*]+)"', block))
    return found


def _commands(path: Path) -> str:
    """A file with every `#` comment line removed.

    THE SAME TRAP `test_the_diagnosis_lands_before_the_rollback` documents, fallen into again while
    this file was being written: the buildspec's comments EXPLAIN the ordering they implement, so a
    comment saying "`command --kind helm-rollback` refuses an unsealed bundle" sits ABOVE the
    seal it
    describes. An ordering check over the raw text then reads the comment's position and fails
    against
    a buildspec that is exactly right.
    """
    return "\n".join(
        line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def test_the_deployed_prompt_is_the_reviewed_prompt() -> None:
    """The bytes Terraform hands the Harness are the bytes in `prompt.py`.

    Asserted here as well as in `build-packages.sh` because the two catch different moments: the
    build
    check stops a PACKAGE whose prompt disagrees with its code, and this stops a COMMIT that edited
    one
    without the other.
    """
    assert PROMPT_FILE.is_file(), (
        f"{PROMPT_FILE.relative_to(ROOT)} is missing, so `infra/terraform/modules/upgrade`'s `file()` read would "
        "fail the plan. Regenerate it from `upgrade_lane/prompt.py::SYSTEM_PROMPT`."
    )
    assert PROMPT_FILE.read_text() == SYSTEM_PROMPT, (
        f"{PROMPT_FILE.relative_to(ROOT)} has drifted from `prompt.py::SYSTEM_PROMPT`. Terraform "
        "deploys the FILE, so the agent is running an instruction that is not the one in the "
        "repository. Regenerate and commit."
    )


def test_the_harness_holds_exactly_the_actions_it_needs() -> None:
    actions = _actions(_policy_body(HARNESS_TF, "harness"))
    assert actions == EXPECTED_HARNESS_ACTIONS, (
        f"the upgrade Harness role holds {sorted(actions)}, not "
        f"{sorted(EXPECTED_HARNESS_ACTIONS)}.\n"
        f"added: {sorted(actions - EXPECTED_HARNESS_ACTIONS)}\n"
        f"removed: {sorted(EXPECTED_HARNESS_ACTIONS - actions)}"
    )


def test_the_harness_reaches_exactly_one_gateway_and_declares_no_inline_tool() -> None:
    """The agent has tools now, so this asserts WHICH ONE it reaches -- not that it has none.

    THIS TEST ASSERTED THE OPPOSITE AND THE PREMISE WAS RETIRED, NOT VIOLATED. It required no
    `InvokeGateway` and no `tool` block, on the reasoning that an argument-taking tool is one a
    model
    can point at a neighbour, so with no tools the lane fence needed no enforcement here. That was
    accurate for the design as built: the agent was handed a sealed bundle in its prompt. The user
    then asked for an agent that reads the cluster itself, which makes a tool mandatory rather than
    forbidden.

    WHAT STILL HOLDS IS THE NARROWING. Three separate agents exist in this account -- research,
    brief
    and upgrade -- precisely so their instructions and their reach are not interchangeable, and a
    `gateway/*` grant would make them so. So the assertion moved from "no gateway" to "one gateway,
    by ARN, and no wildcard".

    AND NO INLINE TOOL, which is a different claim worth keeping. An `inline_function` tool comes
    back
    to the CALLER for execution, so declaring one here would put a tool loop back in the proxy --
    the
    loop whose two bugs cost this stage a run each, including reading a single-pass event stream
    twice
    and reporting "the Harness returned no text" for a response that contained text.
    """
    text = _without_comments(HARNESS_TF.read_text())

    assert '"bedrock-agentcore:InvokeGateway"' in text, (
        "the upgrade Harness role cannot invoke a Gateway, so the tool block is advertised to the "
        "model and fails on use -- the half-wired shape that has cost this stage several runs."
    )
    assert "gateway/*" not in text and '"*"' not in text, (
        "the Harness's Gateway grant is not scoped to one ARN. The research and brief agents have "
        "their own Gateways here; a wildcard makes three separate agents reach each other's tools."
    )
    assert "inline_function" not in text, (
        "the upgrade Harness declares an `inline_function` tool. Those come back to the caller, "
        "which puts the retired tool loop back into the proxy."
    )
    assert "allowed_tools" not in text, (
        "the upgrade Harness sets `allowed_tools`. Measured live, naming a tool there SUPPRESSES "
        "it -- the model returns end_turn with no tool call. Which tools it sees is decided by the "
        "Gateway target's `mcp_tool_schema`, not here."
    )


def test_the_proxy_holds_exactly_the_actions_it_needs() -> None:
    actions = _actions(_policy_body(TRIAGE_TF, "triage"))
    assert actions == EXPECTED_TRIAGE_ACTIONS, (
        f"the triage proxy holds {sorted(actions)}, not {sorted(EXPECTED_TRIAGE_ACTIONS)}.\n"
        f"added: {sorted(actions - EXPECTED_TRIAGE_ACTIONS)}\n"
        f"removed: {sorted(EXPECTED_TRIAGE_ACTIONS - actions)}"
    )


def test_neither_role_can_write_anything_or_reach_a_cluster() -> None:
    """The negative half, named action by action so a regression says which line to look at."""
    forbidden = (
        "s3:PutObject",
        "s3:DeleteObject",
        "eks:DescribeCluster",
        "eks:UpdateAddon",
        "eks:CreateAccessEntry",
        "eks:AccessKubernetesApi",
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "states:SendTaskSuccess",
        # `lambda:InvokeFunction` IS FORBIDDEN AGAIN, AND IT HAS BEEN BOTH THINGS. It was forbidden
        # on the reasoning that this proxy "must not invoke anything", true while the agent judged a
        # bundle handed to it in the prompt. Then the agent got ten `inline_function` tools, which
        # come BACK to the caller, so the proxy had to invoke the function holding the cluster
        # credential and forbidding the action would have forbidden the design -- it moved to
        # EXPECTED, narrowed to one resource.
        #
        # It is forbidden once more because the design moved again, to Gateway tools the harness
        # calls itself. No model-named string reaches an AWS API through this proxy now, so the
        # action has nothing legitimate to do here -- and its return would mean the client-side tool
        # loop is back, which is the bug that made every checkpoint of a real lane `unavailable`.
        "lambda:InvokeFunction",
        "codebuild:StartBuild",
        "iam:PassRole",
    )
    for path, policy in ((HARNESS_TF, "harness"), (TRIAGE_TF, "triage")):
        text = _policy_body(path, policy)
        offenders = sorted(action for action in forbidden if f'"{action}"' in text)
        assert not offenders, (
            f"{path.name}'s {policy} policy grants {offenders}. This agent triages a cluster "
            "change; "
            "it must not be able to make one, read a cluster, write a document, or invoke anything."
        )


def test_the_proxy_reads_only_the_diagnosis_prefix() -> None:
    """Every stage document, decision and release lives in the same bucket.

    A proxy that could read them could put a plan or an authorisation into a model's prompt,
    which is
    the one thing this system's read/write split exists to prevent.
    """
    text = _policy_body(TRIAGE_TF, "triage")
    resources = re.findall(r'resources\s*=\s*\["([^"]*bucket_arn[^"]*)"\]', text)
    assert resources, (
        "could not find the proxy's S3 resource. If the shape changed, fix this test -- the "
        "invariant that it reads ONE prefix is still real."
    )
    for resource in resources:
        assert resource.endswith("/upgrade-diagnosis/*"), (
            f"the proxy's S3 grant names {resource!r}, which is not just the diagnosis prefix. "
            "Every plan, decision and release lives in this bucket."
        )


def test_the_harness_grant_is_scoped_to_this_one_harness() -> None:
    """`harness/*` would let this proxy invoke the research and brief agents' Harnesses.

    Three separate Harnesses exist precisely because their instructions must not be interchangeable:
    the research agent is told to transcribe and not reason past a page, and this one is told to
    reason about logs. One proxy able to reach both would make the distinction a convention.
    """
    text = _policy_body(TRIAGE_TF, "triage")
    assert "local.effective_harness_arn" in text, (
        "the `InvokeHarness` grant does not name `local.effective_harness_arn`, so it is not pinned"
        "to the Harness this component owns."
    )
    assert "harness/*" not in text, "the `InvokeHarness` grant carries a wildcard Harness ARN"


def test_only_the_build_role_may_invoke_the_proxy() -> None:
    """A role-side grant with no resource policy reads as "anyone in the account"."""
    text = _without_comments(TRIAGE_TF.read_text())
    assert 'resource "aws_lambda_permission" "build_invokes_triage"' in text, (
        "there is no resource policy on the triage proxy, so the build's role-side "
        "`lambda:InvokeFunction` is half a permission and nothing states who may reach this "
        "function."
    )
    assert "var.upgrade_build_role_arn" in text, (
        "the resource policy does not name `var.upgrade_build_role_arn`, so the principal permitted"
        "to invoke the agent is not the lane's build role."
    )


def test_the_agent_can_only_ask_for_actions_the_code_already_performs() -> None:
    """`TriageAction` has three members and no fourth, and that is the whole boundary.

    A model that returned `{"proposed_action": "patch the deployment"}` cannot be constructed, so
    there is no code path where an invented action is interpreted. Adding a member here means adding
    deterministic code that performs it and a fence that bounds it -- which is the conversation such
    a
    change should have to have.
    """
    assert {member.value for member in TriageAction} == {
        "NONE",
        "WAIT",
        "ROLLBACK",
        "REFER",
    }, (
        f"TriageAction is {[m.value for m in TriageAction]}. A new member is a new mutation a "
        "model can ask for; it needs deterministic code and a fence, not just an enum entry."
    )
    assert {member.value for member in TriageVerdict} == {
        "HEALTHY",
        "SETTLING",
        "UNHEALTHY",
        "REFER",
    }, (
        f"TriageVerdict is {[m.value for m in TriageVerdict]}. `SETTLING` is what makes the "
        "deadline work -- without it an agent asked 45 seconds in must answer HEALTHY on a pod "
        "that is not Ready or UNHEALTHY on one that was about to come up."
    )


def test_every_verdict_must_say_what_it_read_and_pair_with_one_action() -> None:
    """Two properties, and the first one CHANGED when the agent started reading the cluster.

    A silent HEALTHY used to be merely unhelpful -- the agent was judging a bundle somebody else
    captured. It now does the looking, so a verdict with no observations is a verdict that read
    nothing, and there is no honest reading of a cluster nobody looked at.

    The second is the pairing: the verdict is what was observed and the action is the one branch
    that follows from it, so they are not independent fields. A document claiming HEALTHY and
    ROLLBACK cannot be acted on, and refusing it here is cheaper than deciding which half to
    believe.
    """
    import pytest

    with pytest.raises(ValueError, match="no observations"):
        UpgradeTriage(
            component="metrics-server",
            verdict=TriageVerdict.HEALTHY,
            proposed_action=TriageAction.NONE,
        )
    with pytest.raises(ValueError, match="but asks for"):
        UpgradeTriage(
            component="metrics-server",
            verdict=TriageVerdict.HEALTHY,
            proposed_action=TriageAction.ROLLBACK,
            observations=("list_k8s_resources Pod: 1/1 Ready",),
        )
    # The four shapes that ARE allowed, so this file cannot be satisfied by a contract that
    # refuses everything.
    for verdict, action in (
        (TriageVerdict.HEALTHY, TriageAction.NONE),
        (TriageVerdict.SETTLING, TriageAction.WAIT),
        (TriageVerdict.UNHEALTHY, TriageAction.ROLLBACK),
        (TriageVerdict.REFER, TriageAction.REFER),
    ):
        triage = UpgradeTriage(
            component="metrics-server",
            verdict=verdict,
            proposed_action=action,
            observations=("list_k8s_resources Pod: 1/1 Ready, 0 restarts",),
            checkpoint=2,
            elapsed_seconds=120,
        )
        assert triage.proposed_action is action


def test_the_verdict_decides_and_an_absent_one_does_not_pass() -> None:
    """The rule this used to state was `only REFER stops the rollback`, and it is no longer true.

    THAT WAS RIGHT WHEN DETERMINISTIC CODE JUDGED HEALTH and the agent only triaged a rejection.
    The human corrected the division of labour: the agent is now the JUDGE, so HEALTHY stops the
    rollback as well -- the ordinary outcome of a successful upgrade. What must still hold is the
    other end: an EMPTY verdict must not pass.

    So the three branches asserted here are the ones that would silently misbehave if a comparison
    were loosened -- a pattern match, an inequality, a missing quote -- because an unset variable
    would then satisfy them.
    """
    text = _commands(BUILDSPEC)
    # EACH VERDICT IS MATCHED EXACTLY, in a `case`, so an unset variable matches none of them and
    # falls through to the fallthrough branch rather than satisfying a looser test.
    for arm in ("HEALTHY)", "UNHEALTHY)", "REFER)", "SETTLING)"):
        assert arm in text, (
            f"the checkpoint loop has no `{arm}` arm, so one of the four verdicts is not acted on."
        )
    assert 'if [ -z "${DECISION}" ]; then' in text, (
        "there is no branch for the window ending with no decision. Without it an unjudged upgrade "
        "falls through to whatever the default happens to be, and neither default is honest: "
        "passing reports a component as working that nothing checked, and rolling back undoes a "
        "version move that was probably fine."
    )
    # THE SEAL STILL PRECEDES THE ROLLBACK, which is the ordering `plan_rollback` enforces.
    assert text.index("--phase after --require-complete") < text.index("--kind helm-rollback"), (
        "the rollback command is requested before the bundle is sealed."
    )


def test_the_stage_works_with_no_agent_at_all() -> None:
    """The agent is advisory in the direction that matters, and the wiring says so.

    `upgrade_triage_function_arn` defaults to null, the grant is inside a `dynamic` block keyed on
    it,
    and the env var is `coalesce(..., "")` so the shell's `set -u` does not fail the phase that
    decides
    whether to roll a component back. Nothing advisory may gate a run.
    """
    stage = (ROOT / "infra/terraform/modules/platform/upgrade_stage.tf").read_text()
    match = re.search(
        r'variable "upgrade_triage_function_arn" \{.*?default\s*=\s*(\w+)', stage, re.S
    )
    assert match and match.group(1) == "null", (
        "`upgrade_triage_function_arn` does not default to null, so a deployment without the agent "
        "component would fail to plan rather than degrading to a deterministic rollback."
    )
    # THE ENV VAR DEFAULTS TO EMPTY, AND IT MUST NOT DO SO WITH `coalesce`. This assertion used
    # to require exactly `coalesce(var.upgrade_triage_function_arn, "")`, and a plan proved that
    # expression FAILS when the variable is null: Terraform's `coalesce` returns the first non-null
    # and NON-EMPTY argument, so it has no valid answer. The test had enshrined a broken spelling,
    # which is worse than not testing it -- a deployment without the agent component could not have
    # been planned at all.
    assert 'value = var.upgrade_triage_function_arn == null ? ""' in stage, (
        "the TRIAGE_FUNCTION environment variable does not default to an empty string via a "
        "conditional. An ABSENT variable makes `${TRIAGE_FUNCTION}` fail under `set -u` in the "
        "phase that decides whether to roll a component back, which is the one place a missing "
        "configuration must not become a refusal."
    )
    assert "coalesce(var.upgrade_triage_function_arn" not in stage, (
        "TRIAGE_FUNCTION is defaulted with `coalesce`, which skips empty strings as well as nulls "
        "-- so with the variable unset the plan FAILS rather than yielding an empty value."
    )


def test_the_proxy_can_invoke_no_lambda_at_all() -> None:
    """The strongest version of the fence this used to check.

    WHAT THIS REPLACED. The proxy held `lambda:InvokeFunction` scoped to one function, and this test
    asserted that resource was a single ARN and never a pattern -- because the MODEL chose the tool
    name, so a model-chosen string reached this proxy and became an invocation, and a wildcard there
    would not be a loose grant but a model-steered one.

    The read path is Gateway tools now, which the harness calls itself. The proxy relays nothing to
    any AWS API, so there is no string to fence and the grant is gone rather than narrowed. Asserting
    its ABSENCE is what keeps the class of hazard closed: re-adding a client-side tool loop means
    re-adding this action, and that is the change this test refuses.
    """
    text = _without_comments(TRIAGE_TF.read_text())
    assert "lambda:InvokeFunction" not in text.replace(
        'action        = "lambda:InvokeFunction"', ""
    ), (
        "the triage policy grants `lambda:InvokeFunction` again. The proxy does not execute tools -- "
        "the harness calls its Gateway ones itself -- so this grant means a client-side tool loop is "
        "back, and with it the invoke-time `tools` parameter that suppresses the Gateway entirely."
    )
    assert "InvokeTheReadTools" not in text, (
        "the retired read-tools grant is back in the triage policy."
    )
    assert "effective_tools_function_arn" not in text, (
        "the triage policy still names the retired read-tools function."
    )
