"""An EKS add-on name and the workload identity a reader reports denote the same component.

THE THIRD LINK IN THE MANAGED-ADD-ON FAILURE, and the one that cost the wall-clock time. With the
mutation's rejection laundered into a success the lane ran its full health window, and every one of
the four checkpoints came back `unavailable` -- for a reason of its own. From the run's own
`checkpoints.jsonl` (`00000000-0000-4000-8000-000000000001`, component vpc-cni):

    180s   TriageUnavailable: names component 'aws-vpc-cni (aws-node DaemonSet)', not 'vpc-cni'.
    420s   ... 'aws-vpc-cni (aws-node DaemonSet, kube-system)', not 'vpc-cni'.
    660s   ... 'aws-vpc-cni (aws-node DaemonSet, kube-system)', not 'vpc-cni'.
    900s   ... 'amazon-vpc-cni (aws-node DaemonSet, kube-system)', not 'vpc-cni'.

The guard is right to check identity: a triage about a different component must not be filed under
this lane. It was wrong to compare strings. The lane is keyed on the add-on name `eks update-addon`
takes; an agent that goes and looks names the project and the workload it read. Both denote vpc-cni.

WHAT IS ASSERTED. That the reconciliation accepts every spelling the live run produced, that it
still refuses a genuinely different component, that it names no component of its own -- the standing
requirement that the upgrade lane must work for any CNCF add-on or Helm upgrade and must never be
written against one component's name -- and that the guard in `upgrade_triage.run_agent` uses it.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from eks_addon_readiness.stages import upgrade_triage
from eks_addon_readiness.upgrade_lane import naming
from eks_addon_readiness.upgrade_lane.models import TriageVerdict
from eks_addon_readiness.upgrade_lane.naming import (
    comparable_name,
    denotes_same_component,
)

# Verbatim from the run's `checkpoints.jsonl`, in the order the agent produced them.
ANSWERED_LIVE = (
    "aws-vpc-cni (aws-node DaemonSet)",
    "aws-vpc-cni (aws-node DaemonSet, kube-system)",
    "amazon-vpc-cni (aws-node DaemonSet, kube-system)",
)

# Every component this deployment's lane catalogue carries. Used to prove the reconciliation does
# not collapse two of them into one -- which is the risk a looser rule would carry.
LANE_COMPONENTS = (
    "coredns",
    "kube-proxy",
    "vpc-cni",
    "eks-pod-identity-agent",
    "metrics-server",
)


@pytest.mark.parametrize("answered", ANSWERED_LIVE)
def test_the_answers_the_live_run_refused_are_accepted(answered: str) -> None:
    """The red test for this change: these four checkpoints were thrown away for a name."""
    assert denotes_same_component(answered, expected="vpc-cni"), (
        f"{answered!r} was refused against 'vpc-cni'. It is the same component: the lane is keyed "
        "on the name `eks update-addon` takes and the agent answered with the project's name and "
        "the workload it read."
    )


def test_a_different_component_is_still_refused() -> None:
    """The guard's purpose survives. An answer about another component is evidence of a bad ask."""
    for expected in LANE_COMPONENTS:
        for answered in LANE_COMPONENTS:
            if answered == expected:
                continue
            assert not denotes_same_component(answered, expected=expected), (
                f"{answered!r} was accepted as a triage of {expected!r}. Filing one component's "
                "reading under another's lane is what the identity check exists to prevent."
            )


def test_a_neighbour_that_merely_extends_the_name_is_refused() -> None:
    """`vpc-cni-metrics-helper` is a different add-on that starts with this one's name.

    This is why the rule is positional -- one reduced name must END with the other -- rather than a
    containment test. A vendor prefix precedes a component's name; a different component extends it.
    """
    assert not denotes_same_component("vpc-cni-metrics-helper", expected="vpc-cni")
    assert not denotes_same_component("coredns-autoscaler", expected="coredns")


def test_separator_and_case_differences_are_not_a_different_component() -> None:
    """`CoreDNS`, `coredns` and `core-dns` are one component spelled three ways."""
    for answered in ("CoreDNS", "core-dns", "Core DNS (Deployment, kube-system)"):
        assert denotes_same_component(answered, expected="coredns"), answered


def test_an_unrelated_workload_name_needs_data_and_is_refused_without_it() -> None:
    """`aws-node` shares no text with `vpc-cni`, and no string rule can relate the two.

    Stated as a test rather than left implicit: the caller may pass such a name as an alias -- the
    lane already holds `addon_pod_selector = k8s-app=aws-node` -- and without one the answer is
    refused rather than guessed at.
    """
    assert not denotes_same_component("aws-node (DaemonSet)", expected="vpc-cni")
    assert denotes_same_component(
        "aws-node (DaemonSet)", expected="vpc-cni", aliases=("aws-node",)
    )


def test_an_empty_answer_is_not_a_match() -> None:
    assert not denotes_same_component("", expected="vpc-cni")
    assert not denotes_same_component("()", expected="vpc-cni")
    assert comparable_name("   ") == ""


def test_the_reconciliation_names_no_component() -> None:
    """The standing requirement, asserted on the source.

    The lane's RBAC and fences are generic so any CNCF add-on or Helm release can use them. A
    reconciliation carrying `vpc-cni`, `aws-node` or any other component string would be the
    per-component table that requirement forbids -- and it would have to be edited for every new
    add-on, which is exactly how the previous per-component fences rotted.
    """
    tree = ast.parse(Path(naming.__file__).read_text(encoding="utf-8"))

    # EVERY STRING THE MODULE CAN ACT ON, which is every string constant that is not a docstring.
    # Read from the syntax tree rather than by scanning lines, because the docstrings and their
    # doctests DO name components -- they cite the live evidence and demonstrate the rule, which is
    # prose about the defect and not a table the code consults.
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        )
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    for literal in literals:
        for component in (*LANE_COMPONENTS, "aws-node", "kube-dns", "amazon"):
            assert component not in literal, (
                f"the reconciliation acts on the literal {literal!r}, which names "
                f"{component!r}. It must be a rule about decoration, not a table of "
                "components: the upgrade lane has to work for an add-on nobody has added yet."
            )


class _FakeHarness:
    """Replays one scripted answer, recording nothing else. Same shape as the proxy's own test."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def invoke_harness(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "stream": iter(
                [
                    {"messageStart": {"role": "assistant"}},
                    {
                        "contentBlockDelta": {
                            "contentBlockIndex": 0,
                            "delta": {"text": self.text},
                        }
                    },
                    {"contentBlockStop": {"contentBlockIndex": 0}},
                    {"messageStop": {"stopReason": "end_turn"}},
                ]
            )
        }


def _install(monkeypatch: pytest.MonkeyPatch, component: str) -> _FakeHarness:
    fake = _FakeHarness(
        json.dumps(
            {
                "component": component,
                "verdict": "HEALTHY",
                "proposed_action": "NONE",
                "observations": [
                    "pods.json: both aws-node pods are Running and Ready with no new restarts"
                ],
            }
        )
    )
    import sys
    import types

    module = types.ModuleType("boto3")
    module.client = lambda *_a, **_k: fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", module)
    monkeypatch.setattr(
        upgrade_triage, "HARNESS_ARN", "arn:aws:bedrock-agentcore:x:1:harness/h"
    )
    return fake


def test_the_proxy_accepts_the_decorated_answer_rather_than_discarding_the_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the guard: the live answer now yields a verdict instead of a refusal."""
    _install(monkeypatch, "amazon-vpc-cni (aws-node DaemonSet, kube-system)")
    triage = upgrade_triage.run_agent(
        session_id="upgrade-00000001-vpc-cni-cp1", prompt="p", component="vpc-cni"
    )
    assert triage.verdict is TriageVerdict.HEALTHY, (
        "the checkpoint was still thrown away. Four of these in a row spent the whole 900-second "
        "window and ended in an automatic rollback."
    )


def test_the_proxy_still_refuses_an_answer_about_another_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A triage of coredns is not a triage of this lane, however it is spelled."""
    _install(monkeypatch, "coredns (Deployment, kube-system)")
    with pytest.raises(upgrade_triage.TriageUnavailable, match="does not denote"):
        upgrade_triage.run_agent(
            session_id="upgrade-00000001-vpc-cni-cp1", prompt="p", component="vpc-cni"
        )


def test_the_handler_passes_the_lanes_other_name_for_its_own_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release name is on every lane payload, so the guard gets it without a lookup.

    Asserted on the call rather than on the outcome: the aliases have to reach `run_agent`, or a
    Helm lane whose release name differs from its component key loses the same checkpoints for the
    same reason.
    """
    seen: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise upgrade_triage.TriageUnavailable("not asked")

    monkeypatch.setattr(upgrade_triage, "run_agent", _capture)
    upgrade_triage.handler(
        {
            "component": "vpc-cni",
            "release": "aws-vpc-cni",
            "run_id": "00000001",
            "deadline_seconds": 900,
            "checkpoint": 1,
            "elapsed_seconds": 180,
        }
    )
    assert "aws-vpc-cni" in tuple(seen.get("aliases") or ()), (
        f"the handler passed aliases {seen.get('aliases')!r}. The lane's release name is a name "
        "for the same component and it is already on the payload."
    )
