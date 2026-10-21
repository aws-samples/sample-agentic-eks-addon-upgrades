"""Read boundary for cluster inventory.

The collector depends on this protocol, not on MCP. That keeps the transport
swappable: the EKS MCP Server is the current implementation, and a recorded
implementation is used for tests so no cluster or credentials are required.

Every method returns `(data, error)` rather than raising. A denied or failed read
is normal and must become a typed gap in the snapshot, not an exception that
discards the rest of a successful scan.
"""

from __future__ import annotations

from typing import Any, NamedTuple, Protocol

ReadResult = tuple[dict[str, Any] | None, str | None]


class ListResult(NamedTuple):
    """Outcome of a list read.

    `truncated` exists because the EKS MCP Server caps list results and reports
    no truncation indicator — its status message says "Successfully listed 100
    resources" whether or not more exist. A caller that only checked `error`
    would treat a partial list as complete, which is a worse failure than an
    outright denial because it is invisible.
    """

    items: list[dict[str, Any]]
    error: str | None = None
    truncated: bool = False


class ClusterReader(Protocol):
    """Read-only inventory reads against one cluster."""

    def read_workload_detail(
        self, kind: str, api_version: str, name: str, namespace: str | None
    ) -> ReadResult:
        """One workload's full object, carrying `spec` and `status`."""
        ...

    @property
    def cluster_name(self) -> str:
        """Name of the cluster being read."""
        ...

    def describe_cluster(self) -> ReadResult:
        """Cluster metadata, including Kubernetes and platform version."""
        ...

    def list_addon_names(self) -> tuple[tuple[str, ...], str | None]:
        """Names of EKS-managed add-ons."""
        ...

    def describe_addon(self, addon_name: str) -> ReadResult:
        """Detail for one EKS-managed add-on, including its version."""
        ...

    def describe_addon_versions(self, addon_name: str) -> ListResult:
        """The full published version catalogue for one EKS-managed add-on.

        Returns the `addonVersions[]` list from `describe-addon-versions`: one item per
        published build, each carrying `addonVersion` and `compatibilities[]` (the
        Kubernetes `clusterVersion`s it supports). This is AWS's authoritative catalogue
        and spans every Kubernetes version AWS offers — the source that lets managed
        add-ons be decided deterministically across every hop, rather than the single-hop
        insight. A failure is a typed error, not an exception.
        """
        ...

    def list_workloads(
        self,
        kind: str,
        api_version: str,
        *,
        namespace: str | None = None,
        label_selector: str | None = None,
    ) -> ListResult:
        """Resources of one kind, with labels and annotations.

        `namespace` restricts the query to one namespace. Used to shard a list
        that came back at the transport's cap, since each namespace is far more
        likely to fit under it than the whole cluster.

        `label_selector` restricts the query server-side. Used to fetch exactly
        the cluster-scoped resources that carry an ownership label: only labelled
        resources match, so the result is guaranteed complete even where an
        unfiltered enumeration of the same kind would be capped.
        """
        ...

    def list_api_versions(self) -> tuple[tuple[str, ...], str | None]:
        """API versions served by the cluster."""
        ...

    def list_insights(self) -> tuple[list[dict[str, Any]], str | None]:
        """AWS's upgrade-readiness insights for this cluster, without detail.

        Returns summary records only. `category_specific_summary` — which carries the
        per-add-on compatible versions — is absent from this response and must be
        fetched per insight via `read_insight`.
        """
        ...

    def read_insight(self, insight_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """One insight in full, including `category_specific_summary`."""
        ...
