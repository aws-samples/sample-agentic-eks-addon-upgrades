"""Stage 2 — interpret a snapshot into a component inventory.

A pure layer: identify, resolve, rate, classify. No cluster access, no network,
no model, no clock. The same snapshot always yields the same inventory.

Every module re-exported here is digest-frozen: its source text is hashed into
`report.ruleset_digest()`, so it is kept byte-for-byte from the original rules so the ruleset digest stays comparable.
"""

from .classify import (
    AssessmentPriority,
    Classification,
    ClassificationResult,
    Overrides,
    classify,
    declared_names,
    tokenize,
)
from .confidence import SOURCE_CEILING, Signal, VersionSource, combine, worst
from .identity import IdentitySource, ResolvedIdentity, resolve_identity
from .resolve import ComponentRecord, resolve_inventory
from .versions import (
    is_placeholder,
    normalize_version,
    strip_chart_version,
    versions_agree,
)

__all__ = [
    "SOURCE_CEILING",
    "AssessmentPriority",
    "Classification",
    "ClassificationResult",
    "ComponentRecord",
    "IdentitySource",
    "Overrides",
    "ResolvedIdentity",
    "Signal",
    "VersionSource",
    "classify",
    "combine",
    "declared_names",
    "is_placeholder",
    "normalize_version",
    "resolve_identity",
    "resolve_inventory",
    "strip_chart_version",
    "tokenize",
    "versions_agree",
    "worst",
]
