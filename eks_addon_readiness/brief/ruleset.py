"""The brief's own fingerprint.

The brief decides things -- which releases lie on the walk, whether an issue lookup answered
or failed, whether a section is an all-clear or an absence -- so it carries a ruleset digest
like every other part of this system that decides something, and its document cites it.
Without one, a reader comparing two briefs of the same plan could not tell an answer that
changed because upstream changed from one that changed because a rule here was edited. That
distinction matters more here than anywhere else, because this document is expected to differ
between runs: with no ruleset digest, EVERY difference would be attributable to upstream.

Four modules, and each inclusion is a decision:

- `walk.py` decides which releases the brief must read and which minors it can claim to have
  covered. That is the rule most likely to change the answer.
- `issues.py` decides FOUND versus NONE_OPEN versus NOT_LOOKED. Editing that mapping changes
  whether a reader sees an all-clear, which is the most consequential edit possible here.
- `document.py` holds the document's shape AND the builder that fills it, so the digest covers
  both -- matching `research/freshness.py`, `facts/gaps.py` and `loop/resolve_loop.py`, where
  a document and the rules that produce it live together.
- `reuse.py` is in. It decides whether a stored brief is served instead of researched, which
  changes what a reader sees more directly than any other rule here -- and the ruleset digest is
  part of the stored brief's own ADDRESS, so an edit here correctly makes every previously stored
  brief invisible to the lookup rather than served under changed rules.
- `boundary.py` is in: it is what the agent may say. A field removed or a validator loosened
  changes which answers are admissible, which is a rule change by any reading.
- `prompt.py` is in, and this is the one that could be argued either way. It is an INSTRUCTION,
  not a schema, and the research package excludes `marks.py` from its digest on the grounds that
  a record's shape is not a decision. But a prompt is not a record either: editing "set
  basis=STATED only when the source itself says it", or the list of things not to report,
  changes what the brief says about the same releases. A reader comparing two briefs must be
  told the instruction moved, or they will attribute the difference to upstream. In on the
  conservative side, deliberately -- and it is the module most likely to be tuned, which is
  exactly why its edits must be visible.

Same recipe as every other stage: per module in list order, hash the short name, a NUL, the
source text, a NUL. Conservative on purpose -- a comment-only edit moves the digest, because a
spurious "the rules changed" costs a re-read while a missed one would let a changed rule pass
as the original.

NOT one of the sixteen frozen digests. No baseline value corresponds to this, so there is no value
to compare against and none is asserted.
"""

from __future__ import annotations

from functools import cache

from ..fingerprint import ruleset_digest as _rule_source_digest

RULESET_MODULES: tuple[str, ...] = ("boundary", "document", "issues", "prompt", "reuse", "walk")


@cache
def ruleset_digest() -> str:
    """Digest of the modules that decide what a brief says."""
    return _rule_source_digest(__package__, RULESET_MODULES)
