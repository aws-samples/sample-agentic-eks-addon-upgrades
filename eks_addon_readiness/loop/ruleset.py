"""The loop's own fingerprint.

Stage 7 applies rules -- which gaps are researchable, which facts are stale, whether
the enlarged store warrants a re-run -- so it carries a ruleset digest like every
other stage that decides something, and its dispatch document cites it.

The list holds `resolve_loop` alone. `dispatch.py` is the boundary contract rather
than a rule: it defines what may be *said* across the seam, not what the loop
*decides*, and editing it changes the interface rather than the queue. Keeping it out
means a contract change is visible as a contract change instead of masquerading as a
rule change.

Same recipe as every other stage: per module in list order, hash the short name, a
NUL, the source text, a NUL. Conservative on purpose -- a comment-only edit moves the
digest, because a spurious "the dispatch rules changed" costs a re-read while a
missed one would let a changed rule pass as the original.
"""

from __future__ import annotations

from functools import cache

from ..fingerprint import ruleset_digest as _rule_source_digest

RULESET_MODULES: tuple[str, ...] = ("resolve_loop",)


@cache
def ruleset_digest() -> str:
    """Digest of the modules that decide what to research and whether to resume."""
    return _rule_source_digest(__package__, RULESET_MODULES)
