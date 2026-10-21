"""The Choice that guards the gate must read the path the payload actually has.

WHAT THIS CATCHES, AND IT IS NOT HYPOTHETICAL -- IT WAS THE FIRST DRAFT OF THIS FEATURE. The
obvious path to the approved steps is `$.upgrade_approval.approved_steps`, and it does not
exist. `upgrade_decision_watcher._resume` calls `SendTaskSuccess` with an output of
`{"upgrade_approval": {...}}`, and `Upgrade approval` sets `ResultPath = "$.upgrade_approval"`.
`ResultPath` places the WHOLE task result at that path, so the object nests under its own name
and the live path is DOUBLED:

    $.upgrade_approval.upgrade_approval.approved_steps

Read from a SUCCEEDED execution (`toast-191932`), whose `Upgrade approval` output carries
`upgrade_approval.upgrade_approval.approved_steps = [1, 2]`.

WHY THE SINGLE-NESTED PATH IS A SILENT FAILURE RATHER THAN AN ERROR. A Choice whose Variable
resolves to nothing does not fail -- it takes its `Default`. The Default here is `Done`. So the
whole release gate would be skipped on every execution, the run would go green, and nothing
anywhere would report that a state machine with a gate in it never once used the gate. That is
the same class of drift `test_the_pipeline_table_matches_the_deployed_machine.py` exists for,
and it is why the path is asserted as a string rather than left to a reader to get right.

Deliberately no AWS call: the Terraform is the source, so this runs with no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/workflow.tf"

# The doubled path, verified against the live execution named in the module docstring.
EXPECTED_PREFIX = "$.upgrade_approval.upgrade_approval"


def _text() -> str:
    return WORKFLOW.read_text()


def _local(name: str) -> str:
    """The body of one `<name> = { ... }` local, comments stripped."""
    match = re.search(rf"\n  {re.escape(name)} = \{{(.*?)\n  \}}\n", _text(), re.S)
    assert match, (
        f"could not find the `{name}` local in workflow.tf. If it was renamed, fix this test "
        "rather than deleting it."
    )
    return "\n".join(
        line
        for line in match.group(1).splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_result_path_local_is_the_doubled_one() -> None:
    """One local holds the path, so the four states that read it cannot disagree."""
    match = re.search(r'upgrade_approval_result\s*=\s*"([^"]+)"', _text())
    assert match, (
        "could not find the `upgrade_approval_result` local. The path to the resumed approval "
        "output belongs in one place: four states read it, and three of them agreeing is a "
        "gate that half works."
    )
    assert match.group(1) == EXPECTED_PREFIX, (
        f"`upgrade_approval_result` is {match.group(1)!r}, not {EXPECTED_PREFIX!r}. "
        "`upgrade_decision_watcher._resume` sends `{\"upgrade_approval\": {...}}` and the "
        "state's ResultPath is `$.upgrade_approval`, so the object nests under its own name. "
        "The single-nested path resolves to nothing, the Choice takes its Default, and the "
        "release gate is skipped on every run with nothing reporting it."
    )


def test_the_choice_tests_the_first_element_of_the_approved_steps() -> None:
    body = _local("anything_approved_state")
    variable = re.search(r"Variable\s*=\s*(.+)", body)
    assert variable, "`Anything approved?` declares no Choice Variable"
    value = variable.group(1).strip()
    assert "${local.upgrade_approval_result}" in value, (
        f"`Anything approved?` reads {value}. It must interpolate "
        "`local.upgrade_approval_result` rather than spelling the path out, so the doubled "
        "path is stated once and this test guards one string instead of four."
    )
    assert value.endswith('.approved_steps[0]"'), (
        f"`Anything approved?` reads {value}. JSONPath Choice rules have no array-length "
        "comparator, so `IsPresent` on index 0 is how 'the list is non-empty' is written. "
        "Testing the list itself with `IsPresent` would be TRUE for an empty list and the gate "
        "would page a person with nothing to authorise."
    )
    assert re.search(r"IsPresent\s*=\s*true", body), (
        "`Anything approved?` does not use `IsPresent = true`. Any other comparator on an "
        "array index is either a type error at runtime or a claim about the step order's value "
        "rather than about the list being non-empty."
    )


def test_an_empty_or_unreadable_list_goes_to_done() -> None:
    """`Done` is the fail-closed direction for a state whose job is to GRANT authority.

    This is the opposite choice from `Anything to upgrade?` one state up, whose Default
    proceeds, and the difference is what the two states do. That one invokes a cheap advisory
    stage, so proceeding on an unreadable count costs one Lambda call. This one wakes a person
    to authorise a cluster change, and `Done` authorises nothing.
    """
    body = _local("anything_approved_state")
    default = re.search(r'Default\s*=\s*"([^"]+)"', body)
    assert default, "`Anything approved?` declares no Default"
    assert default.group(1) == "Done", (
        f"`Anything approved?` defaults to {default.group(1)!r}. An empty approved list means "
        "there is nothing to authorise, and a payload this state cannot read means it cannot "
        "know there is. Both must reach the outcome that grants no authority."
    )


def test_the_upgrade_approval_now_hands_off_to_the_choice() -> None:
    """The gate is only reachable if the state before it names it.

    Asserted because the edit that adds six states and forgets this one line produces a
    definition Step Functions accepts -- `Anything approved?` is simply unreachable -- and
    every run behaves exactly as it did before the gate existed.
    """
    body = _local("upgrade_approval_state")
    successor = re.search(r'Next\s*=\s*"([^"]+)"', body)
    assert successor, "`Upgrade approval` declares no Next"
    assert successor.group(1) == "Anything approved?", (
        f"`Upgrade approval` still goes to {successor.group(1)!r}. The release gate is "
        "unreachable unless this edge names the Choice, and an unreachable state is accepted "
        "by Step Functions without complaint."
    )
