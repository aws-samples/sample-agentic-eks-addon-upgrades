"""Judge one extraction against the bytes recorded for its session. No model here.

**This is the deterministic half of Stage 6, and it is now a separate deployable.** The
Harness reads sources and transcribes; this judges. The split is not packaging convenience --
it is the project's thesis expressed as two artifacts:

    the Harness      a model in a tool loop. Reads. Cannot write a fact, cannot judge.
    this handler     deterministic code. Judges. No model client in its package at all.

So "the model never issues a verdict" stops being a claim about discipline and becomes a
claim about what is installed where. The Harness's package has no `judge`, no `bounds`, no
`acceptance`; this one has no model client.

**How the evidence gets here.** The fetch tool records every body it serves, keyed by
session, before the model reads it. This handler rebuilds the `PageSet` from those recorded
objects and checks every quote against them. The model cannot influence what is in the
recording, because the recording happened before it saw the bytes -- the
property that makes the quote check mean anything.

**Why this is stronger than the in-process version it replaces.** The old Lambda held the
bodies in memory and judged them in the same process that fetched them, so the recording was
only as trustworthy as that process. Now the bytes are persisted out-of-band and the receipt
cites their digest, so a third party can fetch the same objects and re-hash them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..decide.kubernetes import expand_hops
from ..models import KubernetesHop
from ..research.acceptance import advisory_note
from ..research.boundary import ResearchExtraction
from ..research.judge import judge
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


class ExtractionRejected(ValueError):
    """The payload did not match the extraction contract.

    Raised rather than coerced. This is the wall that replaces API-level schema enforcement,
    which Bedrock does not offer on this endpoint: the contract forbids unknown fields and
    requires the statement `kind`, so a conflated or padded extraction fails here and never
    becomes a stored value. Salvaging fields out of a malformed extraction is how an
    unchecked value reaches a store.
    """


def _as_str(value: Any) -> str | None:
    """Enum or None -> plain string or None, because a payload carries JSON, not enums."""
    return None if value is None else str(value)


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Judge one extraction and write what it produced. Deterministic; no model call.

    Required in `event`:

        extraction   the ResearchExtraction object the Harness returned
        session_id   the research session, so the recorded bodies can be found

    Optional `installed_version`, `current_kubernetes_version` and
    `target_kubernetes_version` enable the verdict delta, which is the part of the approval
    report a person is actually deciding on.
    """
    settings = from_event(event)
    session_id = str(event["session_id"]).strip()
    researched_at = datetime.now(UTC)

    raw = event.get("extraction")
    if raw is None:
        raise ExtractionRejected("no extraction was supplied")
    try:
        extraction = (
            raw
            if isinstance(raw, ResearchExtraction)
            else ResearchExtraction.model_validate(raw)
        )
    except ValueError as exc:
        raise ExtractionRejected(
            f"the extraction did not match the contract: {exc}"
        ) from exc

    # The bytes the model was shown, read back from where the fetch tool recorded them.
    from .fetch_source import load_recorded_pages

    pages = load_recorded_pages(session_id)
    if len(pages) == 0:
        # No recording means no quote can be verified. Refused rather than judged, because a
        # fact whose quotes were checked against nothing is exactly the defect being fixed.
        raise ExtractionRejected(
            f"no fetched bodies were recorded for session {session_id!r}, so no quote can "
            "be verified. Refusing to judge an extraction against no evidence."
        )

    component_id = extraction.component_id
    current = build_fact_store().find(component_id)
    # installed_version and helm_release ride the event from the dispatch request. The judge
    # uses them only for the Helm chart-vs-application version-scheme guard: a Helm component
    # answered in application versions whose major omits the installed CHART version's major
    # (kyverno chart 3.3.9 answered in 1.x) is rejected with an actionable reason, which
    # research_harness.py re-asks on. Both are None for a managed add-on or an unknown
    # version, leaving every other component judged exactly as before.
    candidate = judge(
        extraction,
        pages,
        current,
        retrieved_at=researched_at,
        installed_version=(
            str(event["installed_version"])
            if event.get("installed_version") is not None
            else None
        ),
        helm_release=(
            str(event["helm_release"]) if event.get("helm_release") is not None else None
        ),
    )

    decision = str(candidate.receipt.decision)
    base_key = candidate_key(
        component_id,
        candidate.version_series,
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
            "session_id": session_id,
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

    # THE MODEL'S OWN OUTPUT, STORED. Until this existed, the extraction was verified,
    # judged and then dropped: only what `bounds.py` derived from it survived, so nobody
    # could later ask what the model actually said. That is the wrong thing to discard --
    # the assessment (recommended version, upgrade ordering, cited sources, caveats, the
    # rationale) is the part a reader acts on, and the statements are what a re-judge under
    # corrected logic would need.
    #
    # A SEPARATE OBJECT rather than a field on `CandidateFact`, deliberately: the candidate
    # is what a person approves and its digest is what an approval is keyed to, so widening
    # it would move every candidate digest and invalidate anything mid-approval. This sits
    # beside the evidence object, under the same base key, and its digest is reported so a
    # reader can tell two runs apart.
    extraction_uri = build_uri(
        settings.bucket, base_key.replace(".json", ".extraction.json")
    )
    write_json(extraction_uri, extraction.model_dump(mode="json"))

    assessment = extraction.assessment
    result: dict[str, Any] = {
        "stage": 6,
        "phase": "judge",
        "component_id": component_id,
        "version_series": candidate.version_series,
        "session_id": session_id,
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
        "pages_recorded": len(pages),
        # Always false. Nothing here can write a fact: only a person, through the approval
        # endpoint, and this handler's role has no PutItem on the fact table.
        "stored": False,
        "pending_approval": candidate.pending,
        "blocks_pipeline": False,
        "summary": candidate.receipt.summary,
        # Where the model's own answer landed, so present -- or a person -- can read it
        # without re-running anything.
        "extraction_uri": extraction_uri,
        "statements": len(extraction.statements),
        "releases": len(extraction.releases),
        # The confidence arithmetic, so a reader can see the model was capped and by what.
        "machine_confidence": _as_str(candidate.receipt.machine_confidence),
        "proposed_confidence": _as_str(candidate.receipt.proposed_confidence),
        "evidence_tier": candidate.receipt.evidence_tier,
        # THE ASSESSMENT ON THE WIRE. Present builds its component rows from the fact store
        # and the gap ledger, so these are the only route by which what the model concluded
        # about THIS upgrade reaches the page. All None when the model returned no
        # assessment, which is the legacy transcribe-only shape.
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

    # The report document is built for EVERY candidate, approvable or not. The
    # component with the richest evidence and no derivable window is exactly the one a
    # person most needs to see, so suppressing its report would be the wrong default.
    # Pure JSON only -- the human-facing page is the present stage's one-pager,
    # so the per-candidate HTML view is gone.
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
        }
    )
    return result


def _hops(event: dict[str, Any]) -> tuple[KubernetesHop, ...]:
    """The hops to compute the verdict delta over, or none.

    Both versions are needed: a delta without a target is not a delta. When either is absent
    the report says so rather than inventing a target.
    """
    current = event.get("current_kubernetes_version")
    target = event.get("target_kubernetes_version")
    if not current or not target:
        return ()
    return tuple(expand_hops(str(current), str(target)))
