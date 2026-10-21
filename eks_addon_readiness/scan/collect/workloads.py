"""Namespaced workloads, and the Helm releases derived from their labels.

Two behaviours here are load-bearing and were empirically purchased. Neither is
simplified:

**Adaptive namespace sharding.** The transport caps list responses and reports no
truncation indicator, so a full-length response cannot be distinguished from a
complete one. The cluster-wide read is tried first because it is one call and is
usually complete; when it comes back at the cap the kind is re-read one namespace
at a time. Sharding is the fallback, not the default — paying one call per
namespace on every scan of every cluster, to guard against a cap most clusters
never reach, would be the wrong trade.

**A failed detail read degrades a workload rather than losing it.** The list
response carries no container images or replica counts, so each workload needs one
detail read. When that read fails the workload is still recorded, with a typed gap
saying its version evidence is weaker.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from ...models import UnresolvedFinding
from ...snapshot import (
    HelmReleaseSnapshot,
    KubernetesResourceReference,
    WorkloadSnapshot,
)
from ..helm_labels import derive_helm_releases
from ..ports import ClusterReader
from ..workloads import build_workload
from .findings import gap, namespace_shard_gap
from .kinds import NAMESPACE_KIND, WORKLOAD_KINDS
from .sanitize import optional_str


class Workloads(NamedTuple):
    """The workload slice of a snapshot, with the gaps collecting it left."""

    workloads: tuple[WorkloadSnapshot, ...]
    helm_releases: tuple[HelmReleaseSnapshot, ...]
    resources: tuple[KubernetesResourceReference, ...]
    gaps: tuple[UnresolvedFinding, ...]


def _list_namespaces(reader: ClusterReader) -> tuple[tuple[str, ...], str | None]:
    """Namespace names, used only to shard a capped workload list."""
    kind, api_version = NAMESPACE_KIND
    items, error, _ = reader.list_workloads(kind, api_version)
    if error:
        return (), error
    names = sorted(
        {
            name
            for item in items
            if isinstance(item, dict) and (name := optional_str(item.get("name")))
        }
    )
    return tuple(names), None


def _list_exhaustively(
    reader: ClusterReader,
    kind: str,
    api_version: str,
    namespaces: tuple[str, ...],
) -> tuple[list[dict[str, Any]], str | None, tuple[str, ...]]:
    """List one workload kind, sharding by namespace only if the cap was hit.

    Returns `(items, error, still_capped_namespaces)`.
    """
    items, error, truncated = reader.list_workloads(kind, api_version)
    if error is not None:
        return [], error, ()
    if not truncated:
        return items, None, ()

    # The cluster-wide list was at the cap, so it is probably incomplete and any
    # workload beyond it would be absent from the inventory entirely rather than
    # merely unversioned. Re-read per namespace.
    merged: dict[tuple[str | None, str], dict[str, Any]] = {}
    still_capped: list[str] = []

    def absorb(records: list[dict[str, Any]]) -> None:
        for record in records:
            if not isinstance(record, dict):
                continue
            name = optional_str(record.get("name"))
            if not name:
                continue
            merged[(optional_str(record.get("namespace")), name)] = record

    # Keep the cluster-wide results: they are incomplete, not wrong.
    absorb(items)

    for namespace in namespaces:
        shard, shard_error, shard_truncated = reader.list_workloads(
            kind, api_version, namespace=namespace
        )
        if shard_error is not None:
            # One unreadable namespace must not discard the others.
            still_capped.append(f"{namespace} (unreadable: {shard_error[:60]})")
            continue
        absorb(shard)
        if shard_truncated:
            still_capped.append(namespace)

    return list(merged.values()), None, tuple(still_capped)


def collect_workloads(reader: ClusterReader) -> Workloads:
    """Read every workload kind, then derive Helm releases from their labels."""
    raw: list[dict[str, Any]] = []
    records: list[WorkloadSnapshot] = []
    resources: list[KubernetesResourceReference] = []
    gaps: list[UnresolvedFinding] = []
    # Namespaces are enumerated once, lazily: only needed if a workload list comes
    # back at the transport's cap and has to be sharded.
    namespaces: tuple[str, ...] | None = None

    for kind, api_version in WORKLOAD_KINDS:
        if namespaces is None:
            namespaces, namespace_error = _list_namespaces(reader)
            if namespace_error:
                gaps.append(
                    gap(
                        "NAMESPACE_LIST_UNAVAILABLE",
                        "Namespaces could not be listed, so a capped workload list "
                        "cannot be sharded.",
                        namespace_error,
                        ("list Namespace (v1)",),
                        (
                            "Verify the EKS access entry grants list on namespaces.",
                            "Workload lists will be reported as-is; on a cluster with "
                            "more workloads of one kind than the transport cap, some "
                            "will be missing from the snapshot entirely.",
                        ),
                    )
                )

        items, error, still_capped = _list_exhaustively(
            reader, kind, api_version, namespaces or ()
        )
        if error:
            gaps.append(
                gap(
                    "WORKLOAD_LIST_UNAVAILABLE",
                    f"{kind} resources could not be listed.",
                    error,
                    (f"list {kind} ({api_version})",),
                    (
                        "Verify the EKS access entry grants a Kubernetes role permitting "
                        f"list on {kind}.",
                        "Helm-installed add-ons backed by this kind will be absent from the "
                        "snapshot until resolved.",
                    ),
                )
            )
            continue
        if still_capped:
            gaps.append(namespace_shard_gap(kind, api_version, still_capped))
        raw.extend(items)

        for item in items:
            name = optional_str(item.get("name"))
            if not name:
                continue
            detail, detail_error = reader.read_workload_detail(
                kind, api_version, name, optional_str(item.get("namespace"))
            )
            if detail_error:
                gaps.append(
                    gap(
                        "WORKLOAD_DETAIL_UNAVAILABLE",
                        f"Container images for {kind}/{name} could not be read.",
                        detail_error,
                        (f"read {kind}/{name}",),
                        (
                            "Version evidence from image tags is unavailable for this "
                            "workload; identity and version may resolve at lower "
                            "confidence or not at all.",
                        ),
                    )
                )
            records.append(build_workload(kind, item, detail))
            resources.append(
                KubernetesResourceReference(
                    api_version=api_version,
                    kind=kind,
                    namespace=optional_str(item.get("namespace")),
                    name=name,
                )
            )

    return Workloads(
        workloads=tuple(
            sorted(records, key=lambda w: (w.kind, w.namespace or "", w.name))
        ),
        helm_releases=derive_helm_releases(raw),
        resources=tuple(resources),
        gaps=tuple(gaps),
    )
