"""The presentation rules: stage documents in, one validated value set out.

This module and `model.py` are the presentation stage's ruleset. Everything a
human reads is computed here, once, and nowhere else. A renderer receives the
result and formats it; it derives nothing.

The rules are deterministic and clock-free, and they issue no verdict. They order,
group, de-duplicate and name -- over verdicts already formed by Stage 4 and a
sequence already ordered by Stage 5. No model participates, here or anywhere
downstream of Stage 1.

THE ONE RULE THAT EXISTS BECAUSE OF A SHIPPED DEFECT
---------------------------------------------------
`_unresolved_rows` joins Stage 4's undecided components with Stage 5's
unsequenceable ones on `workload_ref`. A component UNRESOLVED in Stage 4 is
necessarily also unsequenceable in Stage 5, so the two sets overlap; summing
them in one place, de-duplicating them independently in two others, and omitting the
number entirely in a fourth is how the counts diverge. Here the join happens once and its
result is the only unresolved list in the system.
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple
from urllib.parse import urlparse

from ..decide import ReadinessAssessment
from ..facts import FactStore, GapLedger
from ..knowledge import ComponentKnowledge
from ..models import Confidence
from ..order import StepKind, UpgradePlan
from ..report import InventoryReport
from .model import (
    ABSENT,
    MANAGED_ADDON_INSTALL_METHOD,
    UNSUPPORTED_AT_TARGET_KIND,
    ApprovalControls,
    CitedSource,
    ComponentGroup,
    ComponentRow,
    PendingApproval,
    PresentationDocument,
    Provenance,
    RecommendedRow,
    ReportTotals,
    ReportView,
    SourceRow,
    StepRow,
    UnresolvedOrigin,
    UnresolvedRow,
)
from .ruleset import renderer_digest, ruleset_digest

# Worst first, then by name. A reader scanning a report needs the thing that
# blocks them at the top; alphabetical order buries it.
_VERDICT_ORDER = {
    "INCOMPATIBLE": 0,
    "UNRESOLVED": 1,
    "REQUIRES_COMPONENT_UPGRADE": 2,
    "COMPATIBLE": 3,
}

# The install-method string Stage 2 records for an EKS-managed add-on used to be spelled here.
# It now lives on `model` as `MANAGED_ADDON_INSTALL_METHOD` and is imported above, because
# `StepRow.aws_managed` asks the same question of the same string and a renderer may not import
# this module. One vocabulary, one place. Still a value from the inventory document rather than
# a component name -- per-component behaviour is data in the knowledge store.

# A Helm-installed component's workload reference is minted by
# `derive_helm_releases` with this prefix, so the prefix is the signal rather than
# a list of chart names.
_HELM_WORKLOAD_PREFIX = "HelmRelease/"

# Titles that mark an authoritative compatibility matrix: the upstream project's
# own, and AWS's for a managed add-on. Both earn the official-source mark.
_OFFICIAL_MATRIX_TITLES = (
    "official compatibility matrix",
    "official aws add-on compatibility",
)


def _display_name(identity: str | None, workload_ref: str) -> str:
    """The name a human sees. Resolved identity when there is one, else the ref."""
    return identity or workload_ref


def _group_for(install_method: str, workload_ref: str) -> ComponentGroup:
    if install_method == MANAGED_ADDON_INSTALL_METHOD:
        return ComponentGroup.MANAGED_ADDON
    if workload_ref.startswith(_HELM_WORKLOAD_PREFIX):
        return ComponentGroup.HELM_RELEASE
    return ComponentGroup.OTHER_WORKLOAD


def _is_official_matrix(title: str | None) -> bool:
    lowered = (title or "").lower()
    return any(marker in lowered for marker in _OFFICIAL_MATRIX_TITLES)


def _hop_versions(decision) -> tuple[str, ...]:
    """Every Kubernetes version this component has to cross, in order."""
    versions: list[str] = []
    for hop in decision.hops:
        for version in (hop.hop.current_version, hop.hop.target_version):
            if version and version not in versions:
                versions.append(version)
    return tuple(versions)


def _synopsis_for(knowledge: ComponentKnowledge, decision) -> str:
    """A one-line 'what the evidence says' note, derived from the stored fact.

    Names which recorded series cover the versions on this component's path, so a
    cited source is explained rather than merely linked. Deterministic: it reads
    the fact, never a model.
    """
    from ..decide.kubernetes import permits

    rules = knowledge.compatibility_rules
    if not rules:
        return ""
    targets = _hop_versions(decision)
    supporting = [
        rule.component_version_series
        for rule in rules
        if (not targets or any(permits(rule.kubernetes_versions, t) for t in targets))
    ]
    titles = [c.title for rule in rules for c in rule.evidence]
    matrix = any(_is_official_matrix(t) for t in titles)
    aws = any("aws" in (t or "").lower() for t in titles)
    if matrix and aws:
        origin = "official AWS add-on compatibility"
    elif matrix:
        origin = "official compatibility matrix"
    else:
        origin = "recorded compatibility data"
    if not supporting:
        return f"{origin}: {len(rules)} version series recorded; none covers every hop."
    unique = ", ".join(dict.fromkeys(supporting))
    if len(targets) > 1:
        span = f" across Kubernetes {targets[0]}–{targets[-1]}"
    elif targets:
        span = f" at Kubernetes {targets[0]}"
    else:
        span = ""
    return f"{origin}: {unique} supports the upgrade{span}."


def _insight_sources(decision) -> tuple[str, tuple[SourceRow, ...]]:
    """Source and confidence for a verdict AWS decided, not a stored fact.

    An EKS Insight is computed by AWS for *this* cluster and normally answers only
    the next minor. When it is what decided a component, name it -- with a
    synopsis of which hops it actually covered -- so an insight-decided add-on
    shows its evidence rather than a blank. It has no URL: it is a cluster fact,
    not a document.

    CONFIDENCE IS HIGH WHEN AWS COVERED THE WHOLE PATH, and that is a correction.
    This used to return ABSENT on the reasoning that confidence is a property of a
    researched fact and there is no fact here to rate -- which read as "we have no
    idea" for the one source that is more authoritative than anything research can
    produce. AWS computed the answer for this exact cluster from its own
    control-plane catalogue, and `facts/catalogue.py` already rates that same data
    `Confidence.HIGH` where it builds a fact out of it ("AWS's authoritative
    control-plane catalogue, a single but definitive source"). The report showing a
    dash for it contradicted the fact store's own rating of the identical evidence.

    PARTIAL COVERAGE STAYS ABSENT. An insight answers one minor, so on a multi-hop
    path it can decide the first hop and nothing after it. The synopsis already says
    "the rest need research" there, and rating that HIGH would have the confidence
    column contradict the sentence beside it. Rated only when AWS covered every hop
    the component has to cross.
    """
    covered = [h for h in decision.hops if h.decided_from == "eks-insight"]
    if not covered:
        return ABSENT, ()
    labels = [f"{h.hop.current_version}→{h.hop.target_version}" for h in covered]
    total = len(decision.hops)
    complete = len(covered) == total
    tail = f" ({len(covered)} of {total} hops; the rest need research)." if not complete else "."
    return (str(Confidence.HIGH) if complete else ABSENT), (
        SourceRow(
            title="AWS EKS Insight (this cluster, next minor)",
            url=None,
            official=True,
            synopsis=("AWS assessed this cluster and confirmed " + ", ".join(labels) + tail),
        ),
    )


def _confidence_and_sources(decision, store: FactStore | None) -> tuple[str, tuple[SourceRow, ...]]:
    """The confidence label and cited sources for one component's evidence."""
    knowledge = store.find(decision.identity) if (store and decision.identity) else None
    if knowledge is None:
        return _insight_sources(decision)
    synopsis = _synopsis_for(knowledge, decision)
    sources: list[SourceRow] = []
    seen: set[str] = set()
    for rule in knowledge.compatibility_rules:
        for citation in rule.evidence:
            url = str(citation.url)
            if url in seen:
                continue
            seen.add(url)
            sources.append(
                SourceRow(
                    title=citation.title,
                    url=url,
                    official=_is_official_matrix(citation.title),
                    synopsis=synopsis,
                )
            )
    return str(knowledge.evidence_confidence), tuple(sources)


def _recommended_version(decision, plan: UpgradePlan, store: FactStore | None) -> str | None:
    """The concrete version this component should end up on, or None.

    TWO SOURCES, BECAUSE A COMPONENT HAS ONE OR THE OTHER AND NEVER BOTH, and each is the
    same function Stage 5 uses -- so the report and the plan agree by construction rather
    than by coincidence:

    - `order.moves.newest_supported_concrete` for a component the FACT STORE knows.
    - `order.recommend.catalogue_recommendation` for one AWS decided about this cluster,
      whose compatible-version list arrives as the assessment's `valid_targets`.

    THE SECOND ONE IS THE FIX. Asking only the store was wrong for an EKS-managed add-on,
    which has no stored fact at all: `fact_status` is `ANSWERED_BY_CLUSTER`,
    `knowledge_base_revision` is null, so `store.find()` misses and this fell through to
    "the installed version, for a component that genuinely stays". For example, on a
    cluster targeting 1.36, coredns rendered as `1.13.2-eksbuild.21
    (stay)` while `v1.14.3-eksbuild.16` sat on AWS's own compatible list in the same
    document. "Stay on this version" is the right answer when it is true; it was not true
    here, and the newer version was already in the assessment.

    Falls back to the plan's own move target, then to the installed version for a component
    that genuinely stays. An UNRESOLVED component gets None: there is no honest
    recommendation when the compatibility question is open.
    """
    from ..order.latest import fact_recommendation
    from ..order.recommend import catalogue_recommendation

    if decision.verdict.value == "UNRESOLVED":
        return None

    knowledge = store.find(decision.identity) if (store and decision.identity) else None
    if knowledge is not None:
        # THE ONE POLICY, shared with the plan (`order/latest.py`) and the fact approval page
        # (`research/report.py`). A `KEEP` that names nothing falls through to the plan's own
        # move target, which is where a per-hop path the newest-series policy could not name
        # would be.
        recommendation = fact_recommendation(
            knowledge, decision.installed_version, _hop_versions(decision)
        )
        if recommendation.version is not None:
            return recommendation.version

    catalogued, _at_kubernetes = catalogue_recommendation(decision)
    if catalogued is not None:
        return catalogued

    name = _display_name(decision.identity, decision.workload_ref)
    moves = [s for s in plan.component_moves if s.component == name]
    if moves:
        last = moves[-1]
        return last.recommended_version or last.to_series
    if decision.verdict.value == "COMPATIBLE":
        return decision.installed_version
    return None


def _recommendation_is_unverified(decision, store: FactStore | None) -> bool:
    """Whether the recommended version rests on no compatibility statement at all.

    True exactly when `order/latest.py` answers LATEST_UNVERIFIED for this component: the fact
    is silent about every series above the installed one and names a newer release. The row
    must say so beside the number, because the number alone reads as a covered recommendation.
    """
    from ..order.latest import Basis, fact_recommendation

    knowledge = store.find(decision.identity) if (store and decision.identity) else None
    if knowledge is None or decision.verdict.value == "UNRESOLVED":
        return False
    recommendation = fact_recommendation(
        knowledge, decision.installed_version, _hop_versions(decision)
    )
    return recommendation.basis is Basis.LATEST_UNVERIFIED


def _stays_put(recommended: str | None, decision) -> bool:
    """Whether the report should say this component stays on what it is running.

    DELEGATES TO THE FUNCTION STAGE 5 USES, rather than comparing the two strings here.

    The bug this replaces was raw string equality. `newest_supported_concrete` returns a
    bare concrete version (`1.13.2`); a live EKS-managed add-on reports
    `v1.13.2-eksbuild.21`. Those are the same version, and Stage 5 knows it -- it
    normalises both sides through `facts.series.exact_version` before deciding whether to
    emit a move, and its docstring names this exact pair. The report did not, so Stage 5
    emitted no step while the report rendered the component as moving to the version it
    was already on: the report and the plan disagreeing about a displayed value, which is
    the defect class this whole package exists to remove.

    So it calls `order.interleave._differs_from_installed` -- the same function, not a
    second copy of its four lines. A private name across a package boundary is the lesser
    evil here: `interleave` is digest-frozen, so its name and signature cannot change
    without a deliberate Stage 5 digest move, which is a loud event. Re-implementing the
    comparison is the thing that silently drifts, and drifting is precisely what happened.
    """
    if recommended is None:
        return False
    from ..order.interleave import _differs_from_installed

    return not _differs_from_installed(recommended, decision)


def _inventory_index(inventory: InventoryReport) -> dict[str, tuple[str, str | None]]:
    """workload_ref -> (install_method, namespace), the Stage 2 facts display needs.

    Keyed on `workload_ref` because that is the identifier both documents carry
    verbatim. Joining on a display name would let a component with no resolved
    identity fall out of its own group.
    """
    return {c.workload_ref: (c.install_method, c.namespace) for c in inventory.components}


def _component_rows(
    assessment: ReadinessAssessment,
    inventory: InventoryReport,
    plan: UpgradePlan,
    store: FactStore | None,
) -> tuple[ComponentRow, ...]:
    index = _inventory_index(inventory)
    rows: list[ComponentRow] = []
    for decision in assessment.decisions:
        install_method, namespace = index.get(decision.workload_ref, ("UNKNOWN", None))
        confidence, sources = _confidence_and_sources(decision, store)
        recommended = _recommended_version(decision, plan, store)
        rows.append(
            ComponentRow(
                component=_display_name(decision.identity, decision.workload_ref),
                identity=decision.identity,
                workload_ref=decision.workload_ref,
                namespace=namespace,
                installed_version=decision.installed_version,
                recommended_version=recommended,
                recommendation_unverified=_recommendation_is_unverified(decision, store),
                stays=_stays_put(recommended, decision),
                verdict=str(decision.verdict),
                confidence=confidence,
                priority=decision.assessment_priority,
                fact_status=decision.fact_status,
                group=_group_for(install_method, decision.workload_ref),
                install_method=install_method,
                decided_from=tuple(dict.fromkeys(h.decided_from for h in decision.hops)),
                sources=sources,
            )
        )
    rows.sort(key=lambda r: (_VERDICT_ORDER.get(r.verdict, 4), r.component))
    return tuple(rows)


def _unresolved_rows(
    assessment: ReadinessAssessment, plan: UpgradePlan
) -> tuple[UnresolvedRow, ...]:
    """One row per component that is undecided or unsequenceable. THE join.

    Stage 4's `undecided` and Stage 5's `unsequenceable` overlap by construction:
    a component with no verdict cannot be placed in a sequence, so it appears in
    both. The join is on `workload_ref` -- required and non-empty on both
    documents, and derived by neither -- so it cannot drift the way
    display-name keying could.

    Order is deterministic: undecided components first, in the assessment's
    worst-verdict-then-name order, then the components that were decided yet still
    could not be sequenced. That second group is the only genuinely additional
    one, and it is exactly what an HTML view labels "could not be sequenced".
    """
    unsequenceable = {u.workload_ref: u for u in plan.unsequenceable}
    rows: list[UnresolvedRow] = []
    seen: set[str] = set()

    undecided = sorted(
        assessment.undecided,
        key=lambda d: (
            _VERDICT_ORDER.get(str(d.verdict), 4),
            _display_name(d.identity, d.workload_ref),
        ),
    )
    for decision in undecided:
        ref = decision.workload_ref
        seen.add(ref)
        blocking = decision.blocking_hop
        entry = unsequenceable.get(ref)
        rows.append(
            UnresolvedRow(
                component=_display_name(decision.identity, decision.workload_ref),
                workload_ref=ref,
                verdict=str(decision.verdict),
                origin=(
                    UnresolvedOrigin.BOTH if entry is not None else UnresolvedOrigin.ASSESSMENT_ONLY
                ),
                reason=(
                    blocking.reason
                    if blocking is not None
                    else (entry.reason if entry is not None else "No verdict was formed.")
                ),
            )
        )

    for ref, entry in sorted(unsequenceable.items()):
        if ref in seen:
            continue
        rows.append(
            UnresolvedRow(
                component=_display_name(entry.identity, entry.workload_ref),
                workload_ref=ref,
                verdict=entry.verdict,
                origin=UnresolvedOrigin.PLAN_ONLY,
                reason=entry.reason,
            )
        )
    return tuple(rows)


def _full_versions(components: tuple[ComponentRow, ...]) -> dict[str, str]:
    """component name -> the recommended version as the component table shows it.

    Exists because the frozen `PlanStep.recommended_version` is patterned
    `^\\d+\\.\\d+\\.\\d+$` and so cannot hold an EKS build suffix: a step recommending
    `v1.14.3-eksbuild.16` carries `1.14.3` in that field. The component row carries AWS's
    full string, which is the one an operator actually installs.

    So the step list and the advisory list read this rather than the step's typed field,
    and every surface of the report names one version in one spelling. They are the same
    version either way -- `facts.series.exact_version` reduces both to `1.14.3` -- but a
    reader comparing two tables must not have to know that.
    """
    return {row.component: row.recommended_version for row in components if row.recommended_version}


def _install_methods(components: tuple[ComponentRow, ...]) -> dict[str, str]:
    """component name -> how it was installed, for the steps that move it.

    Joined on the SAME key `_full_versions` joins on -- the display name a step's `component`
    field carries -- so a step that finds its version here also finds its install method, and
    neither can silently match while the other misses.

    Exists because the step list has to explain the ABSENCE of a brief. An AWS-managed add-on
    is deliberately not researched, and a page that reports that as "nothing was read" says
    research failed when in fact it was never asked for. The plan does not carry the install
    method -- `order/plan.py` is digest-frozen, so it cannot grow the field -- and the
    inventory does, so the join happens here, once, rather than a renderer inferring it from
    the shape of a version string.
    """
    return {row.component: row.install_method for row in components}


def _step_rows(
    plan: UpgradePlan,
    full_versions: dict[str, str] | None = None,
    install_methods: dict[str, str] | None = None,
) -> tuple[StepRow, ...]:
    full_versions = full_versions or {}
    install_methods = install_methods or {}
    rows: list[StepRow] = []
    for step in plan.steps:
        if step.kind is StepKind.CLUSTER_UPGRADE:
            rows.append(
                StepRow(
                    order=step.order,
                    kind=str(step.kind),
                    label="CLUSTER",
                    description=(
                        f"Upgrade the cluster {step.from_kubernetes} → {step.to_kubernetes}"
                    ),
                    unordered_with=step.unordered_with,
                )
            )
            continue

        target = (
            full_versions.get(step.component or "") or step.recommended_version or step.to_series
        )
        installed = step.from_series or "installed"
        if step.kind is StepKind.COMPONENT_UPGRADE_RECOMMENDED:
            rows.append(
                StepRow(
                    order=step.order,
                    kind=str(step.kind),
                    label="OPTIONAL",
                    description=(
                        f"Optional: upgrade {step.component} {installed} → "
                        f"{target} at Kubernetes {step.at_kubernetes} "
                        "(already compatible)"
                    ),
                    unordered_with=step.unordered_with,
                    install_method=install_methods.get(step.component or ""),
                )
            )
            continue

        install = f" (install {step.recommended_version})" if step.recommended_version else ""
        rows.append(
            StepRow(
                order=step.order,
                kind=str(step.kind),
                label="REQUIRED",
                description=(
                    f"Move {step.component}: {installed} → {step.to_series}"
                    f"{install} at Kubernetes {step.at_kubernetes}"
                ),
                reversibility=(str(step.reversibility) if step.reversibility else None),
                one_way=(step.reversibility is not None and str(step.reversibility) == "ONE_WAY"),
                unordered_with=step.unordered_with,
                install_method=install_methods.get(step.component or ""),
            )
        )
    return tuple(rows)


def _newest_recorded_release(identity: str | None, store: FactStore | None) -> str | None:
    """The newest release the stored fact records for a component, or None.

    NOT A RECOMMENDATION, AND THE DISTINCTION IS THE WHOLE POINT OF THE FUNCTION.
    `_recommended_version` answers "what should this component end up on", and for a component
    no series of which covers the target it correctly answers None -- there is nothing to
    recommend. But a person being asked to accept running that component unsupported still
    needs to know how far its recorded history actually goes: keda's fact tops out at 2.20.2
    with a window of `[1.33, 1.36)`, and "the newest release anyone recorded is 2.20.2 and it
    still does not reach 1.36" is a different and much more decidable statement than a blank
    cell. The caller is required to render it marked as not covering the target; this returns
    the number, never a claim about it.

    ORDERED BY THE SAME KEY STAGE 5 ORDERS VERSIONS WITH, `order.moves._spec_sort_key`, so the
    report and the plan can never disagree about which recorded release is the newest. A
    private name across a package boundary is the lesser evil for `_stays_put`'s reason:
    `moves` is digest-frozen, so the name cannot move without a deliberate and loud Stage 5
    digest change, whereas a second copy of "parse major.minor.patch and compare numerically"
    is exactly the thing that drifts silently.

    NORMALISED BEFORE SORTING, REPORTED UNNORMALISED. `ReleaseRecord.version` is only
    `min_length=1`, so a fact may legitimately record `v2.20.2` or a pre-release string that
    `_spec_sort_key` would raise `ValueError` on -- and an exception here would fail the whole
    report over a cosmetic spelling in one release row. So the SORT key comes from
    `exact_version`, which strips a `v` and build metadata and returns None for anything with
    no patch level, and unparseable records are skipped rather than fatal. What is returned is
    the record's own spelling, because that is what the fact says and what a reader looking the
    release up will find.
    """
    from ..facts.series import exact_version
    from ..order.moves import _spec_sort_key

    knowledge = store.find(identity) if (store and identity) else None
    if knowledge is None:
        return None
    newest: tuple[tuple[int, int, int], str] | None = None
    for record in knowledge.releases:
        normalized = exact_version(record.version)
        if normalized is None:
            continue
        key = _spec_sort_key(normalized)
        if newest is None or key > newest[0]:
            newest = (key, record.version)
    return newest[1] if newest is not None else None


def _further_than_installed(installed: str | None, newest: str | None) -> str | None:
    """The newest recorded release when it is genuinely AHEAD of what is installed, else None.

    THIS IS WHAT DECIDES WHETHER THE ROW IS A MOVE, and it is a separate question from "is
    there a release recorded at all". A component no series of which reaches the target may
    already be running the last release anybody published -- and a row reading `2.20.x ->
    2.20.2` for a cluster that is on 2.20.2 would invent a move, ask a person to approve it,
    and hand the brief stage a jump with no releases between its ends. So the move exists only
    when the fact records something strictly newer than the installed version; otherwise the
    row keeps its dead-end shape and says the component stays where it is.

    COMPARED THE WAY STAGE 5 COMPARES VERSIONS, through `order.moves._spec_sort_key` over
    `facts.series.exact_version`, for `_newest_recorded_release`'s stated reason: a second copy
    of "parse major.minor.patch and compare numerically" is exactly the thing that drifts
    silently, and `moves` is digest-frozen so the name cannot move without a loud Stage 5
    digest change.

    RETURNS THE NORMALISED VERSION, not the fact's own spelling, because the caller feeds it to
    `PlanStep.recommended_version` whose pattern is `^\\d+\\.\\d+\\.\\d+$` -- a fact that
    legitimately records `v2.20.2` would otherwise fail to validate and cost the component its
    brief. What a reader is SHOWN is still the fact's own spelling; see `_newest_recorded_release`.

    >>> _further_than_installed("2.17.2", "2.20.2")
    '2.20.2'
    >>> _further_than_installed("2.17.2", "v2.20.2")
    '2.20.2'
    >>> _further_than_installed("2.20.2", "2.20.2") is None
    True
    >>> _further_than_installed("2.21.0", "2.20.2") is None
    True
    >>> _further_than_installed(None, "2.20.2") is None
    True
    >>> _further_than_installed("UNRESOLVED", "2.20.2") is None
    True
    >>> _further_than_installed("2.17.2", None) is None
    True
    """
    from ..facts.series import exact_version
    from ..order.moves import _spec_sort_key

    ahead = exact_version(newest)
    here = exact_version(installed)
    if ahead is None or here is None:
        return None
    if _spec_sort_key(ahead) <= _spec_sort_key(here):
        return None
    return ahead


class UnsupportedAtTarget(NamedTuple):
    """One component that cannot reach the target, addressed the way two stages must address it.

    **THIS TYPE EXISTS TO KILL A SILENT MIS-JOIN, and that is worth stating before the fields.**
    An unsupported-at-target row is not a plan step: `view/build.py` mints it from the plan's
    `unsequenceable` array and numbers it after the last real step, while the brief stage has to
    request a brief for the same component and key that request on the same number -- because
    `view/model.py::AdviceView.for_step(order)` joins a brief to a row BY `step_order`. Two
    places computing `max(order) + 1` is two places for the arithmetic to diverge, and the
    failure mode is invisible: the row renders `No brief`, the brief attaches to nothing, and no
    error is raised anywhere. So the number is computed ONCE, here, and both callers read it.

    `order` IS THE JOIN KEY AND NOT `component`, for the reason `StepAdvice` gives: a plan may
    move one component twice -- cert-manager does -- so a component name is not a key, and a
    second join rule for these rows alone would mean the page had two ways to find a brief.

    `move_to` IS THE ONE DERIVED FIELD. Non-None means the fact records a release strictly newer
    than what is installed, so the row is a real move to it (which still does not reach the
    target). None means there is nothing to move to and the row states a dead end. Both callers
    branch on it, so the page and the brief cannot disagree about whether there is a jump.
    """

    order: int
    workload_ref: str
    # The display name: the resolved identity when there is one, else the workload reference.
    # The same string `_display_name` gives every other surface of the report.
    component: str
    identity: str | None
    installed_version: str | None
    # `major.minor.x` for the installed version, or None when it cannot be reduced to one. What
    # the row shows in the Installed column, so the row reads like every other move.
    installed_series: str | None
    # The newest release the fact records, IN THE FACT'S OWN SPELLING. Display only.
    newest_release: str | None
    # The normalised version to move to, or None when there is no move. See
    # `_further_than_installed`.
    move_to: str | None

    @property
    def is_move(self) -> bool:
        """Whether approving this row upgrades the component as far as its releases go."""
        return self.move_to is not None and self.installed_series is not None


def installed_from_assessment(
    assessment: ReadinessAssessment | None,
) -> dict[str, str | None]:
    """workload_ref -> the installed version, as Stage 4 recorded it.

    ONE OF TWO ADAPTERS ONTO THE SAME NUMBER, and they exist as named functions rather than as a
    comprehension at each call site so that the claim they rest on is written down once: THE
    INVENTORY AND THE ASSESSMENT CARRY THE SAME STRING, verbatim. `inventory/resolve.py` writes
    `ComponentReport.version`, `facts/gaps.py` copies it to `GapEntry.version`, `decide/engine.py`
    copies that to `ComponentDecision.installed_version`, and nothing on the way normalises,
    truncates or re-derives it.

    THAT MATTERS BECAUSE TWO STAGES READ IT FROM DIFFERENT DOCUMENTS. The report has the
    assessment open and uses this; `stages/upgrade_brief.py` has the inventory open and uses
    `installed_from_inventory`. Both feed `unsupported_at_target`, which decides from this value
    whether an unreachable component has a move at all -- so if the chain above ever stopped
    copying, the page could draw a move the brief stage did not request, or the reverse.
    `tests/view/test_an_unsupported_component_gets_a_row.py` asserts the two agree.

    WHY NOT ONE DOCUMENT FOR BOTH. The brief stage would then have to open the assessment, and its
    role is scoped to `plans/*` and `inventories/*` (`terraform/brief/main.tf`). A read it is not
    permitted would be caught, treated as absent, and cost every unreachable component its brief --
    silently, because an advisory stage may not fail a run. A feature that needs an IAM change to
    do anything, and reports nothing when it does not have one, is worse than one that reads the
    document already in its hand.
    """
    return {
        decision.workload_ref: decision.installed_version
        for decision in (assessment.decisions if assessment is not None else ())
    }


def installed_from_inventory(
    inventory: InventoryReport | None,
) -> dict[str, str | None]:
    """workload_ref -> the installed version, as Stage 2 observed it.

    The other adapter. See `installed_from_assessment` for why there are two and for the chain
    that makes them the same string.
    """
    return {
        component.workload_ref: component.version
        for component in (inventory.components if inventory is not None else ())
    }


def unsupported_at_target(
    plan: UpgradePlan,
    installed: dict[str, str | None] | None = None,
    store: FactStore | None = None,
) -> tuple[UnsupportedAtTarget, ...]:
    """Every component that cannot reach the target, numbered as the report numbers it.

    **THE ONE PLACE A STEP ORDER IS MINTED FOR A ROW THAT IS NOT A PLAN STEP.** Called by
    `_unsupported_rows` to build the rows, and by `stages/upgrade_brief.py` to key the brief it
    requests for the same components. Neither may compute a number of its own: see
    `UnsupportedAtTarget` for the mis-join that costs.

    NUMBERED AFTER EVERY REAL PLAN STEP, WHICH IS LOAD-BEARING RATHER THAN TIDY. A
    `StepDecision` is keyed on `step_order` and a `StepGrant` is minted against it, so a row
    inserted anywhere but the end would renumber the steps below it -- and every decision
    already recorded against this plan would then point at a different step than the person
    read. `max(order) + 1` and NOT `len(steps) + 1`, because the plan's own orders need not be
    contiguous: `order/recommend.py` appends its optional steps by the same arithmetic, and a
    count would collide with a step that exists.

    ORDERED BY `workload_ref`, the key `_unresolved_rows` orders its own unsequenceable tail by:
    it is carried verbatim by the plan and derived by nobody, so two runs over the same plan
    mint the same rows in the same order and neither the page's bytes nor the brief's step
    numbers can depend on the order the plan happened to list them in.

    ONLY THE INCOMPATIBLE ENTRIES. An UNRESOLVED component is in `unsequenceable` because
    nothing is known about it, and offering "accept running it unsupported" for a component
    whose compatibility question is merely open would ask a person to consent to a risk nobody
    has measured. Those stay in the Unresolved section, where the honest answer is research.

    `installed` IS PASSED IN, keyed on `workload_ref` -- the identifier the plan carries verbatim
    and derives from nothing. The plan does not record an installed version for an unsequenceable
    component; Stages 2 and 4 both do, and `installed_from_inventory` /
    `installed_from_assessment` are the two adapters that produce this map from whichever of those
    documents the caller has open. Empty means no installed version and therefore no move, which
    is the safe direction: a row that says the component stays put, or a brief nobody asked for,
    are both harmless -- an invented move is not.
    """
    installed = installed or {}
    # THE ARITHMETIC LIVES HERE AND NOWHERE ELSE. `tests/view/
    # test_an_unsupported_component_gets_a_row.py` fails if any other module grows a copy.
    first_order = max((step.order for step in plan.steps), default=0) + 1
    entries = sorted(
        (e for e in plan.unsequenceable if e.verdict == "INCOMPATIBLE"),
        key=lambda e: e.workload_ref,
    )
    from ..facts.series import version_series

    records: list[UnsupportedAtTarget] = []
    for offset, entry in enumerate(entries):
        version = installed.get(entry.workload_ref)
        newest = _newest_recorded_release(entry.identity, store)
        records.append(
            UnsupportedAtTarget(
                order=first_order + offset,
                workload_ref=entry.workload_ref,
                component=_display_name(entry.identity, entry.workload_ref),
                identity=entry.identity,
                installed_version=version,
                installed_series=version_series(version),
                newest_release=newest,
                move_to=_further_than_installed(version, newest),
            )
        )
    return tuple(records)


def _unsupported_rows(
    plan: UpgradePlan,
    installed: dict[str, str | None],
    store: FactStore | None,
    install_methods: dict[str, str],
) -> tuple[StepRow, ...]:
    """One row per component that cannot reach the target, so a person can decide about it.

    THE ROW EXISTS BECAUSE ITS ABSENCE WAS UNDECIDABLE. A component whose fact records no
    series covering the target is INCOMPATIBLE in Stage 4 and lands in Stage 5's
    `unsequenceable` with `feasible` false. Both documents are right and neither can be
    changed -- they are digest-frozen and, more importantly, they are TRUE. But the only place
    the component then surfaced was the could-not-be-sequenced table, which carries no
    controls, so the approval page asked a person about every step of an upgrade while saying
    nothing they could answer about the one component that will not survive it. The decision
    "this cluster accepts running keda unsupported at 1.36" is a per-cluster decision, and this
    is the per-cluster approval, so the row is minted here rather than in the planner.

    THE PLAN IS NOT REWRITTEN AND `feasible` STAYS FALSE. This maps a truthful document onto a
    decision surface; it does not edit the document. Approving one of these rows changes
    nothing about whether the plan reaches the target, and `render/html.py` is required to say
    so in the row's own words.

    TWO SHAPES, ONE KIND, AND THE FACT DECIDES WHICH. When the fact records a release strictly
    newer than what is installed the row is a REAL MOVE to it -- keda's `2.17.x -> 2.20.2` --
    because that upgrade exists, is worth taking, and is exactly what an operator will do with a
    component they have decided to keep running unsupported. When it does not, there is nothing
    to move to and the row states the dead end it always stated. Both carry the same
    `UNSUPPORTED_AT_TARGET_KIND`, deliberately: the kind is what makes the row separately
    identifiable to `AUTHORISABLE_KINDS`, to the pill, and to the note above the table, and
    promoting the move-shaped half to `COMPONENT_MOVE` would put a row that does NOT reach the
    target in the same bucket as the rows that do.

    THE WARNING SURVIVES THE MOVE, which is the whole risk of this shape. `2.20.2` in a
    Recommended column beside an Approve button reads as "press this and keda is fine"; it is
    not, and the sentence says so twice -- the series statement first, then "does NOT cover
    Kubernetes 1.36 either" against the very release being moved to.

    NUMBERED AND ORDERED BY `unsupported_at_target`, not here. That function is shared with the
    brief stage, and the reason it has to be is the mis-join documented on
    `UnsupportedAtTarget`.
    """
    rows: list[StepRow] = []
    for record in unsupported_at_target(plan, installed, store):
        name = record.component
        target = plan.target_kubernetes_version
        # THE SENTENCE IS THE ROW'S CONTRACT WITH TWO PARSERS AND A READER.
        # `render/html.py::_step_parts` and `stages/upgrade_approval.py::_step_parts` both
        # split it into (component, installed, recommended, at_kubernetes) -- the second is
        # what a durable `StepDecision` records, so a shape neither can parse degrades the
        # decision trail to "the whole description went in the component field". BOTH shapes
        # deliberately contain neither `(install ` nor the word "upgrade", because those are the
        # literals the two general move patterns search for: the specific patterns are offered
        # first, but a sentence that could ALSO match a general one is one wording change away
        # from filling the columns with the wrong four facts.
        if record.is_move:
            description = (
                f"Unsupported at target: {name} has no recorded version series "
                f"supporting Kubernetes {target}. "
                f"Move {record.installed_series} → {record.newest_release}, the newest "
                f"recorded release, which does NOT cover Kubernetes {target} either. "
                f"Installed {record.installed_version}. "
                f"Approving records that this cluster takes {name} as far as its recorded "
                f"releases go AND accepts running it unsupported at Kubernetes {target}; "
                "it does not make the component reach the target."
            )
        else:
            tail = (
                f"newest recorded release {record.newest_release} does NOT cover target"
                if record.newest_release
                else "no release is recorded"
            )
            description = (
                f"Unsupported at target: {name} has no recorded version series "
                f"supporting Kubernetes {target}. "
                f"Installed {record.installed_version or 'unknown'}; "
                f"{tail}. Approving records that this cluster accepts running "
                f"{name} unsupported at Kubernetes "
                f"{target}; it does not make the "
                "component reach the target."
            )
        rows.append(
            StepRow(
                order=record.order,
                kind=UNSUPPORTED_AT_TARGET_KIND,
                # NOT `REQUIRED` AND NOT `OPTIONAL` EVEN WHEN THERE IS A MOVE. Those two words
                # describe a step that carries the cluster to its target: one you must take, one
                # you may. This row's move does not carry anything to the target -- it takes the
                # component as far as it goes and leaves it unsupported -- so labelling it
                # `REQUIRED` would put it in the same column position, and the same reading, as
                # the steps that do.
                label="UNSUPPORTED",
                description=description,
                install_method=install_methods.get(name),
            )
        )
    return tuple(rows)


def _recommended_rows(
    plan: UpgradePlan, full_versions: dict[str, str] | None = None
) -> tuple[RecommendedRow, ...]:
    """The advisory upgrades, named the same way the component table names them.

    `full_versions` carries the component row's own recommended version, so this list and
    the component table cannot show one component two spellings of one version. See
    `_full_versions` for why the step's typed field is the narrower of the two.
    """
    full_versions = full_versions or {}
    rows = [
        RecommendedRow(
            component=step.component or ABSENT,
            from_series=step.from_series or "installed",
            recommended_version=(
                full_versions.get(step.component or "")
                or step.recommended_version
                or step.to_series
                or ABSENT
            ),
            at_kubernetes=step.at_kubernetes or ABSENT,
        )
        for step in plan.recommended_upgrades
    ]
    rows.sort(key=lambda r: r.component)
    return tuple(rows)


def _totals(
    components: tuple[ComponentRow, ...],
    unresolved: tuple[UnresolvedRow, ...],
    recommended: tuple[RecommendedRow, ...],
    steps: tuple[StepRow, ...],
    ledger: GapLedger | None,
) -> ReportTotals:
    """Every headline number, computed once from the lists above.

    `ReportView`'s validator recomputes all of this and refuses the document on
    any disagreement, so this function cannot silently drift from the lists it
    summarises.
    """
    verdicts = [r.verdict for r in components]
    groups = [r.group for r in components]
    ledger_counts = ledger.counts() if ledger is not None else {}
    return ReportTotals(
        detected=len(components),
        compatible=verdicts.count("COMPATIBLE"),
        upgrade_needed=(
            verdicts.count("INCOMPATIBLE") + verdicts.count("REQUIRES_COMPONENT_UPGRADE")
        ),
        upgrade_recommended=len(recommended),
        unresolved=len(unresolved),
        unresolved_undecided=sum(
            1
            for u in unresolved
            if u.origin in (UnresolvedOrigin.BOTH, UnresolvedOrigin.ASSESSMENT_ONLY)
        ),
        unresolved_unsequenceable_only=sum(
            1 for u in unresolved if u.origin is UnresolvedOrigin.PLAN_ONLY
        ),
        steps=len(steps),
        cluster_upgrades=sum(1 for s in steps if s.kind == "CLUSTER_UPGRADE"),
        component_moves=sum(1 for s in steps if s.kind == "COMPONENT_MOVE"),
        one_way_moves=sum(1 for s in steps if s.one_way),
        managed_addons=groups.count(ComponentGroup.MANAGED_ADDON),
        helm_releases=groups.count(ComponentGroup.HELM_RELEASE),
        other_workloads=groups.count(ComponentGroup.OTHER_WORKLOAD),
        answered_gaps=int(ledger_counts.get("answered", 0)),
        researchable_gaps=int(ledger_counts.get("researchable", 0)),
    )


class DocumentMismatch(ValueError):
    """Raised when the documents handed in do not belong to the same run.

    Rendering a Stage 4 assessment against some other run's Stage 5 plan would
    produce a report whose every digest looks valid while two of them describe
    different evidence. The same check Stage 4 makes against the ledger, applied
    to presentation.
    """


def _require_same_run(
    assessment: ReadinessAssessment,
    plan: UpgradePlan,
    inventory: InventoryReport,
    ledger: GapLedger,
) -> None:
    problems: list[str] = []
    if plan.assessment_digest != assessment.digest():
        problems.append(
            f"the plan cites assessment {plan.assessment_digest} but the "
            f"assessment supplied digests to {assessment.digest()}"
        )
    if ledger.digest() != assessment.ledger_digest:
        problems.append(
            f"the assessment cites ledger {assessment.ledger_digest} but the "
            f"ledger supplied digests to {ledger.digest()}"
        )
    if inventory.digest() != assessment.report_digest:
        problems.append(
            f"the assessment cites inventory {assessment.report_digest} but the "
            f"inventory supplied digests to {inventory.digest()}"
        )
    if problems:
        raise DocumentMismatch(
            "these documents are not from one run, so a report over them would "
            "misattribute its own numbers: " + "; ".join(problems)
        )


def build_report_view(
    assessment: ReadinessAssessment,
    plan: UpgradePlan,
    inventory: InventoryReport,
    ledger: GapLedger | None = None,
    store: FactStore | None = None,
) -> ReportView:
    """Derive the one value set every view renders."""
    components = _component_rows(assessment, inventory, plan, store)
    unresolved = _unresolved_rows(assessment, plan)
    # Derived once from the component rows and handed to both lists, so every surface of
    # the report names a recommended version in exactly one spelling.
    full_versions = _full_versions(components)
    recommended = _recommended_rows(plan, full_versions)
    install_methods = _install_methods(components)
    steps = _step_rows(plan, full_versions, install_methods)
    # APPENDED, NEVER INTERLEAVED, AND THE ARITHMETIC IS THE REASON. A `StepDecision` and the
    # `StepGrant` it is minted from are keyed on `step_order`; renumbering the plan's own steps
    # would leave every decision already recorded against this plan pointing at a different
    # step than the person read.
    #
    # THE NUMBER IS NOT COMPUTED HERE ANY MORE, and that is the fix rather than a tidy-up. It
    # used to be `max(order) + 1` at this call site, which meant the brief stage -- which has to
    # request a brief keyed on the SAME number -- had nowhere to read it from and would have had
    # to repeat the arithmetic. `unsupported_at_target` is now the single source both read, so a
    # brief cannot silently attach to the wrong row or to nothing.
    steps = steps + _unsupported_rows(
        plan, installed_from_assessment(assessment), store, install_methods
    )
    return ReportView(
        cluster=plan.cluster,
        current_kubernetes_version=plan.current_kubernetes_version,
        target_kubernetes_version=plan.target_kubernetes_version,
        hops=tuple(f"{h.current_version}→{h.target_version}" for h in assessment.hops),
        feasible=plan.feasible,
        components=components,
        unresolved=unresolved,
        recommended_upgrades=recommended,
        steps=steps,
        store_errors=(ledger.store_errors if ledger is not None else ()),
        totals=_totals(components, unresolved, recommended, steps, ledger),
    )


def region_of(endpoint: str, fallback: str = "us-east-1") -> str:
    """The region to sign for, taken from the endpoint's own host.

    THE HOST IS AUTHORITATIVE and the caller's environment is not. A Lambda function
    URL is `https://<id>.lambda-url.<region>.on.aws/`, so the region the request must
    be signed for is written into the URL being signed. Reading it from the *rendering*
    process's `AWS_REGION` instead -- which is what this first did -- produces
    permanently unsignable commands the moment the report is rendered anywhere but the
    endpoint's own region, and the failure surfaces as a signature mismatch that names
    neither region.

    Falls back only when the host is not a function URL at all, which is the local and
    test shape.

    >>> region_of("https://abc123.lambda-url.eu-west-1.on.aws/")
    'eu-west-1'
    >>> region_of("https://approve.example.test/", fallback="us-west-2")
    'us-west-2'
    """
    host = urlparse(endpoint).hostname or ""
    parts = host.split(".")
    if len(parts) >= 4 and parts[1] == "lambda-url":
        return parts[2]
    return fallback


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def approvals_from_research(
    research_results: object,
    *,
    endpoint: str | None,
    region: str,
) -> ApprovalControls:
    """The approve / reject surface, read off the research stage's own results.

    The research stage is a Step Functions Map whose per-item results land beside
    the payload, so the presentation stage already receives them and needs no new
    wiring, no new document and no new permission to offer the control.

    THREE RULES, and each one is here rather than in a renderer:

    1. **Only a pending candidate gets a control.** A rejected candidate has no
       usable fact to store and an already-approved one carries an audit record
       that must not be overwritten, so neither is something a person can act on.
    2. **A candidate with no report digest is skipped.** The endpoint refuses an
       approval whose report digest it cannot re-derive, so rendering a control
       without one would render a control that cannot succeed.
    3. **Sorted by component then series.** Map result order is not a guarantee,
       and the rendered bytes must not depend on it.

    Anything malformed is skipped rather than raised on: a report that fails to
    render because one of three research results came back odd is a worse outcome
    than a report that shows two controls and omits the third.

    **THAT POLICY HAS TO COVER THE FINAL CONSTRUCTION TOO**, which at first it did not.
    A duplicate `(component, series)` pair -- or an empty region -- raised
    `ValidationError` straight out of the present handler, and the run emitted NO REPORT
    AT ALL. Skip-not-raise is worth nothing if the last step can still raise. Duplicates
    are therefore dropped here, and the construction below is guarded.

    THE SIGNING REGION COMES FROM THE ENDPOINT, not from the caller. A function URL's
    host carries the region its requests must be signed for, so the URL being signed is
    the authority on it; `region` is the fallback for a host that is not a function URL.
    Taking the rendering process's own region instead produced permanently unsignable
    commands as soon as a report was rendered outside the endpoint's region, failing as
    a signature mismatch that named neither region.
    """
    rows: list[PendingApproval] = []
    seen: set[tuple[str, str]] = set()

    def _text(value) -> str | None:
        """A stated string, or None. Never the literal "None"."""
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _flag(value) -> bool | None:
        """A stated boolean, or None -- and None is not False here.

        "the evidence does not say whether this covers the target" and "it does not
        cover the target" are different findings, and collapsing them would tell an
        approver the second when the truth is the first.
        """
        return value if isinstance(value, bool) else None

    def _cited(value) -> tuple[CitedSource, ...]:
        """The cited-source list, skipping any row missing a field it needs.

        A malformed entry is dropped rather than failing the whole report: the run
        report is the page a person opens to make a decision, and one bad citation
        must not be what stops them seeing the rest.
        """
        out: list[CitedSource] = []
        for item in value or ():
            if not isinstance(item, dict):
                continue
            url, revision, used_for = (
                _text(item.get("url")),
                _text(item.get("revision")),
                _text(item.get("used_for")),
            )
            if url and revision and used_for:
                out.append(CitedSource(url=url, revision=revision, used_for=used_for))
        return tuple(out)

    for entry in research_results if isinstance(research_results, list) else ():
        if not isinstance(entry, dict):
            continue
        if not entry.get("pending_approval"):
            continue
        key = (
            str(entry.get("component_id", "")),
            str(entry.get("version_series", "")),
        )
        if key in seen:
            # Two results for one component and series. The first is kept rather than
            # the last -- arbitrary but deterministic. What must not happen is offering
            # a human two controls for one decision, or raising out of the handler.
            continue
        try:
            rows.append(
                PendingApproval(
                    component=str(entry["component_id"]),
                    version_series=str(entry["version_series"]),
                    candidate_uri=str(entry["candidate_uri"]),
                    candidate_digest=str(entry["candidate_digest"]),
                    report_digest=str(entry["report_digest"]),
                    report_html_uri=(
                        str(entry["report_html_uri"]) if entry.get("report_html_uri") else None
                    ),
                    confidence=str(entry.get("confidence") or ABSENT),
                    change_direction=str(entry.get("change_direction") or ABSENT),
                    headline=str(entry.get("approval_headline") or entry.get("summary") or ABSENT),
                    values_verified=int(entry.get("values_verified") or 0),
                    values_total=int(entry.get("values_total") or 0),
                    # The assessment, copied straight off the research result. `_text`
                    # keeps a missing value as None rather than the string "None", so the
                    # renderer can tell "not stated" from a value.
                    installed_version=_text(entry.get("installed_version")),
                    installed_version_status=_text(entry.get("installed_version_status")),
                    recommended_version=_text(entry.get("recommended_version")),
                    recommended_series=_text(entry.get("recommended_series")),
                    action=_text(entry.get("action")),
                    upgrade_ordering=_text(entry.get("upgrade_ordering")),
                    covers_current_kubernetes=_flag(entry.get("covers_current_kubernetes")),
                    covers_target_kubernetes=_flag(entry.get("covers_target_kubernetes")),
                    machine_confidence=_text(entry.get("machine_confidence")),
                    proposed_confidence=_text(entry.get("proposed_confidence")),
                    evidence_tier=_text(entry.get("evidence_tier")),
                    confidence_rationale=_text(entry.get("confidence_rationale")),
                    extraction_uri=_text(entry.get("extraction_uri")),
                    cited_sources=_cited(entry.get("cited_sources")),
                    caveats=tuple(str(c) for c in (entry.get("caveats") or ()) if str(c).strip()),
                    not_found=tuple(
                        str(c) for c in (entry.get("not_found") or ()) if str(c).strip()
                    ),
                    chart_repository=_text(entry.get("chart_repository")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
        seen.add(key)
    rows.sort(key=lambda p: (p.component, p.version_series))
    signing_region = region_of(endpoint or "", fallback=(region or "us-east-1"))
    try:
        return ApprovalControls(
            endpoint=endpoint or None,
            region=signing_region,
            pending=tuple(rows),
        )
    except ValueError:
        # The last place skip-not-raise could have been broken. A report with no
        # approval section is a worse report; a run with no report at all is a
        # failed run, and the approval surface is not worth one.
        return ApprovalControls(endpoint=endpoint or None, region=signing_region)


def build_document(
    assessment: ReadinessAssessment,
    plan: UpgradePlan,
    inventory: InventoryReport,
    ledger: GapLedger,
    store: FactStore | None = None,
    approvals: ApprovalControls | None = None,
) -> tuple[PresentationDocument, str, str]:
    """The presentation document plus its HTML and Markdown views.

    Returns `(document, html, markdown)`. The document cites the digest of each
    rendered view, which is what brings rendering under the replay guarantee: the
    views are pure functions of `document.view`, so re-deriving the view from
    these same upstream documents and re-rendering it must reproduce the bytes.

    The JSON view is deliberately not returned separately -- it *is*
    `document.canonical_json()`. A hand-built JSON subset can omit the very field
    its own defect is about; making JSON the document
    itself removes the place that could happen.

    `approvals` is a carried input, not a derived one, and it is RECORDED on the
    returned document. Both halves matter: carried, because a pending candidate is
    framed by Stage 6 and this stage must not reframe it; recorded, because the
    rendered bytes depend on it and `verify` re-renders to check them. Omitted, the
    report renders with no control and says plainly that nothing is awaiting
    approval, which is the correct page for a run where research did not go.
    """
    from .render import to_html, to_markdown

    _require_same_run(assessment, plan, inventory, ledger)
    # Carried verbatim from the plan, which already cites the whole chain. Read
    # from one document rather than assembled from four, so the report cannot
    # attribute itself to a mixture of runs.
    provenance = Provenance(
        snapshot_digest=plan.snapshot_digest,
        report_digest=plan.report_digest,
        ledger_digest=plan.ledger_digest,
        assessment_digest=plan.assessment_digest,
        plan_digest=plan.digest(),
        store_digest=plan.store_digest,
    )
    view = build_report_view(assessment, plan, inventory, ledger, store)
    controls = approvals if approvals is not None else ApprovalControls()
    html = to_html(view, provenance)
    markdown = to_markdown(view, provenance, controls)
    document = PresentationDocument(
        approvals=controls,
        cluster=plan.cluster,
        snapshot_digest=provenance.snapshot_digest,
        report_digest=provenance.report_digest,
        ledger_digest=provenance.ledger_digest,
        assessment_digest=provenance.assessment_digest,
        plan_digest=provenance.plan_digest,
        store_digest=provenance.store_digest,
        ruleset_digest=ruleset_digest(),
        renderer_digest=renderer_digest(),
        view=view,
        view_digest=view.digest(),
        html_digest=_sha256(html),
        markdown_digest=_sha256(markdown),
    )
    return document, html, markdown
