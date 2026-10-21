"""Drop a planned step that installs a version series the plan abandons before any hop.

WHY THIS MODULE EXISTS
----------------------
On a cluster at Kubernetes 1.35 targeting 1.36, a plan for one
run can carry five component moves for three components:

    s1  cert-manager   1.13.x -> 1.19.x  install 1.19.6  at Kubernetes 1.35
    s2  cert-manager   1.19.x -> 1.21.x  install 1.21.2  at Kubernetes 1.35
    s3  keda           2.17.x -> 2.20.x  install 2.20.2  at Kubernetes 1.35
    s4  keda           2.20.x -> 2.21.x  install 2.21.0  at Kubernetes 1.35
    s5  metrics-server  0.6.x ->  0.9.x  install 0.9.0   at Kubernetes 1.35

`s1` and `s3` are unforced. Read off THE SAME stored facts the plan was built from,
cert-manager 1.21.x spans `[1.33, 1.37)` and keda 2.21.x spans `[1.34, 1.37)`, so each covers
the cluster as it stands AND the target, and one move reaches the target from where the
component is installed. The intermediate series is installed and replaced WHILE THE CLUSTER HAS
NOT MOVED: both steps of each pair carry the same `at_kubernetes`, which is the observable form
of that. A series the component never runs on at any cluster version is not a stop on the way;
it is an extra Helm upgrade and an extra approval row.

WHERE IT COMES FROM, MEASURED RATHER THAN GUESSED
-------------------------------------------------
`moves.plan_moves` walks the hop versions one at a time (`moves.py:203`) and, when the held
series stops covering a hop, takes `candidates[0]` (`moves.py:248`) -- the LOWEST series
covering both the hop being unblocked and the version being moved at, ordered ascending by
`moves.py:108`. There is no lookahead: a candidate that also clears every LATER hop is not
preferred over one that does not. So at the first hop the sequencer picks the lowest legal
series, and at the next hop that series is out of window and a second move is forced.

That greedy-with-lowest-tie-break shape is what the evidence shows, and it is not the same as
either of the two rules it superficially resembles. On the live keda fact the same function
emits two moves for `1.35 -> 1.36`, three for `1.34 -> 1.36` and four for `1.33 -> 1.36`: a
sequencer that made a component legal on the current version and then on the target would emit
two whatever the path length, and a rule forbidding a skipped series would need four for
`1.35 -> 1.36` (2.17 -> 2.18 -> 2.19 -> 2.20 -> 2.21) rather than the two observed.

WHY THE TIE-BREAK'S OWN RATIONALE DOES NOT COVER THIS SHAPE
-----------------------------------------------------------
`moves.plan_moves` prefers the lowest candidate because "a smaller jump crosses fewer breaking
changes". For a series the plan leaves at once that is not true, and `reversibility` is the
proof: `resolve_reversibility` reads the `(from, to)` span, so `2.17.x -> 2.20.x -> 2.21.x`
crosses the identical set of breaking changes `2.17.x -> 2.21.x` crosses, and adds one more
install on top. Both paths end on 2.21.x. The tie-break is right whenever the series it picks
survives to the target; it costs a step and buys nothing whenever a later hop forces it out.

THE PROJECT ALREADY HAS THIS RULE, MEASURED DIFFERENTLY
-------------------------------------------------------
`dedupe` removes "a decision with no consequence ... the one thing an approval page must never
contain", and measures consequence as the version string a step installs. By that measure the
pair above has consequence, because the two steps install different versions. By the measure
the sentence is about -- what the cluster ends up running -- it does not: approving `s3` and
`s4` and approving `s4` alone leave keda on 2.21.0 either way, and rejecting `s3` while
approving `s4` also leaves it on 2.21.0. `dedupe`'s docstring names the cert-manager pair as
"a genuine multi-hop ... exactly the shape GAP-2 and `moves.plan_moves` exist to express",
which was asserted from the two steps installing different versions and never checked against
whether 1.21.x already covered 1.35. It did.

WHAT IT COLLAPSES, AND WHY THAT IS PROVABLY WITHOUT CONSEQUENCE
---------------------------------------------------------------
A run of `COMPONENT_MOVE` steps for ONE component collapses when all of these hold, and each
is load-bearing:

  * **Same `at_kubernetes`.** The whole argument. `plan_moves` places a move at the last
    cluster version the outgoing series still covered, and the hop versions strictly increase,
    so two moves for one component can only share an `at_kubernetes` when the first is the
    `index == 0` case (`moves.py:226`) whose `move_at` is the hop itself. The second's
    `to_series` was then chosen from candidates filtered to cover that same `move_at`
    (`moves.py:233`) -- so it covers everything the first move was emitted to unblock, plus the
    hop the first could not reach. Coverage is a superset, never a trade.
  * **The chain joins.** `next.from_series == previous.to_series`. Two steps that do not join
    are not one path, and collapsing them would invent a move from an origin no step named.
  * **The store answers for the component.** Reversibility must be RE-RESOLVED over the
    widened span; see below. Without the fact there is nothing to re-resolve against, so the
    run is kept whole rather than narrowed on a guess.
  * **The surviving series covers the version the move happens at.** `plan_moves` guarantees
    this, and the rule checks it anyway rather than trusting the producer. `_series_covers`
    reads the FIRST matching rule, so a fact describing one series twice with windows that
    disagree can make this refuse a collapse that was in fact safe -- which keeps two
    redundant steps, never drops a required one.

REVERSIBILITY IS RE-RESOLVED, NOT INHERITED, AND THAT IS THE DANGEROUS HALF
--------------------------------------------------------------------------
The surviving step's `reversibility` was computed by `interleave` for ITS OWN narrow span. A
one-way change introduced inside the span the dropped step covered is outside the survivor's,
so carrying the survivor's value forward would tell an approver that a CRD stored-version
rewrite can be rolled back -- the exact inversion `Reversibility` exists to prevent. So the
collapsed step is re-resolved through `reversibility.resolve_reversibility` over
`(first.from_series, last.to_series)`, which is what `interleave` would have computed had the
sequencer emitted the single move itself. Pinned by
`tests/test_a_plan_never_stops_on_a_series_it_leaves_at_once.py`.

The description is rebuilt for the same reason in a smaller key: the survivor's reads "Move
keda from 2.20.x", naming a series the cluster is not on and now never reaches. One row that is
false is worse than the two it replaced, which were merely redundant. The wording is
`interleave._placed_move`'s, unchanged, so the collapsed step is indistinguishable on the page
from one the sequencer emitted directly -- deliberately, because the approval page and the
report are being redesigned separately and this must not land a second voice in them.

WHAT IT DELIBERATELY KEEPS
--------------------------
A TRUE interleaved multi-hop: moves at DIFFERENT `at_kubernetes`, separated by a cluster hop,
where no single series spans the whole path. That is the forcing case `order/moves.py`'s
docstring is written around and the reason `UpgradePlan.steps` lets a component appear more
than once. No test held one before this change -- the fixtures called `_GENUINE_MULTI_HOP` in
`tests/test_a_step_never_installs_what_a_step_before_it_installs.py` and
`tests/test_a_plan_never_moves_a_cluster_backwards.py` are both the unforced shape, since their
`1.21.x [1.35, 1.38)` covers both hops. Those files assert on `without_redundant_steps` and
`without_backward_steps` directly, both of which correctly keep both steps at their own layer,
so they are untouched by this; but `_TRUE_MULTI_HOP` in this rule's test file is the first real
one in the suite.

WHAT IS NOT FIXED HERE, AND IS NOT CLAIMED TO BE
------------------------------------------------
`moves.plan_moves` still picks the lowest candidate with no lookahead. The one-line repair is
inside it -- prefer a candidate that also clears the remaining hops -- and `moves` is kept
byte-for-byte from the original rules so the ruleset digest stays comparable: its source text
is hashed into
`order.ruleset_digest()`, so editing it changes every plan digest ever produced. So this is
Stage 5's eighth rule module. All four carried sources stay byte-identical and
the Stage 5 digest moves once, loudly, which is what a plan whose steps changed is supposed to
do.

Two shapes therefore remain out of reach, stated rather than hidden. A move planned to a series
that is neither the installed one nor the one this rule keeps -- `plan_moves` choosing wrongly
rather than choosing twice -- is not visible as a collapsible run. And the facts themselves
declare `default_upgrade_strategy: SEQUENTIAL_MINOR` for both live components, which nothing in
this repository reads: `research/judge.py:267` and `facts/catalogue.py:186` both hardcode it
when they mint a fact, and no ordering code consumes it. The behaviour before this change did
not honour it either -- `2.17.x -> 2.20.x` skips 2.18.x and 2.19.x -- so collapsing removes no
guarantee that was being kept. Whether that field should become policy is a decision about what
the plan promises, not a defect in how it is computed, and it is left to a
person rather than answered here.

IT RUNS AFTER EVERY PRODUCER AND BEFORE `floor`
-----------------------------------------------
`floor.build_plan` calls this instead of `recommend.build_plan`, so the pipeline is
`interleave` -> `dedupe` -> `recommend` -> `collapse` -> `floor`. Every step every producer
emits reaches this rule, and `floor` still runs last, which its own docstring requires: a
collapsed step whose widened `from_series` equals its `to_series` -- a chain that returns to
where it started -- is then judged by the backward-step floor exactly as a directly emitted one
would be.
"""

from __future__ import annotations

from .dedupe import renumbered_plan
from .moves import _series_covers
from .plan import PlanStep, StepKind, UpgradePlan
from .recommend import build_plan as _recommended_plan
from .reversibility import resolve_reversibility


def _joins(previous: PlanStep, here: PlanStep) -> bool:
    """Whether `here` continues `previous` at the same cluster version.

    Same `at_kubernetes` is the substantive condition -- see the module docstring. The series
    equality is what makes the two one path rather than two independent steps: without it the
    collapsed move would name an origin no step named.

    A `CLUSTER_UPGRADE` between them cannot pass this, because `interleave._weave` emits every
    move keyed at a version before the hop that leaves it, so two steps either side of a hop
    have different `at_kubernetes` by construction.
    """
    return (
        here.at_kubernetes == previous.at_kubernetes
        and here.from_series is not None
        and here.from_series == previous.to_series
    )


def _collapsible_runs(steps: tuple[PlanStep, ...]) -> list[list[int]]:
    """Index runs of two or more joined moves for one component at one cluster version.

    Grouped by component FIRST and only then walked in order, so an unrelated component's step
    sitting between two of this component's moves does not break the run. `_weave` sorts by
    name and Python's sort is stable, so in practice they are already adjacent; relying on that
    would make the rule depend on a tie-break inside a frozen module.

    Only `COMPONENT_MOVE`. A `COMPONENT_UPGRADE_RECOMMENDED` is optional and carries no
    reversibility, so folding one into a required move would make an optional change mandatory
    and folding a required move into one would make a mandatory change optional.
    """
    by_component: dict[str, list[int]] = {}
    for index, step in enumerate(steps):
        if (
            step.kind is StepKind.COMPONENT_MOVE
            and step.component
            and step.at_kubernetes
        ):
            by_component.setdefault(step.component, []).append(index)

    runs: list[list[int]] = []
    for indices in by_component.values():
        run = [indices[0]]
        for index in indices[1:]:
            if _joins(steps[run[-1]], steps[index]):
                run.append(index)
                continue
            if len(run) > 1:
                runs.append(run)
            run = [index]
        if len(run) > 1:
            runs.append(run)
    return runs


def without_unforced_intermediates(plan: UpgradePlan, store) -> UpgradePlan:
    """The plan with every series it would install and abandon before a hop removed.

    Each collapsible run becomes its LAST step, widened to start from the FIRST step's
    `from_series`, with the description rebuilt and the reversibility re-resolved over the whole
    span. Survivors are renumbered through `dedupe.renumbered_plan`, the same function
    `without_redundant_steps` and `without_backward_steps` use, so the three rules cannot
    disagree about what a plan numbered 1, 2, 4 should become.

    Returns the plan itself, unchanged and un-copied, when there is nothing to collapse -- which
    keeps the digest the stages before it produced for every run that never had the defect.
    """
    runs = _collapsible_runs(plan.steps)
    if not runs:
        return plan

    widened: dict[int, PlanStep] = {}
    dropped: set[int] = set()
    for run in runs:
        first, last = plan.steps[run[0]], plan.steps[run[-1]]
        knowledge = store.find(last.component) if last.component else None
        if knowledge is None:
            continue
        if not _series_covers(knowledge, last.to_series, last.at_kubernetes):
            continue
        reversibility, reason = resolve_reversibility(
            knowledge, first.from_series, last.to_series
        )
        install_clause = (
            f" Install {last.recommended_version}."
            if last.recommended_version is not None
            else ""
        )
        widened[run[-1]] = last.model_copy(
            update={
                "from_series": first.from_series,
                # `interleave._placed_move`'s wording, unchanged. See the module docstring.
                "description": (
                    f"Move {last.component} from {first.from_series or 'installed'} to "
                    f"{last.to_series} while the cluster is at Kubernetes "
                    f"{last.at_kubernetes}.{install_clause}"
                ),
                "reversibility": reversibility,
                "reversibility_reason": reason,
            }
        )
        dropped.update(run[:-1])

    if not dropped:
        return plan

    kept = [
        widened.get(index, step)
        for index, step in enumerate(plan.steps)
        if index not in dropped
    ]
    return renumbered_plan(plan, kept)


def build_plan(assessment, store, target_kubernetes_version: str) -> UpgradePlan:
    """The recommended plan with no series in it that the plan leaves before a hop.

    Wraps `recommend.build_plan`; `floor.build_plan` wraps this, so `order.build_plan` still
    resolves to `floor` and Stage 5 still has one entry point.
    """
    return without_unforced_intermediates(
        _recommended_plan(assessment, store, target_kubernetes_version),
        store,
    )
