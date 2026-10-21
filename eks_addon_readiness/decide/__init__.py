"""Stage 4 — the deterministic decision engine.

The first stage that accepts a target Kubernetes version, and therefore the first
permitted to turn a fact into a verdict. Still no model: the verdict is computed by
rules over a digest-cited ledger, so it replays.

Two properties are load-bearing. A missing fact yields `UNRESOLVED`, never
`INCOMPATIBLE`. And an EKS Insight decides only the first hop, because AWS assessed
only the next minor.

`kubernetes`, `rules` and `assessment` are digest-frozen: their source text is
hashed into `decide.ruleset_digest()`, so they are kept byte-for-byte from the original
rules so the ruleset digest stays comparable.
"""

from .assessment import ComponentDecision, HopDecision, ReadinessAssessment
from .engine import LedgerMismatch, build_assessment
from .kubernetes import MAX_HOPS, VersionError, expand_hops, parse_minor, permits
from .rules import applicable_removals, series_permitted_at, verdict_for_hop
from .ruleset import RULESET_MODULES, RULESET_UNAVAILABLE, ruleset_digest

__all__ = [
    "MAX_HOPS",
    "RULESET_MODULES",
    "RULESET_UNAVAILABLE",
    "ComponentDecision",
    "HopDecision",
    "LedgerMismatch",
    "ReadinessAssessment",
    "VersionError",
    "applicable_removals",
    "build_assessment",
    "expand_hops",
    "parse_minor",
    "permits",
    "ruleset_digest",
    "series_permitted_at",
    "verdict_for_hop",
]
