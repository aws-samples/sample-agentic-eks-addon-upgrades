"""A brief served from storage belongs to THIS plan's step, not the one it was written against.

THE DEFECT THESE PIN, in the shape it was found. Consider metrics-server's brief for
0.6.x -> 0.9.0, researched when that move was step 4 of one plan. On a later run the same move
was step 3 and the control-plane upgrade was step 4. `Brief plan` got it right -- `brief_reused`
named metrics-server at step 3 -- and then the assembler read the stored document and appended it
verbatim, step_order 4 and all. `briefed_steps` came out `[1, 2, 4]`, `AdviceView.for_step(3)` found
nothing, and the person who approved step 3 on the upgrade approval page was shown a card that said
no brief was available while a complete brief sat in the run's own document, attached to the cluster
upgrade. Nothing raised anywhere. cert-manager escaped because its two moves were steps 1 and 2 in
both plans, which is exactly the coincidence that let this ship.

THE SAME LINE CARRIED A SECOND STALE FIELD. All three reused briefs reported `reuse_note` of
"researched fresh; no brief was stored for this upgrade" -- the sentence the earlier run wrote --
while `brief_reused` said each was served from a days-old stored brief and
`brief_research_count` was 0. The payload's `reused_briefs` map therefore stated the exact opposite
of what happened, in the one field whose job is to stop a cached known-issues section reading as a
fresh one.

AND A THIRD NOBODY HAD NOTICED, which is what
`test_a_reused_brief_is_assessed_at_this_clusters_minor` exists for. `at_kubernetes` is not in the
reuse key either, and it is a version-correctness claim rather than a display detail: the same
component move sequenced BEFORE the control-plane upgrade applies at the current minor and sequenced
AFTER it applies at the target. A carried-over value asserts the move was assessed at a minor the
cluster has already left.

WHAT IS UNDER TEST IS THE SPLIT, NOT ONLY THE THREE FIELDS. A stored brief is addressed by component
and version pair and nothing else, so some of its fields are reusable by construction and some are
not, and `test_every_field_of_a_stored_brief_is_classified` fails if a field is added to the
contract without that decision being made. Two fields are KEPT deliberately -- `observed_at`,
because
`UpgradeBrief.reused` derives "served from storage" by comparing it against the run's own, and
`evidence_session`, because it names the bytes every quote in the document was checked against.
Regenerating either would break something real, so they are as much a decision as the six replaced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from eks_addon_readiness.brief.document import (
    ComponentBrief,
    SectionState,
    UpgradeBrief,
    component_brief,
)
from eks_addon_readiness.brief.walk import plan_walk
from eks_addon_readiness.knowledge import Reversibility
from eks_addon_readiness.models import ClusterReference
from eks_addon_readiness.order.plan import PlanStep, StepKind, UpgradePlan
from eks_addon_readiness.research.pages import PageSet
from eks_addon_readiness.stages import upgrade_brief
from eks_addon_readiness.view.advice import advice_from_brief

_CLUSTER = ClusterReference(name="my-eks-cluster", region="us-east-1")
_DIGEST = "sha256:" + "a" * 64
_STORE_DIGEST = "sha256:" + "5" * 64
_RULES = "sha256:" + "6" * 64

# The live numbers. metrics-server 0.6.4 installed, recorded up to 0.9.0.
_RELEASES = (
    "0.6.1",
    "0.6.2",
    "0.6.3",
    "0.6.4",
    "0.7.0",
    "0.7.1",
    "0.7.2",
    "0.8.0",
    "0.8.1",
    "0.9.0",
)

# When the stored brief was written, and when the run that reuses it happens.
_STORED_AT = "2026-09-20T08:51:43.569447+00:00"
_RUN_AT = "2026-09-26T03:57:57.514536+00:00"

# The sentence `brief-plan` computes for this run, from `brief/reuse.py::expiry_note`.
_THIS_RUNS_NOTE = (
    "served from a brief written 6 day(s) ago; 1 stored for this upgrade, within the 7-day "
    "reuse window"
)
# The sentence the run that WROTE the brief recorded, which is the one that leaked through.
_THE_OTHER_RUNS_NOTE = "researched fresh; no brief was stored for this upgrade"

_SESSION = "brief-metrics-server-20260920-085143-eab8350698ff"


def _move(order: int, at_kubernetes: str) -> PlanStep:
    """The metrics-server move, at a given position and a given cluster minor."""
    return PlanStep(
        order=order,
        kind=StepKind.COMPONENT_MOVE,
        description=f"Move metrics-server from 0.6.x to 0.9.0 on Kubernetes {at_kubernetes}",
        component="metrics-server",
        from_series="0.6.x",
        to_series="0.9.x",
        recommended_version="0.9.0",
        at_kubernetes=at_kubernetes,
        reversibility=Reversibility.REVERSIBLE,
    )


def _cluster_upgrade(order: int) -> PlanStep:
    return PlanStep(
        order=order,
        kind=StepKind.CLUSTER_UPGRADE,
        description="Advance the control plane from 1.35 to 1.36",
        from_kubernetes="1.35",
        to_kubernetes="1.36",
    )


def _plan(*steps: PlanStep) -> UpgradePlan:
    return UpgradePlan(
        cluster=_CLUSTER,
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        steps=steps,
        snapshot_digest=_DIGEST,
        report_digest=_DIGEST,
        ledger_digest=_DIGEST,
        assessment_digest=_DIGEST,
        store_digest=_STORE_DIGEST,
        ruleset_digest=_RULES,
    )


def _stored_brief(step: PlanStep) -> ComponentBrief:
    """The document the earlier run stored, built by the real writer from its own step.

    Built through `component_brief` rather than hand-assembled, because the point is that the
    WRITER is correct: it copies the header off the step it was given, and every stale field in the
    live document is a field that was true of that run. `extraction=None` is the honest total
    failure -- five NOT_LOOKED sections -- which keeps this test out of inventing an agent's answer
    while still carrying a real `coverage` for the reuse to preserve.
    """
    brief = component_brief(
        step,
        walk=plan_walk("0.6.x", "0.9.0", _RELEASES),
        extraction=None,
        pages=PageSet(),
        session_id=_SESSION,
        observed_at=_STORED_AT,
        reuse_note=_THE_OTHER_RUNS_NOTE,
        failure_reason="the fixture supplied no extraction",
    )
    # The one field a `None` extraction cannot supply. Set here so the kept-fields assertion has a
    # real value to compare rather than None against None: the chart that carries 0.9.0 is a
    # property of the version the reuse key pins, so it has to survive re-anchoring.
    return brief.model_copy(update={"chart_version": "3.12.2"})


def _assembled(
    plan: UpgradePlan,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reused: tuple[dict[str, Any], ...] = (),
    results: tuple[dict[str, Any], ...] = (),
    stored: ComponentBrief | None = None,
) -> tuple[dict[str, Any], UpgradeBrief]:
    """Run `stages/upgrade_brief.py` in `assemble` mode, with no AWS at all.

    No bucket, so `_publish` reports that it could not store the document and the payload carries
    `brief_not_published` -- the stage's own non-fatal path, which is what makes the assembler
    exercisable here. `store_digest` is supplied so no fact store is opened.
    """

    def _read(uri: str, model: type) -> Any:
        if model is UpgradePlan:
            assert uri == "s3://b/plan.json"
            return plan
        assert model is ComponentBrief and uri == "s3://b/stored.json"
        assert stored is not None, "the test asked for a stored brief it did not supply"
        return stored

    monkeypatch.setattr(upgrade_brief, "read_document", _read)
    monkeypatch.delenv("DOCUMENT_BUCKET", raising=False)
    payload = upgrade_brief.handler(
        {
            "plan_uri": "s3://b/plan.json",
            "mode": "assemble",
            "observed_at": _RUN_AT,
            "store_digest": _STORE_DIGEST,
            "brief_reused": list(reused),
            "brief_results": list(results),
        }
    )
    document = UpgradeBrief(
        observed_at=payload["observed_at"],
        cluster=plan.cluster,
        target_kubernetes_version=plan.target_kubernetes_version,
        plan_digest=plan.digest(),
        store_digest=_STORE_DIGEST,
        ruleset_digest=payload["brief_ruleset_digest"],
    )
    assert document.briefs == (), "the rebuild below is what carries the briefs, not this shell"
    return payload, document


def _reuse_entry(step: PlanStep, uri: str = "s3://b/stored.json") -> dict[str, Any]:
    """The `brief_reused` entry `brief-plan` writes for this run's step."""
    return {
        "step_order": step.order,
        "component": str(step.component or "metrics-server"),
        "uri": uri,
        "written_on": "2026-09-20T00:00:00+00:00",
        "age_days": 6,
        "reuse_note": _THIS_RUNS_NOTE,
        "step": step.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# THE PLAN SIDE: `brief-plan` has to hand the assembler this run's step
# ---------------------------------------------------------------------------


def test_the_reuse_entry_carries_this_runs_step(monkeypatch) -> None:
    """The step travels on the payload, because it is the assembler's only source for it.

    `_unsupported_steps` mints steps that are deliberately NOT in `plan.steps` -- a component no
    version series of which reaches the target gets a work item and a row but never a plan step --
    so an assembler that looked this run's step up in the plan document would find nothing for
    exactly the rows the last redesign of this stage existed to give a brief.
    """
    plan = _plan(_move(3, "1.35"), _cluster_upgrade(4))
    stored = ("s3://b/stored.json",)
    # RELATIVE TO NOW, NOT A FIXED DATE. The handler decides reuse against the real clock and a
    # seven-day window, so a hard-coded `written_on` makes this test pass for a week after it is
    # written and fail forever after -- the brief ages out and is (correctly) researched fresh.
    # One day old is inside any window this stage could be configured with.
    written_on = datetime.now(UTC) - timedelta(days=1)

    monkeypatch.setattr(upgrade_brief, "read_document", lambda uri, model: plan)
    monkeypatch.setattr(upgrade_brief, "build_fact_store", lambda: _NoFacts())
    monkeypatch.setattr(
        upgrade_brief,
        "_stored_for",
        lambda bucket, component, frm, to: (
            upgrade_brief.reuse.StoredBrief(uri=stored[0], written_on=written_on),
        ),
    )
    payload = upgrade_brief.handler(
        {"plan_uri": "s3://b/plan.json", "mode": "plan", "bucket": "b"}
    )

    entry = next(e for e in payload["brief_reused"] if e["component"] == "metrics-server")
    assert entry["step_order"] == 3
    assert PlanStep.model_validate(entry["step"]).order == 3, (
        "the assembler re-anchors the stored document to this step, so the step has to reach it"
    )
    assert entry["reuse_note"].startswith("served from a brief written")


class _NoFacts:
    """A fact store that answers nothing. `_work_item` tolerates it; the reuse path never asks."""

    def find(self, _component: str) -> None:
        return None

    def digest(self) -> str:
        return _STORE_DIGEST


# ---------------------------------------------------------------------------
# THE DEFECT ITSELF
# ---------------------------------------------------------------------------


def test_a_reused_brief_lands_on_this_plans_step_and_not_the_one_it_was_written_at(
    monkeypatch,
) -> None:
    """The live failure, reproduced end to end: stored at step 4, served into step 3.

    Every number here is the run's. The brief is written against a plan whose metrics-server move
    is step 4, and reused by a plan whose metrics-server move is step 3 and whose step 4 is the
    control-plane upgrade -- so a brief that keeps its own step number attaches to the cluster
    upgrade, which is what happened.
    """
    written_against = _plan(_move(4, "1.35"), _cluster_upgrade(3))
    stored = _stored_brief(_move(4, "1.35"))
    assert stored.step_order == 4, "the fixture must reproduce the stale number, not dodge it"

    this_run = _plan(_move(3, "1.35"), _cluster_upgrade(4))
    payload, _ = _assembled(
        this_run, monkeypatch, reused=(_reuse_entry(_move(3, "1.35")),), stored=stored
    )

    assert payload["briefed_steps"] == [3], (
        "the brief was filed against the step the plan it was WRITTEN for gave it. The live run "
        f"reported briefed_steps [1, 2, 4] for the same reason; this one reported "
        f"{payload['briefed_steps']}"
    )
    assert written_against.steps[0].order == 4, "the two plans must disagree for this to prove it"
    # The honesty lists are addressed the same way and move with it.
    assert [s["step"] for s in payload["sections_not_looked"]] == [3] * 5


def test_the_approval_page_finds_a_reused_brief_on_the_card_it_offers(monkeypatch) -> None:
    """THE JOIN THE PERSON ACTUALLY MADE. Approved step 3; the brief has to be on step 3.

    `stages/upgrade_approval.py` builds `{step.step_order for step in advice.steps}` and the page
    calls `AdviceView.for_step(order)`. Both are asserted, because the failure was silent in both:
    the mail counted the row as unbriefed and the card rendered `No brief`.
    """
    this_run = _plan(_move(3, "1.35"), _cluster_upgrade(4))
    payload, _ = _assembled(
        this_run,
        monkeypatch,
        reused=(_reuse_entry(_move(3, "1.35")),),
        stored=_stored_brief(_move(4, "1.35")),
    )
    document = _rebuilt(payload, this_run, _stored_brief(_move(4, "1.35")), _move(3, "1.35"))

    advice = advice_from_brief(document)
    assert advice is not None
    assert {step.step_order for step in advice.steps} == {3}, (
        "the set stages/upgrade_approval.py joins on. 4 here means the mail says the approved row "
        "has no brief"
    )
    assert advice.for_step(3) is not None
    assert advice.for_step(4) is None, "step 4 is the control-plane upgrade and has no brief"
    assert advice.for_step(3).reuse_note == _THIS_RUNS_NOTE


def test_a_reused_brief_says_it_came_from_storage(monkeypatch) -> None:
    """The second stale field. The stored sentence says the opposite of what the run did.

    The live payload reported every reused brief as "researched fresh; no brief was stored for this
    upgrade" beside a `brief_research_count` of 0 and three entries in `brief_reused`. A reader of
    `reused_briefs` was told a five-day-old reading of an issue tracker had just been taken.
    """
    this_run = _plan(_move(3, "1.35"), _cluster_upgrade(4))
    payload, _ = _assembled(
        this_run,
        monkeypatch,
        reused=(_reuse_entry(_move(3, "1.35")),),
        stored=_stored_brief(_move(4, "1.35")),
    )

    assert payload["reused_briefs"] == {"metrics-server": _THIS_RUNS_NOTE}
    assert _THE_OTHER_RUNS_NOTE not in str(payload["reused_briefs"])
    assert payload["brief_counts"]["reused_from_storage"] == 1, (
        "derived by comparing each brief's observed_at against the run's, so a re-anchoring that "
        "overwrote observed_at would zero this and hide the reuse entirely"
    )


def test_a_reused_brief_is_assessed_at_this_clusters_minor(monkeypatch) -> None:
    """THE FIELD NOBODY HAD NOTICED, and the only one of the three that is a correctness claim.

    The reuse key is component and version pair -- it says nothing about the cluster. So the same
    0.6.x -> 0.9.0 brief is served to a plan that sequences the move BEFORE the control-plane
    upgrade (1.35) and to one that sequences it after (1.36). Here the stored brief was written for
    a pre-upgrade move and is reused by a plan that moves metrics-server after the cluster: a
    carried-over 1.35 would have the document assert the move was assessed at a minor the cluster
    has already left.
    """
    this_run = _plan(_cluster_upgrade(1), _move(2, "1.36"))
    step = _move(2, "1.36")
    payload, _ = _assembled(
        this_run, monkeypatch, reused=(_reuse_entry(step),), stored=_stored_brief(_move(4, "1.35"))
    )
    document = _rebuilt(payload, this_run, _stored_brief(_move(4, "1.35")), step)

    assert payload["briefed_steps"] == [2]
    assert document.brief_for(2).at_kubernetes == "1.36", (
        "the stored brief was written for a move at 1.35 and is being shown against a move at 1.36"
    )


def test_a_reused_brief_keeps_everything_the_reuse_key_pins(monkeypatch) -> None:
    """The other half of the verdict: what re-anchoring must NOT touch.

    Re-anchoring a stored brief into a new plan is only worth doing if the research survives it.
    `coverage`, `sequencing` and the five sections are what the component-and-version key pins, and
    they are the entire reason a brief is stored. `observed_at` and `evidence_session` describe the
    earlier research act and are kept for reasons that are load-bearing rather than incidental --
    see the module docstring.
    """
    stored = _stored_brief(_move(4, "1.35"))
    this_run = _plan(_move(3, "1.35"), _cluster_upgrade(4))
    payload, _ = _assembled(
        this_run, monkeypatch, reused=(_reuse_entry(_move(3, "1.35")),), stored=stored
    )
    served = _rebuilt(payload, this_run, stored, _move(3, "1.35")).brief_for(3)

    # EVERY KEPT FIELD, DRIVEN OFF THE LISTS THEMSELVES rather than a hand-picked few, so a field
    # moved into a keep list has to survive the round trip through `model_dump`/`model_validate`.
    kept = upgrade_brief._THE_COMPONENT_UPGRADE + upgrade_brief._THE_EARLIER_RESEARCH
    before, after = stored.model_dump(), served.model_dump()
    assert {f: after[f] for f in kept} == {f: before[f] for f in kept}, (
        "re-anchoring disturbed something the reuse key pins. It dumps and revalidates the whole "
        "document to replace six fields, so a lossy round trip shows up here first"
    )

    assert served.component == "metrics-server"
    assert served.coverage == stored.coverage, "the walk is about the hop, which the key pins"
    assert served.coverage.minors_entered == ("0.7", "0.8", "0.9")
    assert served.sequencing == stored.sequencing
    assert served.sections == stored.sections, (
        "the sections ARE the brief. A re-anchoring that disturbed them would make reuse pointless"
    )
    assert all(s.state is SectionState.NOT_LOOKED for s in served.sections)
    assert served.observed_at == _STORED_AT != _RUN_AT, (
        "kept deliberately: UpgradeBrief.reused compares this against the run's own observed_at, "
        "so overwriting it would make a six-day-old brief indistinguishable from a fresh one"
    )
    assert served.evidence_session == _SESSION, (
        "kept deliberately: it names the recorded bytes every quote in these sections was checked "
        "against, so dropping it would leave verified quotes with nothing to verify against"
    )


def test_every_field_of_a_stored_brief_is_classified() -> None:
    """Adding a field to `ComponentBrief` must force the same decision, not default to carrying it.

    THE DEFECT WAS A DEFAULT, not a mistake in reasoning: the reuse loop appended the document and
    every field came along because nothing said which ones may. This fails the moment the contract
    grows a field that has not been put in one of the four lists, which is the only way that
    default stops being reintroduced.
    """
    classified = (
        upgrade_brief._ANCHORED_TO_THIS_PLAN
        + upgrade_brief._ANCHORED_TO_THIS_RUN
        + upgrade_brief._THE_COMPONENT_UPGRADE
        + upgrade_brief._THE_EARLIER_RESEARCH
    )
    fields = set(ComponentBrief.model_fields)

    assert len(classified) == len(set(classified)), (
        f"a field is in two lists: {sorted({f for f in classified if classified.count(f) > 1})}"
    )
    assert set(classified) == fields, (
        f"unclassified: {sorted(fields - set(classified))}; named but not a field: "
        f"{sorted(set(classified) - fields)}. Decide whether each new field describes the "
        "COMPONENT UPGRADE (reusable -- that is what storing a brief is for) or the PLAN or RUN "
        "it was written in (must be re-anchored or dropped)."
    )
    # The six that are re-anchored are exactly the header fields a reader checks a brief against
    # the step it claims to describe -- `ComponentBrief`'s own stated reason for carrying them.
    assert set(upgrade_brief._ANCHORED_TO_THIS_PLAN) == {
        "step_order",
        "step_kind",
        "from_series",
        "to_series",
        "recommended_version",
        "at_kubernetes",
    }


def test_two_stored_briefs_that_share_a_step_number_no_longer_lose_the_document(
    monkeypatch,
) -> None:
    """A latent whole-document failure the same line carried, found reviewing the fix.

    Stored step numbers come from whatever plan wrote each brief, so two briefs for DIFFERENT
    components can both carry the same one -- metrics-server stored at 4 and cert-manager stored at
    4, by two plans that happened to sequence them there. Appending both verbatim gave
    `UpgradeBrief.every_step_appears_once` two entries at step 4, which raises, which loses the
    document and every brief in it rather than one. The live run escaped only because its three
    stored numbers happened to be distinct. Re-anchoring makes the numbers this plan's, and this
    plan's orders are unique by construction.
    """
    other = PlanStep(
        order=4,
        kind=StepKind.COMPONENT_MOVE,
        description="Move cert-manager from 1.19.x to 1.21.2 on Kubernetes 1.35",
        component="cert-manager",
        from_series="1.19.x",
        to_series="1.21.x",
        recommended_version="1.21.2",
        at_kubernetes="1.35",
        reversibility=Reversibility.REVERSIBLE,
    )
    this_run = _plan(_move(3, "1.35"), other)

    stored = _stored_brief(_move(4, "1.35"))
    collided = stored.model_copy(update={"component": "cert-manager"})
    assert stored.step_order == collided.step_order == 4, "both stored copies claim step 4"

    def _read(uri: str, model: type) -> Any:
        if model is UpgradePlan:
            return this_run
        return stored if uri == "s3://b/stored.json" else collided

    monkeypatch.setattr(upgrade_brief, "read_document", _read)
    monkeypatch.delenv("DOCUMENT_BUCKET", raising=False)
    payload = upgrade_brief.handler(
        {
            "plan_uri": "s3://b/plan.json",
            "mode": "assemble",
            "observed_at": _RUN_AT,
            "store_digest": _STORE_DIGEST,
            "brief_reused": [
                _reuse_entry(_move(3, "1.35")),
                _reuse_entry(other, uri="s3://b/other.json"),
            ],
        }
    )
    assert payload["briefed_steps"] == [3, 4], (
        "two briefs at two steps. Verbatim, both claimed step 4 and every_step_appears_once raised"
    )
    assert "brief_digest" in payload, "the document was written rather than lost to a collision"
    assert payload["brief_counts"]["reused_from_storage"] == 2


def test_the_fallback_lookup_refuses_a_step_that_names_another_component(monkeypatch) -> None:
    """A step matched by number alone could re-anchor the version fields to the wrong move.

    Not reachable through `brief-plan` -- both numbers come from the same content-addressed plan --
    so this drives the guard directly. The join key is still re-anchored, because that is what the
    approval page renders by and the entry states it independently; the five version fields are
    left alone rather than taken from a step about a different component.
    """
    this_run = _plan(_move(3, "1.35"), _cluster_upgrade(4))
    entry = _reuse_entry(_move(3, "1.35"))
    del entry["step"]
    entry["component"] = "not-the-component-at-step-3"

    step = upgrade_brief._this_runs_step(entry, this_run)
    assert step is None, "a step naming another component must not be used to re-anchor this brief"

    served = upgrade_brief._reanchored(_stored_brief(_move(4, "1.35")), entry, this_run)
    assert served.step_order == 3, "the join key comes off the entry and is always re-anchored"
    assert served.reuse_note == _THIS_RUNS_NOTE
    assert served.at_kubernetes == "1.35", "left as stored rather than taken from the wrong step"


# ---------------------------------------------------------------------------
# THE FRESH PATH, WHICH IS CORRECT AND HAS TO STAY THAT WAY
# ---------------------------------------------------------------------------


def test_a_freshly_researched_brief_needs_no_re_anchoring(monkeypatch) -> None:
    """The other half of the reuse loop is right for a reason worth pinning.

    `_work_item` hands `brief_harness` this plan's step and this run's `reuse_note`, and
    `component_brief` copies the header straight off that step -- so a fresh brief already carries
    this plan's numbers and `_assemble` appends it as it stands. The asymmetry is deliberate; this
    test fails if a future change makes the fresh path read a stored document too.
    """
    this_run = _plan(_move(3, "1.35"), _cluster_upgrade(4))
    step = _move(3, "1.35")
    fresh = component_brief(
        step,
        walk=plan_walk("0.6.x", "0.9.0", _RELEASES),
        extraction=None,
        pages=PageSet(),
        session_id="brief-metrics-server-20260926-035757-000000000000",
        observed_at=_RUN_AT,
        reuse_note="researched fresh; no brief was stored for this upgrade",
        failure_reason="the fixture supplied no extraction",
    )

    assert fresh.step_order == 3
    assert fresh.at_kubernetes == "1.35"
    assert fresh.observed_at == _RUN_AT, (
        "a fresh brief's observed_at IS the run's, which is what makes UpgradeBrief.reused "
        "exclude it"
    )

    def _read(uri: str, model: type) -> Any:
        if model is UpgradePlan:
            return this_run
        return fresh

    monkeypatch.setattr(upgrade_brief, "read_document", _read)
    monkeypatch.delenv("DOCUMENT_BUCKET", raising=False)
    payload = upgrade_brief.handler(
        {
            "plan_uri": "s3://b/plan.json",
            "mode": "assemble",
            "observed_at": _RUN_AT,
            "store_digest": _STORE_DIGEST,
            "brief_results": [
                {
                    "stage": "brief-component",
                    "step_order": 3,
                    "component": "metrics-server",
                    "briefed": True,
                    "brief_uri": "s3://b/fresh.json",
                }
            ],
        }
    )

    assert payload["briefed_steps"] == [3]
    assert payload["reused_briefs"] == {}, "nothing was served from storage on this path"
    assert payload["brief_counts"]["reused_from_storage"] == 0


# ---------------------------------------------------------------------------
# THE DEGRADED PATHS. An advisory stage may not fail a run, whatever it is handed.
# ---------------------------------------------------------------------------


def test_a_reused_brief_is_still_re_anchored_when_the_payload_predates_the_step(
    monkeypatch,
) -> None:
    """A deploy landing between `Brief plan` and `Brief assemble` leaves an entry with no step.

    Both states invoke the same Lambda within a run, so this is only reachable across a deploy in
    flight -- which is exactly when it would happen unobserved. The step number is on the entry
    regardless, so the join is re-anchored either way; a real plan step is then recovered by its
    number so the remaining five fields are too.
    """
    this_run = _plan(_move(3, "1.35"), _cluster_upgrade(4))
    entry = _reuse_entry(_move(3, "1.35"))
    del entry["step"]

    payload, _ = _assembled(
        this_run, monkeypatch, reused=(entry,), stored=_stored_brief(_move(4, "1.35"))
    )
    served = _rebuilt(payload, this_run, _stored_brief(_move(4, "1.35")), _move(3, "1.35"))

    assert payload["briefed_steps"] == [3]
    assert served.brief_for(3).at_kubernetes == "1.35"
    assert payload["reused_briefs"] == {"metrics-server": _THIS_RUNS_NOTE}


def test_a_brief_that_cannot_be_re_anchored_is_named_and_not_shown(monkeypatch) -> None:
    """The refusal direction, and it has to be a named absence rather than a stale brief.

    Unreachable through `brief-plan`, which only reuses steps `unbriefable_reason` has already
    passed. Reached here by handing the assembler an entry with no `reuse_note`, which is what a
    hand-built payload or a replay against an older shape produces. Showing the un-anchored brief
    would reintroduce the defect; letting it raise would lose the whole document and every brief
    beside it. `unbriefed` is this stage's own word for "we could not tell you".
    """
    this_run = _plan(_move(3, "1.35"), _cluster_upgrade(4))
    entry = _reuse_entry(_move(3, "1.35"))
    del entry["reuse_note"]

    payload, _ = _assembled(
        this_run, monkeypatch, reused=(entry,), stored=_stored_brief(_move(4, "1.35"))
    )

    assert payload["briefed_steps"] == []
    assert set(payload["unbriefed_steps"]) == {3}, "named against THIS run's step, not step 4"
    assert "could not be re-anchored" in payload["unbriefed_steps"][3]
    assert payload["blocking"] is False and payload["advisory"] is True


# ---------------------------------------------------------------------------


def _rebuilt(
    payload: dict[str, Any], plan: UpgradePlan, stored: ComponentBrief, step: PlanStep
) -> UpgradeBrief:
    """The run document, as `_assemble` wrote it, reconstructed for the consumer-side assertions.

    The stage returns a payload rather than the document -- it publishes the document and reports
    its digest -- so the assertions that need the object rebuild it from the same inputs and check
    the digest matches. That keeps this a test of the stage's output and not of a parallel fixture.
    """
    document = UpgradeBrief(
        observed_at=payload["observed_at"],
        cluster=plan.cluster,
        target_kubernetes_version=plan.target_kubernetes_version,
        plan_digest=plan.digest(),
        store_digest=_STORE_DIGEST,
        ruleset_digest=payload["brief_ruleset_digest"],
        briefs=(
            upgrade_brief._reanchored(
                stored,
                {
                    "step_order": step.order,
                    "reuse_note": _THIS_RUNS_NOTE,
                    "step": step.model_dump(mode="json"),
                },
                plan,
            ),
        ),
    )
    assert document.digest() == payload["brief_digest"], (
        "the rebuild diverged from what the stage actually wrote, so the assertions below would be "
        "about the fixture rather than about the stage"
    )
    return document
