"""Stage 1 — read a cluster, write a sanitized snapshot.

    in    cluster name, region, a read-only role to assume
    out   sanitized cluster snapshot + its digest

The only stage that touches a cluster. It assumes a per-cluster read-only role
before reading anything, so the identity doing the reading is the least-privileged
one available and is auditable per cluster rather than per deployment.
"""

from __future__ import annotations

import os
from typing import Any

from ..scan import collect_snapshot, snapshot_digest
from ..storage import build_uri, snapshot_key, write_document
from .settings import from_event


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Collect one cluster's snapshot and store it.

    Returns the payload Stage 2 consumes: where the snapshot is and what it hashes
    to. The digest travels alongside the URI so every later stage can prove it read
    the document it was told to read.
    """
    settings = from_event(event)
    role_arn = str(event.get("reader_role_arn") or os.environ.get("READER_ROLE_ARN", "")).strip()

    from ..scan.adapters.eks_mcp import cluster_reader

    with cluster_reader(
        cluster_name=settings.cluster_name,
        region=settings.region,
        role_arn=role_arn or None,
    ) as reader:
        snapshot = collect_snapshot(
            reader, region=settings.region, account_alias=event.get("account_alias")
        )

    digest = snapshot_digest(snapshot)
    uri = build_uri(
        settings.bucket,
        snapshot_key(settings.cluster_name, digest, snapshot.captured_at.isoformat()),
    )
    write_document(uri, snapshot)

    return {
        "stage": 1,
        "cluster_name": settings.cluster_name,
        "region": settings.region,
        "bucket": settings.bucket,
        "target_kubernetes_version": settings.target_kubernetes_version,
        "snapshot_uri": uri,
        "snapshot_digest": digest,
        # The observation time, not the run time. Every later stage keys its object
        # by it, so the whole chain for one scan lands under one date partition.
        "snapshot_captured_at": snapshot.captured_at.isoformat(),
        "kubernetes_version": snapshot.kubernetes_version,
        "scan_counts": {
            "eks_addons": len(snapshot.eks_addons),
            "helm_releases": len(snapshot.helm_releases),
            "workloads": len(snapshot.workloads),
            "resources": len(snapshot.resources),
            "collection_gaps": len(snapshot.collection_gaps),
        },
    }
