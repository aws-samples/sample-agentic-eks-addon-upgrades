"""Typed collection gaps.

A failed read is normal. It becomes an `UnresolvedFinding` on the snapshot, never
an exception that discards the rest of a successful scan — an empty collection
where access was denied is indistinguishable from a cluster that genuinely has
nothing, which is the outcome the scan layer exists to prevent.

Gap codes and their prose are **output**: they reach the snapshot, the ledger and
the plan. They are kept byte-for-byte from the original codes so a document produced now
reads identically to one produced before.
"""

from __future__ import annotations

from ...models import UnresolvedFinding
from .kinds import OWNERSHIP_LABEL


def gap(
    code: str,
    summary: str,
    reason: str,
    missing: tuple[str, ...],
    steps: tuple[str, ...],
) -> UnresolvedFinding:
    """One typed gap: what is missing, why, and what would close it."""
    return UnresolvedFinding(
        code=code,
        summary=summary,
        reason=reason,
        missing_evidence=missing,
        resolution_steps=steps,
    )


def namespace_shard_gap(
    kind: str, api_version: str, namespaces: tuple[str, ...]
) -> UnresolvedFinding:
    """A namespace shard was itself at the cap, so even sharding did not exhaust it."""
    return gap(
        "WORKLOAD_LIST_INCOMPLETE",
        f"{kind} list is incomplete even after sharding by namespace.",
        (
            f"The cluster-wide {kind} list hit the transport's item cap, so it was "
            "re-read one namespace at a time. These namespaces returned a full page "
            "and may still be truncated: " + ", ".join(namespaces)
        ),
        (f"a complete enumeration of {kind} ({api_version})",),
        (
            f"Cross-check: kubectl get {kind.lower()} -A --no-headers | wc -l",
            "Workloads missing from this snapshot are absent from the inventory "
            "entirely, not merely unversioned.",
            f"Do not treat absence of a {kind} from this snapshot as evidence it does "
            "not exist.",
        ),
    )


def api_group_gap(kind: str, api_version: str, returned: int) -> UnresolvedFinding:
    """Record that API-group enumeration is incomplete, while ownership is not.

    Deliberately narrower than a blanket "this kind is unreadable". Ownership
    evidence for this kind *is* complete, because it is fetched with an existence
    selector on `app.kubernetes.io/name` and only labelled resources match. What
    remains incomplete is the enumeration of every resource of this kind, which is
    used only as one of several sources of API-group names.
    """
    return gap(
        "API_GROUP_ENUMERATION_INCOMPLETE",
        f"{kind} enumeration returned exactly {returned} items and may be incomplete.",
        (
            f"The EKS MCP Server caps list responses at {returned} items, offers no "
            "pagination on its Kubernetes list tools, and reports no truncation "
            "indicator, so a full-length response cannot be distinguished from a "
            f"complete one. {kind} is cluster-scoped, so it cannot be sharded by "
            "namespace the way workloads can. Ownership evidence for this kind is "
            f"unaffected: it is read separately with a {OWNERSHIP_LABEL} existence "
            "selector, which only labelled resources match and is therefore complete."
        ),
        (f"a complete enumeration of {kind} ({api_version})",),
        (
            f"Cross-check the true count: kubectl get {kind.lower()} --no-headers | wc -l",
            "API groups are also derived from served API versions and from "
            "APIService names, so a group missing here may still be present.",
            f"Do not treat absence of a {kind} from this snapshot as evidence it does "
            "not exist.",
        ),
    )
