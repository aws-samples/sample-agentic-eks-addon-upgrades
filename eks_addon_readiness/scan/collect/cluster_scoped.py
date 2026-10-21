"""Cluster-scoped kinds: CRDs, APIServices and admission webhooks.

TWO READS PER KIND, WITH DELIBERATELY DIFFERENT GUARANTEES
----------------------------------------------------------
    unfiltered  every resource of this kind, capped by the transport. Used only
                as one source of API-group names. Incomplete, not wrong.
    labelled    resources carrying an ownership label, fetched with an existence
                selector. COMPLETE, because only labelled resources match and
                there are far fewer of them than the cap.

This is why enumerating all 271 CRDs on the reference cluster was never the
requirement: the two things wanted from them — ownership and API groups — are both
obtainable under the cap. Both reads are kept. Dropping the unfiltered one loses
API-group names; dropping the labelled one makes completeness depend on a
heuristic, because the labelled read is issued even when the unfiltered read
returned under the cap.

This is the path that carries cert-manager, Kyverno and any other CRD-owning
component, so it is preserved exactly.
"""

from __future__ import annotations

from typing import NamedTuple

from ...models import UnresolvedFinding
from ...snapshot import KubernetesResourceReference
from ..ports import ClusterReader
from .findings import api_group_gap, gap
from .kinds import CLUSTER_SCOPED_KINDS, OWNERSHIP_LABEL
from .sanitize import optional_str


class ClusterScoped(NamedTuple):
    """The cluster-scoped slice of a snapshot, with the gaps collecting it left."""

    resources: tuple[KubernetesResourceReference, ...]
    served_api_versions: tuple[str, ...]
    gaps: tuple[UnresolvedFinding, ...]


def collect_cluster_scoped(reader: ClusterReader) -> ClusterScoped:
    """Read each cluster-scoped kind twice, then the cluster's served API versions."""
    resources: list[KubernetesResourceReference] = []
    gaps: list[UnresolvedFinding] = []

    for kind, api_version in CLUSTER_SCOPED_KINDS:
        items, error, truncated = reader.list_workloads(kind, api_version)
        if truncated:
            gaps.append(api_group_gap(kind, api_version, len(items)))
        if error:
            gaps.append(
                gap(
                    "CLUSTER_SCOPED_READ_FORBIDDEN",
                    f"{kind} resources could not be listed.",
                    error,
                    (f"list {kind} ({api_version}) at cluster scope",),
                    (
                        "AmazonEKSViewPolicy does not cover cluster-scoped "
                        f"{kind} reads.",
                        "Bind a supplementary ClusterRole granting get/list/watch on "
                        f"{kind} to this principal, scoped to that type only. Do not "
                        "escalate to a broader managed access policy.",
                        f"Until resolved, {kind}-derived findings are unavailable and "
                        "must not be treated as absent.",
                    ),
                )
            )
            continue

        # The complete ownership set. Fetched even when the unfiltered read
        # succeeded without truncation, because the two are then identical and
        # merging costs nothing — whereas skipping it when the cap was *not*
        # detected would make completeness depend on a heuristic.
        owned, owned_error, _ = reader.list_workloads(
            kind, api_version, label_selector=OWNERSHIP_LABEL
        )
        if owned_error:
            gaps.append(
                gap(
                    "OWNERSHIP_LABEL_READ_UNAVAILABLE",
                    f"{kind} resources carrying {OWNERSHIP_LABEL} could not be listed.",
                    owned_error,
                    (f"list {kind} ({api_version}) with a {OWNERSHIP_LABEL} selector",),
                    (
                        "Component-to-API-ownership correlation falls back to API group "
                        "name matching, which is weaker.",
                        f"Cross-check: kubectl get {kind.lower()} "
                        f"-l {OWNERSHIP_LABEL} --show-labels",
                    ),
                )
            )
            owned = []

        # Merged by name so a resource present in both reads appears once, and the
        # labelled read fills in anything the capped read cut off.
        by_name: dict[str, KubernetesResourceReference] = {}
        for item in [*items, *owned]:
            name = optional_str(item.get("name"))
            if not name:
                continue
            raw_labels = item.get("labels")
            labels = (
                {
                    k: v
                    for k, v in raw_labels.items()
                    if isinstance(k, str) and isinstance(v, str)
                }
                if isinstance(raw_labels, dict)
                else {}
            )
            by_name[name] = KubernetesResourceReference(
                api_version=api_version,
                kind=kind,
                namespace=None,
                name=name,
                labels=labels,
            )
        resources.extend(by_name[name] for name in sorted(by_name))

    api_versions, error = reader.list_api_versions()
    if error:
        gaps.append(
            gap(
                "API_VERSIONS_UNAVAILABLE",
                "The cluster's served API versions could not be listed.",
                error,
                ("list_api_versions",),
                (
                    "Re-run the scan.",
                    "Deprecated and removed API analysis cannot run without this data.",
                ),
            )
        )

    return ClusterScoped(
        resources=tuple(resources),
        served_api_versions=api_versions,
        gaps=tuple(gaps),
    )
