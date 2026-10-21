"""EKS add-on upgrade-readiness domain package.

Five stages, each a pure function of documents. Stage 1 reads a cluster; Stages
2-5 read only what an earlier stage wrote. Nothing here reaches AWS except the
Stage 1 cluster reader, and nothing here holds a clock except the moment a
snapshot is captured.

The stage entrypoints live in `stages/`, one module per stage. `entrypoints/stages.py` at the
repository root sequences them.
"""

from .knowledge import ComponentKnowledge
from .models import ClusterReference, CompatibilityVerdict, Confidence, Severity
from .snapshot import ClusterSnapshot

__all__ = [
    "ClusterReference",
    "ClusterSnapshot",
    "CompatibilityVerdict",
    "ComponentKnowledge",
    "Confidence",
    "Severity",
]
