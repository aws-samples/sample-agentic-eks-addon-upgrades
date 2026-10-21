"""Drop a planned step that installs the version a step before it already installs.

WHY THIS MODULE EXISTS
----------------------
On a cluster targeting Kubernetes 1.36, a plan can carry keda twice:

    s3  keda  2.17.x -> install 2.20.2  at Kubernetes 1.35
    s4  keda  2.20.x -> install 2.20.2  at Kubernetes 1.35

`s4` asks an operator to authorise installing the exact version `s3` installs. Whatever they
answer, the cluster ends up in the same place, so the row is A DECISION WITH NO CONSEQUENCE --
which is the one thing an approval page must never contain. A reader who approves it has
approved nothing, a reader who rejects it has rejected nothing, and both spend their attention
finding that out. It is also the more expensive error than a missing step, because it teaches a
reader that rows on this page may be noise.

`cert-manager` in that same plan is the case this must NOT touch:

    s1  cert-manager  1.13.x -> install 1.19.6  at Kubernetes 1.35
    s2  cert-manager  1.19.x -> install 1.21.2  at Kubernetes 1.35

Two steps for one component, two different versions installed: a genuine multi-hop, and
exactly the shape GAP-2 and `moves.plan_moves` exist to express. So THE RULE IS ABOUT THE
VERSION A STEP INSTALLS and never about how many steps a component has. A rule counting steps
per component would delete the case the sequencer was written for.

WHERE THE REDUNDANT STEP COMES FROM, MEASURED RATHER THAN GUESSED
-----------------------------------------------------------------
`moves._series_covers` RETURNS ON THE FIRST RULE whose `component_version_series` matches the
series it is asked about, while `moves._series_supported_at` scans every rule. A fact that
describes one series with more than one `CompatibilityRule` -- the shape a compatibility matrix
scraped one row per Kubernetes minor produces -- makes those two functions disagree: the series
is judged unsupported at a hop by the first, and listed as supported at that hop by the second.
The sequencer then plans a move from a series to ITSELF. Reproduced exactly, with three rules
and two releases, in `tests/test_a_step_never_installs_what_a_step_before_it_installs.py`:

    2.17.x -> 2.20.x install 2.20.2 at 1.35
    2.20.x -> 2.20.x install 2.20.2 at 1.35

-- the live pair in every field the page shows, from a fact authored in the test file. That is
one cause, established. The rule below is deliberately NOT written against it: it drops a step
by what the step SAYS, the version it installs, so any other path to the same output is covered
too and no reader has to trust that the cause list is complete.

WHY IT IS A NEW MODULE AND NOT AN EDIT TO `moves`
------------------------------------------------
The obvious repair is one word in `moves._series_covers`: scan the rules instead of returning
on the first. `moves`, `reversibility`, `interleave` and `plan` are kept byte-for-byte from
the original rules so the ruleset digest stays comparable -- their source text is hashed into
`order.ruleset_digest()`, a field on
every plan document -- so editing one changes every plan digest ever produced, and the oracle
that compares a plan against its baseline fails on step one.

So this is Stage 5's sixth rule module, exactly as `recommend` was its fifth and for the same
reason. All four carried sources stay byte-identical, the Stage 5 rules digest moves once and
loudly, and a plan whose steps changed does not carry the rules digest of the plan before it --
which is the silent drift these digests exist to catch.

THE COST OF EXPRESSING IT HERE, STATED RATHER THAN HIDDEN
--------------------------------------------------------
This module removes the step; it does not remove the disagreement that produced it. A fact
whose series is described by several rules will still be judged by only the first inside
`moves`, and the visible consequence -- a duplicated step -- is now filtered. Two shapes are
therefore possible that this cannot reach: a move that is planned to the wrong series (rather
than to the same one), and a hop wrongly called infeasible because the first rule's window did
not cover it. Neither is what was observed, and neither is fixable without editing a frozen
module. It is bounded by the frozen contract, not chosen.
"""

from __future__ import annotations

from .plan import PlanStep, UpgradePlan


def renumbered_plan(plan: UpgradePlan, kept: list[PlanStep]) -> UpgradePlan:
    """`plan` reduced to `kept`, renumbered 1..n with `unordered_with` remapped.

    SHARED WITH `order/floor.py`, which drops a step for a different reason and must
    renumber it the same way. Two copies of this arithmetic is two places for a plan to end
    up numbered 1, 2, 4 -- which reads as a step that went missing, the same reader-alarming
    shape the rules that call this exist to remove. Callers: `without_redundant_steps` below
    and `floor.without_backward_steps`.

    References to a dropped step are dropped rather than remapped: there is nothing left to
    be unordered against.

    Returns the plan itself, unchanged and un-copied, when nothing was dropped -- which
    keeps its digest identical to what the stage before it produced.
    """
    if len(kept) == len(plan.steps):
        return plan

    renumbered = {step.order: position for position, step in enumerate(kept, start=1)}
    steps = tuple(
        step.model_copy(
            update={
                "order": renumbered[step.order],
                "unordered_with": tuple(
                    renumbered[other]
                    for other in step.unordered_with
                    if other in renumbered
                ),
            }
        )
        for step in kept
    )
    return plan.model_copy(update={"steps": steps})


def without_redundant_steps(plan: UpgradePlan) -> UpgradePlan:
    """The plan with any step that installs an already-installed version removed.

    A step is dropped when it names a component, names a concrete version to install, and an
    EARLIER SURVIVING step for THAT SAME COMPONENT already installs that same version. Three
    conditions, and each one is load-bearing:

    - **Names a component.** A cluster hop installs nothing, so it can never be redundant.
    - **Names a concrete version.** `recommended_version` is None when the fact records no
      release to name in the target series -- the series is still the answer, the patch just
      cannot be named. Two such steps would compare `None == None` and the second would be
      dropped as a duplicate of the first, deleting a real multi-hop over a value that was
      never a version. So a step with nothing to install is always kept.
    - **Same component.** Two components installing the same version number is a coincidence
      of numbering, not a repetition.

    Comparison is a plain string equality, which is exact here rather than approximate:
    `PlanStep.recommended_version` is patterned `^\\d+\\.\\d+\\.\\d+$`, so the field is already
    the normalised form every stage keys on and there is no `v` prefix or build suffix to
    reduce first.

    THE SURVIVORS ARE RENUMBERED, and `unordered_with` is remapped with them -- through
    `renumbered_plan` above, which `order/floor.py` shares. A plan numbered 1, 2, 3, 5 reads
    as a step that went missing, which is the same reader-alarming shape this is meant to
    remove.

    Returns the plan itself, unchanged and un-copied, when nothing is redundant -- which is
    the ordinary case, and it keeps the plan's digest identical to what `interleave` produced
    for every run that never had the defect.
    """
    installed: dict[str, set[str]] = {}
    kept: list[PlanStep] = []
    for step in plan.steps:
        if step.component and step.recommended_version:
            already = installed.setdefault(step.component, set())
            if step.recommended_version in already:
                continue
            already.add(step.recommended_version)
        kept.append(step)

    return renumbered_plan(plan, kept)
