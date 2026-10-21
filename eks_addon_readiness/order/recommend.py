"""Recommend the newest AWS-published version for an add-on AWS itself decided.

WHY THIS MODULE EXISTS
----------------------
`moves.newest_supported_concrete` is the recommendation policy for a component the
FACT STORE knows: newest series covering every hop, at its latest recorded patch. An
EKS-managed add-on has no stored fact. Its compatibility was decided by AWS for this
exact cluster, and AWS's own list of compatible versions reaches Stage 5 as the
assessment's `HopDecision.valid_targets` -- not through the store at all.

Both the plan (here) and the report (`view/build.py`) used to ask the store and only
the store. For a managed add-on that lookup misses, so the plan emitted no advisory
step and the report fell through to "the installed version, staying put". For example, a
cluster targeting 1.36 with coredns installed `1.13.2-eksbuild.21` and
`v1.14.3-eksbuild.16` on AWS's own compatible list, rendered as "1.13.2-eksbuild.21
(stay)", and `plan_counts.recommended_upgrades` 0 on every run. Saying "stay on this
version" when a newer compatible version exists is the defect; saying it when it is
true is the whole point of the field.

So this module reads the recommendation off `valid_targets`, and BOTH the plan and the
report call the same function -- the property `_recommended_version`'s docstring
asserts, that the two agree by construction rather than by coincidence.

WHY IT IS A NEW MODULE AND NOT AN EDIT TO `interleave`
-----------------------------------------------------
`moves`, `reversibility`, `interleave` and `plan` are kept byte-for-byte from the original
rules so the ruleset digest stays comparable:
their source text is hashed into `order.ruleset_digest()`, which is
a field on the plan document. Editing one changes every plan digest ever produced.

But a change to what the plan SAYS must move that digest -- a plan carrying new steps
under the original rules digest is exactly the silent drift the digests exist to prevent. So
this module is added to Stage 5's `RULESET_MODULES` instead. All 16 frozen sources stay
byte-identical, the Stage 5 digest moves deliberately and loudly, and `view/build.py`
already names this event: "a deliberate Stage 5 digest move, which is a loud event".

WHAT "OPTIONAL" MEANS HERE, PRECISELY
------------------------------------
- **Never required.** `feasible` is untouched, no required step is reordered or gated
  on one of these, and no verdict changes. Skipping every one of them still reaches the
  target.
- **Separately approvable.** Each is its own numbered `PlanStep`, so a grant model that
  authorises named steps can approve or decline it on its own. It is never folded into
  a required step and approving the required work never implies it.
- **Still positioned by the data.** `at_kubernetes` is the earliest cluster version from
  which AWS lists this version as compatible through the end of the path, so a version
  that only becomes compatible later keeps that position instead of floating to the end.
- **Never doubled with a required move.** A component that already has any step in the
  plan -- a forced `COMPONENT_MOVE`, or a store-derived advisory one -- gets nothing
  here. `metrics-server` must move, so it gets one required step and no optional twin.

A MANAGED ADD-ON THAT IS BEHIND GETS A REQUIRED STEP, NOT AN OPTIONAL ONE, AND NOT NOTHING
------------------------------------------------------------------------------------------
This module used to handle only `COMPATIBLE`. An AWS-decided `REQUIRES_COMPONENT_UPGRADE`
was left where the frozen sequencer put it: in `plan.unsequenceable`, because that
sequencer plans required moves from stored windows and an insight is a flat list. The
report then showed the add-on as "Upgrade needed" with no version to go to. Live: vpc-cni
at `1.22.4-eksbuild.3` targeting 1.36, AWS listing exactly `v1.23.1-eksbuild.1`, and the
plan silent. `with_required_catalogue_moves` now turns that into a `COMPONENT_MOVE` to the
newest version AWS lists as compatible with the target, applied on the cluster as it
stands (the move must precede the hop the installed version cannot survive), and removes
the component from `unsequenceable`. Reversibility is `REVERSIBLE`: the lane records the
running version from `describe-addon` before the update and `update-addon`s back to it,
which is the rollback path every managed add-on already has.

THE POLICY WHEN SEVERAL VERSIONS CLEAR THE TARGET
-------------------------------------------------
Two different questions, two different answers, and they are deliberately not the same:

- A **required** move (`moves.plan_moves`) takes the LOWEST series that clears every hop
  it has to cross. Smallest jump, fewest breaking changes; reaching higher "to be safe"
  would invent an upgrade the evidence does not require. Unchanged here.
- An **optional** upgrade takes the NEWEST version AWS lists. There is nothing to clear,
  so "smallest" has no meaning -- the only reason to offer the step at all is that a
  newer version exists.

So a component that must move is never also told to go further than its requirement,
and a component that need not move is told the newest it could go. Reversibility is not
computed for an optional step and none is claimed: the frozen `PlanStep` validator
requires one only for a `COMPONENT_MOVE`, and an `UNKNOWN` on a required move stays
`UNKNOWN`.

WHAT THIS MODULE CANNOT EXPRESS, STATED RATHER THAN HIDDEN
----------------------------------------------------------
`PlanStep.recommended_version` is patterned `^\\d+\\.\\d+\\.\\d+$` and `plan` is frozen,
so a step cannot hold an EKS build suffix. The typed field therefore carries
`1.14.3` while the description and the report carry AWS's full `v1.14.3-eksbuild.16`.
The consequence is deliberate: a recommendation that differs from the installed version
ONLY in its `-eksbuild.N` ordinal is not emitted, because the step's typed fields would
read as a move to the version already installed -- the no-op step `dedupe` removes. A
build-only bump is a real upgrade on EKS and this is a real limitation; it is bounded by
the frozen contract, not chosen.
"""

from __future__ import annotations

import re

from ..facts.series import exact_version, version_series
from ..models import CompatibilityVerdict
from .dedupe import without_redundant_steps
from .interleave import _differs_from_installed
from .interleave import build_plan as _interleaved_plan
from .latest import with_unverified_latest
from .plan import PlanStep, StepKind, UpgradePlan

# `HopDecision.decided_from` for a hop AWS answered about this cluster rather than one a
# stored fact answered. `decide/engine.py` writes `stored-fact`, `eks-insight` or `none`,
# and only `eks-insight` means "AWS published the compatible-version list we are reading".
CATALOGUE_SOURCE = "eks-insight"

# The EKS build ordinal. AWS versions a managed add-on as
# `v<major>.<minor>.<patch>-eksbuild.<n>`, and `n` is its own ordinal: `eksbuild.16` is
# newer than `eksbuild.9`, which no comparison of the two strings can tell you.
_EKSBUILD = re.compile(r"-eksbuild\.(\d+)")

# A concrete major.minor.patch, the only shape the frozen `PlanStep` fields accept.
_EXACT = re.compile(r"^\d+\.\d+\.\d+$")


def catalogue_version_key(version: str | None) -> tuple[int, int, int, int] | None:
    """Order an AWS add-on version. `None` when it is not a version at all.

    THE COMPARISON HAS TO BE VERSION-AWARE, and this is the one place that says so.
    Sorting AWS's list as strings gets three separate things wrong at once:
    `v1.4.0-eksbuild.2` sorts above `v1.14.3-eksbuild.16` (4 above 1 in the second
    field), `eksbuild.9` sorts above `eksbuild.16`, and the `v` prefix shifts everything.

    The numeric core is taken from `facts.series.exact_version` rather than parsed again
    here -- the same normaliser Stages 3, 4 and 5 key their lookups on, so a version this
    function orders is a version those stages can match. The build ordinal is appended
    because nothing in the codebase orders it and something has to.

    >>> catalogue_version_key("v1.14.3-eksbuild.16")
    (1, 14, 3, 16)
    >>> catalogue_version_key("v1.4.0-eksbuild.2") < catalogue_version_key("v1.14.3-eksbuild.9")
    True
    >>> catalogue_version_key("v1.14.3-eksbuild.9") < catalogue_version_key("v1.14.3-eksbuild.16")
    True
    >>> catalogue_version_key("1.3.10-eksbuild.3") < catalogue_version_key("v1.4.0-eksbuild.1")
    True
    >>> catalogue_version_key("1.13.2")
    (1, 13, 2, 0)
    >>> catalogue_version_key("alpine") is None
    True
    >>> catalogue_version_key(None) is None
    True
    """
    core = exact_version(version)
    if core is None:
        return None
    major, minor, patch = (int(p) for p in core.split("."))
    match = _EKSBUILD.search(version or "")
    return (major, minor, patch, int(match.group(1)) if match else 0)


def _hop_targets(hop) -> dict[tuple[int, int, int, int], str]:
    """One hop's `valid_targets`, keyed by version order, value the string AWS published.

    Keyed on the parsed order rather than the raw text so two spellings of one version
    (`1.14.3-eksbuild.16` and `v1.14.3-eksbuild.16`) cannot be counted as two versions.
    A target that does not parse is dropped: it cannot be ordered, so it cannot be
    called the newest.
    """
    out: dict[tuple[int, int, int, int], str] = {}
    for raw in hop.valid_targets:
        key = catalogue_version_key(raw)
        if key is not None:
            out[key] = raw
    return out


def catalogue_recommendation(decision) -> tuple[str | None, str | None]:
    """The newest AWS-listed version worth moving to, and where in the path it applies.

    Returns `(version, at_kubernetes)`, or `(None, None)` when there is no honest
    recommendation. THE SINGLE SOURCE for both the plan's optional step and the report's
    recommended-version column.

    Five gates, each one a case that must stay honest:

    1. **Only `COMPATIBLE`.** An `UNRESOLVED` component gets no recommendation, because
       there is no honest one when the compatibility question is open. A component that
       must move is left to the required-move path: `valid_targets` from an insight is a
       flat list with no windows behind it, so the "lowest series that clears the hop"
       policy a required move uses cannot be computed from it, and answering with the
       newest instead would apply a second policy to the same verdict. Such a component
       is already named in `plan.unsequenceable` with AWS's own list in its reason, so
       nothing is lost -- see the module docstring.
    2. **Only when no stored fact decided it.** `knowledge_base_revision` is non-null
       exactly when `store.find()` answered (`decide/engine.py`), and that component
       belongs to `moves.newest_supported_concrete`. So the two paths cannot both fire
       for one component and cannot disagree about one.
    3. **Every hop AWS-decided, with a list.** An insight answers one minor, so a
       multi-hop component is `UNRESOLVED` beyond the first hop and never reaches here.
       Requiring it of every hop keeps that true if the catalogue is ever read for more.
    4. **Strictly newer, by version order.** Not "different" -- an older or equal target
       is not a recommendation.
    5. **Newer at major.minor.patch**, via the same `_differs_from_installed` the report's
       `stays` flag uses. Keeps the two in exact agreement, and excludes the build-only
       bump the frozen `PlanStep` cannot express as anything but a no-op.

    `at_kubernetes` is the earliest cluster version from which the chosen version is
    listed continuously through the end of the path. For the single-hop case that is the
    target; for a longer path it holds the step where the data puts it rather than
    floating it to the end.
    """
    if decision.verdict is not CompatibilityVerdict.COMPATIBLE:
        return None, None
    if decision.knowledge_base_revision is not None:
        return None, None

    hops = decision.hops
    if not all(hop.decided_from == CATALOGUE_SOURCE for hop in hops):
        return None, None
    per_hop = [_hop_targets(hop) for hop in hops]
    if not all(per_hop):
        return None, None

    installed = catalogue_version_key(decision.installed_version)
    final = per_hop[-1]

    best: tuple[int, int, int, int] | None = None
    best_at: str | None = None
    for key, raw in final.items():
        if installed is not None and key <= installed:
            continue
        if not _differs_from_installed(raw, decision):
            continue
        index = len(hops) - 1
        while index > 0 and key in per_hop[index - 1]:
            index -= 1
        if best is None or key > best:
            best, best_at = key, hops[index].hop.target_version

    if best is None:
        return None, None
    return final[best], best_at


def _recommendation_step(
    component: str,
    decision,
    version: str,
    at_kubernetes: str,
    order: int,
    siblings: tuple[int, ...],
) -> PlanStep:
    """One optional step, in the existing `COMPONENT_UPGRADE_RECOMMENDED` vocabulary.

    No `reversibility`: the frozen validator requires one only for a `COMPONENT_MOVE`,
    and an optional step crosses nothing the plan is asking for. Claiming one here would
    put a classification on the document that the run's own data does not support.
    """
    concrete = exact_version(version)
    return PlanStep(
        order=order,
        kind=StepKind.COMPONENT_UPGRADE_RECOMMENDED,
        description=(
            f"Recommended: upgrade {component} from "
            f"{decision.installed_version or 'the installed version'} to {version} once "
            f"the cluster reaches Kubernetes {at_kubernetes}. AWS lists {version} as "
            "compatible with this cluster there, and the component is already compatible "
            "as installed, so this step is OPTIONAL: declining it does not block the "
            "cluster upgrade."
        ),
        component=component,
        from_series=decision.installed_series,
        to_series=version_series(version) or concrete,
        recommended_version=(concrete if concrete and _EXACT.match(concrete) else None),
        at_kubernetes=at_kubernetes,
        unordered_with=siblings,
    )


def required_catalogue_move(decision) -> str | None:
    """The newest AWS-listed version for an add-on AWS says MUST move, or None.

    Same gates as `catalogue_recommendation` except the verdict: `REQUIRES_COMPONENT_UPGRADE`,
    AWS-decided, no stored fact, every hop with a list. Newest rather than lowest, for the
    reason the optional path gives: an insight is a flat list of what works at the target, and
    "smallest jump" is a policy over windows this component does not have.
    """
    if decision.verdict is not CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE:
        return None
    if decision.knowledge_base_revision is not None:
        return None
    hops = decision.hops
    if not hops or not all(hop.decided_from == CATALOGUE_SOURCE for hop in hops):
        return None
    per_hop = [_hop_targets(hop) for hop in hops]
    if not all(per_hop):
        return None
    installed = catalogue_version_key(decision.installed_version)
    final = per_hop[-1]
    newer = [key for key in final if installed is None or key > installed]
    if not newer:
        return None
    return final[max(newer)]


def _required_step(component: str, decision, version: str, order: int) -> PlanStep:
    """One required `COMPONENT_MOVE` for a managed add-on AWS says is behind."""
    from ..knowledge import Reversibility

    concrete = exact_version(version)
    current = decision.hops[0].hop.current_version
    target = decision.hops[-1].hop.target_version
    return PlanStep(
        order=order,
        kind=StepKind.COMPONENT_MOVE,
        description=(
            f"Upgrade {component} from {decision.installed_version or 'the installed version'} "
            f"to {version} before the cluster moves to Kubernetes {target}. AWS does not list the "
            f"installed version as compatible with {target}; {version} is the newest it does list. "
            "Required: without it the cluster upgrade leaves this add-on unsupported."
        ),
        component=component,
        from_series=decision.installed_series,
        to_series=version_series(version) or concrete,
        recommended_version=concrete if concrete and _EXACT.match(concrete) else None,
        at_kubernetes=current,
        reversibility=Reversibility.REVERSIBLE,
        reversibility_reason=(
            "An EKS-managed add-on is moved with `eks update-addon`; the lane records the "
            "running version from `describe-addon` before the update and updates back to it "
            "on rollback."
        ),
    )


def with_required_catalogue_moves(plan: UpgradePlan, assessment) -> UpgradePlan:
    """Give every AWS-decided add-on that must move a required step; unlist it as unsequenceable.

    PLACED BEFORE THE FIRST CLUSTER HOP, after whatever component moves the sequencer already put
    there: the installed version does not survive the hop, so the move belongs on the cluster as
    it stands, which is also what its `at_kubernetes` says. With no hop in the plan it goes after
    the last required step. Every later step is renumbered and its `unordered_with` remapped, the
    way `dedupe.renumbered_plan` does it. `feasible` is left alone: it was not false on account
    of these components.
    """
    taken = {step.component for step in plan.steps if step.component}
    additions: list[tuple[str, object, str]] = []
    for decision in assessment.decisions:
        component = decision.identity or decision.workload_ref
        if component in taken:
            continue
        version = required_catalogue_move(decision)
        if version is None:
            continue
        additions.append((component, decision, version))
    if not additions:
        return plan
    additions.sort(key=lambda entry: entry[0])
    added = {component for component, _, _ in additions}

    ordered = list(plan.steps)
    hop_index = next(
        (i for i, s in enumerate(ordered) if s.kind is StepKind.CLUSTER_UPGRADE),
        None,
    )
    if hop_index is None:
        hop_index = len(
            [s for s in ordered if s.kind is not StepKind.COMPONENT_UPGRADE_RECOMMENDED]
        )
    # Provisional orders for the new steps: unique, impossible for an existing step, remapped below.
    provisional = [
        _required_step(component, decision, version, order=10_000 + n)
        for n, (component, decision, version) in enumerate(additions)
    ]
    ordered[hop_index:hop_index] = provisional

    renumbered = {step.order: position for position, step in enumerate(ordered, start=1)}
    steps = tuple(
        step.model_copy(
            update={
                "order": renumbered[step.order],
                "unordered_with": tuple(
                    renumbered[other] for other in step.unordered_with if other in renumbered
                ),
            }
        )
        for step in ordered
    )
    unsequenceable = tuple(
        u for u in plan.unsequenceable if (u.identity or u.workload_ref) not in added
    )
    return plan.model_copy(update={"steps": steps, "unsequenceable": unsequenceable})


def with_catalogue_recommendations(plan: UpgradePlan, assessment) -> UpgradePlan:
    """Append an optional step per AWS-decided component that has a newer version.

    Appended after every step `interleave` produced, which is where that module already
    puts its own advisory upgrades: they are optional and independent, so they belong
    after the required sequence. Each is marked `unordered_with` the others, because no
    evidence orders one against another.

    A component that already appears on ANY step is skipped -- that covers both a forced
    `COMPONENT_MOVE` and a store-derived advisory step, so a component can never carry
    both a required and an optional move. Nothing existing is renumbered, reordered or
    modified, and `feasible` is not touched.
    """
    taken = {step.component for step in plan.steps if step.component}

    additions: list[tuple[str, object, str, str]] = []
    for decision in assessment.decisions:
        component = decision.identity or decision.workload_ref
        if component in taken:
            continue
        version, at_kubernetes = catalogue_recommendation(decision)
        if version is None or at_kubernetes is None:
            continue
        additions.append((component, decision, version, at_kubernetes))

    if not additions:
        return plan

    additions.sort(key=lambda entry: entry[0])
    first = max((step.order for step in plan.steps), default=0) + 1
    orders = list(range(first, first + len(additions)))

    steps = list(plan.steps)
    for position, (component, decision, version, at_kubernetes) in enumerate(additions):
        order = orders[position]
        steps.append(
            _recommendation_step(
                component,
                decision,
                version,
                at_kubernetes,
                order,
                tuple(other for other in orders if other != order),
            )
        )
    return plan.model_copy(update={"steps": tuple(steps)})


def build_plan(assessment, store, target_kubernetes_version: str) -> UpgradePlan:
    """Stage 5's entry point: the interleaved plan, plus AWS-derived optional upgrades.

    THIS is what `order.build_plan` resolves to, so there is one Stage 5 entry point and
    no caller can obtain a plan that is missing the optional steps. `interleave.build_plan`
    keeps its name and signature -- it is frozen, and it still does all the sequencing;
    this only adds the steps it has no store to derive.

    `without_redundant_steps` RUNS FIRST, AND THE ORDER OF THESE TWO IS NOT INTERCHANGEABLE.
    It renumbers the survivors, while `with_catalogue_recommendations` numbers its additions
    from `max(order) + 1`. Deduplicating afterwards would leave the optional steps numbered
    above a gap the deduplication had just closed, and deduplicating first means they are
    numbered against the sequence a reader is actually shown.
    """
    plan = with_catalogue_recommendations(
        with_required_catalogue_moves(
            without_redundant_steps(
                _interleaved_plan(assessment, store, target_kubernetes_version)
            ),
            assessment,
        ),
        assessment,
    )
    return with_unverified_latest(plan, assessment, store)
