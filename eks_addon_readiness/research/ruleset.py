"""The sweep's own fingerprint.

The freshness sweep decides something -- whether a stored fact's source has moved -- and it
now publishes that decision as a document. So it carries a ruleset digest like every other
part of this system that decides something, and its document cites it. Without one, a reader
comparing two sweeps could not tell a changed answer that came from upstream moving from one
that came from the comparison rule being edited.

The list holds `freshness` alone, and both halves of that choice are deliberate:

- `freshness.py` is in, and it carries the document model as well as the rules. That matches
  `facts/gaps.py` (which holds `GapLedger`) and `loop/resolve_loop.py` (which holds
  `DispatchDocument`): where a document's shape and the rules that fill it live together, the
  digest covers both, and an edit to either is reported as a change.
- `marks.py` is out. `FactMark` and `MarkLedger` are the audit record's shape -- what may be
  *said* about a withdrawn fact -- not what the sweep *decides*. Editing them changes the
  record's schema rather than the staleness rule, and folding them in would report a schema
  change as a rule change. The same distinction `loop/ruleset.py` draws when it excludes
  `dispatch.py`.

Same recipe as every other stage: per module in list order, hash the short name, a NUL, the
source text, a NUL. Conservative on purpose -- a comment-only edit moves the digest, because
a spurious "the sweep's rules changed" costs a re-read while a missed one would let a changed
rule pass as the original.

NOT one of the sixteen frozen digests. No baseline value corresponds to this, so there is
no value to compare against and none is asserted; `tests/test_check_suites.py` pins the four
that must equal their frozen baseline and this is deliberately not among them.
"""

from __future__ import annotations

from functools import cache

from ..fingerprint import ruleset_digest as _rule_source_digest

RULESET_MODULES: tuple[str, ...] = ("freshness",)


@cache
def ruleset_digest() -> str:
    """Digest of the module that decides whether a fact's source has moved."""
    return _rule_source_digest(__package__, RULESET_MODULES)
