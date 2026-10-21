"""Digest of the Stage 4 rules.

Its own digest, for the reason `facts/ruleset.py` has one: sharing across stages would
mean a Stage 3 gap-rule edit moved the Stage 4 digest, telling a reader the verdict rules
changed when they did not.
"""

from __future__ import annotations

from functools import cache

from ..fingerprint import ruleset_digest as _rule_source_digest

RULESET_MODULES: tuple[str, ...] = (
    "kubernetes",
    "rules",
    "assessment",
)

# A legacy placeholder for "source unreadable" this pipeline never produces. See fingerprint.py.
RULESET_UNAVAILABLE = "UNAVAILABLE"


@cache
def ruleset_digest() -> str:
    """Digest of the Stage 4 rule modules' source.

    Raises `fingerprint.RulesetUnavailable` if a module's source cannot be read.
    """
    return _rule_source_digest(__package__, RULESET_MODULES)
