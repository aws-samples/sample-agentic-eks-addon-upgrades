"""Merge cluster hops and per-component moves into one ordered upgrade sequence.

Consumes a Stage 4 `ReadinessAssessment` (for the hops and which components are decided)
and the fact store (for windows, dependencies and breaking changes). Produces the Stage 5
`UpgradePlan`.

The ordering knowledge is entirely derived, never invented:

- **Cluster hops** are ordered by the version algebra — one minor at a time, ascending.
- **Component moves** are placed by their `at_kubernetes`: a move must complete while the
  cluster sits at that version, so it is ordered immediately before the cluster advances
  past it.
- **Between components at the same point**, the only ordering signal is a declared
  dependency (`ComponentKnowledge.dependencies`): a component is ordered after anything it
  depends on that also moves here. With no declared dependency the order is arbitrary, and
  the plan says so via `unordered_with` rather than implying a constraint.

A component Stage 4 could not decide is not sequenced. It is reported by name in
`unsequenceable`, because ordering what cannot be decided would be exactly the invention
this stage refuses.
"""

from __future__ import annotations

import re

from ..decide.assessment import ReadinessAssessment
from ..decide.kubernetes import parse_minor
from ..facts import FactStore
from ..facts.series import version_series
from ..models import CompatibilityVerdict
from .moves import ComponentMove, newest_supported_concrete, plan_moves

# A concrete major.minor.patch version, for deciding whether a recommendation names a
# patch (stored on the step) or only a series.
_EXACT = re.compile(r"^\d+\.\d+\.\d+$")
from .plan import (
    PlanStep,
    StepKind,
    UnsequenceableComponent,
    UpgradePlan,
)
from .reversibility import resolve_reversibility
from .ruleset import ruleset_digest


class AssessmentMismatch(ValueError):
    """Raised when the assessment was not built against the store now in use."""


def _hop_versions(assessment: ReadinessAssessment) -> tuple[str, ...]:
    """Every Kubernetes version the cluster occupies, in order.

    The hop boundaries flattened: for hops (1.35->1.36, 1.36->1.37) this is
    (1.35, 1.36, 1.37).
    """
    versions = [assessment.hops[0].current_version]
    versions.extend(h.target_version for h in assessment.hops)
    return tuple(versions)


def _placed_move(
    component: str,
    move: ComponentMove,
    reversibility,
    reason: str,
    order: int,
) -> PlanStep:
    install_clause = (
        f" Install {move.recommended_version}."
        if move.recommended_version is not None
        else ""
    )
    return PlanStep(
        order=order,
        kind=StepKind.COMPONENT_MOVE,
        description=(
            f"Move {component} from {move.from_series or 'installed'} to "
            f"{move.to_series} while the cluster is at Kubernetes "
            f"{move.at_kubernetes}.{install_clause}"
        ),
        component=component,
        from_series=move.from_series,
        to_series=move.to_series,
        recommended_version=move.recommended_version,
        at_kubernetes=move.at_kubernetes,
        reversibility=reversibility,
        reversibility_reason=reason,
    )


def _differs_from_installed(recommended: str, decision) -> bool:
    """Whether a recommended concrete version is a real change from what is installed.

    Both sides are reduced with the same normaliser the fact store keys on, so an
    `v1.14.3-eksbuild.16` install and a `1.14.3` recommendation are recognised as the same
    version (no spurious recommendation), while `0.6.4` installed vs `0.9.3` recommended
    differ. A series-only recommendation (no patch could be named) is compared by series,
    so it fires only when the installed series itself is older.
    """
    from ..facts.series import exact_version, version_series

    installed = decision.installed_version
    if not installed:
        return True  # nothing to compare against; surfacing the newest is the safe call
    inst_exact = exact_version(installed)
    rec_exact = exact_version(recommended)
    if inst_exact and rec_exact:
        return inst_exact != rec_exact
    # Fall back to series comparison when a patch cannot be named on either side.
    return version_series(installed) != version_series(recommended)


def _placed_recommendation(
    component: str, from_series: str | None, to_concrete: str, at_kubernetes: str, order: int
) -> PlanStep:
    to_series = version_series(to_concrete) or to_concrete
    return PlanStep(
        order=order,
        kind=StepKind.COMPONENT_UPGRADE_RECOMMENDED,
        description=(
            f"Recommended: upgrade {component} from {from_series or 'installed'} to "
            f"{to_concrete} once the cluster reaches Kubernetes {at_kubernetes}. The "
            "component is already compatible; this moves it to the newest supported "
            "version and is optional."
        ),
        component=component,
        from_series=from_series,
        to_series=to_series,
        recommended_version=to_concrete if _EXACT.match(to_concrete) else None,
        at_kubernetes=at_kubernetes,
    )


def build_plan(
    assessment: ReadinessAssessment,
    store: FactStore,
    target_kubernetes_version: str,
) -> UpgradePlan:
    """Assemble the ordered, interleaved upgrade plan.

    The store must be the one the assessment was built against, checked via the store
    digest the assessment carries.
    """
    if store.digest() != assessment.store_digest:
        raise AssessmentMismatch(
            "the assessment was built against a different fact store. Assessment cites "
            f"{assessment.store_digest}, store is {store.digest()}. Re-run the decision "
            "engine before planning."
        )

    hop_versions = _hop_versions(assessment)
    target_version = hop_versions[-1]

    # Gather each decided component's moves, keyed by the cluster version they happen at,
    # so cluster hops and moves can be woven together in version order.
    moves_at: dict[str, list[tuple[str, ComponentMove, object, str]]] = {}
    # Advisory upgrades: (component, from_series, to_concrete) recommended at the target,
    # for components that need no forced move but have a newer supported version.
    recommended: list[tuple[str, str | None, str]] = []
    unsequenceable: list[UnsequenceableComponent] = []
    infeasible = False

    for decision in assessment.decisions:
        # UNRESOLVED means Stage 4 could not decide, so Stage 5 cannot sequence. Named,
        # not dropped.
        if decision.verdict is CompatibilityVerdict.UNRESOLVED:
            unsequenceable.append(
                UnsequenceableComponent(
                    workload_ref=decision.workload_ref,
                    identity=decision.identity,
                    verdict=str(decision.verdict),
                    reason=(
                        "No compatibility verdict was reached in Stage 4, so no order "
                        "can be derived. This is a gap in knowledge, not a blocker."
                    ),
                )
            )
            continue

        # COMPATIBLE with no series change needs no move and no mention in the steps; it
        # rides the cluster upgrade untouched.
        if decision.verdict is CompatibilityVerdict.COMPATIBLE and decision.installed_series:
            knowledge = store.find(decision.identity) if decision.identity else None
            if knowledge is None:
                # Compatible via an EKS insight, no stored windows to plan moves from.
                # It is compatible at the assessed hop and needs no move there.
                continue
            plan = plan_moves(
                knowledge,
                decision.installed_series,
                hop_versions,
                installed_version=decision.installed_version,
            )
        else:
            knowledge = store.find(decision.identity) if decision.identity else None
            if knowledge is None or not decision.installed_series:
                unsequenceable.append(
                    UnsequenceableComponent(
                        workload_ref=decision.workload_ref,
                        identity=decision.identity,
                        verdict=str(decision.verdict),
                        reason=(
                            "The component must move, but no stored windows are "
                            "available to order the moves. Its verdict came from a "
                            "single-hop cluster insight, which cannot plan a sequence."
                        ),
                    )
                )
                continue
            plan = plan_moves(
                knowledge,
                decision.installed_series,
                hop_versions,
                installed_version=decision.installed_version,
            )

        if not plan.feasible:
            infeasible = True
            unsequenceable.append(
                UnsequenceableComponent(
                    workload_ref=decision.workload_ref,
                    identity=decision.identity,
                    verdict=str(decision.verdict),
                    reason=plan.reason,
                )
            )
            continue

        # No forced move: the component rides the upgrade as-is. But if a NEWER supported
        # version exists (newest series covering every hop, latest patch), recommend it —
        # advisory, distinct from a forced move. Only when it actually differs from what
        # is installed; a component already on the newest supported version gets nothing.
        if not plan.moves:
            newest = newest_supported_concrete(knowledge, hop_versions)
            component = decision.identity or decision.workload_ref
            if newest is not None and _differs_from_installed(newest, decision):
                recommended.append((component, decision.installed_series, newest))
            continue

        for move in plan.moves:
            rev, rev_reason = resolve_reversibility(
                knowledge, move.from_series, move.to_series
            )
            moves_at.setdefault(move.at_kubernetes, []).append(
                (decision.identity or decision.workload_ref, move, rev, rev_reason)
            )

    steps = _weave(assessment, hop_versions, moves_at, store, recommended, target_version)

    return UpgradePlan(
        cluster=assessment.cluster,
        current_kubernetes_version=assessment.current_kubernetes_version,
        target_kubernetes_version=target_kubernetes_version,
        feasible=not infeasible,
        steps=steps,
        unsequenceable=tuple(unsequenceable),
        snapshot_digest=assessment.snapshot_digest,
        report_digest=assessment.report_digest,
        ledger_digest=assessment.ledger_digest,
        assessment_digest=assessment.digest(),
        store_digest=assessment.store_digest,
        ruleset_digest=ruleset_digest(),
    )


def _weave(
    assessment: ReadinessAssessment,
    hop_versions: tuple[str, ...],
    moves_at: dict[str, list[tuple[str, ComponentMove, object, str]]],
    store: FactStore,
    recommended: list[tuple[str, str | None, str]],
    target_version: str,
) -> tuple[PlanStep, ...]:
    """Interleave moves and cluster hops in version order, assigning step numbers.

    At each cluster version, every component move keyed there is emitted first — the move
    must complete before the cluster advances — then the cluster hop that leaves that
    version. Moves at the same version are ordered by declared dependency, then by name
    for a stable tie-break.

    Advisory `recommended` upgrades are emitted LAST, after the cluster has reached the
    target: they are optional and independent, so they belong after every required step,
    each marked as unordered with the others. Ordered by component name for stability.
    """
    steps: list[PlanStep] = []
    order = 1

    for index, version in enumerate(hop_versions):
        pending = moves_at.get(version, [])
        ordered_moves = _order_by_dependency(pending, store)

        # Moves sharing this version with no dependency between them are genuinely
        # unordered; record that rather than implying the emitted order is required.
        move_orders = list(range(order, order + len(ordered_moves)))
        for local_index, (component, move, rev, rev_reason) in enumerate(ordered_moves):
            siblings = tuple(o for o in move_orders if o != order)
            step = _placed_move(component, move, rev, rev_reason, order)
            steps.append(step.model_copy(update={"unordered_with": siblings}))
            order += 1

        # The cluster hop leaving this version, if there is one (the last version is the
        # target and has no outgoing hop).
        if index < len(hop_versions) - 1:
            steps.append(
                PlanStep(
                    order=order,
                    kind=StepKind.CLUSTER_UPGRADE,
                    description=(
                        f"Upgrade the cluster from Kubernetes {version} to "
                        f"{hop_versions[index + 1]}."
                    ),
                    from_kubernetes=version,
                    to_kubernetes=hop_versions[index + 1],
                )
            )
            order += 1

    # Advisory upgrades last, at the target version, each unordered with the others.
    ordered_recs = sorted(recommended, key=lambda r: r[0])
    rec_orders = list(range(order, order + len(ordered_recs)))
    for component, from_series, to_concrete in ordered_recs:
        siblings = tuple(o for o in rec_orders if o != order)
        step = _placed_recommendation(
            component, from_series, to_concrete, target_version, order
        )
        steps.append(step.model_copy(update={"unordered_with": siblings}))
        order += 1

    return tuple(steps)


def _order_by_dependency(
    moves: list[tuple[str, ComponentMove, object, str]],
    store: FactStore,
) -> list[tuple[str, ComponentMove, object, str]]:
    """Order moves at one cluster version: dependencies first, then by name.

    A component is placed after any component it declares a dependency on that also moves
    here. The dependency graph among add-ons is shallow in practice, so a stable
    name-sorted pass that respects declared edges is sufficient and fully deterministic.
    """
    by_name = sorted(moves, key=lambda entry: entry[0])
    present = {entry[0] for entry in by_name}

    dependency_of: dict[str, set[str]] = {}
    for component, _move, _rev, _reason in by_name:
        knowledge = store.find(component)
        deps = set(knowledge.dependencies) if knowledge else set()
        dependency_of[component] = deps & present

    ordered: list[tuple[str, ComponentMove, object, str]] = []
    placed: set[str] = set()

    # Deterministic topological pass: repeatedly emit the name-sorted components whose
    # in-graph dependencies are already placed. A cycle (should not occur among add-ons)
    # falls back to name order so the function always terminates.
    remaining = list(by_name)
    while remaining:
        progressed = False
        for entry in list(remaining):
            component = entry[0]
            if dependency_of[component] <= placed:
                ordered.append(entry)
                placed.add(component)
                remaining.remove(entry)
                progressed = True
        if not progressed:
            # Dependency cycle: emit the rest in name order rather than looping forever.
            ordered.extend(remaining)
            break

    return ordered
