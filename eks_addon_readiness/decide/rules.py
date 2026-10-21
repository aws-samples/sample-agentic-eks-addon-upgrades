"""The verdict rules: per component, per hop, may it stay and what may it move to.

Two properties govern everything here.

**A missing fact yields `UNRESOLVED`, never `INCOMPATIBLE`.** Absence of knowledge is
not evidence of incompatibility. Getting this wrong would make the tool most alarming
exactly where it knows least, and an operator who is told a healthy component is
incompatible learns to ignore the tool. Stage 3 already separates "no fact" from
"unwelcome fact"; this stage must not re-merge them.

**An EKS Insight answers exactly one hop.** AWS assesses the next minor only, so an
insight taken on a 1.35 cluster says nothing about 1.36 → 1.37. Applying it to later
hops would manufacture confidence AWS never expressed. This is the operational form of
the caveat Stage 3 records.
"""

from __future__ import annotations

from ..facts import FactStatus, GapEntry
from ..facts.series import spec_matches_version
from ..knowledge import ApiRemoval, ComponentKnowledge
from ..models import CompatibilityVerdict, Finding, KubernetesHop, Severity
from .kubernetes import parse_minor, permits


def _spec_sort_key(spec: str) -> tuple[int, int, int]:
    """Order a version spec by (major, minor, patch).

    A series (`0.9.x`) sorts by its major.minor with patch 0, an exact release (`3.2.4`)
    by all three. Numeric, so `0.9.x` sorts before `0.10.x` and `3.2.10` after `3.2.9`.
    """
    body = spec.removesuffix(".x")
    parts = [int(p) for p in body.split(".")]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def specs_permitted_at(
    knowledge: ComponentKnowledge, kubernetes_version: str
) -> tuple[str, ...]:
    """Compatibility specs (series or exact release) whose window contains this version.

    Widened from series-only in Stage 6.5: a spec may be `0.9.x` or `3.2.4`. Sorted
    numerically by (major, minor, patch) so ordering is stable across both shapes.
    """
    matching = [
        rule.component_version_series
        for rule in knowledge.compatibility_rules
        if permits(rule.kubernetes_versions, kubernetes_version)
    ]
    return tuple(sorted(matching, key=_spec_sort_key))


# Retained name for callers that still speak of "series"; identical behaviour, and it
# now returns exact-release specs too where the fact is keyed that way.
series_permitted_at = specs_permitted_at


def applicable_removals(
    knowledge: ComponentKnowledge, series: str | None, target_kubernetes: str
) -> tuple[ApiRemoval, ...]:
    """API removals that bite this series at or before the target version.

    An empty `affected_component_series` means every series is affected — the honest
    default for a dependency the project has never dropped, and it stops a
    partially-researched fact from exempting series nobody checked.
    """
    target = parse_minor(target_kubernetes)
    return tuple(
        removal
        for removal in knowledge.removed_api_versions
        if parse_minor(removal.removed_in_kubernetes) <= target
        and (
            not removal.affected_component_series
            or (series is not None and series in removal.affected_component_series)
        )
    )


def _removal_finding(removal: ApiRemoval, series: str | None) -> Finding:
    scope = (
        "every series of this component"
        if not removal.affected_component_series
        else f"series {series}"
    )
    replacement = (
        f" Use {removal.replacement_api_version} instead."
        if removal.replacement_api_version
        else " No replacement API is recorded."
    )
    return Finding(
        code="REMOVED_API_VERSION",
        severity=Severity.BLOCKER,
        summary=(
            f"{removal.api_version} was removed in Kubernetes "
            f"{removal.removed_in_kubernetes}"
        ),
        details=(
            f"This component depends on {removal.api_version}, which Kubernetes removed "
            f"in {removal.removed_in_kubernetes}. Recorded as affecting {scope}."
            f"{replacement}"
        ),
        citations=removal.evidence,
    )


def verdict_for_hop(
    gap: GapEntry,
    knowledge: ComponentKnowledge | None,
    hop: KubernetesHop,
    *,
    is_first_hop: bool,
) -> tuple[CompatibilityVerdict, tuple[str, ...], tuple[Finding, ...], str]:
    """Decide one component against one hop.

    Returns the verdict, the component series valid at the hop's target, any findings,
    and a plain-language reason.

    `is_first_hop` exists solely so an EKS Insight is not stretched beyond the single
    minor AWS assessed.
    """
    target = hop.target_version

    # A stored rule outranks an insight here, even though Stage 3 reports the insight as
    # the stronger *status*. The two answer different questions: Stage 3 asks "did anyone
    # answer", where AWS assessing this exact cluster is the better answer; Stage 4 asks
    # "what is permitted at this target", where a range spanning every hop beats a
    # single-hop yes or no.
    if knowledge is not None:
        return _verdict_from_knowledge(gap, knowledge, target)

    if gap.status is FactStatus.ANSWERED_BY_CLUSTER:
        return _verdict_from_insight(gap, hop, is_first_hop=is_first_hop)

    # No fact of any kind. Carry Stage 3's reason forward rather than restating it, so
    # one explanation travels the whole pipeline.
    if gap.status is FactStatus.NOT_ASKABLE:
        return (
            CompatibilityVerdict.UNRESOLVED,
            (),
            (),
            (
                f"No compatibility decision is possible: {gap.reason} This is not a "
                "finding about the upgrade — it is a gap in what we can observe."
            ),
        )
    return (
        CompatibilityVerdict.UNRESOLVED,
        (),
        (),
        (
            f"No compatibility fact is held for this component. {gap.reason} Absence of "
            "a fact is not evidence of incompatibility."
        ),
    )


def _verdict_from_knowledge(
    gap: GapEntry, knowledge: ComponentKnowledge, target: str
) -> tuple[CompatibilityVerdict, tuple[str, ...], tuple[Finding, ...], str]:
    valid = specs_permitted_at(knowledge, target)
    installed = gap.version_series
    removals = applicable_removals(knowledge, installed, target)
    findings = tuple(_removal_finding(r, installed) for r in removals)
    revision = knowledge.knowledge_base_revision

    # The spec — series or exact release — that actually covers the installed version,
    # decided by the same matcher Stage 3 uses. `installed_matches` replaces the old
    # `installed in valid` string test, which could not see that installed `3.2.0` is
    # covered by an exact-keyed rule `3.2.0` (its derived series is `3.2.x`).
    installed_spec = next(
        (
            r.component_version_series
            for r in knowledge.compatibility_rules
            if spec_matches_version(r.component_version_series, gap.version)
        ),
        None,
    )
    installed_matches = installed_spec is not None and installed_spec in valid

    if installed is None:
        # An entry exists but the installed version could not be reduced to a series, so
        # there is nothing to compare. Reported as unresolved rather than assumed either
        # way.
        #
        # `valid` is deliberately dropped rather than passed through. Naming the series
        # that support the target would read as "move to one of these", and we cannot say
        # that without knowing where the component is starting from — it may already be on
        # one of them. The supported set is still recoverable from the fact store.
        return (
            CompatibilityVerdict.UNRESOLVED,
            (),
            findings,
            (
                f"Entry {revision} is held, but no usable installed version was "
                f"resolved, so it cannot be compared against Kubernetes {target}. "
                f"{len(valid)} series support {target}, but which move is needed "
                "cannot be determined without the installed version."
            ),
        )

    if not valid:
        return (
            CompatibilityVerdict.INCOMPATIBLE,
            (),
            findings,
            (
                f"No series recorded in {revision} supports Kubernetes {target}. "
                "Upgrading the component cannot resolve this, so the component must be "
                "replaced or removed, or the target reconsidered."
            ),
        )

    if removals:
        # A removed API is decisive regardless of the window. A rule saying the series is
        # supported and a removal saying its API is gone cannot both be acted on, and the
        # blocker is the safe reading.
        alternatives = tuple(
            series
            for series in valid
            if not applicable_removals(knowledge, series, target)
        )
        if alternatives:
            return (
                CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE,
                alternatives,
                findings,
                (
                    f"Series {installed} depends on an API removed by Kubernetes "
                    f"{target}. Move to {', '.join(alternatives)} first."
                ),
            )
        return (
            CompatibilityVerdict.INCOMPATIBLE,
            (),
            findings,
            (
                f"Every series supported on Kubernetes {target} depends on an API that "
                f"{target} removes."
            ),
        )

    if installed_matches:
        window = next(
            r.kubernetes_versions
            for r in knowledge.compatibility_rules
            if r.component_version_series == installed_spec
        )
        ceiling = (
            "with no recorded ceiling"
            if window.maximum_exclusive is None
            else f"up to but excluding {window.maximum_exclusive}"
        )
        return (
            CompatibilityVerdict.COMPATIBLE,
            valid,
            findings,
            (
                f"{revision} records {installed_spec} as supported from "
                f"{window.minimum_inclusive} {ceiling}, which includes {target}."
            ),
        )

    return (
        CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE,
        valid,
        findings,
        (
            f"Series {installed} is not supported on Kubernetes {target}. "
            f"{revision} records {', '.join(valid)} as supported there."
        ),
    )


def _verdict_from_insight(
    gap: GapEntry, hop: KubernetesHop, *, is_first_hop: bool
) -> tuple[CompatibilityVerdict, tuple[str, ...], tuple[Finding, ...], str]:
    target = hop.target_version
    assessed = gap.insight_kubernetes_version

    # AWS assessed one specific version. Anything else is outside what it claimed, and
    # both checks matter: the hop must be the first, and its target must be the version
    # AWS actually named.
    if not is_first_hop or assessed != target:
        scope = assessed or "the next minor"
        return (
            CompatibilityVerdict.UNRESOLVED,
            (),
            (),
            (
                f"AWS assessed this add-on against Kubernetes {scope} only, so it says "
                f"nothing about the hop to {target}. A stored compatibility range is "
                "needed to decide later hops."
            ),
        )

    if gap.insight_lists_installed_version:
        return (
            CompatibilityVerdict.COMPATIBLE,
            gap.insight_compatible_versions,
            (),
            (
                f"AWS lists the installed version among the {len(gap.insight_compatible_versions)} "
                f"version(s) compatible with Kubernetes {target} on this cluster."
            ),
        )

    return (
        CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE,
        gap.insight_compatible_versions,
        (),
        (
            f"AWS does not list the installed version among the versions compatible "
            f"with Kubernetes {target}. It lists "
            f"{', '.join(gap.insight_compatible_versions) or 'none'}."
        ),
    )
