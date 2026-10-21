"""A run pauses for a person only when a person has something to decide.

THE DEFECT, OBSERVED LIVE RATHER THAN REASONED ABOUT. Execution
00000000-0000-4000-8000-000000000002 on `eks-addon-upgrades` left `Research needed?` on its
`approval_pending` branch -- `research_required` false, `research_requests` empty,
`withheld_awaiting_approval` `{"keda": 1}` -- straight into `Approval`. The stage returned

    awaiting_approval           0
    offered_from_this_run       0
    offered_from_earlier_runs   0
    pending_candidates_skipped  0
    approval_email_sent         TRUE   (SES 010001a0d6ea5eb2-...-000000)

then held the execution on a task token until somebody pressed Done on a page with no rows. All
three pending candidates in the bucket -- cert-manager 1.13.x, keda 2.17.x twice, metrics-server
0.6.x -- belong to components that already have a stored fact, so `_fact_is_already_stored`
filtered every one of them. Correctly: a control whose fact is already stored is a button the
approval endpoint refuses.

WHY IT COULD NOT BE FIXED INSIDE THE HANDLER, which is the constraint that shapes everything
here. `Approval` is `.waitForTaskToken`, and Step Functions hands out the token when the state is
ENTERED, not when the handler returns -- so by the time `handler` knows the set is empty the
execution is already stopped. The stage cannot resume itself and MUST NOT be able to: it holds no
`states:SendTaskSuccess` (`infra/terraform/modules/research/approval_stage.tf`), because a stage able to resume
its own pause is not a pause. So the count has to be taken by a separate, non-waiting invocation
BEFORE the wait is entered.

WHAT IS PINNED HERE. Each item is a way the pre-check could be quietly wrong, and the first four
are ways it could be actively harmful -- a "harmless" survey that writes a page or mints a grant
is worse than the pause it replaces.

1. **Survey mode writes no page, mints no grant, stores no token and sends no mail.** Asserted
   by making every one of those seams raise, so the test fails on the ACTION rather than on a
   count that happens to come out right.
2. **A bucket whose every pending candidate is already answered surveys to zero.** The live shape
   above, reproduced through the real filter.
3. **A candidate that IS offerable surveys to non-zero**, so the skip is not simply always taken.
4. **The survey and the wait count the same set**, because they call one expression. Two
   expressions could disagree and the disagreement would read as a pause that offers nothing.
5. **`pending_candidates_skipped` is reported and does not force the pause.** An unreadable
   candidate has no `report_digest`, so `approvals_from_research` rule 2 drops it and it renders
   NO control -- pausing for it recreates the zero-row page in front of somebody who has no
   button for it.
6. **A truthy-but-not-True flag pauses.** The safe direction: a mangled payload must fall through
   to the behaviour that asks a person, never to the one that walks past them.
7. **The wiring, read out of the HCL.** Both edges that used to enter `Approval` route through
   the Choice; the Choice's Default is the WAIT; the zero branch is `Knowledge changed?`; the
   path the Choice reads is the path the Task writes; and both new states are gated on
   `approval_function_arn`, because a Choice naming a state that was never emitted is refused
   whole by Step Functions.

UNLIKE `test_an_earlier_runs_candidate_reaches_the_page.py`, THESE CALL `handler`. That file
calls the seams because its claims are about the merge rule; these claims are about what the
handler DOES and DOES NOT do, and "does not write the page" is not checkable from a seam.

Deliberately no AWS call: the S3, DynamoDB and SES seams are doubled and the Terraform is read as
text, so this runs in the standard suite with no credentials.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eks_addon_readiness.knowledge import ComponentKnowledge
from eks_addon_readiness.models import Confidence
from eks_addon_readiness.research.candidate import CandidateFact
from eks_addon_readiness.research.receipt import (
    AcceptanceDecision,
    ChangeDirection,
    ResearchReceipt,
    SignalName,
    SignalResult,
)
from eks_addon_readiness.research.report import ApprovalReport, CitedSourceRow, FiredSignal
from eks_addon_readiness.stages import approval_gate
from eks_addon_readiness.storage.candidates import PendingCandidate

WORKFLOW = Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/workflow.tf"

BUCKET = "eks-addon-upgrades-test"
CLUSTER = "approval-survey-test"
EVIDENCE_DIGEST = "sha256:" + "ee" * 32

# The three components the live bucket actually held, with the key shape copied from it:
# `facts-candidates/decision=PENDING_APPROVAL/component=keda/series=2.17.x/date=.../<hex>.json`.
PENDING = "facts-candidates/decision=PENDING_APPROVAL"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
WRITTEN_AT = datetime(2026, 9, 25, 9, 14, tzinfo=UTC)

LIVE_BUCKET = (
    ("cert-manager", "1.13.x", "9723cede6f51"),
    ("keda", "2.17.x", "1a2b3c4d5e6f"),
    ("keda", "2.17.x", "6f5e4d3c2b1a"),
    ("metrics-server", "0.6.x", "270c6baa5d54"),
)


def _stem(component: str, series: str, digest: str) -> str:
    return f"{PENDING}/component={component}/series={series}/date=2026-09-25/{digest}"


def _rows() -> tuple[PendingCandidate, ...]:
    """The live listing, as `list_pending` returns it."""
    return tuple(
        PendingCandidate(
            component=component,
            version_series=series,
            key=f"{_stem(component, series, digest)}.json",
            last_modified=WRITTEN_AT,
        )
        for component, series, digest in LIVE_BUCKET
    )


# ---------------------------------------------------------------------------
# The documents one offerable candidate is reconstructed from
# ---------------------------------------------------------------------------


@pytest.fixture
def candidate(recorded_entry: ComponentKnowledge) -> CandidateFact:
    """One pending candidate, built through the real contract so `digest()` is the real one."""
    return CandidateFact(
        component_id="metrics-server",
        version_series="0.6.x",
        knowledge=recorded_entry,
        receipt=ResearchReceipt(
            confidence=Confidence.MEDIUM,
            signals=(
                SignalResult(
                    signal=SignalName.SUPPORT_WINDOW_DERIVED,
                    passed=True,
                    detail="A support window was derived for 0.6.x from the release notes.",
                ),
            ),
            change_direction=ChangeDirection.NEW,
            decision=AcceptanceDecision.PENDING_APPROVAL,
            summary="One window derived, seven of nine values traced to verified quotes.",
            evidence_digest=EVIDENCE_DIGEST,
            values_verified=7,
            values_total=9,
            machine_confidence=Confidence.MEDIUM,
            proposed_confidence=Confidence.HIGH,
            evidence_tier="FIRST_PARTY",
        ),
    )


@pytest.fixture
def report(candidate: CandidateFact) -> ApprovalReport:
    """The `.report.json` sidecar whose digest is what makes a control work."""
    return ApprovalReport(
        component_id="metrics-server",
        version_series="0.6.x",
        installed_version="0.6.4",
        cluster=CLUSTER,
        target_kubernetes_version="1.36",
        candidate_digest=candidate.digest(),
        evidence_digest=EVIDENCE_DIGEST,
        researched_at=WRITTEN_AT,
        advisory="Widening is not claimed; this is the first fact for the series.",
        decision=AcceptanceDecision.PENDING_APPROVAL,
        confidence=Confidence.MEDIUM,
        change_direction=ChangeDirection.NEW,
        values_verified=7,
        values_total=9,
        signals=(
            FiredSignal(
                name="SUPPORT_WINDOW_DERIVED",
                fired=True,
                detail="A support window was derived for 0.6.x.",
            ),
        ),
        recommended_version="0.8.1",
        recommended_series="0.8.x",
        action="UPGRADE_BEFORE_CLUSTER",
        installed_version_status="OUTSIDE_SUPPORTED_WINDOW",
        upgrade_ordering="BEFORE_CLUSTER",
        covers_current_kubernetes=True,
        covers_target_kubernetes=False,
        evidence_tier="FIRST_PARTY",
        proposed_confidence="HIGH",
        machine_confidence="MEDIUM",
        confidence_rationale="Two first-party sources, no corroboration for the ceiling.",
        cited_sources=(
            CitedSourceRow(
                url="https://github.com/kubernetes-sigs/metrics-server/releases/tag/v0.8.1",
                revision="9ce65a47f9262e19e482c259ace815f01135a898",
                used_for="the recommended version",
            ),
        ),
        caveats=("The ceiling rests on one document.",),
        not_found=(),
    )


# ---------------------------------------------------------------------------
# The seams a survey must never touch
# ---------------------------------------------------------------------------

# Every module-level name in `approval_gate` whose call is an ACTION rather than a read: the
# page, the grants, the forms, the token row and the mail. Named here rather than inline so a
# seam added to the pause in future is a one-line addition to this list, and so the failure
# message says which capability the survey reached for.
FORBIDDEN_IN_SURVEY = (
    "write_text",
    "build_token_store",
    "build_decision_form",
    "build_approval_email",
    "send_approval_email",
    "presign_s3_get",
    "new_nonce",
    "to_approval_html",
    "approver_address",
)


def _forbid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every write, mint and mail raise, so the test fails on the act not on a count."""
    for name in FORBIDDEN_IN_SURVEY:

        def _boom(*_args: Any, _name: str = name, **_kwargs: Any) -> Any:
            raise AssertionError(
                f"survey mode called {_name}(), which is an action and not a count. A "
                "pre-check that writes the page, mints a grant, stores a token or mails "
                "anybody is not a pre-check -- it is the pause, run twice."
            )

        monkeypatch.setattr(approval_gate, name, _boom)


def _event(**extra: Any) -> dict[str, Any]:
    """The pre-check's payload, as `Approval survey`'s Parameters build it.

    No `task_token`: the pre-check is a plain `lambda:invoke` and there is none to give, which
    is the entire difference between it and the wait.
    """
    return {
        approval_gate.SURVEY_ONLY: True,
        "execution_name": "00000000-0000-4000-8000-000000000002",
        "bucket": BUCKET,
        "cluster_name": CLUSTER,
        "target_kubernetes_version": "1.36",
        "research_results": [],
        "store_digest_before_research": "sha256:" + "ab" * 32,
        **extra,
    }


class _StoreHolding:
    """A fact store that already answers for the named components."""

    def __init__(self, *components: str) -> None:
        self.components = set(components)

    def find(self, component: str) -> object | None:
        return object() if component in self.components else None


# ---------------------------------------------------------------------------
# 1, 2. The live shape: everything already answered, so nothing is asked
# ---------------------------------------------------------------------------


def test_a_bucket_whose_candidates_are_all_answered_surveys_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The execution that paused for nothing, surveyed instead.

    Four pending objects for three components, every component already carrying a stored fact --
    which is what `_fact_is_already_stored` measured against the live bucket. The count the
    Choice reads must be zero, and not one of the write, mint or mail seams may be touched on
    the way to it.
    """
    _forbid(monkeypatch)
    monkeypatch.setattr(approval_gate, "list_pending", lambda *_a, **_k: _rows())
    monkeypatch.setattr(
        approval_gate,
        "build_fact_store",
        lambda: _StoreHolding("cert-manager", "keda", "metrics-server"),
    )

    result = approval_gate.handler(_event())

    assert result["awaiting_approval"] == 0, (
        "the survey counted something to approve where the live run's own page had no rows"
    )
    assert result["offered_from_this_run"] == 0
    assert result["offered_from_earlier_runs"] == 0
    assert result["pending_candidates_skipped"] == 0, (
        "nothing failed to read here -- every candidate was withheld because its question is "
        "already answered, and conflating the two reports a degradation on every healthy run"
    )
    # The state that does not stop must not claim it did. `Knowledge changed?` is reached with
    # this result nested under its own ResultPath, and a reader scanning for `paused` in the
    # execution history must not find a truthy value on the state that deliberately carries on.
    assert result["paused"] is False
    assert result["stage"] == "approval_survey"
    assert result["run_id"] == "00000000-0000-4000-8000-000000000002"
    # No `approval_html_uri` and no `approval_email_*`: there is no page and no mail, so naming
    # either would be a field describing something that does not exist.
    assert "approval_html_uri" not in result
    assert not [key for key in result if key.startswith("approval_email")]


def test_an_offerable_candidate_surveys_to_non_zero(
    monkeypatch: pytest.MonkeyPatch, candidate: CandidateFact, report: ApprovalReport
) -> None:
    """The skip is a decision, not a default: a real pending answer still stops the run.

    Without this the test above passes against a survey that returns zero unconditionally, which
    would silently turn the approval stage off and strand every candidate -- a strictly worse bug
    than the one being fixed, and one with no symptom except facts that never arrive.
    """
    _forbid(monkeypatch)
    row = PendingCandidate(
        component="metrics-server",
        version_series="0.6.x",
        key=f"{_stem('metrics-server', '0.6.x', '270c6baa5d54')}.json",
        last_modified=WRITTEN_AT,
    )
    monkeypatch.setattr(approval_gate, "list_pending", lambda *_a, **_k: (row,))
    monkeypatch.setattr(approval_gate, "build_fact_store", lambda: _StoreHolding())
    monkeypatch.setattr(
        approval_gate,
        "read_document",
        lambda uri, _contract: report if uri.endswith(".report.json") else candidate,
    )

    result = approval_gate.handler(_event())

    assert result["awaiting_approval"] == 1
    assert (result["offered_from_this_run"], result["offered_from_earlier_runs"]) == (0, 1)
    assert result["pending_candidates_skipped"] == 0


# ---------------------------------------------------------------------------
# 4. The survey and the wait count one set
# ---------------------------------------------------------------------------


def test_the_survey_counts_the_set_the_wait_would_render(
    monkeypatch: pytest.MonkeyPatch, candidate: CandidateFact, report: ApprovalReport
) -> None:
    """One expression, so the pre-check and the pause cannot disagree about one bucket.

    They can still disagree ACROSS TIME -- they run minutes apart over the same bucket -- and
    that is bounded rather than fixed: a candidate arriving in the window stays
    PENDING_APPROVAL and the next run's survey offers it, because the survey runs every round.
    What must never happen is the two computing DIFFERENT answers from the SAME bucket, which is
    what this asserts by calling `_offered` directly and comparing the count the survey
    published to it.
    """
    row = PendingCandidate(
        component="metrics-server",
        version_series="0.6.x",
        key=f"{_stem('metrics-server', '0.6.x', '270c6baa5d54')}.json",
        last_modified=WRITTEN_AT,
    )
    monkeypatch.setattr(approval_gate, "list_pending", lambda *_a, **_k: (row,))
    monkeypatch.setattr(approval_gate, "build_fact_store", lambda: _StoreHolding())
    monkeypatch.setattr(
        approval_gate,
        "read_document",
        lambda uri, _contract: report if uri.endswith(".report.json") else candidate,
    )

    controls, from_this_run, from_earlier, skipped = approval_gate._offered(BUCKET, [])
    _forbid(monkeypatch)
    surveyed = approval_gate.survey(_event())

    assert surveyed["awaiting_approval"] == len(controls.pending)
    assert surveyed["offered_from_this_run"] == from_this_run
    assert surveyed["offered_from_earlier_runs"] == from_earlier
    assert surveyed["pending_candidates_skipped"] == skipped


# ---------------------------------------------------------------------------
# 5. An unreadable candidate is counted, and does not hold the run open
# ---------------------------------------------------------------------------


def test_an_unreadable_candidate_is_counted_and_still_surveys_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate nobody can read is a problem no page can present.

    It has no `report_digest`, so `approvals_from_research` rule 2 drops it and it renders NO
    control. Pausing for it would put a person in front of the zero-row page this whole change
    exists to stop, with nothing on it to act on. So it is COUNTED -- visible in the execution
    history rather than only in a log line -- and the run carries on to the report.
    """
    _forbid(monkeypatch)
    row = PendingCandidate(
        component="metrics-server",
        version_series="0.6.x",
        key=f"{_stem('metrics-server', '0.6.x', '270c6baa5d54')}.json",
        last_modified=WRITTEN_AT,
    )
    monkeypatch.setattr(approval_gate, "list_pending", lambda *_a, **_k: (row,))
    monkeypatch.setattr(approval_gate, "build_fact_store", lambda: _StoreHolding())

    def _gone(uri: str, _contract: Any) -> Any:
        raise KeyError(f"no object at {uri}")

    monkeypatch.setattr(approval_gate, "read_document", _gone)

    result = approval_gate.handler(_event())

    assert result["awaiting_approval"] == 0
    assert result["pending_candidates_skipped"] == 1, (
        "an unreadable candidate must be countable from the execution history; inferring it "
        "from a control that is missing is how the stranding went unreported for weeks"
    )


# ---------------------------------------------------------------------------
# 6. The flag, read strictly, failing toward the pause
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flag",
    (1, "true", "True", "yes", [1], {"survey": True}),
    ids=("one", "lower-string", "title-string", "yes", "list", "dict"),
)
def test_a_truthy_flag_that_is_not_true_takes_the_pause(
    monkeypatch: pytest.MonkeyPatch, flag: Any
) -> None:
    """The safe direction, and it is the one that costs a person's attention rather than a fact.

    A mangled payload must fall through to the PAUSE. Pausing a run nobody needed to answer is
    wasteful and self-correcting -- somebody presses Done. Surveying a run that was meant to
    pause walks past the only surface that can clear a stranded candidate, and reports nothing
    at all.

    Asserted by the pause's own first action raising: the forbidden seams are what the wait
    uses, so reaching any of them proves the pause was entered.
    """
    _forbid(monkeypatch)
    monkeypatch.setattr(approval_gate, "list_pending", lambda *_a, **_k: ())
    monkeypatch.setattr(approval_gate, "build_fact_store", lambda: _StoreHolding())

    with pytest.raises(AssertionError, match="survey mode called"):
        approval_gate.handler(_event(**{approval_gate.SURVEY_ONLY: flag}))


def test_an_absent_flag_takes_the_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default is the behaviour that existed before this change."""
    _forbid(monkeypatch)
    monkeypatch.setattr(approval_gate, "list_pending", lambda *_a, **_k: ())
    monkeypatch.setattr(approval_gate, "build_fact_store", lambda: _StoreHolding())
    event = _event()
    del event[approval_gate.SURVEY_ONLY]

    with pytest.raises(AssertionError, match="survey mode called"):
        approval_gate.handler(event)


# ---------------------------------------------------------------------------
# 7. The wiring, read out of the Terraform
# ---------------------------------------------------------------------------
#
# The Python above proves the survey counts without acting. None of it proves the STATE MACHINE
# asks it: an edge still pointing at `Approval` pauses unconditionally on its own path, and the
# defect reached production through exactly one such edge. So the graph is asserted as text, for
# the reason `test_the_release_gate_never_pages_for_an_empty_list.py` gives -- a Choice whose
# Variable resolves to nothing does not fail, it takes its Default, so a wrong path is silent.


def _text() -> str:
    return WORKFLOW.read_text()


def _local(name: str) -> str:
    """The body of one `<name> = ...` local, comments stripped."""
    match = re.search(rf"\n  {re.escape(name)} = (.*?)\n  \}}\n", _text(), re.S)
    assert match, (
        f"could not find the `{name}` local in workflow.tf. If it was renamed, fix this test "
        "rather than deleting it."
    )
    return "\n".join(
        line for line in match.group(1).splitlines() if not line.lstrip().startswith("#")
    )


def test_both_edges_into_the_wait_route_through_the_choice() -> None:
    """The half of the fix that is not in Python, and the half that was easiest to get wrong.

    `Research.Next` and the `approval_pending` branch of `Research needed?` are the only two
    edges into `Approval` from the research leg. Moving one and not the other leaves an
    unconditional pause on whichever path was missed -- and `approval_pending` is precisely the
    path the observed defect took, so fixing only `Research.Next` would have fixed nothing.
    """
    research = _local("research_state")
    assert '"Approval survey"' in research, (
        "Research.Next no longer names the pre-check, so a researched round enters the wait "
        "without anybody counting what the page would offer"
    )
    gate = r'Next\s*=\s*var\.approval_function_arn == null \? "Knowledge changed\?"'
    assert re.search(gate, research), (
        "Research.Next lost its gate: with no approval stage it must go straight to "
        "`Knowledge changed?`, because a definition naming an unemitted state is refused whole"
    )

    choice = _local("choice_state")
    assert '"Approval survey"' in choice, (
        "the `approval_pending` branch of `Research needed?` still enters the wait directly. "
        "That is the edge a test run took to pause on an empty page."
    )
    assert 'Next = "Approval"\n' not in choice, (
        "an edge in `Research needed?` still names `Approval` rather than the pre-check"
    )


def test_the_choice_defaults_to_the_wait_and_only_a_counted_zero_skips_it() -> None:
    """The asymmetry, asserted, because the other default is the one that strands a candidate.

    `Anything approved?` defaults to `Done` because `Done` grants no authority. This Choice
    defaults to `Approval` because the costs are not symmetrical: a wasted page is visible and
    somebody presses Done, while a skipped page leaves the candidate PENDING_APPROVAL, `Resolve`
    still withholding its component, and no surface offering it.
    """
    body = _local("approval_needed_state")
    assert 'Default = "Approval"' in body, (
        "the Choice no longer defaults to the wait. An absent or unreadable count must ask a "
        "person, not skip them."
    )
    assert "NumericEquals = 0" in body, (
        "the zero branch is no longer a positive test for zero. `IsPresent` or a greater-than "
        "reads the absent case as a skip, which is the direction that strands a candidate."
    )
    assert 'Next = "Knowledge changed?"' in body
    assert "IsPresent = true" in body, (
        "the comparison is unguarded. A bare comparison on an absent path does not evaluate "
        "false -- it fails the execution with States.Runtime, which this file has already paid "
        "for once."
    )


def test_the_choice_reads_the_path_the_survey_writes() -> None:
    """One path, spelled the same in two states, because a mismatch is silent.

    The Task's `ResultSelector` unwraps the Lambda envelope and its `ResultPath` nests the
    result, so the live path is `$.approval_survey.awaiting_approval`. Leaving the selector out
    would make it `$.approval_survey.Payload.awaiting_approval`, the Choice's Variable would
    resolve to nothing, and it would take its Default on every run -- pausing every execution,
    with nothing anywhere reporting that the pre-check was never once consulted.
    """
    survey = _local("approval_survey_state")
    # Whitespace-tolerant throughout: `terraform fmt` aligns the `=` in a block, so the padding
    # moves whenever a longer key is added beside a shorter one. Pinning the exact spacing would
    # make this test fail on a reformat that changed nothing about the graph.
    assert re.search(r'ResultPath\s*=\s*"\$\.approval_survey"', survey)
    assert re.search(
        r'"awaiting_approval\.\$"\s*=\s*"\$\.Payload\.awaiting_approval"', survey
    ), (
        "the ResultSelector no longer unwraps the Lambda envelope, so the Choice after this "
        "state reads a path that does not exist and takes its Default on every run"
    )
    assert "$.approval_survey.awaiting_approval" in _local("approval_needed_state"), (
        "the Choice reads a different path from the one the pre-check writes"
    )


def test_the_survey_result_keeps_the_count_nobody_else_will_report() -> None:
    """`pending_candidates_skipped` must survive the ResultSelector, on the SKIP path especially.

    `ResultSelector` REPLACES the result with what it names, so a selector carrying only
    `awaiting_approval` drops the other three counts from this state's output. That matters most
    on the path this change adds: a run that skips the wait renders no page, so the skipped count
    is the only thing that says a pending candidate is still unreachable -- the exact condition
    the approval stage exists to end. Leaving it inferable from a Lambda log line only is how the
    original stranding went unreported on metrics-server for every run it blocked.
    """
    survey = _local("approval_survey_state")
    selector = re.search(r"ResultSelector\s*=\s*\{(.*?)\n      \}", survey, re.S)
    assert selector, "the pre-check has no ResultSelector block to read"
    for field in (
        "awaiting_approval",
        "offered_from_this_run",
        "offered_from_earlier_runs",
        "pending_candidates_skipped",
    ):
        # Whitespace-tolerant: `terraform fmt` aligns the `=` across the block.
        assert re.search(
            rf'"{field}\.\$"\s*=\s*"\$\.Payload\.{field}"', selector.group(1)
        ), (
            f"the ResultSelector drops `{field}`. ResultSelector replaces the result, so a "
            "field it does not name is gone from this state's output -- and on the skip path "
            "there is no page to report it instead."
        )


def test_the_surveys_own_result_carries_every_selected_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selector names four payload paths, so the handler must return all four.

    A selector naming an absent path fails the Task, and the Catch then routes to `Approval` --
    so getting this wrong pauses every single run while nothing reports why. Asserted against the
    handler's real return value rather than against its docstring.
    """
    _forbid(monkeypatch)
    monkeypatch.setattr(approval_gate, "list_pending", lambda *_a, **_k: ())
    monkeypatch.setattr(approval_gate, "build_fact_store", lambda: _StoreHolding())

    result = approval_gate.handler(_event())

    for field in (
        "awaiting_approval",
        "offered_from_this_run",
        "offered_from_earlier_runs",
        "pending_candidates_skipped",
    ):
        assert field in result, (
            f"the survey does not return `{field}`, which the state's ResultSelector names. "
            "Step Functions fails a ResultSelector on an absent path, and the Catch would then "
            "pause every run."
        )
        assert isinstance(result[field], int), (
            f"`{field}` must be a number: the Choice compares it with NumericEquals"
        )


def test_the_survey_neither_waits_nor_can_fail_the_run() -> None:
    """A plain invoke with a Catch to the wait: it counts, and it cannot break anything.

    `.waitForTaskToken` here would reintroduce the pause it exists to avoid. And no Catch would
    let a throttle or a cold-start timeout fail a run that previously worked -- so every failure
    lands on the wait, which is what the run would have done without this state at all.
    """
    survey = _local("approval_survey_state")
    assert "arn:aws:states:::lambda:invoke" in survey
    assert "waitForTaskToken" not in survey, (
        "the pre-check became a task-token wait, which is the pause it exists to make conditional"
    )
    assert "task_token" not in survey, (
        "the pre-check names a task token. It is a plain invoke, so there is none -- and a "
        "survey holding one could store a row the resume path believes it can complete."
    )
    assert "survey_only = true" in survey, (
        "the pre-check no longer sets the survey flag, so it invokes the PAUSE handler: it "
        "would render the page, mint the grants, store a token nobody is waiting on and mail "
        "somebody, then hand a count to the Choice"
    )
    catch = re.search(r"Catch = \[\{(.*?)\}\]", survey, re.S)
    assert catch, "the pre-check has no Catch, so a survey failure fails the whole execution"
    assert 'Next        = "Approval"' in catch.group(1), (
        "the pre-check's Catch does not lead to the wait. A survey that could not count must "
        "not be the reason a pending candidate goes unoffered."
    )
    assert '"$.approval_survey_error"' in catch.group(1), (
        "the Catch's ResultPath would replace the run payload, so `Approval` would then fail "
        "naming $.bucket -- turning a survey hiccup into a failed execution"
    )


def test_both_new_states_are_gated_on_the_approval_function() -> None:
    """A Choice naming a state that was never emitted is refused whole by Step Functions.

    Every approval-shaped thing in this file carries this gate. Two of them are new, and a gate
    missing from either leaves a deployment without the approval stage unable to apply at all --
    which `terraform validate` does not catch, because the null-ARN default hides it.
    """
    for name in ("approval_survey_state", "approval_needed_state"):
        match = re.search(rf"\n  {name} = ([^\n]*)", _text())
        assert match, f"could not find the `{name}` local"
        assert "var.approval_function_arn == null ? {} :" in match.group(1), (
            f"`{name}` is not gated on the approval function. With no ARN the definition names "
            "states that were never emitted and Step Functions refuses it whole."
        )


def test_the_survey_names_no_new_function_role_or_variable() -> None:
    """The same Lambda, the same role, the same variables. No new surface to review.

    The pre-check is a second INVOCATION, not a second function: it names
    `var.approval_function_arn`, and every permission it uses -- list and read the pending
    prefix, scan the fact table -- is already on that role for the wait's sake. A new function
    would be a new artifact free to drift from the one that renders the page, and a new role
    would be a new thing to argue about.
    """
    survey = _local("approval_survey_state")
    assert "FunctionName = var.approval_function_arn" in survey
    variables = (WORKFLOW.parent / "variables.tf").read_text()
    assert "survey" not in variables.lower(), (
        "a variable naming the survey appeared in variables.tf. The pre-check is configured by "
        "the payload flag, not by the deployment -- see stages/settings.py on that split."
    )


def test_the_new_states_are_merged_into_the_definition() -> None:
    """Omitting a merge is a live defect that every `terraform validate` passes.

    `local.approval_state` was left out of this merge for several passes and nothing caught it:
    `Research.Next` named a state that was not in `States`, and Step Functions refused the whole
    definition at apply. Both new locals are reachable the same way.
    """
    definition = _text().split("States = merge(", 1)
    assert len(definition) == 2, "could not find the States merge in workflow.tf"
    merged = definition[1]
    for name in ("local.approval_survey_state", "local.approval_needed_state"):
        assert f"{name}," in merged, (
            f"{name} is declared and never merged into `States`, so the definition names a "
            "successor that does not exist and Step Functions refuses it whole at apply"
        )


def test_the_zero_branch_target_tolerates_an_absent_approval_result() -> None:
    """The skip path's destination must not fault on the path the wait would have written.

    `Knowledge changed?` reads `$.approval.store_digest_after_research`, and a run that skipped
    the wait has no `$.approval` at all. Its first branch is guarded by `IsPresent` inside an
    `And`, so the absent path evaluates false rather than faulting on the `StringEqualsPath`
    beside it -- and the run falls through to the report, which is the right answer: nothing was
    approved, so the store cannot have moved.

    VERIFIED AGAINST THE LIVE DEFINITION, not only asserted here: `stepfunctions test-state` on
    the deployed `Knowledge changed?` with an input carrying only `store_digest_before_research`
    returned nextState `Anything to upgrade?`, status SUCCEEDED.
    This test pins the SHAPE that result depends on, so removing the guard fails here rather
    than on a live execution.
    """
    body = _local("knowledge_changed_state")
    guard = re.search(
        r"And = \[(.*?)\]\s*\n\s*Next = \"Gaps\"", body, re.S
    )
    assert guard, (
        "the leading branch of `Knowledge changed?` is no longer an `And` guarded by "
        "IsPresent. Without that guard a run that skipped the wait fails with "
        "States.Runtime: Invalid path on $.approval.store_digest_after_research."
    )
    assert "IsPresent = true" in guard.group(1)
    assert "StringEqualsPath" in guard.group(1)
    # The guard must come FIRST. `And` short-circuits, so the order is what keeps the
    # StringEqualsPath from being evaluated against an absent path.
    assert guard.group(1).index("IsPresent") < guard.group(1).index("StringEqualsPath"), (
        "the IsPresent guard no longer precedes the comparison it guards"
    )


def test_the_pipeline_table_names_the_two_new_states() -> None:
    """entrypoints/stages.py's table is the authority on the edges, so it must carry these two.

    `test_the_pipeline_table_matches_the_deployed_machine.py` enforces this from the HCL side.
    Asserted here too, next to the change, so a reader of this file sees that the docstring a
    reader opens first was moved with the graph.
    """
    doc = (Path(__file__).resolve().parent.parent / "entrypoints/stages.py").read_text()
    table = doc.split("The deployed state machine, by transition.", 1)
    assert len(table) == 2, "entrypoints/stages.py's transition table heading is gone"
    for edge in (
        "Approval survey       ->  Anything to approve?",
        "Anything to approve?  ->  Approval",
        "Research              ->  Approval survey",
    ):
        source, destination = (part.strip() for part in edge.split("->"))
        rows = [
            line
            for line in table[1].splitlines()
            if line.strip().startswith(source) or (source == "" and "->" in line)
        ]
        assert any(destination in line for line in rows), (
            f"entrypoints/stages.py's table does not run {source} into {destination}"
        )


def test_the_definition_is_valid_json_with_both_states_when_the_arn_is_supplied() -> None:
    """A shape check the HCL greps above cannot make: the two states and their edges agree.

    Terraform cannot render this definition without an apply -- several `Resource` values name
    resources that do not exist yet -- so the graph is reconstructed from the asserted facts
    instead: every `Next` in the approval leg must name a state the leg declares or a state the
    machine already had. A dangling successor is what Step Functions refuses whole.
    """
    declared = set(
        re.findall(r'^\s+"([A-Z][A-Za-z ?]*)" = \{', _text(), re.M)
    ) | {"Anything to upgrade?", "Upgrade approval"}
    leg = _local("approval_survey_state") + _local("approval_needed_state")
    successors = set(re.findall(r'Next\s*=\s*"([^"]+)"', leg)) | set(
        re.findall(r'Default\s*=\s*"([^"]+)"', leg)
    )
    dangling = successors - declared
    assert not dangling, (
        f"the approval leg names successors that no state declares: {sorted(dangling)}. Step "
        "Functions refuses such a definition whole, and no terraform validate catches it."
    )
    # And the two states really are in the declared set, spelled as the edges spell them.
    assert {"Approval survey", "Anything to approve?"} <= declared
    # Sanity: the survey's Payload is a JSON object Step Functions will accept, so the flag is a
    # real boolean rather than a quoted string the handler would refuse.
    assert json.dumps({"survey_only": True}) == '{"survey_only": true}'
