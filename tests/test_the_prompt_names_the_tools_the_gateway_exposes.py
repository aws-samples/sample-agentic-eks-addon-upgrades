"""The agent is told about exactly the tools it has, under the names it will see them by.

WHY THIS TEST EXISTS, and it is not a style check. The upgrade agent's read path moved from ten
hand-written `inline_function` tools to four EKS MCP tools behind a Gateway. The prompt kept naming
the ten. Nothing failed: the model was told it could call `read_my_pods`, the tools on offer were
`eks-mcp___list_k8s_resources` and friends, and the disagreement surfaced only as a lane that spent
its whole 900-second window reporting `unavailable` and then rolled a healthy component back.

THREE THINGS HAVE TO AGREE and only two of them are in this repository:

    infra/terraform/modules/upgrade/mcp_tools.json    what the Gateway target DECLARES     (checked here)
    prompt.py MCP_TOOLS                 what the agent is TOLD it has        (checked here)
    the live Gateway                    what the model is actually OFFERED   (not checkable here)

So this holds the first two together and the third is held by the target being built from the same
JSON file. That is the honest limit of it: a tool the server stops serving still passes this test.

THE PREFIX IS THE PART MOST LIKELY TO ROT. An AgentCore Gateway namespaces a target's tools with the
TARGET's name, so `infra/terraform/modules/upgrade/gateway.tf`'s `name = "eks-mcp"` is what turns
`list_k8s_resources` into `eks-mcp___list_k8s_resources`. Renaming that target silently renames every
tool the model can call, and the prompt would go on naming the old ones -- so the prefix is asserted
against the Terraform, not hard-coded twice.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from eks_addon_readiness.upgrade_lane.prompt import MCP_TOOLS, user_prompt

ROOT = Path(__file__).resolve().parents[1]
TOOLS_JSON = ROOT / "infra" / "terraform" / "modules" / "upgrade" / "mcp_tools.json"
GATEWAY_TF = ROOT / "infra" / "terraform" / "modules" / "upgrade" / "gateway.tf"

# The separator the Gateway puts between a target name and a tool name. Three underscores, from the
# live stream: `eks-mcp___list_k8s_resources`.
SEPARATOR = "___"


@pytest.fixture(scope="module")
def declared_tool_names() -> set[str]:
    """The unprefixed tool names the Gateway target declares."""
    payload = json.loads(TOOLS_JSON.read_text(encoding="utf-8"))
    assert isinstance(payload, dict) and "tools" in payload, (
        "mcp_tools.json must be an object with a 'tools' array -- a bare array is accepted by "
        "`terraform plan` and rejected by the apply with "
        "'mcpToolSchema must be an object with tools array'."
    )
    names = {tool["name"] for tool in payload["tools"]}
    assert names, "the target declares no tools, so the agent would have nothing to call"
    return names


@pytest.fixture(scope="module")
def target_prefix() -> str:
    """The Gateway target's name, read from the Terraform that creates it."""
    text = GATEWAY_TF.read_text(encoding="utf-8")
    # The target resource's own `name`, which is a bare literal rather than an interpolation -- that
    # is what distinguishes it from the role and gateway names above it in the same file.
    match = re.search(
        r'resource\s+"aws_bedrockagentcore_gateway_target"[^{]*\{.*?^\s*name\s*=\s*"([^"]+)"',
        text,
        re.DOTALL | re.MULTILINE,
    )
    assert match, "could not find the gateway target's name in gateway.tf"
    return match.group(1)


def test_the_prompt_names_every_declared_tool_and_no_others(
    declared_tool_names: set[str], target_prefix: str
) -> None:
    """The inventory the agent is shown is exactly the tool surface, prefix included."""
    expected = {f"{target_prefix}{SEPARATOR}{name}" for name in declared_tool_names}
    listed = {name for name, _description in MCP_TOOLS}

    missing = expected - listed
    extra = listed - expected
    assert not missing, (
        f"the Gateway offers {sorted(missing)} and the prompt never mentions them, so the agent "
        "will not know to call them"
    )
    assert not extra, (
        f"the prompt tells the agent it may call {sorted(extra)}, which the Gateway does not "
        "declare. This is the exact shape of the bug that cost a lane its whole health window: the "
        "prompt named ten tools that were not on offer."
    )


def test_every_named_tool_carries_the_target_prefix(target_prefix: str) -> None:
    """A bare server-side name in the prompt is a tool the model cannot call."""
    for name, _description in MCP_TOOLS:
        assert name.startswith(f"{target_prefix}{SEPARATOR}"), (
            f"{name!r} is missing the Gateway's {target_prefix!r} namespace. Unprefixed, it names "
            "nothing the model can reach, and the failure is quiet -- the model improvises another "
            "tool or answers without reading."
        )


def test_every_named_tool_says_what_it_is_for(declared_tool_names: set[str]) -> None:
    """A name with no description is a tool the agent will not reach for."""
    for name, description in MCP_TOOLS:
        assert description.strip(), f"{name} is listed with no description"
        assert len(description) > 40, (
            f"{name}'s description is too short to tell the agent when to use it: "
            f"{description!r}"
        )


def test_the_rendered_prompt_actually_contains_the_tool_names() -> None:
    """The constant is not enough: the prompt has to render it.

    Held separately because the inventory is built with a column width -- a name longer than the
    column silently ran into its description before this was widened, and the model would have been
    shown a mangled name.
    """
    rendered = user_prompt(
        component="metrics-server",
        cluster="my-eks-cluster",
        namespace="kube-system",
        release="metrics-server",
        install_method="HELM",
        installed="0.8.0",
        applied="3.14.0",
        summary="upgraded",
        floor_facts=(),
        elapsed_seconds=180,
        deadline_seconds=900,
        checkpoint=1,
        is_final=False,
    )
    for name, _description in MCP_TOOLS:
        assert f"  {name} " in rendered or f"  {name}\n" in rendered, (
            f"{name} does not appear intact in the rendered prompt -- check the column width in "
            "the inventory line"
        )


def test_user_prompt_defaults_to_the_gateway_tools() -> None:
    """Omitting `tools` must not produce a prompt that names nothing.

    The parameter was required and every caller passed the ten inline tools. Defaulting it means a
    new caller gets the right inventory by doing nothing, rather than the wrong one by copying an
    old call.
    """
    rendered = user_prompt(
        component="c",
        cluster="k",
        namespace="n",
        release="r",
        install_method="HELM",
        installed="1",
        applied="2",
        summary="s",
        floor_facts=(),
        elapsed_seconds=1,
        deadline_seconds=900,
        checkpoint=1,
        is_final=False,
    )
    assert MCP_TOOLS[0][0] in rendered
