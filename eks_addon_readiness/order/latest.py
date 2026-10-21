"""What the plan recommends for a component a STORED FACT decided -- one function, used everywhere.

    in    the approved fact, the installed version, the Kubernetes versions to cross
    out   the version to recommend and the BASIS for it: covered by evidence, or the newest
          recorded release with nothing stating compatibility

THE DEFECT THIS CLOSES. The fact approval page showed the MODEL's `recommended_version`; the plan
recommended from the fact's compatibility RULES. Those are two policies, and on argo-rollouts they
disagreed: the page said 1.10.0, the plan said "stay on 1.6.6". The model had found no
compatibility table -- the only statement it could quote was a chart README's "Kubernetes 1.7+",
attributed to the installed 1.6.x -- so the fact carried one rule (1.6.x, no ceiling) and seven
recorded releases up to 1.10.0. `moves.newest_supported_concrete` recommends from rules alone,
found nothing newer than 1.6.x, and the brief was made against a version nobody had approved.

THE RULE: THE BRIEF IS MADE AGAINST THE APPROVED FACT, AND THE APPROVAL PAGE SHOWS WHAT THE PLAN
WILL DO. So there is one recommendation function, it reads only the fact, and every surface calls
it: the plan (`with_unverified_latest`), the readiness report's Recommended column
(`view/build.py::_recommended_version`) and the fact approval page (`research/report.py`). A number
the model proposed is evidence the reader may weigh in the extraction; it is not what gets planned.

THE POLICY, in order:

  COVERED            `moves.newest_supported_concrete`: the newest series whose recorded window
                     covers every hop, at its latest recorded patch. The frozen policy, unchanged.
  LATEST_UNVERIFIED  The fact records NO rule for any series newer than the installed one, and it
                     records a stable release that is newer. Then the newest such release, and the
                     basis says so. This is the research prompt's Tier 5 ("no compatibility
                     statement of any kind -> the latest stable release, LOW") reaching the plan;
                     it used to stop at the prompt.
  KEEP               Otherwise: the fact speaks to newer series and none covers the path, or
                     nothing newer is recorded. The installed version stays; `version` is what
                     the frozen policy named for it (its series' latest recorded patch), or None.

WHAT THIS DELIBERATELY DOES NOT DO. It does not fall back to the latest release when the fact HAS
rules for newer series that fail to cover the target: a matrix with rising floors ("2.19 needs
1.33+") is the fact saying "not yet", and the newest release would contradict it. The fallback
fires only where the fact is silent about everything newer, which is the one case where "latest,
unverified" is the honest answer rather than a guess over evidence.

Pre-releases are not candidates: only `major.minor.patch` releases are considered, so an `rc`
recorded in `releases` is never recommended.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import NamedTuple

from ..facts.series import exact_version, version_series
from .interleave import _differs_from_installed
from .moves import _spec_sort_key, newest_supported_concrete
from .plan import PlanStep, StepKind, UpgradePlan

# A concrete major.minor.patch, the only shape the frozen `PlanStep` fields accept and the only
# shape treated as a stable release here.
_EXACT = re.compile(r"^\d+\.\d+\.\d+$")


class Basis(StrEnum):
    COVERED = "COVERED"
    LATEST_UNVERIFIED = "LATEST_UNVERIFIED"
    KEEP = "KEEP"


class FactRecommendation(NamedTuple):
    version: str | None
    basis: Basis


def _newer_than_installed(candidate: str, installed_version: str | None) -> bool:
    """Whether a version spec is strictly ahead of the installed version, by version order.

    A concrete candidate (`1.10.0`) is compared to the installed release; a series candidate
    (`1.10.x`, when the fact names no patch) to the installed series. Nothing installed means
    anything recorded is ahead.
    """
    if _EXACT.match(candidate):
        installed = exact_version(installed_version)
    else:
        installed = version_series(installed_version)
    if installed is None:
        return True
    return _spec_sort_key(candidate) > _spec_sort_key(installed)


def _fact_speaks_to_newer_series(knowledge, installed_version: str | None) -> bool:
    """Whether any compatibility rule names a series above the installed one."""
    installed_series = version_series(installed_version)
    if installed_series is None:
        return bool(knowledge.compatibility_rules)
    installed_key = _spec_sort_key(installed_series)
    return any(
        _spec_sort_key(rule.component_version_series) > installed_key
        for rule in knowledge.compatibility_rules
    )


def newest_stable_release(knowledge, installed_version: str | None) -> str | None:
    """The newest `major.minor.patch` release the fact records, ahead of what is installed."""
    candidates = [
        r.version
        for r in knowledge.releases
        if _EXACT.match(r.version) and _newer_than_installed(r.version, installed_version)
    ]
    if not candidates:
        return None
    return max(candidates, key=_spec_sort_key)


def fact_recommendation(
    knowledge, installed_version: str | None, hop_versions: tuple[str, ...]
) -> FactRecommendation:
    """The one recommendation policy for a fact-decided component. See the module docstring."""
    covered = newest_supported_concrete(knowledge, hop_versions) if hop_versions else None
    if covered is not None and _newer_than_installed(covered, installed_version):
        return FactRecommendation(covered, Basis.COVERED)
    if not _fact_speaks_to_newer_series(knowledge, installed_version):
        latest = newest_stable_release(knowledge, installed_version)
        if latest is not None:
            return FactRecommendation(latest, Basis.LATEST_UNVERIFIED)
    # KEEP carries what the frozen policy named (the installed series' own latest recorded
    # patch, typically), or None when it named nothing; the caller decides the fallback.
    return FactRecommendation(covered, Basis.KEEP)


def _unverified_step(
    component: str,
    decision,
    version: str,
    at_kubernetes: str,
    order: int,
    siblings: tuple[int, ...],
) -> PlanStep:
    """One OPTIONAL step to the newest recorded release, worded as what it is."""
    return PlanStep(
        order=order,
        kind=StepKind.COMPONENT_UPGRADE_RECOMMENDED,
        description=(
            f"Recommended, UNVERIFIED: upgrade {component} from "
            f"{decision.installed_version or 'the installed version'} to {version}, the newest "
            f"release its approved fact records. No compatibility statement was found for any "
            f"series newer than the installed one, so nothing states that {version} supports "
            f"Kubernetes {at_kubernetes}; this is the latest release because the fact is silent, "
            "not because evidence covers it. The installed version is compatible as it stands, so "
            "this step is OPTIONAL: declining it does not block the cluster upgrade."
        ),
        component=component,
        from_series=decision.installed_series,
        to_series=version,
        recommended_version=version,
        at_kubernetes=at_kubernetes,
        unordered_with=siblings,
    )


def _hop_versions(decision) -> tuple[str, ...]:
    versions: list[str] = []
    for hop in decision.hops:
        for version in (hop.hop.current_version, hop.hop.target_version):
            if version and version not in versions:
                versions.append(version)
    return tuple(versions)


def with_unverified_latest(plan: UpgradePlan, assessment, store) -> UpgradePlan:
    """Append an optional step per fact-decided component whose fact is silent above it.

    Only for a component with a stored fact (`knowledge_base_revision` set), only when no step
    already names it -- the frozen sequencer's advisory step and a forced move both take
    precedence -- and only when `fact_recommendation` answers LATEST_UNVERIFIED. Appended after
    every existing step, unordered among themselves, like the catalogue recommendations.
    """
    if store is None:
        return plan
    taken = {step.component for step in plan.steps if step.component}
    additions: list[tuple[str, object, str, str]] = []
    for decision in assessment.decisions:
        if decision.knowledge_base_revision is None or not decision.identity:
            continue
        component = decision.identity
        if component in taken:
            continue
        knowledge = store.find(component)
        if knowledge is None:
            continue
        hops = _hop_versions(decision)
        if not hops:
            continue
        recommendation = fact_recommendation(knowledge, decision.installed_version, hops)
        if recommendation.basis is not Basis.LATEST_UNVERIFIED or recommendation.version is None:
            continue
        if not _differs_from_installed(recommendation.version, decision):
            continue
        additions.append((component, decision, recommendation.version, hops[-1]))
    if not additions:
        return plan
    additions.sort(key=lambda entry: entry[0])
    first = max((step.order for step in plan.steps), default=0) + 1
    orders = list(range(first, first + len(additions)))
    steps = list(plan.steps)
    for position, (component, decision, version, at_kubernetes) in enumerate(additions):
        order = orders[position]
        steps.append(
            _unverified_step(
                component,
                decision,
                version,
                at_kubernetes,
                order,
                tuple(other for other in orders if other != order),
            )
        )
    return plan.model_copy(update={"steps": tuple(steps)})
