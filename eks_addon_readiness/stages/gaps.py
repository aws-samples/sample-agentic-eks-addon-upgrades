"""Stage 3 — ledger of what is known and what is missing, every unknown typed.

    in    snapshot, inventory, fact store
    out   ledger of known vs missing, every unknown typed

The answer here is a statement about *knowledge*, never about risk. A component
with no stored fact is `FACT_MISSING` — researchable, because its name and version
are known — or `NOT_ASKABLE`, because there is no usable version to ask about.
Neither is a verdict. Verdicts need a target version and belong to Stage 4.

This is the stage that proves generality: a component the fact store has never
heard of produces a typed gap and flows on, rather than crashing or being skipped.
"""

from __future__ import annotations

from typing import Any

from ..facts import build_ledger, ruleset_digest
from ..report import InventoryReport
from ..snapshot import ClusterSnapshot
from ..storage import build_fact_store, build_uri, gaps_key, read_document, write_document
from .settings import from_event


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Build the gap ledger from the snapshot, the inventory and the fact store."""
    settings = from_event(event)
    snapshot = read_document(event["snapshot_uri"], ClusterSnapshot)
    report = read_document(event["inventory_uri"], InventoryReport)
    store = build_fact_store()

    ledger = build_ledger(report, snapshot, store)

    uri = build_uri(
        settings.bucket,
        gaps_key(
            settings.cluster_name,
            ledger.snapshot_digest,
            ledger.store_digest,
            ledger.ruleset_digest,
            report.snapshot_captured_at,
        ),
    )
    write_document(uri, ledger)

    return {
        **event,
        "stage": 3,
        "ledger_uri": uri,
        "ledger_digest": ledger.digest(),
        "store_digest": ledger.store_digest,
        "gaps_ruleset_digest": ruleset_digest(),
        "gaps_counts": ledger.counts(),
        # Surfaced, not swallowed. A store that failed to read is not a store with
        # no facts, and the difference decides whether a verdict means anything.
        "store_errors": list(ledger.store_errors),
    }
