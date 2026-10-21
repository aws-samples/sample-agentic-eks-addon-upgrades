"""Stage 6 -- research one unresolved component into a fact PROPOSED for human approval.

    in    a component identity and version series, from the Stage 3 ledger
    out   a candidate fact, its receipt, the page bytes it rests on, and an approval report

**Stage 6 never writes a fact.** It proposes one. Every candidate that derives a usable
window is `PENDING_APPROVAL`, and it reaches the live store only when a person approves it
through `stages/approve.py`.

**NOT DEPLOYED BY THIS SAMPLE.** This is the in-process research path: the model loop runs
inside this process. The deployed pipeline researches through the AgentCore Harness instead
(`stages/research_harness.py`, then `stages/judge_extraction.py`). This module is kept because
it is the reference path the test suite drives with a stand-in model, and it shares every
judging and acceptance rule with the deployed one.

**A pending fact does not block anything.** It is not in the store, so Stage 3 reports the
component as a typed gap and Stage 4 says UNRESOLVED with "absence of a fact is not evidence
of incompatibility". The run completes and answers every other component. Nothing waits on a
human -- which is what makes always-approve affordable rather than an operational tax.

**Every candidate is written, whatever was decided.** Pending and rejected candidates go to
the candidate prefix with their receipts, and a pending one also gets an approval report as
a digested document plus an HTML view of it. Writing only accepted facts and raising on the
rest sends the reason a component stayed a gap to a log rather than an artifact -- and lets
its pending area accumulate stale objects nobody can route to or find.

**The evidence is stored beside the receipt** so a fact can be re-judged later under
corrected logic without re-fetching, which is how a revoked fact gets replaced by a right
one rather than by another guess.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..decide.kubernetes import expand_hops
from ..models import KubernetesHop
from ..research.acceptance import advisory_note
from ..research.extract import ResearchError, research_component
from ..research.report import (
    bases_for,
    build_report,
    chart_missing_note,
    chart_summary,
    quoted_claims_for,
)
from ..storage import (
    build_fact_store,
    build_uri,
    candidate_key,
    write_document,
    write_json,
)
from .settings import from_event

# The IAM-authenticated approval endpoint used to feed the per-candidate HTML view, which
# was removed -- research emits pure JSON, and the approve control lives on the
# present stage's one-pager. present.py reads its own copy of this variable.


def _as_str(value: Any) -> str | None:
    """Enum or None -> plain string or None, because a payload carries JSON, not enums."""
    return None if value is None else str(value)


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Research one component into a proposal. Returns what was decided and where it landed.

    Required in `event`: `component_id` and `version_series`. Optional `installed_version`,
    `current_kubernetes_version` and `target_kubernetes_version` enable the verdict delta,
    which is the part of the report a person is actually deciding on -- without them the
    report still renders and says plainly that no verdict change was computed.
    """
    settings = from_event(event)
    component_id = str(event["component_id"]).strip()
    version_series = str(event["version_series"]).strip()
    researched_at = datetime.now(UTC)

    # The marked store, so a prior that is stale or revoked does not answer here either:
    # classifying the change direction against a fact already withdrawn would compare the
    # proposal to something no longer in force.
    current = build_fact_store().find(component_id)

    try:
        candidate, pages, extraction = research_component(
            component_id,
            version_series,
            current,
            # Task inputs. With a target the model returns an assessment of THIS
            # upgrade; without one it transcribes only. Passed straight from the event,
            # which the Map fills from the dispatch request and the run payload.
            installed_version=event.get("installed_version"),
            current_kubernetes_version=event.get("current_kubernetes_version"),
            target_kubernetes_version=event.get("target_kubernetes_version"),
            helm_release=event.get("helm_release"),
            model_id=event.get("model_id"),
            effort=event.get("effort"),
        )
    except ResearchError as exc:
        # No extraction at all. A typed outcome rather than a stack trace: the component
        # stays a researchable gap, which is the honest state.
        return {
            **event,
            "stage": 6,
            "component_id": component_id,
            "version_series": version_series,
            "decision": "NO_EXTRACTION",
            "confidence": "UNRESOLVED",
            "stored": False,
            "pending_approval": False,
            "reason": str(exc),
        }

    decision = str(candidate.receipt.decision)
    base_key = candidate_key(
        component_id,
        version_series,
        decision,
        candidate.digest(),
        researched_at.isoformat(),
    )

    candidate_uri = build_uri(settings.bucket, base_key)
    write_document(candidate_uri, candidate)

    evidence_uri = build_uri(settings.bucket, base_key.replace(".json", ".evidence.json"))
    write_json(
        evidence_uri,
        {
            "evidence_digest": pages.digest(),
            "pages": [
                {
                    "url": page.url,
                    "revision": page.revision,
                    "status": page.status,
                    "digest": page.digest,
                    "body": page.body,
                }
                for _, page in sorted(pages.pages.items())
            ],
        },
    )

    # THE MODEL'S OWN OUTPUT, STORED -- same reason as the harness path: until this
    # existed the extraction was verified, judged and then dropped, so nobody could ask
    # afterwards what the model actually said. A separate object rather than a field on
    # `CandidateFact`, because the candidate's digest is what an approval is keyed to.
    extraction_uri = build_uri(
        settings.bucket, base_key.replace(".json", ".extraction.json")
    )
    write_json(extraction_uri, extraction.model_dump(mode="json"))

    assessment = extraction.assessment
    result: dict[str, Any] = {
        **event,
        "stage": 6,
        "component_id": component_id,
        "version_series": version_series,
        "decision": decision,
        "confidence": str(candidate.receipt.confidence),
        "change_direction": str(candidate.receipt.change_direction),
        "values_verified": candidate.receipt.values_verified,
        "values_total": candidate.receipt.values_total,
        "tested_windows": list(candidate.receipt.tested_windows),
        "candidate_uri": candidate_uri,
        "candidate_digest": candidate.digest(),
        "evidence_uri": evidence_uri,
        "evidence_digest": candidate.receipt.evidence_digest,
        "pages_fetched": len(pages),
        # Always false here. Stage 6 cannot write a fact, by role and by design.
        "stored": False,
        "pending_approval": candidate.pending,
        "summary": candidate.receipt.summary,
        "extraction_uri": extraction_uri,
        "statements": len(extraction.statements),
        "releases": len(extraction.releases),
        "machine_confidence": _as_str(candidate.receipt.machine_confidence),
        "proposed_confidence": _as_str(candidate.receipt.proposed_confidence),
        "evidence_tier": candidate.receipt.evidence_tier,
        # The assessment on the wire: present builds its rows from the fact store and the
        # gap ledger, so this is the only route by which what the model concluded about
        # THIS upgrade reaches the page.
        "recommended_version": getattr(assessment, "recommended_version", None),
        "recommended_series": getattr(assessment, "recommended_series", None),
        "action": _as_str(getattr(assessment, "action", None)),
        "installed_version_status": _as_str(
            getattr(assessment, "installed_version_status", None)
        ),
        "upgrade_ordering": _as_str(getattr(assessment, "upgrade_ordering", None)),
        "covers_target_kubernetes": getattr(assessment, "covers_target_kubernetes", None),
        "covers_current_kubernetes": getattr(
            assessment, "covers_current_kubernetes", None
        ),
        "confidence_rationale": getattr(assessment, "confidence_rationale", None),
        "cited_sources": [
            c.model_dump(mode="json")
            for c in getattr(assessment, "cited_sources", ()) or ()
        ],
        "caveats": list(getattr(assessment, "caveats", ()) or ()),
        "not_found": chart_missing_note(
            event.get("helm_release"),
            candidate.knowledge,
            list(getattr(assessment, "not_found", ()) or ()),
        ),
        # THE CHART, AS THE FACT CARRIES IT -- only what verified. The approval page shows
        # it beside the recommended version because it is what the upgrade lane will run
        # `helm` on once the fact is approved.
        **chart_summary(candidate.knowledge),
    }

    # Every candidate gets its report document, approvable or not -- the reason a
    # component stayed a gap belongs in an artifact, not only in a result field. Built
    # from the extraction and the recorded pages, which is why `research_component`
    # returns both. Pure JSON only; the per-candidate HTML view is gone, because the
    # human-facing page is the present stage's one-pager.
    report = build_report(
        candidate,
        current=current,
        quoted_claims=quoted_claims_for(extraction, pages),
        proposed_bases=bases_for(extraction.statements),
        researched_at=researched_at,
        installed_version=event.get("installed_version"),
        cluster=event.get("cluster_name"),
        hops=_hops(event),
        advisory=advisory_note(
            candidate.receipt.confidence,
            candidate.receipt.contradictions,
            candidate.receipt.change_direction,
        ),
        # What the model concluded, after judge.py re-checked it. None for an extraction
        # from the older transcribe-only prompt, and the report renders without it.
        assessment=extraction.assessment,
    )
    report_uri = build_uri(settings.bucket, base_key.replace(".json", ".report.json"))
    write_document(report_uri, report)

    result.update(
        {
            "report_uri": report_uri,
            "report_digest": report.digest(),
            # THE REPORT'S LIST, NOT THE ASSESSMENT'S: the report falls back to the pages the
            # quotes were read from when the model left `cited_sources` empty, and the page
            # must show the same rows the report a person approves against carries.
            "cited_sources": [c.model_dump(mode="json") for c in report.cited_sources],
            "approval_headline": report.headline(),
            "verdict_changes": len(report.changed_hops),
            # Stated in the result so a caller reading only this sees the invariant: a
            # pending fact drives nothing, and the run did not wait for anyone.
            "blocks_pipeline": False,
        }
    )
    return result


def _hops(event: dict[str, Any]) -> tuple[KubernetesHop, ...]:
    """The hops to compute the verdict delta over, or none.

    Both versions are needed: a delta without a target is not a delta. When either is
    absent the report says so rather than inventing a target, because a verdict shown
    against a target nobody asked for is worse than no verdict section.
    """
    current = event.get("current_kubernetes_version")
    target = event.get("target_kubernetes_version")
    if not current or not target:
        return ()
    return tuple(expand_hops(str(current), str(target)))
