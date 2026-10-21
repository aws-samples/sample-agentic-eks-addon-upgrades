"""The just-in-time grant must come off on EVERY exit path, so no path may reach an end without it.

WHAT THIS CATCHES. `Lane open` creates an EKS access entry: a standing cluster-write grant for the
build role, live until something deletes it. `Lane cleanup` is that something. If any path through
the Map's Iterator can reach an end without traversing it -- a success edge somebody rewires, a
`Catch` somebody points at a terminal to "fail fast", a Fail state somebody adds -- the run finishes
and the grant stays. Nothing fails, nothing is logged, and the cluster keeps a writer nobody asked
for. That is the single worst outcome this stage can produce, and it is invisible.

SO THE PROPERTY IS ASSERTED AS A GRAPH REACHABILITY CLAIM, NOT AS AN ORDERING ONE. The design note
said "cleanup before the Fail state, not after it"; the shape actually built is stronger and this
file is what holds it: every state's every successor -- its `Next` AND its `Catch` targets -- leads
to `Lane cleanup`, `Lane cleanup` is the only state with an edge to the Iterator's single end, and
there is no `Fail` inside the Iterator at all. A reachability walk catches all three regressions
with one assertion; a test that checked "cleanup comes before the Fail" would pass a graph that
routed around both.

WHY IT PARSES THE HCL. The Iterator is a Terraform local whose values include conditionals, so
there is no rendered definition to read without an apply -- the same reason
`test_the_pipeline_table_matches_the_deployed_machine.py` transcribes its edges rather than deriving
them. What IS unconditional is the Iterator's own state map, so that is what is read here.

Deliberately no AWS call: the Terraform is the source, so this runs with no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

TERRAFORM = (
    Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/upgrade_stage.tf"
)

# The states that must not be skippable, and the state the lane starts at.
#
# ONE CLEANUP, AND IT WAS TWO. While the agent read the cluster under its own read-only entry the
# lane minted two principals, and two `deleteAccessEntry` calls cannot be one state -- so there was a
# `Lane cleanup` for the reader whose BOTH edges led to a `Lane cleanup writer` for the build, the
# dangerous one going last.
#
# The agent reads through an AgentCore Gateway now, under a standing Terraform-managed access entry
# carrying `AmazonEKSAdminViewPolicy`. The lane mints one principal and deletes one, so the reader states
# are gone and the writer's cleanup carries the name every comment in this stage already used for it.
CLEANUPS = ("Lane cleanup",)
FINAL_CLEANUP = "Lane cleanup"
ENTRY = "Lane open"


def _iterator_block() -> str:
    """The `States = { ... }` body of `local.upgrade_lane_iterator`.

    Sliced by brace counting rather than by regex, because the body contains nested objects
    (`Parameters`, `Retry`, `Catch`) and a non-greedy match to the first `}` stops inside the first
    one. A greedy match runs past the end of the local.
    """
    text = TERRAFORM.read_text()
    anchor = "upgrade_lane_iterator = {"
    assert anchor in text, (
        f"{TERRAFORM.name} no longer declares `upgrade_lane_iterator`. If the Iterator moved, "
        "point this test at it rather than deleting it -- the invariant it guards is still real."
    )
    start = text.index("States = {", text.index(anchor)) + len("States = {")
    depth = 1
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    raise AssertionError("the Iterator's States block is not brace-balanced")


def _states() -> dict[str, str]:
    """Each iterator state's name mapped to its own body text."""
    body = _iterator_block()
    # State declarations sit at one indent level inside `States = {`. Captured by name, then the
    # body is everything up to the next declaration -- which is why the names are found first and
    # sliced afterwards rather than matched with one pattern.
    declarations = [
        (match.group(1), match.start())
        for match in re.finditer(r'"([A-Z][A-Za-z ?]*)" = \{', body)
    ]
    assert declarations, (
        "found no state declarations in the Iterator. The `\"Name\" = {` form this test matches "
        "has probably changed shape -- fix the pattern rather than deleting the test."
    )
    bounds = [position for _, position in declarations] + [len(body)]
    return {
        name: body[bounds[index] : bounds[index + 1]]
        for index, (name, _) in enumerate(declarations)
    }


def _successors(state_body: str) -> set[str]:
    """Every state this one can lead to: its `Next` and every `Catch`'s `Next`.

    ONE PATTERN FOR BOTH, deliberately. A test that read `Next` and forgot `Catch` would pass a
    graph whose failure path skipped the cleanup entirely -- which is the exact regression this
    file exists to catch, since the failure path is the one nobody exercises by hand.
    """
    return set(re.findall(r'Next\s*=\s*"([^"]+)"', state_body))


def _is_terminal(state_body: str) -> bool:
    return bool(re.search(r"End\s*=\s*true", state_body))


def test_the_iterator_has_exactly_one_end_and_the_cleanup_is_its_only_predecessor() -> None:
    states = _states()
    ends = sorted(name for name, body in states.items() if _is_terminal(body))
    assert len(ends) == 1, (
        f"the lane Iterator has {len(ends)} terminal states ({ends}). One end is what makes the "
        "cleanup unskippable: a second end is a second way to finish, and whoever adds it will "
        "not be thinking about the access entry."
    )
    end = ends[0]
    predecessors = sorted(
        name for name, body in states.items() if end in _successors(body)
    )
    assert predecessors == [FINAL_CLEANUP], (
        f"{end!r} is reachable from {predecessors}, not from {[FINAL_CLEANUP]} alone. Every path "
        "out of this lane must pass through the state that deletes the WRITE access entry, "
        "which is the last one to come off."
    )


def test_no_path_through_the_lane_reaches_an_end_without_the_cleanup() -> None:
    """The whole property, as a walk: remove a cleanup and nothing may still finish.

    ASSERTED BY DELETING THE NODE, which is the clearest form of "cannot be skipped": if the graph
    with a cleanup taken out can still reach a terminal state from `Lane open`, then a path exists
    that leaves a grant standing. No path length to reason about and no ordering to argue over.
    """
    states = _states()
    assert ENTRY in states, f"the lane Iterator no longer starts at {ENTRY!r}"

    # DELETED ONE AT A TIME, so a failure names WHICH cleanup became skippable. Removing both at
    # once would prove only that some cleanup is needed, not that each is.
    for cleanup in CLEANUPS:
        assert cleanup in states, (
            f"{cleanup!r} is gone from the lane Iterator. The access entry created at {ENTRY!r} is "
            "a standing cluster grant, and it carries cluster-admin for the life of the lane; "
            "something must delete it on every path."
        )
        reachable: set[str] = set()
        frontier = [ENTRY]
        while frontier:
            name = frontier.pop()
            if name in reachable or name == cleanup:
                continue
            reachable.add(name)
            frontier.extend(sorted(_successors(states.get(name, ""))))

        still_terminal = sorted(
            name for name in reachable if _is_terminal(states.get(name, ""))
        )
        assert not still_terminal, (
            f"with {cleanup!r} removed from the graph, {still_terminal} are still reachable from "
            f"{ENTRY!r} and still terminal. That is a path which finishes the lane without "
            "deleting an access entry, leaving a standing cluster grant nobody asked for."
        )


def test_every_state_before_the_cleanup_catches_into_it() -> None:
    """A `Next` to the cleanup is not enough: the FAILURE edge has to go there too.

    Called out separately from the walk so a regression names the actual problem. A state whose
    `Catch` pointed elsewhere -- or which had no `Catch` at all, so an error propagated out of the
    Map -- would abandon the access entry on the one path where the lane is most likely to have
    already changed something.
    """
    states = _states()
    for name, body in states.items():
        # The final cleanup and the terminal are the two that do not catch onward: the first because
        # its own Catch reaches the terminal, the second because it IS the terminal.
        if name == FINAL_CLEANUP or _is_terminal(body):
            continue
        assert "Catch" in body, (
            f"{name!r} declares no `Catch`, so an error there propagates out of the Iterator and "
            f"{CLEANUPS!r} never run. Every state before the cleanups must catch into one."
        )
        catch_targets = set(
            re.findall(r'Next\s*=\s*"([^"]+)"', body[body.index("Catch") :])
        )
        # EVERY state before the cleanups catches into the FIRST one; the first catches into the
        # second. Either is a route that still closes both entries, and nothing else is.
        expected = {FINAL_CLEANUP} if name == CLEANUPS[0] else {CLEANUPS[0]}
        assert catch_targets == expected, (
            f"{name!r} catches to {sorted(catch_targets)} rather than to {sorted(expected)}. A "
            "failure path that does not close the lane is the leak this stage is built to prevent."
        )


def test_the_lane_open_failure_path_also_runs_the_cleanup() -> None:
    """The case that looks redundant and is not.

    `createAccessEntry` can succeed at the API and still fail the state -- a `States.Timeout`, a
    response the integration could not read. The entry exists and the state machine believes the
    call failed. Routing that to a terminal would leak precisely the grant this design exists to
    bound, so `Lane open`'s Catch names the cleanup like every other.
    """
    body = _states()[ENTRY]
    assert CLEANUPS[0] in _successors(body), (
        f"{ENTRY!r} cannot reach {CLEANUPS[0]!r}. A create that times out after the API succeeded "
        "leaves an access entry behind, so even the failure path must delete one."
    )


def test_the_iterator_contains_no_fail_state() -> None:
    """A Fail inside the Iterator is a terminal somebody can later point an edge at.

    It would also abandon the components after it and skip the report -- and the report is this
    stage's deliverable. A lane that could not finish its job is a REFERRED ROW; the execution ends
    red at `Anything referred?`, after the report is durable in S3.
    """
    body = _iterator_block()
    offenders = re.findall(r'Type\s*=\s*"Fail"', body)
    assert not offenders, (
        "the lane Iterator declares a Fail state. Inside a Map that abandons every component after "
        "this one and skips the report, and it adds a terminal that does not pass through "
        f"{FINAL_CLEANUP!r}. A lane that cannot finish is a row in the report, not a failure."
    )


def test_the_write_entry_is_deleted_and_nothing_else_needs_to_be() -> None:
    """One entry is minted per lane, so one must come off -- and it is the dangerous one.

    THIS USED TO ASSERT TWO, IN ORDER: the agent's reader entry first, the build's writer entry last,
    so that if only one delete got through it was the write grant still pending and therefore still
    being retried. There is one now, and it is the write grant, so the ordering property has nothing
    left to order -- but the thing the ordering protected is unchanged and is asserted here.

    Asserted against the PrincipalArn the state names, not against the state name, because the name
    could change without the behaviour changing and this test would not notice.
    """
    states = _states()
    cleanup = states[FINAL_CLEANUP]

    assert "deleteAccessEntry" in cleanup, (
        f"{FINAL_CLEANUP!r} no longer calls `deleteAccessEntry`, so the lane's access entry "
        "survives the lane that opened it."
    )
    assert "aws_iam_role.upgrade_build" in cleanup, (
        f"{FINAL_CLEANUP!r} does not delete the BUILD role's entry, which is the cluster-WRITE "
        "grant and the only one this lane mints."
    )
    assert "upgrade_tools_role_arn" not in cleanup, (
        f"{FINAL_CLEANUP!r} still names the retired read-tools role. That principal and its entry "
        "are deleted; deleting an entry for it would fail on every lane."
    )


def test_the_lane_opens_one_entry_and_goes_straight_to_the_build() -> None:
    """The chain is open, grant, build -- with no second entry minted in between.

    WHAT THIS REPLACED, because the deletion is the point rather than the simplification: there was a
    `Lane open reader` between `Lane grant` and `Lane upgrade` that minted the agent's read-only
    entry, and this test asserted that ordering hop by hop. The agent's read path is an AgentCore
    Gateway with a standing entry of its own, so a lane that minted one would be granting a principal
    that already has what it needs -- and holding a second cluster grant open for the run to do it.

    Asserted hop by hop for the original reason: a one-hop check would pass on a graph that opened
    the entry and never reached the build.
    """
    states = _states()
    assert "Lane open reader" not in states, (
        "'Lane open reader' is back. The agent reads through a Gateway whose access entry is "
        "standing and Terraform-managed, so a per-lane read entry is a second grant open for the "
        "life of the run with nothing asking for it."
    )
    assert "Lane grant" in _successors(states[ENTRY]), (
        f"{ENTRY!r} does not lead to 'Lane grant', so the access entry is created and the policy "
        "that makes it useful is never associated."
    )
    assert "Lane upgrade" in _successors(states["Lane grant"]), (
        "'Lane grant' does not lead to 'Lane upgrade', so the lane is granted cluster-admin and the "
        "build that is supposed to use it never runs."
    )
