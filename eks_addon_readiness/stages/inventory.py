"""Stage 2 — turn a snapshot into one record per component.

    in    snapshot
    out   one record per component: identity, true version from Helm chart
          provenance, confidence, classification

A pure function of the snapshot. No cluster access, no network, no clock. Its
document cites the snapshot digest it was derived from and the digest of the rules
that derived it, which is what makes it verifiable rather than merely stored.
"""

from __future__ import annotations

from typing import Any

from ..report import build_report, ruleset_digest
from ..snapshot import ClusterSnapshot
from ..storage import build_uri, inventory_key, read_document, write_document
from .settings import from_event


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Assess the snapshot named on the payload and store the inventory report."""
    settings = from_event(event)
    snapshot = read_document(event["snapshot_uri"], ClusterSnapshot)
    report = build_report(snapshot, event["snapshot_digest"])

    uri = build_uri(
        settings.bucket,
        inventory_key(
            settings.cluster_name,
            report.snapshot_digest,
            report.ruleset_digest,
            report.snapshot_captured_at,
        ),
    )
    write_document(uri, report)

    return {
        **event,
        "stage": 2,
        "inventory_uri": uri,
        "inventory_digest": report.digest(),
        "inventory_ruleset_digest": ruleset_digest(),
        "inventory_counts": report.counts(),
    }
