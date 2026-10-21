"""Per-component move planning: when, across a cluster upgrade, must a component move.

This is the heart of GAP-2. The question is not "does this component support the
target" — Stage 4 answered that — but "given that the cluster crosses several minors one
at a time, can this component's *current* version ride along, and if not, when must it
change and to what."

The knowledge required is exactly the compatibility windows Stage 4 already consumes,
read differently. **No new facts.** A component that cannot be decided by Stage 4 cannot
be sequenced here either, and this module never guesses one into existence.

The forcing case: on Kubernetes 1.31 running cert-manager 1.14 (window ends 1.31),
targeting 1.33. 1.20 needs 1.32+, 1.21 needs 1.33+,
so no single release spans 1.31 through 1.33. The component must move *during* the
cluster upgrade, not before or after — and it may move more than once.
"""

from __future__ import annotations

from pydantic import Field

from ..decide.kubernetes import permits
from ..facts.series import spec_matches_version, version_series
from ..knowledge import ComponentKnowledge, is_exact_spec, is_series_spec
from ..models import ContractModel


class ComponentMove(ContractModel):
    """A required change of a component's version series at a point in the upgrade.

    `before_kubernetes` is the cluster version that must be reached *before* this move,
    and the move must happen before the cluster advances past it. A move keyed on the
    starting cluster version is a pre-flight step; one keyed on an intermediate version
    is the interleaved case GAP-2 exists for.
    """

    # A version *spec* — series (`0.6.x`) or exact release (`3.2.0`). Widened in
    # Stage 6.5 so a release-keyed component's move names the exact version to install.
    from_series: str | None = Field(
        default=None, pattern=r"^(?:\d+\.\d+\.x|\d+\.\d+\.\d+)$"
    )
    to_series: str = Field(pattern=r"^(?:\d+\.\d+\.x|\d+\.\d+\.\d+)$")
    # The cluster minor at which the component must already be on `to_series`. The move
    # is performed while the cluster is at this version, before it advances.
    at_kubernetes: str = Field(pattern=r"^\d+\.\d+$")
    # The concrete version to actually install (Stage 6.5). For a series target it is the
    # latest released patch in that series drawn from the fact's `releases` list
    # (`0.7.x` -> `0.7.5`); for an exact-release target it is that version. None when the
    # fact records no release in the target series — the series is still the answer, we
    # just can't name a patch. `to_series` says what is *compatible*; this says what to
    # *install*.
    recommended_version: str | None = Field(
        default=None, pattern=r"^\d+\.\d+\.\d+$"
    )
    reason: str = Field(min_length=1)


class MovePlan(ContractModel):
    """Every move one component needs across the whole cluster upgrade, in order.

    `feasible=False` means no sequence of moves keeps the component supported across
    every cluster hop — an explicit dead end, never an empty plan pretending success.
    """

    moves: tuple[ComponentMove, ...] = ()
    feasible: bool = True
    reason: str = Field(min_length=1)


def _spec_sort_key(spec: str) -> tuple[int, int, int]:
    """Order a version spec by (major, minor, patch), series or exact release.

    A series (`0.9.x`) sorts with patch 0; an exact release (`3.2.4`) by all three.
    Numeric so `0.9.x` precedes `0.10.x`.
    """
    parts = [int(p) for p in spec.removesuffix(".x").split(".")]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def _concrete_version_for(knowledge: ComponentKnowledge, target_spec: str) -> str | None:
    """The concrete version to install for a target spec (Stage 6.5).

    - Exact-release spec (`3.2.0`): that version, provided the fact records it as a real
      release; otherwise None (we will not invent a release that was never published).
    - Series spec (`0.7.x`): the latest released patch in that series drawn from the
      fact's `releases` list, by numeric (major, minor, patch) order. None when the fact
      records no release in the series — the series is still the compatible answer, we
      just cannot name a patch.

    `to_series` names what is *compatible*; this names what to *install*. Deterministic:
    a pure max over the recorded releases, no clock, no network.
    """
    published = {r.version for r in knowledge.releases}
    if is_exact_spec(target_spec):
        return target_spec if target_spec in published else None
    in_series = [
        r.version for r in knowledge.releases if version_series(r.version) == target_spec
    ]
    if not in_series:
        return None
    return max(in_series, key=_spec_sort_key)


def _series_supported_at(knowledge: ComponentKnowledge, kubernetes_version: str) -> list[str]:
    """Version specs (series or exact) whose window covers this version, lowest first."""
    return sorted(
        (
            rule.component_version_series
            for rule in knowledge.compatibility_rules
            if permits(rule.kubernetes_versions, kubernetes_version)
        ),
        key=_spec_sort_key,
    )


def newest_supported_concrete(
    knowledge: ComponentKnowledge, hop_versions: tuple[str, ...]
) -> str | None:
    """The concrete latest patch of the NEWEST series that supports EVERY hop.

    The recommendation policy, in one place so the plan (Stage 5) and the report agree by
    construction: don't just confirm the installed version still works — recommend the
    newest series whose window covers every Kubernetes version on the path, at its latest
    recorded patch. For metrics-server 0.6.4 on a path 0.9.x also spans, this returns the
    latest recorded 0.9.x patch.

    A series must cover *all* hops to be a candidate (the intersection across hops), so the
    recommendation is valid for the whole upgrade, not just the final target. Returns None
    when no series covers every hop (nothing safe to recommend) or the fact records no
    patch to name for the chosen series (falls back to the series spec via the caller).
    """
    if not hop_versions:
        return None
    permitted_sets = [
        set(_series_supported_at(knowledge, v)) for v in hop_versions
    ]
    permitted = set.intersection(*permitted_sets) if permitted_sets else set()
    if not permitted:
        return None
    newest = max(permitted, key=_spec_sort_key)
    return _concrete_version_for(knowledge, newest) or newest


def _series_covers(
    knowledge: ComponentKnowledge, series: str, kubernetes_version: str
) -> bool:
    """Whether one specific series' window covers a Kubernetes version.

    A held series that no longer appears in the rules (removed from the KB between
    scans) covers nothing, which forces a move rather than silently passing.
    """
    for rule in knowledge.compatibility_rules:
        if rule.component_version_series == series:
            return permits(rule.kubernetes_versions, kubernetes_version)
    return False


def plan_moves(
    knowledge: ComponentKnowledge,
    installed_series: str,
    hop_versions: tuple[str, ...],
    installed_version: str | None = None,
) -> MovePlan:
    """Compute the moves keeping one component supported across every cluster version.

    `hop_versions` is every Kubernetes version the cluster occupies, in order, starting
    with the current version and ending with the target — the hop boundaries flattened.
    For 1.35 → 1.37 that is `("1.35", "1.36", "1.37")`.

    `installed_series` is the held spec — historically always a series (`1.14.x`). When
    the fact is release-keyed the held spec is an exact release (`3.1.0`), and the
    caller's *derived* series (`3.1.x`) will not equal any rule's spec, so no rule would
    match and every hop would spuriously force a move. `installed_version` (Stage 6.5) is
    the concrete installed version; when given, the starting spec is resolved from it with
    the same matcher Stage 3 and Stage 4 use, so series and release-keyed facts agree by
    construction. Omitting it preserves the series-only behaviour exactly.

    The algorithm is greedy and deliberately minimal: hold the current series as long as
    its window covers the cluster, and move only when forced. When a move is forced,
    choose the *lowest* still-supported series that also covers the version being moved
    at — lowest because a smaller jump crosses fewer breaking changes, and choosing the
    highest "to be safe" would invent upgrades the evidence does not require.

    Determinism comes from the fixed hop order and the lowest-series tie-break; there is
    no search and no clock.
    """
    current = installed_series
    if installed_version is not None:
        matched = next(
            (
                rule.component_version_series
                for rule in knowledge.compatibility_rules
                if spec_matches_version(rule.component_version_series, installed_version)
            ),
            None,
        )
        if matched is not None:
            current = matched
    moves: list[ComponentMove] = []

    for index, version in enumerate(hop_versions):
        supported = _series_supported_at(knowledge, version)
        if not supported:
            # No series at all covers this cluster version. Upgrading the component
            # cannot help, so the whole path is infeasible — surfaced, not hidden.
            return MovePlan(
                moves=tuple(moves),
                feasible=False,
                reason=(
                    f"No known version series supports Kubernetes {version}, so the "
                    "component cannot ride the upgrade past that point."
                ),
            )

        if _series_covers(knowledge, current, version):
            continue

        # The held series does not cover this version, so the component must already be
        # on a new series by the time the cluster reaches here. The move is therefore
        # performed at the PREVIOUS cluster version — the last one the old series still
        # covered — so there is never a moment where the running component is
        # unsupported. Preparing the component before advancing the cluster is the whole
        # point of interleaving; placing the move at `version` itself would strand it.
        move_at = hop_versions[index - 1] if index > 0 else version

        # The chosen series must cover both the version we move at and the version we are
        # unblocking, otherwise the move solves one hop while breaking the one before it.
        # Prefer the lowest such series: the smallest jump crosses the fewest breaking
        # changes, and reaching higher "to be safe" invents upgrades the evidence does
        # not require.
        candidates = [
            series
            for series in supported
            if _series_covers(knowledge, series, move_at)
        ]
        if not candidates:
            return MovePlan(
                moves=tuple(moves),
                feasible=False,
                reason=(
                    f"No series supports both Kubernetes {move_at} and {version}, so "
                    "the component cannot cross that boundary without a gap in support."
                ),
            )

        target_series = candidates[0]
        concrete = _concrete_version_for(knowledge, target_series)
        install_clause = (
            f" Install {concrete}, the latest recorded release in {target_series}."
            if concrete is not None and is_series_spec(target_series)
            else f" Install {concrete}."
            if concrete is not None
            else ""
        )
        moves.append(
            ComponentMove(
                from_series=current,
                to_series=target_series,
                at_kubernetes=move_at,
                recommended_version=concrete,
                reason=(
                    f"Series {current or 'installed'} does not support Kubernetes "
                    f"{version}; move to {target_series} while the cluster is still at "
                    f"{move_at}, which {target_series} also supports.{install_clause}"
                ),
            )
        )
        current = target_series

    if not moves:
        return MovePlan(
            moves=(),
            feasible=True,
            reason=(
                f"Series {installed_series} supports every Kubernetes version on the "
                "path, so the component may stay as installed."
            ),
        )

    return MovePlan(
        moves=tuple(moves),
        feasible=True,
        reason=(
            f"{len(moves)} move(s) keep the component supported across the upgrade."
        ),
    )
