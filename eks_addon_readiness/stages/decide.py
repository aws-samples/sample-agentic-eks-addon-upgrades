"""Stage 4 — a verdict per component per version hop, citing its evidence.

    in    snapshot, inventory, ledger, fact store, ruleset
    out   verdict per component per version hop, citing evidence

The first stage that accepts a target Kubernetes version, and therefore the first
permitted to turn a fact into a verdict. No model participates: the verdict is
computed by rules over a digest-cited ledger, so it replays.

Two properties are load-bearing. A missing fact yields `UNRESOLVED`, never
`INCOMPATIBLE` — absence of knowledge is not evidence of incompatibility. And an
EKS Insight decides only the first hop, because AWS assessed only the next minor.
"""

from __future__ import annotations

from typing import Any

from ..decide import build_assessment, ruleset_digest
from ..facts import GapLedger
from ..storage import (
    build_fact_store,
    build_uri,
    decisions_key,
    read_document,
    write_document,
)
from .settings import from_event


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Decide every component in the ledger against every hop to the target."""
    settings = from_event(event)
    ledger = read_document(event["ledger_uri"], GapLedger)
    store = build_fact_store()

    assessment = build_assessment(ledger, store, settings.target_kubernetes_version)

    uri = build_uri(
        settings.bucket,
        decisions_key(
            settings.cluster_name,
            settings.target_kubernetes_version,
            assessment.snapshot_digest,
            assessment.store_digest,
            assessment.ruleset_digest,
            event["snapshot_captured_at"],
        ),
    )
    write_document(uri, assessment)

    return {
        **event,
        "stage": 4,
        "assessment_uri": uri,
        "assessment_digest": assessment.digest(),
        "decide_ruleset_digest": ruleset_digest(),
        "hops": [f"{h.current_version}->{h.target_version}" for h in assessment.hops],
        "decide_counts": assessment.counts(),
    }
