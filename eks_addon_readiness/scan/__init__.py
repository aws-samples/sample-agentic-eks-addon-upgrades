"""Stage 1 — read-only cluster inventory.

Public surface:

    collect_snapshot     build a sanitized ClusterSnapshot from a ClusterReader
    snapshot_digest      content digest over the snapshot's canonical JSON
    ClusterReader        the read boundary (protocol)

**This module imports no transport.** `facts/gaps.py` is digest-frozen and does
`from ..scan import snapshot_digest`, so importing this package is unavoidable in
Stages 3, 4 and 5 — and importing a transport here would drag the MCP client and
its dependency tree into every stage's deployment package. The MCP-backed reader
lives in `scan.adapters.eks_mcp` and is imported only by the Stage 1 entrypoint.

`snapshot_digest` and `helm_labels.derive_helm_releases` are pinned names: the
digest-frozen `facts/gaps.py` and `inventory/resolve.py` import them by path.
"""

from .collect import collect_snapshot, normalize_kubernetes_version
from .digest import snapshot_digest
from .helm_labels import derive_helm_releases, split_chart_label
from .ports import ClusterReader, ListResult, ReadResult

__all__ = [
    "ClusterReader",
    "ListResult",
    "ReadResult",
    "collect_snapshot",
    "derive_helm_releases",
    "normalize_kubernetes_version",
    "snapshot_digest",
    "split_chart_label",
]
