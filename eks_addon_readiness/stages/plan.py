"""Stage 5 — the ordered, interleaved upgrade sequence.

    in    assessment, facts
    out   ordered interleaved upgrade sequence, each step marked reversible or not

Cluster hops interleaved with component moves, in an order derived from Stage 4's
compatibility windows and declared dependencies — never invented. A component
Stage 4 could not decide is not sequenced: it is named in `unsequenceable` with its
reason, which is the honest output for an undecided component and the reason the
plan can be trusted where it *is* definite.

Every step carries `reversibility`. A ONE_WAY step is one you cannot walk back —
a CRD storage-version migration, for instance — and knowing which steps those are
before starting is the difference between a maintenance window and an incident.
"""

from __future__ import annotations

from typing import Any

from ..decide import ReadinessAssessment
from ..order import build_plan, ruleset_digest
from ..storage import build_fact_store, build_uri, plan_key, read_document, write_document
from .settings import from_event


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Sequence the assessment into an ordered plan and store it."""
    settings = from_event(event)
    assessment = read_document(event["assessment_uri"], ReadinessAssessment)
    store = build_fact_store()

    plan = build_plan(assessment, store, settings.target_kubernetes_version)

    uri = build_uri(
        settings.bucket,
        plan_key(
            settings.cluster_name,
            settings.target_kubernetes_version,
            plan.snapshot_digest,
            plan.store_digest,
            plan.ruleset_digest,
            event["snapshot_captured_at"],
        ),
    )
    write_document(uri, plan)

    return {
        **event,
        "stage": 5,
        "plan_uri": uri,
        "plan_digest": plan.digest(),
        "plan_ruleset_digest": ruleset_digest(),
        "feasible": plan.feasible,
        "plan_counts": plan.counts(),
        "one_way_steps": len(plan.one_way_steps),
    }
