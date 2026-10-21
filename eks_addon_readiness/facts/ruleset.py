"""Digest of the Stage 3 rules.

Deliberately separate from `report.ruleset_digest()`, which hashes the Stage 2
`inventory.*` modules. Sharing one digest across stages would mean an identity-rule
edit in Stage 2 moved the Stage 3 digest, telling a reader the gap rules changed when
they did not — and the digest exists precisely so that claim can be trusted.

The **list** below is what must stay per-stage. The ~20-line hashing mechanism, rather than
being duplicated across four modules, lives once in `fingerprint.py`. Extracting it
changes no digest: the four modules named below are what gets hashed, and `ruleset`
itself is deliberately not among them.
"""

from __future__ import annotations

from functools import cache

from ..fingerprint import ruleset_digest as _rule_source_digest

# Fixed order, listed explicitly: the digest must not depend on filesystem
# enumeration, and adding a rule module should be a deliberate act. `ruleset` itself
# is absent because it is plumbing that computes the digest, not a rule that decides
# an outcome.
RULESET_MODULES: tuple[str, ...] = (
    "series",
    "store",
    "gaps",
    "ledger",
)

# A legacy placeholder for "source unreadable" that this pipeline never produces; kept only
# so a stored legacy document carrying it can still be recognised rather than compared
# against as though it were a real digest.
RULESET_UNAVAILABLE = "UNAVAILABLE"


@cache
def ruleset_digest() -> str:
    """Digest of the Stage 3 rule modules' source.

    Conservative in the same direction as Stage 2's: raw source is hashed, so a
    comment-only edit moves the digest. A spurious "the rules changed" costs a
    re-read; a missed one would let a changed rule pass as the original.

    Raises `fingerprint.RulesetUnavailable` if a module's source cannot be read.
    """
    return _rule_source_digest(__package__, RULESET_MODULES)
