"""Both modes of the report stage looked for `run_id` where the state machine does not put it.

WHAT THIS CATCHES, AND IT FAILED THE FIRST END-TO-END RUN. `Upgrade lanes` and `Upgrade report` pass
the whole run payload, and both modes read `event["run_id"]`. The state has no top-level `run_id` --
the authoritative one is at `release.run_id`, written by `Release recorded`. So the lookup returned
the empty string and the empty string went straight into an S3 key:

    releases/cluster=my-eks-cluster/run=/release.json

The lane's IAM policy does not match that shape, so S3 answered AccessDenied, and the planner
reported that nothing said the run was authorised. THE MESSAGE NAMED A PERMISSIONS PROBLEM AND THE
CAUSE WAS AN EMPTY PATH SEGMENT -- `run=` with nothing after it is the only tell, and it is easy to
read past when the rest of the key is correct.

WHY NO EXISTING TEST CAUGHT IT. Every test built its own event dict and put `run_id` at the top
level, because that is what the function asked for. The tests and the code agreed with each other
and both disagreed with the state machine, which is the failure mode a hand-written fixture invites.

SO THE FIXTURE HERE IS REAL. `tests/fixtures/upgrade_lanes_state_input.json` is the actual input
Step Functions handed `Upgrade lanes` in a test run, trimmed to the keys this stage reads.
It is evidence rather than an assumption: if the state's shape changes, this fixture stops matching
it and the test has to be re-captured deliberately.

Deliberately no AWS call: `_run_id_from` is a pure function of the payload, which is exactly why the
bug was cheap to fix and should have been cheap to catch.
"""

from __future__ import annotations

import json
from pathlib import Path

from eks_addon_readiness.stages.upgrade_report import _run_id_from

FIXTURE = Path(__file__).resolve().parent / "fixtures/upgrade_lanes_state_input.json"
RUN_IN_FIXTURE = "00000000-0000-4000-8000-000000000006"


def test_the_real_state_input_yields_its_run_id() -> None:
    """The exact payload that failed, now resolving. The regression, stated as data."""
    state = json.loads(FIXTURE.read_text())
    assert "run_id" not in state, (
        "the captured state has a top-level `run_id`, which would make this test prove nothing -- "
        "the whole point is that the state does NOT carry one there. Re-capture the fixture."
    )
    assert _run_id_from(state) == RUN_IN_FIXTURE, (
        "the report stage cannot find the run id in the payload the state machine actually sends. "
        "An empty run id becomes `run=/release.json`, which reads as AccessDenied rather than as a "
        "missing value."
    )


def test_an_empty_run_id_is_never_returned_as_a_usable_value() -> None:
    """The failure mode is an empty STRING, not a missing key, so blank-ish inputs must not pass.

    `run=/release.json` is what an empty string produces. A whitespace-only id would produce the
    same key with the same misleading AccessDenied, so it has to be refused identically.
    """
    for payload in (
        {},
        {"run_id": ""},
        {"run_id": "   "},
        {"execution_name": ""},
        {"release": {}},
        {"release": {"run_id": ""}},
        {"release": {"run_id": "  "}},
        {"release": None},
        {"release": "not-a-dict"},
    ):
        assert _run_id_from(payload) == "", (
            f"{payload!r} produced a non-empty run id. A blank id must stay blank so the caller "
            "raises MissingParameter, rather than becoming an empty S3 path segment."
        )


def test_an_explicit_run_id_wins_over_the_release_document() -> None:
    """Direct invocation keeps working: a test or an operator can name the run outright."""
    payload = {"run_id": "explicit", "release": {"run_id": "from-release"}}
    assert _run_id_from(payload) == "explicit"


def test_the_release_record_document_is_read_as_a_fallback() -> None:
    """`Release recorded` writes it there too, and a payload carrying only that is still answerable."""
    payload = {"release_record": {"document": {"run_id": "from-record"}}}
    assert _run_id_from(payload) == "from-record"
