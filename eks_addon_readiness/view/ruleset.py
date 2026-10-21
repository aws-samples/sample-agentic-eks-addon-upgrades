"""The presentation stage's two fingerprints, and why there are two.

TWO DIGESTS, NOT ONE. `ruleset_digest` covers the modules that compute what the
report *says*; `renderer_digest` covers the modules that turn those values into
bytes. Both appear on the presentation document as separate fields.

The reason is the same one that keeps four per-stage ruleset digests apart instead
of merging them (`facts/ruleset.py`): a reader who finds two differing reports must
be able to tell *which* input moved. Sharing one digest would mean a stylesheet
tweak reported that the numbers changed, and a changed count reported that the
stylesheet changed -- so neither answer would mean anything.

Both use the same recipe as every other stage: for each module in list order, hash
its short name, a NUL, its source text, a NUL. Conservative on purpose -- a
comment-only edit moves the digest. A spurious "presentation changed" costs a
re-read; a missed one would let a changed count pass as the original, which is the
failure these digests exist to prevent.

`verify.py` is deliberately in neither list. Verification is a check on the report,
not part of what the report says.
"""

from __future__ import annotations

from functools import cache

from ..fingerprint import ruleset_digest as _rule_source_digest

# The modules that decide what the report says. Listed explicitly and in a fixed
# order so the digest never depends on filesystem enumeration, and so adding a
# rule module is a deliberate act rather than a silent one.
RULESET_MODULES: tuple[str, ...] = (
    "model",
    "build",
)

# The modules that decide how it looks. The JSON view is absent on purpose: it is
# not a renderer. The JSON view IS the document's canonical JSON, so there is no
# separate code path that could omit a field -- which is exactly what a hand-built JSON
# view does with the one number its own defect is about.
RENDERER_MODULES: tuple[str, ...] = (
    # The signed approve/reject command. Listed here and NOT in RULESET_MODULES: it
    # produces bytes a human reads and decides nothing, so editing it must move the
    # renderer fingerprint. It started life in `model.py`, which had the two digests
    # saying the exact opposite of what they exist to say.
    "command",
    "html",
    "markdown",
    # The approval page: the pending-only table a person decides from, rendered by the
    # approval stage rather than by `present`. Fingerprinted for exactly the same reason as
    # the others -- it is bytes a human reads and ACTS on, so an edit to it must move
    # `renderer_digest` rather than changing a decision surface silently.
    "approval",
)


@cache
def ruleset_digest() -> str:
    """Digest of the modules that compute the report's values."""
    return _rule_source_digest(__package__, RULESET_MODULES)


@cache
def renderer_digest() -> str:
    """Digest of the modules that render those values."""
    return _rule_source_digest(f"{__package__}.render", RENDERER_MODULES)
