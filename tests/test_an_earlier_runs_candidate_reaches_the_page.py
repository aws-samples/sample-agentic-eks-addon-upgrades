"""The approval page offers the answers already waiting, not only the ones this run produced.

THE TRAP THESE TESTS CLOSE. `stages/resolve.py` withholds a component whose answer is already
awaiting a decision, so it is not researched twice. The approval stage built its page from
`event["research_results"]` -- this execution's Map output and nothing else. Together those
strand a component permanently: the pending candidate suppresses the research AND is invisible
to the only surface that could clear it. Observed on metrics-server, whose candidate was
written by an execution somebody stopped and which then blocked every later run while
appearing on no page.

WHAT IS PINNED HERE, and each item is a way the fix could be quietly wrong:

1. **A stored pending candidate becomes an offered control**, digests included. A row whose
   `report_digest` is missing is dropped by `approvals_from_research` rule 2, so a
   reconstruction that gets that field wrong does not render a broken button -- it renders
   NOTHING, and the candidate stays exactly as stranded as before.
2. **This run's own result wins a `(component, series)` collision.** The rule keeps the FIRST
   entry it sees, so the merge order is the whole mechanism; the inverted order is asserted
   too, so the test states what the order buys rather than merely exercising it.
3. **A rejected candidate is never offered**, and no filter says so. Only the
   `decision=PENDING_APPROVAL/` prefix is ever listed, so the rejected object is excluded by
   the prefix that was never asked for -- asserted by checking which keys were read.
4. **A candidate past its TTL is never offered**, so an abandoned answer stops being a
   question. It is also not deleted, which is `storage/candidates.py`'s decision.
5. **An unreadable candidate is skipped, never raised on.** This handler runs behind
   `.waitForTaskToken`: a raise leaves the execution paused with a token nobody can redeem and
   no page to redeem it from, which is strictly worse than the stranding being fixed.
6. **An empty bucket changes nothing.** With no stored candidates the offered set is exactly
   what `approvals_from_research` returned before this existed, which is what keeps the
   rendered bytes of an unchanged page unchanged.

WHY THESE CALL THE SEAMS AND NOT `handler`. Exercising the handler needs S3, DynamoDB, SES and
a Step Functions task token -- most of a deployment, for the same reason
`test_the_stage_calls_match_their_signatures.py` gives. The listing, the reconstruction and the
merge are each callable on their own, and they are where every claim above lives.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from eks_addon_readiness.research.report import (
    ApprovalReport,
    CitedSourceRow,
    FiredSignal,
)
from eks_addon_readiness.stages import approval_gate
from eks_addon_readiness.view import approvals_from_research
from eks_addon_readiness.view.render.approval import to_approval_html

BUCKET = "eks-addon-upgrades-test"
ENDPOINT = "https://abcdefghijklmnopqrstuvwxyz.lambda-url.us-east-1.on.aws/"
EVIDENCE_DIGEST = "sha256:" + "ee" * 32

# The key shape of a real stored candidate, copied from the live bucket rather than invented:
# `facts-candidates/decision=PENDING_APPROVAL/component=metrics-server/series=0.6.x/
# date=2026-09-15/270c6baa5d54.json`. The `component=` and `series=` segments are what the
# listing rule reads, and the three sidecars share the stem -- which is why they are present in
# every fake bucket below.
PENDING = "facts-candidates/decision=PENDING_APPROVAL"
REJECTED = "facts-candidates/decision=REJECTED"
STEM = f"{PENDING}/component=metrics-server/series=0.6.x/date=2026-09-15/270c6baa5d54"
CANDIDATE_KEY = f"{STEM}.json"
CANDIDATE_URI = f"s3://{BUCKET}/{CANDIDATE_KEY}"
REPORT_URI = f"s3://{BUCKET}/{STEM}.report.json"

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
WRITTEN_AT = datetime(2026, 9, 15, 9, 14, tzinfo=UTC)


# ---------------------------------------------------------------------------
# The two stored documents, built through the real contracts
# ---------------------------------------------------------------------------


@pytest.fixture
def candidate(recorded_entry: ComponentKnowledge) -> CandidateFact:
    """One candidate as Stage 6 wrote it: a fact, and the receipt that judged it.

    Built through `CandidateFact` rather than as a dict so `digest()` is the real expression
    the approval endpoint checks against, and so a change to the contract breaks this fixture
    instead of letting the reconstruction drift away from what it reads.
    """
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
    """The `.report.json` sidecar, carrying the assessment an approver actually reads.

    `candidate_digest` is the candidate's own, because the two objects share a key stem and
    the reconstruction refuses a pair that does not agree.
    """
    return ApprovalReport(
        component_id="metrics-server",
        version_series="0.6.x",
        installed_version="0.6.4",
        cluster="approval-gate-test",
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
# The two pieces of I/O, doubled
# ---------------------------------------------------------------------------


class _Bucket:
    """The one listing call `list_pending` makes, answered from an in-memory bucket.

    Records every prefix it was asked for, so a test can assert that the rejected area was
    never even listed rather than merely that nothing from it was offered.
    """

    def __init__(self, objects: dict[str, datetime]) -> None:
        self.objects = objects
        self.prefixes: list[str] = []

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.prefixes.append(str(kwargs["Prefix"]))
        return {
            "Contents": [
                {"Key": key, "LastModified": stamp}
                for key, stamp in sorted(self.objects.items())
                if key.startswith(str(kwargs["Prefix"]))
            ]
        }


class _Reader:
    """`read_document`, answered from a dict of URIs, recording what was opened.

    A URI that is not held raises, which is the real failure mode: the object is gone, the
    role cannot read it, or the sidecar was never written.
    """

    def __init__(self, documents: dict[str, Any]) -> None:
        self.documents = documents
        self.opened: list[str] = []

    def __call__(self, uri: str, _contract: Any) -> Any:
        self.opened.append(uri)
        if uri not in self.documents:
            raise KeyError(f"no object at {uri}")
        held = self.documents[uri]
        if isinstance(held, Exception):
            raise held
        return held


def _one_pending(written_at: datetime = WRITTEN_AT) -> dict[str, datetime]:
    """One candidate and the three sidecars Stage 6 writes beside it, under one stem."""
    return {
        CANDIDATE_KEY: written_at,
        f"{STEM}.evidence.json": written_at,
        f"{STEM}.extraction.json": written_at,
        f"{STEM}.report.json": written_at,
    }


def _fresh_result(recommended: str, digest_byte: str) -> dict[str, Any]:
    """A research result for the SAME component and series, as this run's Map returned it."""
    return {
        "component_id": "metrics-server",
        "version_series": "0.6.x",
        "pending_approval": True,
        "candidate_uri": f"s3://{BUCKET}/{PENDING}/component=metrics-server/"
        "series=0.6.x/date=2026-09-15/ffffffffffff.json",
        # 64 hex characters, because `PendingApproval` pins the shape of both digests and a
        # short one is DROPPED by the same skip-not-raise path a malformed result takes -- a
        # fixture that failed that check would silently be testing the empty case.
        "candidate_digest": "sha256:" + digest_byte * 64,
        "report_digest": "sha256:" + digest_byte * 64,
        "confidence": "HIGH",
        "change_direction": "NEW",
        "approval_headline": "Approving this changes one verdict for metrics-server.",
        "recommended_version": recommended,
        "values_verified": 9,
        "values_total": 9,
    }


# ---------------------------------------------------------------------------
# 1. A stored candidate becomes a control
# ---------------------------------------------------------------------------


def test_a_stored_pending_candidate_becomes_an_offered_control(
    monkeypatch: pytest.MonkeyPatch, candidate: CandidateFact, report: ApprovalReport
) -> None:
    """The candidate no page ever offered is offered, with the digests that make it work.

    Both digests are asserted against the documents' own expressions, not against constants:
    `candidate_digest` is what the endpoint compares to the object it loads, and
    `report_digest` is what it compares to a report it re-derives. A row is worth nothing if
    either is a plausible-looking value from somewhere else.
    """
    reader = _Reader({CANDIDATE_URI: candidate, REPORT_URI: report})
    monkeypatch.setattr(approval_gate, "read_document", reader)

    entries, skipped = approval_gate.entries_from_earlier_runs(
        BUCKET, now=NOW, client=_Bucket(_one_pending())
    )

    assert skipped == 0
    assert len(entries) == 1, (
        f"one candidate and three sidecars share a stem; {len(entries)} entries means the "
        "sidecars are being counted as questions, which is what reported four pending "
        "answers for metrics-server where there was one"
    )
    entry = entries[0]
    assert entry["pending_approval"] is True
    assert entry["candidate_digest"] == candidate.digest()
    assert entry["report_digest"] == report.digest()
    assert entry["candidate_uri"] == CANDIDATE_URI
    # Reconstructed from the documents rather than from the key: the series and component are
    # in the path too, so reading them off the object is what proves the object was opened.
    assert (entry["component_id"], entry["version_series"]) == ("metrics-server", "0.6.x")
    assert entry["confidence"] == "MEDIUM"
    assert entry["machine_confidence"] == "MEDIUM"
    assert entry["proposed_confidence"] == "HIGH"
    assert entry["approval_headline"] == report.headline()
    assert entry["recommended_version"] == "0.8.1"
    assert entry["installed_version"] == "0.6.4"
    assert entry["cited_sources"][0]["used_for"] == "the recommended version"
    # Stage 6 stopped writing a per-candidate HTML view, so there is no such object for a
    # stored candidate or a fresh one. None, rather than a link to a key that does not exist.
    assert entry.get("report_html_uri") is None

    controls, from_this_run, from_earlier = approval_gate.offered_approvals(
        [], entries, endpoint=ENDPOINT, region="us-east-1"
    )
    assert (from_this_run, from_earlier) == (0, 1)
    assert [p.candidate_digest for p in controls.pending] == [candidate.digest()]
    assert [p.report_digest for p in controls.pending] == [report.digest()]
    assert controls.pending[0].recommended_version == "0.8.1"


def test_a_page_with_no_research_results_offers_exactly_one_control(
    monkeypatch: pytest.MonkeyPatch, candidate: CandidateFact, report: ApprovalReport
) -> None:
    """The shape the workflow now reaches: nothing researched, one answer waiting.

    `Research needed?` branches to `Approval` on `$.approval_pending`, so this stage is
    entered with an EMPTY `research_results` and pending candidates in the bucket. That page
    used to offer nothing at all, which is the remaining half of the stranding bug.

    Rendered, and the rows counted, because "offers one control" is a claim about the page a
    person opens rather than about a tuple length.
    """
    reader = _Reader({CANDIDATE_URI: candidate, REPORT_URI: report})
    monkeypatch.setattr(approval_gate, "read_document", reader)
    entries, _ = approval_gate.entries_from_earlier_runs(
        BUCKET, now=NOW, client=_Bucket(_one_pending())
    )

    controls, from_this_run, from_earlier = approval_gate.offered_approvals(
        None, entries, endpoint=ENDPOINT, region="us-east-1"
    )

    assert (len(controls.pending), from_this_run, from_earlier) == (1, 0, 1)
    page = to_approval_html(
        controls,
        run_id="local",
        cluster="approval-gate-test",
        target_kubernetes_version="1.36",
        rendered_at="2026-09-15T12:00:00+00:00",
    )
    # The row's own class, not the bare name: `row-await` also appears once in the shared
    # stylesheet, so counting that would count the rule that styles the row as a row.
    assert page.count('<tr class="arow row-await">') == 1
    assert "metrics-server" in page
    assert candidate.digest() in page


def test_an_empty_bucket_changes_nothing_about_the_offered_set() -> None:
    """No stored candidate means the same controls the stage built before this existed.

    Byte-for-byte identical, asserted on the document the renderer is a pure function of. The
    page for an unchanged input set must not move, and this is where that would first show.
    """
    fresh = _fresh_result("0.8.1", "a")
    merged, from_this_run, from_earlier = approval_gate.offered_approvals(
        [fresh], (), endpoint=ENDPOINT, region="us-east-1"
    )
    before = approvals_from_research([fresh], endpoint=ENDPOINT, region="us-east-1")

    assert merged.canonical_json() == before.canonical_json()
    assert (from_this_run, from_earlier) == (1, 0)


# ---------------------------------------------------------------------------
# 2. The collision, and the order that decides it
# ---------------------------------------------------------------------------


def test_this_runs_own_result_wins_a_collision_with_a_stored_one(
    monkeypatch: pytest.MonkeyPatch, candidate: CandidateFact, report: ApprovalReport
) -> None:
    """Two answers to one question: the one this run just researched is the one offered.

    `approvals_from_research` keeps the FIRST entry it sees for a `(component, series)` pair,
    so the merge order is the entire mechanism. The inverted order is asserted alongside it --
    not to test the rule, but so this test states what the order buys: invert the
    concatenation and a person is shown the stale answer while the fresh one sits unoffered
    beside it, with no symptom anywhere except a recommendation two releases out of date.

    The counts are asserted too. A row offered because this run researched it and a row
    offered because it was stranded are different facts about a paused run, and the payload
    must not conflate them.
    """
    reader = _Reader({CANDIDATE_URI: candidate, REPORT_URI: report})
    monkeypatch.setattr(approval_gate, "read_document", reader)
    stored, _ = approval_gate.entries_from_earlier_runs(
        BUCKET, now=NOW, client=_Bucket(_one_pending())
    )
    fresh = _fresh_result("0.9.0", "a")

    controls, from_this_run, from_earlier = approval_gate.offered_approvals(
        [fresh], stored, endpoint=ENDPOINT, region="us-east-1"
    )

    assert len(controls.pending) == 1, (
        "one component and one series is one decision; two rows would ask a person to "
        "decide the same thing twice"
    )
    offered = controls.pending[0]
    assert offered.candidate_digest == fresh["candidate_digest"]
    assert offered.recommended_version == "0.9.0"
    assert offered.candidate_digest != candidate.digest()
    assert (from_this_run, from_earlier) == (1, 0)

    # The same two entries, concatenated the other way round. Stated as an assertion rather
    # than a comment so the consequence of inverting the order is recorded in a test that
    # fails if the rule's first-wins behaviour ever changes underneath it.
    inverted = approvals_from_research(
        [*stored, fresh], endpoint=ENDPOINT, region="us-east-1"
    )
    assert inverted.pending[0].recommended_version == "0.8.1", (
        "the stale answer wins when the stored entries go first -- which is exactly the "
        "outcome the order in offered_approvals exists to prevent"
    )


# ---------------------------------------------------------------------------
# 3, 4, 5. The three candidates that must never become a control
# ---------------------------------------------------------------------------


def test_a_rejected_candidate_is_never_offered(
    monkeypatch: pytest.MonkeyPatch, candidate: CandidateFact, report: ApprovalReport
) -> None:
    """A refusal is not a question, and no code here filters for that.

    Only `decision=PENDING_APPROVAL/` is ever listed, and `approve.py` records a refusal as a
    NEW object under `decision=REJECTED/` rather than by moving the pending one -- so the
    rejected series is excluded by the prefix nobody asked for. Asserted on the prefixes
    listed and the URIs opened, because "no filter" is only safe if it is checkable: a later
    reader who added a rejected filter would be handling a case the listing cannot produce.
    """
    rejected_key = (
        f"{REJECTED}/component=cert-manager/series=1.13.x/date=2026-09-15/9723cede6f51.json"
    )
    listing = _Bucket({**_one_pending(), rejected_key: WRITTEN_AT})
    reader = _Reader({CANDIDATE_URI: candidate, REPORT_URI: report})
    monkeypatch.setattr(approval_gate, "read_document", reader)

    entries, skipped = approval_gate.entries_from_earlier_runs(
        BUCKET, now=NOW, client=listing
    )

    assert skipped == 0
    assert [e["component_id"] for e in entries] == ["metrics-server"]
    assert listing.prefixes == [f"{PENDING}/"]
    assert all(REJECTED not in uri for uri in reader.opened), (
        f"a rejected candidate was opened: {reader.opened}. Nothing may read that prefix "
        "here; a refused claim has no fact a person could approve."
    )


def test_a_candidate_past_its_ttl_is_never_offered(
    monkeypatch: pytest.MonkeyPatch, candidate: CandidateFact, report: ApprovalReport
) -> None:
    """An answer nobody came back for in a day stops being a question, and is not deleted.

    The run that asked is long gone and the releases it read may have moved, so re-researching
    is more honest than offering a stale answer -- which is the same judgement that stops it
    suppressing research in `stages/resolve.py`. Nothing is opened, because age is decided
    from the listing metadata alone.
    """
    reader = _Reader({CANDIDATE_URI: candidate, REPORT_URI: report})
    monkeypatch.setattr(approval_gate, "read_document", reader)
    aged = datetime(2026, 9, 13, 9, 14, tzinfo=UTC)

    entries, skipped = approval_gate.entries_from_earlier_runs(
        BUCKET, now=NOW, client=_Bucket(_one_pending(aged))
    )

    assert (entries, skipped) == ((), 0)
    assert reader.opened == []


@pytest.mark.parametrize(
    ("held", "why"),
    (
        ({}, "the candidate object is gone or unreadable"),
        ("candidate-only", "the report sidecar is missing, so no digest can be re-derived"),
        ("not-pending", "the object's own receipt is no longer PENDING_APPROVAL"),
        ("mismatched-report", "the report beside it was built for another candidate"),
    ),
    ids=("unreadable", "no-report", "not-pending", "wrong-report"),
)
def test_a_candidate_that_cannot_be_reconstructed_is_skipped_not_raised(
    monkeypatch: pytest.MonkeyPatch,
    candidate: CandidateFact,
    report: ApprovalReport,
    recorded_entry: ComponentKnowledge,
    held: Any,
    why: str,
) -> None:
    """Four ways one row can fail, and none of them may fail the stage.

    This is a `.waitForTaskToken` handler: raising leaves the execution paused holding a token
    nobody can redeem and no page to redeem it from -- worse than the stranding this whole
    change exists to fix, because at least a stranded candidate leaves a working page for
    everything else. Each failure is counted so it is visible in the execution history rather
    than only in a log line.
    """
    documents: dict[str, Any] = {}
    if held == "candidate-only":
        documents = {CANDIDATE_URI: candidate}
    elif held == "not-pending":
        settled = candidate.model_copy(
            update={
                "receipt": candidate.receipt.model_copy(
                    update={"decision": AcceptanceDecision.REJECTED}
                ),
                "knowledge": None,
            }
        )
        documents = {CANDIDATE_URI: settled, REPORT_URI: report}
    elif held == "mismatched-report":
        other = candidate.model_copy(update={"version_series": "0.7.x"})
        documents = {
            CANDIDATE_URI: candidate,
            REPORT_URI: report.model_copy(update={"candidate_digest": other.digest()}),
        }

    reader = _Reader(documents)
    monkeypatch.setattr(approval_gate, "read_document", reader)

    entries, skipped = approval_gate.entries_from_earlier_runs(
        BUCKET, now=NOW, client=_Bucket(_one_pending())
    )

    assert (entries, skipped) == ((), 1), f"expected one skipped candidate because {why}"

    # And the page still builds. A run whose only stored candidate is unreadable pauses with
    # an honest empty page and a count saying so, not with a failed execution.
    controls, from_this_run, from_earlier = approval_gate.offered_approvals(
        None, entries, endpoint=ENDPOINT, region="us-east-1"
    )
    assert (len(controls.pending), from_this_run, from_earlier) == (0, 0, 0)


def test_a_candidate_whose_fact_is_already_stored_is_not_offered(
    monkeypatch: pytest.MonkeyPatch, candidate: CandidateFact, report: ApprovalReport
) -> None:
    """A control the endpoint would refuse must never be drawn.

    THE FAILURE THIS PREVENTS WAS MEASURED, not imagined. The endpoint does not hash the
    stored report -- `approve.py::_rebuild_report` re-derives one with
    `current=build_fact_store().find(component_id)` and compares. So once a fact exists the
    rebuild no longer matches the sidecar this entry's `report_digest` came from. Against the
    live bucket with the store populated, cert-manager and keda both rendered controls the
    endpoint would have refused, while metrics-server -- the only one with no stored fact --
    was the only one that would have worked.

    So a stored fact means the question is ANSWERED and the candidate is a leftover: `Resolve`
    has stopped withholding the component, the gap is closed, and there is nothing to decide.
    Filtering is the repair rather than making the digests agree, because the alternative is
    asking somebody to re-approve what they already approved, with a button that fails.
    """
    reader = _Reader({CANDIDATE_URI: candidate, REPORT_URI: report})
    monkeypatch.setattr(approval_gate, "read_document", reader)

    class _Store:
        def find(self, component: str):
            return object() if component == candidate.component_id else None

    monkeypatch.setattr(approval_gate, "build_fact_store", lambda: _Store())

    entries, skipped = approval_gate.entries_from_earlier_runs(
        BUCKET, now=NOW, client=_Bucket(_one_pending())
    )

    assert entries == ()
    # NOT counted as unreadable: nothing failed. Conflating "already answered" with "could not
    # be read" would report a degradation on every run after a normal approval.
    assert skipped == 0
    assert reader.opened == [], (
        "a candidate withheld by the store must not be opened at all -- the check is there to "
        "avoid work as well as to avoid a dead control"
    )


def test_an_unreachable_fact_store_withholds_nothing(
    monkeypatch: pytest.MonkeyPatch, candidate: CandidateFact, report: ApprovalReport
) -> None:
    """The safe direction, and it is the opposite of the obvious one.

    A store that cannot be read must not suppress the page. The cost of offering is one control
    that may be refused at the endpoint; the cost of withholding is silently hiding the only
    surface that can clear a stranded component -- which is the bug this whole path exists to
    fix, reintroduced by a failed API call.
    """
    reader = _Reader({CANDIDATE_URI: candidate, REPORT_URI: report})
    monkeypatch.setattr(approval_gate, "read_document", reader)

    def _explode():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(approval_gate, "build_fact_store", _explode)

    entries, skipped = approval_gate.entries_from_earlier_runs(
        BUCKET, now=NOW, client=_Bucket(_one_pending())
    )

    assert len(entries) == 1
    assert skipped == 0
