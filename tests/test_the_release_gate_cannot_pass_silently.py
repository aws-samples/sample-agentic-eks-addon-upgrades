"""A release nobody authorised must FAIL the execution. It must never reach `Done`.

WHAT THIS CATCHES. The whole value of this gate is the difference between "a named principal
approved this release" and "the run finished". If a rejection, an expiry, or an error starting
the automation routed onward instead of failing, both outcomes would look identical from
outside: `Done`, the same final payload shape, the same green execution in the console. The
gate would still exist, still send mail, still pause -- and mean nothing.

WHY A CATCH TO `Done` IS THE TEMPTING BUG rather than an unlikely one. Three of the brief
stage's edges legitimately catch onward, because a brief is advisory and must not fail a run;
`brief_exit` exists to make that uniform. Someone copying that pattern into the release gate
would be following the file's own most-repeated idiom, and it would be exactly wrong here. So
the asymmetry is asserted rather than trusted to be noticed.

Deliberately no AWS call: the Terraform is the source, so this runs with no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/workflow.tf"

# The locals that make up the release gate. Named individually rather than matched by prefix
# so that adding a state without deciding what its failures do fails this test.
RELEASE_LOCALS = (
    "anything_approved_state",
    "release_gate_state",
    "release_waiting_state",
    "release_poll_state",
    "release_decided_state",
    "release_approved_state",
    "release_rejected_state",
)

# The two states that call SSM, and therefore the two that can fail in transport.
CALLING_STATES = ("release_gate_state", "release_poll_state")


def _local(name: str) -> str:
    """The body of one `<name> = { ... }` local from workflow.tf, comments stripped.

    Comments come out for the reason the sibling policy test gives: these blocks explain in
    prose why a Catch does NOT go to `Done`, and scanning the raw text would match the
    explanation and fail a definition that is right.
    """
    text = WORKFLOW.read_text()
    match = re.search(rf"\n  {re.escape(name)} = \{{(.*?)\n  \}}\n", text, re.S)
    assert match, (
        f"could not find the `{name}` local in workflow.tf. If it was renamed, fix this test "
        "rather than deleting it -- the invariant it guards is still real."
    )
    return "\n".join(
        line
        for line in match.group(1).splitlines()
        if not line.lstrip().startswith("#")
    )


def _catch_destinations(body: str) -> list[str]:
    """Every `Next` a `Catch` in `body` routes to.

    MATCHED AS `[{ ... }]` RATHER THAN `[ ... ]`, and the difference is not cosmetic. A
    non-greedy `\\[(.*?)\\]` stops at the FIRST `]` in the block, which is the one closing
    `ErrorEquals = ["States.ALL"]` -- so the capture ends before `Next` is ever reached. The
    first draft of this file did exactly that: one assertion failed loudly and its sibling
    PASSED, having looked at a fragment containing no destinations at all. A green test proving
    nothing is worse here than a red one, which is the same lesson
    `test_the_pipeline_table_matches_the_deployed_machine.py` records about its own table scan.
    """
    found: list[str] = []
    for catch in re.findall(r"Catch\s*=\s*\[\{(.*?)\}\]", body, re.S):
        found.extend(re.findall(r'Next\s*=\s*"([^"]+)"', catch))
    return found


def test_every_state_that_calls_ssm_catches_to_the_fail_state() -> None:
    for name in CALLING_STATES:
        body = _local(name)
        assert re.search(r"Catch\s*=\s*\[", body), (
            f"`{name}` declares no Catch. It calls an AWS API, so it can be denied, throttled "
            "or refused -- and an uncaught error there fails the execution with a raw states "
            "error rather than naming the release gate as the thing that stopped."
        )
        destinations = _catch_destinations(body)
        assert destinations, (
            f"`{name}` has a Catch that names no destination. The `Catch = [{{ ... }}]` shape "
            "this test matches has probably changed -- fix the pattern rather than deleting the "
            "test, and read `_catch_destinations` on why a sloppy pattern here passes silently."
        )
        assert destinations == ["Release rejected"], (
            f"`{name}`'s Catch routes to {destinations}. Every way this state can fail means "
            "nobody authorised the release, so every one of them must land on the state that "
            "says so."
        )


def test_no_release_state_routes_a_failure_onward() -> None:
    """The asymmetry with the brief, asserted where it can be checked.

    A `Catch` naming `Done` -- or naming `Release approved`, which is the same mistake wearing
    a different hat -- would turn an unauthorised release into a successful run.
    """
    for name in RELEASE_LOCALS:
        for destination in _catch_destinations(_local(name)):
            assert destination == "Release rejected", (
                f"`{name}` has a Catch routing to {destination!r}. A brief may catch onward "
                "because it is advisory; an authorisation may not. The only destination for a "
                "failure here is `Release rejected`."
            )


def test_the_rejection_state_is_a_fail_and_not_a_pass() -> None:
    body = _local("release_rejected_state")
    kind = re.search(r'Type\s*=\s*"([A-Za-z]+)"', body)
    assert kind, "`release_rejected_state` declares no Type"
    assert kind.group(1) == "Fail", (
        f"`Release rejected` is a {kind.group(1)!r}. A Pass would carry the payload onward and "
        "the execution would SUCCEED having authorised nothing, which is the one outcome this "
        "state exists to prevent."
    )
    assert re.search(r'Error\s*=\s*"ReleaseRejected"', body), (
        "`Release rejected` declares no `Error = \"ReleaseRejected\"`. The error name is what "
        "a reader of the console and a future alarm match on; an unnamed Fail is a red "
        "execution nobody can classify."
    )
    assert re.search(r"Cause\s*=", body), (
        "`Release rejected` declares no Cause. The three ways to arrive here -- rejected, "
        "expired, could not start -- look identical without one, and the person reading it "
        "needs to know whether to re-run or to go and ask somebody."
    )


def test_no_release_state_carries_a_retry_on_the_human_wait() -> None:
    """Re-entering the wait would ask a second time and abandon the first request.

    `Upgrade approval` documents this: a `States.Timeout` must not be retried, because
    re-entering mints a fresh capability and the person who was merely slow finds their answer
    attached to nothing. The same holds here -- a retried gate would start a SECOND automation
    while the first still sits in the console waiting for the same approver.
    """
    body = _local("release_decided_state")
    assert "Retry" not in body, (
        "`Release decided?` carries a Retry. It is a Choice over a status that was just read; "
        "retrying it cannot change the answer and only obscures where the run is."
    )
