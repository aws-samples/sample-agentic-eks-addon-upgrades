"""Stage 5 — the ordering solver.

Produces the interleaved cluster-and-component upgrade sequence: cluster hops
interleaved with component moves, each step marked REVERSIBLE or ONE_WAY.

The order is derived from Stage 4's compatibility windows and declared dependencies,
never invented. A component Stage 4 could not decide is not sequenced — it is named in
`plan.unsequenceable` with its reason.

`moves`, `reversibility`, `interleave` and `plan` are digest-frozen: their source
text is hashed into `order.ruleset_digest()`, so they are kept byte-for-byte from the
original rules so the ruleset digest stays comparable. `recommend` is the fifth rule module,
`dedupe` the sixth, `floor` the seventh and
`collapse` the eighth, all added rather than editing one of them.

`build_plan` exported here is `floor.build_plan`, NOT `interleave.build_plan`. It runs the
frozen sequencer, drops any step that installs a version an earlier step for the same
component already installs, adds the optional upgrades AWS's own compatible-version list
implies for a managed add-on (which `interleave` has no store to derive), collapses a run of
moves that installs a series the plan abandons before the cluster has hopped at all, and
finally drops any step that would move a component to where it already is or behind it.
Rebinding the name at the package boundary rather than at each caller is deliberate: Stage 5
keeps one entry point, so no caller can end up holding a plan with those steps missing, with
the redundant one still in it, with an abandoned-series stop still in it, or with a backward
one still in it.
"""

from .collapse import without_unforced_intermediates
from .dedupe import renumbered_plan, without_redundant_steps
from .floor import build_plan, installed_versions, without_backward_steps
from .interleave import AssessmentMismatch
from .moves import ComponentMove, MovePlan, plan_moves
from .plan import (
    PlanStep,
    StepKind,
    UnsequenceableComponent,
    UpgradePlan,
)
from .recommend import (
    catalogue_recommendation,
    catalogue_version_key,
    with_catalogue_recommendations,
)
from .reversibility import resolve_reversibility
from .ruleset import RULESET_MODULES, RULESET_UNAVAILABLE, ruleset_digest

__all__ = [
    "RULESET_MODULES",
    "RULESET_UNAVAILABLE",
    "AssessmentMismatch",
    "ComponentMove",
    "MovePlan",
    "PlanStep",
    "StepKind",
    "UnsequenceableComponent",
    "UpgradePlan",
    "build_plan",
    "catalogue_recommendation",
    "catalogue_version_key",
    "installed_versions",
    "plan_moves",
    "renumbered_plan",
    "resolve_reversibility",
    "ruleset_digest",
    "with_catalogue_recommendations",
    "without_backward_steps",
    "without_redundant_steps",
    "without_unforced_intermediates",
]
