"""Stage 3 — the compatibility fact store and gap detection.

Answers one question per installed component: *do we hold a fact about this?* The
answer is a statement about knowledge, never about risk. Verdicts need a target
Kubernetes version and belong to Stage 4.

`series`, `store`, `gaps` and `ledger` are digest-frozen: their source text is
hashed into `facts.ruleset_digest()`, so they are kept byte-for-byte from the original
rules so the ruleset digest stays comparable.

`decide/rules.py` imports `FactStatus` and `GapEntry` from this package, and
`order/interleave.py` imports `FactStore` from it. Both are digest-frozen, so
those three names must stay re-exported here exactly.
"""

from .gaps import EvidenceMismatch, build_ledger, detect_gaps
from .ledger import FactStatus, GapDetail, GapEntry, GapLedger
from .ruleset import RULESET_MODULES, RULESET_UNAVAILABLE, ruleset_digest
from .series import version_series
from .store import (
    DEFAULT_STORE_ROOT,
    STORE_UNAVAILABLE,
    EmptyFactStore,
    FactStore,
    FileFactStore,
)

__all__ = [
    "DEFAULT_STORE_ROOT",
    "RULESET_MODULES",
    "RULESET_UNAVAILABLE",
    "STORE_UNAVAILABLE",
    "EmptyFactStore",
    "EvidenceMismatch",
    "FactStatus",
    "FactStore",
    "FileFactStore",
    "GapDetail",
    "GapEntry",
    "GapLedger",
    "build_ledger",
    "detect_gaps",
    "ruleset_digest",
    "version_series",
]
