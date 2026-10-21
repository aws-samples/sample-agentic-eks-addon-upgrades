"""Content digest for snapshots.

The digest is taken over the contract's canonical JSON — sorted keys, no
insignificant whitespace — so the same snapshot content always produces the same
digest regardless of field ordering or serialisation incidentals. Later stages
reference this digest to prove which inputs produced a verdict.
"""

from __future__ import annotations

import hashlib

from ..snapshot import ClusterSnapshot

DIGEST_PREFIX = "sha256:"


def snapshot_digest(snapshot: ClusterSnapshot) -> str:
    """Return `sha256:<hex>` over the snapshot's canonical JSON."""
    canonical = snapshot.canonical_json().encode("utf-8")
    return f"{DIGEST_PREFIX}{hashlib.sha256(canonical).hexdigest()}"
