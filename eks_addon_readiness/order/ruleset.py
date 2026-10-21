"""Digest of the Stage 5 rules. Its own, for the reason every stage has one."""

from __future__ import annotations

from functools import cache

from ..fingerprint import ruleset_digest as _rule_source_digest

# `recommend` and `dedupe` are appended, not inserted: the digest hashes the list in order,
# so a new module goes on the end and the four carried ones keep their positions.
#
# ADDING ONE MOVES THE STAGE 5 DIGEST, DELIBERATELY. `recommend` produces the plan steps
# `interleave` had no store to derive -- the optional upgrade for an add-on AWS decided
# from its own catalogue -- and a plan whose steps changed must not carry the rules digest
# of the plan before them. That silent drift is what these digests exist to catch. The
# alternative was editing `interleave` itself, which the carried-module byte freeze forbids
# for a stronger reason: it
# would change every plan digest ever produced rather than only those produced from here
# on. All four carried modules stay byte-identical; the digest moves once, on purpose, and
# `view/build.py` already calls this event by name.
#
# `dedupe` is the sixth, on identical terms and for a defect rather than a gap: it removes a
# step that installs the version an earlier step for the same component already installs,
# which is a decision with no consequence and the one thing an approval page must not carry.
# Its own docstring records the one-word repair inside `moves._series_covers` that the
# carried-module byte freeze forbids
# and that this module stands in for, and what that substitution does not reach.
#
# `floor` is the seventh, on identical terms and for the more dangerous half of the same
# defect: a step naming a version AT OR BELOW the one installed, which `dedupe` correctly
# keeps because no earlier step installs it. The rule "strictly ahead of what is installed"
# was already written in `recommend` and in `view/build.py`, and on neither path that
# produces a forced move -- so a plan could ask an operator to move a cluster backwards.
# `dedupe` is edited in the same change to share its renumbering, so this digest moves once
# for the whole change rather than twice, which is what a plan whose steps changed is
# supposed to do.
#
# `collapse` is the eighth, on identical terms and for the remaining half of the same family: a
# step that installs a series the plan ABANDONS before the cluster hops at all.
# `moves.plan_moves` takes the lowest series covering the hop it is unblocking with no
# lookahead, so when a later hop forces that series out the component is moved twice at one
# cluster version to land where one move would have put it. `dedupe` keeps the pair because the
# two steps install different versions, and `floor` keeps it because both are ahead of what is
# running; neither measures what the cluster ends up on. `floor` is edited in the same change to
# call this instead of `recommend`, so `floor` still runs last and now sees a widened
# `from_series`.
#
# `latest` is the ninth. It holds the ONE recommendation policy for a fact-decided component --
# `newest_supported_concrete` first, and the newest recorded stable release when the fact is
# silent about every series above the installed one -- and `recommend` calls it for the optional
# step that policy implies. The same change teaches `recommend` to give an AWS-decided add-on
# that is BEHIND a required step instead of leaving it unsequenceable. Both change what the plan
# says, so the digest moves, once.
RULESET_MODULES: tuple[str, ...] = (
    "moves",
    "reversibility",
    "interleave",
    "plan",
    "recommend",
    "dedupe",
    "floor",
    "collapse",
    "latest",
)

# A legacy placeholder for "source unreadable" this pipeline never produces. See fingerprint.py.
RULESET_UNAVAILABLE = "UNAVAILABLE"


@cache
def ruleset_digest() -> str:
    """Digest of the Stage 5 rule modules' source.

    Raises `fingerprint.RulesetUnavailable` if a module's source cannot be read.
    """
    return _rule_source_digest(__package__, RULESET_MODULES)
