"""Build a sanitized `ClusterSnapshot` from read-only cluster inventory.

`collect_snapshot` below is the whole of Stage 1's orchestration. Each source it
consults is one module in this package, each returns its own slice of the snapshot
plus the typed gaps that collecting it left, and none of them knows about the
others. This was once a single ~317-line function; the behaviour is unchanged.

    cluster.py          identity and Kubernetes version   (fatal if unreadable)
    addons.py           EKS-managed add-ons + catalogue
    workloads.py        namespaced workloads + Helm releases
    cluster_scoped.py   CRDs, APIServices, webhooks + served API versions
    insights.py         AWS's own upgrade-readiness opinion

Only the cluster read is fatal: without a Kubernetes version there is nothing to
assess. Every other failure becomes an `UnresolvedFinding` in `collection_gaps`,
because an empty collection where access was denied is indistinguishable from a
cluster that genuinely has nothing.

Sanitization is by construction, in `sanitize.py`: only an explicit allowlist of
fields is copied out of each response, so the cluster endpoint, cluster role ARN,
CA data and any Secret content cannot reach the snapshot even though some of them
appear in the underlying responses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from ...models import ClusterReference
from ...snapshot import ClusterSnapshot, SnapshotSource
from ..ports import ClusterReader
from .addons import collect_managed_addons
from .cluster_scoped import collect_cluster_scoped
from .insights import collect_insights
from .sanitize import normalize_kubernetes_version, optional_str
from .workloads import collect_workloads

__all__ = ["collect_snapshot", "normalize_kubernetes_version"]


def collect_snapshot(
    reader: ClusterReader,
    *,
    region: str | None = None,
    account_alias: str | None = None,
    snapshot_id: UUID | None = None,
    captured_at: datetime | None = None,
) -> ClusterSnapshot:
    """Collect a sanitized snapshot of one cluster.

    `snapshot_id` and `captured_at` are injectable so a caller can assert
    byte-stable output; in normal use they are generated.
    """
    cluster_data, error = reader.describe_cluster()
    if error or cluster_data is None:
        raise RuntimeError(
            f"cannot build a snapshot without cluster metadata: {error or 'no data'}"
        )

    raw_version = optional_str(cluster_data.get("version")) or ""
    kubernetes_version = normalize_kubernetes_version(raw_version)
    if kubernetes_version is None:
        raise RuntimeError(
            f"cluster reported an unusable Kubernetes version: {raw_version!r}"
        )

    managed = collect_managed_addons(reader)
    workloads = collect_workloads(reader)
    cluster_scoped = collect_cluster_scoped(reader)
    insights = collect_insights(reader)

    resources = [*workloads.resources, *cluster_scoped.resources]

    return ClusterSnapshot(
        snapshot_id=snapshot_id or uuid4(),
        captured_at=captured_at or datetime.now(UTC),
        source=SnapshotSource.LIVE_READ_ONLY,
        sanitized=True,
        cluster=ClusterReference(
            name=reader.cluster_name,
            account_alias=account_alias,
            region=region,
        ),
        kubernetes_version=kubernetes_version,
        platform_version=optional_str(cluster_data.get("platformVersion")),
        eks_addons=managed.addons,
        eks_addon_catalogue=managed.catalogue,
        helm_releases=workloads.helm_releases,
        workloads=workloads.workloads,
        resources=tuple(
            sorted(resources, key=lambda r: (r.kind, r.namespace or "", r.name))
        ),
        served_api_versions=cluster_scoped.served_api_versions,
        insights=insights.insights,
        collection_gaps=(
            *managed.gaps,
            *workloads.gaps,
            *cluster_scoped.gaps,
            *insights.gaps,
        ),
    )
