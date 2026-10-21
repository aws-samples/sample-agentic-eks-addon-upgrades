"""Drop a planned step that would move a component to where it already is, or behind it.

WHY THIS MODULE EXISTS
----------------------
A plan step's `recommended_version` is the newest release THE STORED FACT RECORDS in the
series the step moves to (`moves._concrete_version_for`). It is never compared against the
version actually running. So when a fact's catalogue is thinner than the cluster -- and it
routinely is, because a catalogue harvested from documentation records the patch each docs
page happened to pin -- the step can name a version at or below the installed one.

Consider a keda fact whose catalogue
records exactly one release in 2.20.x, `2.20.0`. For a cluster running `2.20.2`, targeting
Kubernetes 1.36:

    keda  2.20.x -> 2.20.x  install 2.20.0  at Kubernetes 1.35

A required `COMPONENT_MOVE` installing 2.20.0 over 2.20.2. Nothing refused it. It reaches
the approval page as an ordinary row beside a live Approve button, and an executor that
honours it moves the cluster BACKWARDS -- the one outcome a readiness plan must never
propose. `order/dedupe.py` keeps it, correctly by its own rule: no EARLIER step installs
2.20.0, so it is not a repeat of anything.

WHY THE EXISTING FLOOR DOES NOT COVER IT
----------------------------------------
The rule "must be strictly ahead of what is installed" is already written twice, and
neither copy is on this path:

  * `order/recommend.py:210` gates the optional upgrade derived from AWS's catalogue for a
    managed add-on. It is exact and it works -- for components with no stored fact, which
    is the one class this module's case is not.
  * `view/build.py::_further_than_installed` has exactly ONE call site,
    `view/build.py::_unsupported_at_target`, which mints rows from `plan.unsequenceable` --
    components that CANNOT reach the target. Its docstring names this very hazard ("a row
    reading `2.20.x -> 2.20.2` for a cluster that is on 2.20.2 would invent a move, ask a
    person to approve it"). The guard is right; it is simply on the other path. An ordinary
    `COMPONENT_MOVE` from `plan.steps` never passes through it.

So the floor existed in the report and in the managed-add-on path, and not in the plan.
This puts it in the plan, where the step an executor would run is decided.

WHAT IT DROPS, AND WHY THAT IS PROVABLY WITHOUT CONSEQUENCE
-----------------------------------------------------------
A step is dropped only when it is at-or-behind AND IT DOES NOT CHANGE THE SERIES. Both
halves are load-bearing:

  * **At or behind.** Strictly ahead is an upgrade and is never touched. Equal is the no-op
    the report already refuses to render.
  * **Same series.** `from_series == to_series` means the step changes nothing about the
    compatibility answer -- the component is already in the series the step moves it to.
    Combined with a version that is not ahead, there is nothing the step can accomplish:
    approving it moves the cluster sideways or backwards, declining it changes nothing. That
    is a decision with no upside, and removing it removes no information.

WHAT IT DELIBERATELY KEEPS, STATED RATHER THAN HIDDEN
-----------------------------------------------------
A step that installs a version BELOW the installed one while genuinely changing series
(`from_series != to_series`) is KEPT. `moves.plan_moves` chooses the LOWEST series that
clears every hop it has to cross, and on some windows that series is numerically below the
installed one -- in which case the downgrade may be the only way across the boundary.
Dropping it would leave the plan claiming `feasible` with nothing to make it so, which is a
worse error than showing a backward step: the first is silent, the second is at least
readable. This module does not reshape `feasible`, and it is not the right place to decide
that a compatibility-forced downgrade is acceptable. No such step has been observed; if one
appears it will appear on the page, which is the intended outcome.

THE CAUSE UPSTREAM IS NOT FIXED HERE, AND IS NOT CLAIMED TO BE
--------------------------------------------------------------
The keda case exists because that approved fact describes series `2.20.x` TWICE with
windows that cannot both be true -- `[1.33, 1.36)` from the compatibility table and
`[1.30, unbounded)` from the install prose -- which makes `moves._series_covers` (first
matching rule) and `moves._series_supported_at` (every rule) disagree about 2.20.x at 1.36
and forces a move to the series the component is already in. No detector flagged it:
`research/signals.py::find_contradictions` will not compare across statement kinds,
`_self_inconsistencies` only looks at pairs citing the same page, and
`rule_containment_contradictions` requires the narrower spec to be an exact release. That
is a separate limitation of the research stage, and NOT addressed here: widening a detector changes
the confidence and approval path of every fact for every component, which is its own change
and its own review.

WHAT DROPPING THE STEP THEREFORE LEAVES UNSAID, because it is worth saying. With the step
gone, the plan asserts that keda on 2.20.2 reaches 1.36 untouched -- which the install prose
supports and the compatibility table denies. That assertion is not introduced here: the step
this rule removes made the SAME claim about the series and added a wrong action on top of it,
so removing it strictly reduces what the plan gets wrong. The fact still needs fixing.

WHY IT IS A NEW MODULE AND NOT AN EDIT TO `moves`
-------------------------------------------------
`moves`, `reversibility`, `interleave` and `plan` are kept byte-for-byte from the original
rules so the ruleset digest stays comparable:
their source text is hashed into `order.ruleset_digest()`, a field on
every plan document, so editing one changes every plan digest ever produced. This is
Stage 5's seventh rule module. All four carried sources stay byte-identical, and the Stage 5
digest moves once, deliberately and loudly -- which is what a plan whose steps changed is
supposed to do.

IT RUNS LAST, AND THAT IS THE POINT
-----------------------------------
`floor.build_plan` wraps `recommend.build_plan`, so every step every producer emits --
`interleave`'s forced moves, `interleave`'s store-derived advisory upgrades, and
`recommend`'s catalogue upgrades -- passes this rule. A floor applied to only the producer
that reported the defect would leave the same hazard in the two that did not: in
particular `interleave._placed_recommendation` gates its advisory step on
`_differs_from_installed`, which asks whether the version DIFFERS and not whether it is
newer, so it has the same gap and is covered here rather than separately.
"""

from __future__ import annotations

from ..facts.series import exact_version
from .collapse import build_plan as _collapsed_plan
from .dedupe import renumbered_plan
from .moves import _spec_sort_key
from .plan import PlanStep, UpgradePlan


def installed_versions(assessment) -> dict[str, str | None]:
    """Each assessed component's installed version, keyed the way a step names it.

    `decision.identity or decision.workload_ref` is exactly how `interleave` and
    `recommend` derive `PlanStep.component`, so the key is the step's own component string
    rather than a second convention that could fail to join. A component with no decision
    is absent rather than None-valued, and both read the same at the call site: no
    comparison is possible, so the step is kept.

    TWO DECISIONS CAN SHARE ONE KEY, and then this reports UNKNOWN rather than picking one.
    Two Helm releases of the same component in different namespaces resolve to the same
    `identity`, so a plain dict comprehension would keep whichever decision came last and
    compare the step against the wrong cluster. That is a wrong drop, which is the one
    failure this rule must not have -- a step deleted on a comparison against a version no
    part of the plan is about. So a key claimed twice with two different installed versions
    resolves to None, and the step survives. `interleave` already collapses those two
    releases onto one `PlanStep.component`, so the ambiguity is upstream of here; this
    declines to guess at it.
    """
    out: dict[str, str | None] = {}
    for decision in assessment.decisions:
        key = decision.identity or decision.workload_ref
        version = decision.installed_version
        if key in out and exact_version(out[key]) != exact_version(version):
            out[key] = None
            continue
        out[key] = version
    return out


def _moves_backward_or_nowhere(step: PlanStep, installed: dict[str, str | None]) -> bool:
    """Whether this step asks for a version that is not ahead of what is running.

    False -- keep the step -- whenever the question cannot be answered, and each of those
    cases is a real one rather than defensive padding:

    - **No component, or no version to install.** A cluster hop installs nothing, and
      `recommended_version` is None when the fact records no release to name in the target
      series. Neither can be compared.
    - **The installed version is unknown or unparseable.** `exact_version` returns None for
      `UNRESOLVED`, for a digest-pinned image reference, and for anything without a patch
      level. A step is never dropped on a comparison that did not happen.
    - **The series changes.** See the module docstring: a genuine series change is kept even
      when it goes backwards, because it may be the only way across a hop boundary.

    `PlanStep.recommended_version` is patterned `^\\d+\\.\\d+\\.\\d+$`, so `_spec_sort_key`
    cannot raise on it; the installed side is normalised through `exact_version` first for
    the same reason `view/build.py::_further_than_installed` does it -- a fact or a label
    may legitimately spell a version `v2.20.2`.
    """
    if not step.component or not step.recommended_version:
        return False
    if step.from_series is None or step.from_series != step.to_series:
        return False
    here = exact_version(installed.get(step.component))
    if here is None:
        return False
    return _spec_sort_key(step.recommended_version) <= _spec_sort_key(here)


def without_backward_steps(plan: UpgradePlan, assessment) -> UpgradePlan:
    """The plan with every step that moves a component to where it already is removed.

    Returns the plan itself, unchanged and un-copied, when nothing is backward -- which is
    the ordinary case, and it keeps the digest the stages before it produced for every run
    that never had the defect.

    Survivors are renumbered through `dedupe.renumbered_plan`, the same function
    `without_redundant_steps` uses, so the two rules cannot disagree about what a plan
    numbered 1, 2, 4 should become or about how `unordered_with` follows the renumbering.
    """
    installed = installed_versions(assessment)
    kept = [step for step in plan.steps if not _moves_backward_or_nowhere(step, installed)]
    return renumbered_plan(plan, kept)


def build_plan(assessment, store, target_kubernetes_version: str) -> UpgradePlan:
    """Stage 5's entry point: the recommended plan, with no backward step left in it.

    THIS is what `order.build_plan` resolves to, so there is one Stage 5 entry point and no
    caller can obtain a plan this rule has not been applied to. It runs LAST so it sees
    every step every producer emits; see the module docstring.

    `collapse.build_plan` was spliced in ahead of it -- it wraps `recommend.build_plan`, which
    this used to call directly -- so a step whose `from_series` was WIDENED by the collapse is
    judged by this floor rather than escaping it. The chain is `interleave` -> `dedupe` ->
    `recommend` -> `collapse` -> `floor`, and this is still the outermost rule.
    """
    return without_backward_steps(
        _collapsed_plan(assessment, store, target_kubernetes_version),
        assessment,
    )
