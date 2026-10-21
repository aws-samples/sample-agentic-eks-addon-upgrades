"""Gap detection: decide, per component, whether a compatibility fact exists.

Two knowledge sources are consulted, and they are genuinely different in kind:

1. **EKS Insights**, captured into the Stage 1 snapshot. AWS's own assessment of this
   specific cluster, scoped to the next Kubernetes minor. Authoritative and narrow.
2. **The fact store**, a keyed set of `ComponentKnowledge` entries. Gives a version
   *range* per component series, so it can answer hops the insight cannot.

Neither is consulted at verdict time. Insights were frozen into the snapshot at Stage 1
precisely so replay stays honest, and the store
is a plain keyed lookup rather than model-mediated recall for the same reason.

The output is a statement about *knowledge*, never about risk. "No fact" is reported as
no fact; it does not become "probably incompatible". Turning absence into a pessimistic
verdict is the failure this stage exists to prevent.
"""

from __future__ import annotations

from ..inventory.versions import versions_agree
from ..report import InventoryReport
from ..scan import snapshot_digest
from ..snapshot import ClusterSnapshot
from .ledger import FactStatus, GapDetail, GapEntry, GapLedger
from .ruleset import ruleset_digest
from .series import spec_matches_version, version_series
from .store import STORE_UNAVAILABLE, FactStore


class EvidenceMismatch(ValueError):
    """Raised when a report and a snapshot do not belong to each other.

    Building a ledger from a mismatched pair would produce a document whose four
    digests each look valid while describing two different clusters. Refusing is the
    only safe answer.
    """


def _insight_index(
    snapshot: ClusterSnapshot,
) -> dict[str, tuple[str, str | None, tuple[str, ...]]]:
    """Map add-on name -> (insight_id, assessed Kubernetes version, versions).

    Insights are visited in sorted `insight_id` order and the first mention of an
    add-on wins, so the result does not depend on the order AWS happened to return.
    In practice one upgrade-readiness insight covers all managed add-ons.
    """
    index: dict[str, tuple[str, str | None, tuple[str, ...]]] = {}
    for insight in sorted(snapshot.insights, key=lambda i: i.insight_id):
        for addon_name, versions in sorted(insight.addon_compatible_versions.items()):
            index.setdefault(
                addon_name,
                (insight.insight_id, insight.kubernetes_version, tuple(versions)),
            )
    return index


def _entry_for(
    component,  # ComponentReport
    *,
    insights: dict[str, tuple[str, str | None, tuple[str, ...]]],
    store: FactStore,
    store_readable: bool,
) -> GapEntry:
    """Classify one component against both knowledge sources."""
    identity = component.identity
    series = version_series(component.version)

    common = {
        "workload_ref": component.workload_ref,
        "identity": identity,
        "version": component.version,
        "version_series": series,
        "assessment_priority": str(component.assessment_priority),
    }

    # Nothing can be looked up without a name. Checked before the version, because an
    # unnamed component cannot be researched even when its version is perfectly clear.
    if not identity:
        return GapEntry(
            **common,
            status=FactStatus.NOT_ASKABLE,
            detail_code=GapDetail.NO_IDENTITY,
            reason=(
                "No identity was resolved for this workload, so no compatibility "
                "question can be formed. Stage 2's evidence is the place to look."
            ),
        )

    # Both sources are consulted before a status is chosen, so the entry can carry
    # everything known even though the headline reports only the strongest answer.
    insight = insights.get(identity)
    knowledge = store.find(identity) if store_readable else None
    rule = None
    if knowledge and component.version:
        # Match the installed version against each rule's spec — series-contains or
        # exact-release, decided by `spec_matches_version`. Sorted so a store holding
        # more than one matching rule stays deterministic. Keyed on the installed version
        # rather than the derived `series` so a release-keyed fact (`3.2.0`) is found even
        # though `series` is `3.2.x`.
        rule = next(
            (
                r
                for r in sorted(
                    knowledge.compatibility_rules,
                    key=lambda r: r.component_version_series,
                )
                if spec_matches_version(r.component_version_series, component.version)
            ),
            None,
        )

    from_insight = (
        {
            "insight_id": insight[0],
            "insight_kubernetes_version": insight[1],
            "insight_compatible_versions": insight[2],
            # Decided here rather than left to prose. See the field's docstring.
            "insight_lists_installed_version": any(
                versions_agree(component.version, listed) for listed in insight[2]
            ),
        }
        if insight
        else {}
    )
    from_store = (
        {
            "fact_knowledge_base_revision": knowledge.knowledge_base_revision,
            "fact_series": rule.component_version_series,
            "fact_kubernetes_versions": rule.kubernetes_versions,
        }
        if rule and knowledge
        else {}
    )
    sources = {**from_insight, **from_store}

    if insight:
        target = insight[1] or "the next minor"
        # Stated rather than implied. A reader given only "AWS named 9 versions" has to
        # eyeball whether the installed one is among them, and so does a model.
        listed = (
            "The installed version is among them"
            if from_insight.get("insight_lists_installed_version")
            else "The installed version is NOT among them"
        )
        return GapEntry(
            **common,
            **sources,
            status=FactStatus.ANSWERED_BY_CLUSTER,
            detail_code=GapDetail.INSIGHT_MATCH,
            reason=(
                f"AWS assessed this add-on against Kubernetes {target} on this "
                f"cluster and named {len(insight[2])} compatible version(s). "
                f"{listed}. Scoped to that one hop; a stored range is still needed "
                "for planning further ahead."
            ),
        )

    if rule:
        maximum = rule.kubernetes_versions.maximum_exclusive
        window = (
            f"{rule.kubernetes_versions.minimum_inclusive} or later"
            if maximum is None
            else f"{rule.kubernetes_versions.minimum_inclusive} up to "
            f"but excluding {maximum}"
        )
        return GapEntry(
            **common,
            **sources,
            status=FactStatus.FACT_AVAILABLE,
            detail_code=GapDetail.RULE_MATCH,
            reason=(
                f"The fact store holds a rule for series {series}: Kubernetes "
                f"{window}."
            ),
        )

    # From here on nothing answered.
    #
    # The missing version is checked before the store's readability, and the order is
    # load-bearing. Having no version is a property of the component, so it holds
    # whatever state the store is in — a component nobody can name a version for is
    # not researchable even if the store were perfectly readable. Checking the store
    # first would promote those components into the researchable set the moment the
    # store path was wrong, sending someone after a question that cannot be asked.
    if not series:
        return GapEntry(
            **common,
            **sources,
            status=FactStatus.NOT_ASKABLE,
            detail_code=GapDetail.NO_USABLE_VERSION,
            reason=(
                f"'{identity}' has no usable version"
                + (
                    f" ('{component.version}' does not reduce to a series)"
                    if component.version
                    else ""
                )
                + ". Compatibility is published per version series, so there is no "
                "question to research until a version is resolved."
            ),
        )

    if not store_readable:
        return GapEntry(
            **common,
            **sources,
            status=FactStatus.FACT_MISSING,
            detail_code=GapDetail.STORE_UNREADABLE,
            reason=(
                "The fact store could not be read, so it is unknown whether a fact "
                "exists. Treated as missing, but the cause is configuration, not an "
                "absent fact."
            ),
        )

    if knowledge is None:
        return GapEntry(
            **common,
            **sources,
            status=FactStatus.FACT_MISSING,
            detail_code=GapDetail.NO_ENTRY,
            reason=(
                f"The fact store holds no entry for '{identity}'. Identity and "
                f"version series {series} are both known, so this is researchable."
            ),
        )

    known = ", ".join(
        sorted(r.component_version_series for r in knowledge.compatibility_rules)
    )
    return GapEntry(
        **common,
        **sources,
        status=FactStatus.FACT_MISSING,
        detail_code=GapDetail.NO_RULE_FOR_SERIES,
        reason=(
            f"Entry '{knowledge.canonical_id}' ({knowledge.knowledge_base_revision}) "
            f"covers series {known} but not the installed {series}. Extending an "
            "existing entry, not authoring a new one."
        ),
    )


def detect_gaps(
    report: InventoryReport,
    snapshot: ClusterSnapshot,
    store: FactStore,
) -> tuple[GapEntry, ...]:
    """One entry per component in the report, in report order."""
    insights = _insight_index(snapshot)
    store_readable = store.digest() != STORE_UNAVAILABLE
    return tuple(
        _entry_for(
            component,
            insights=insights,
            store=store,
            store_readable=store_readable,
        )
        for component in report.components
    )


def build_ledger(
    report: InventoryReport,
    snapshot: ClusterSnapshot,
    store: FactStore,
) -> GapLedger:
    """Assemble the Stage 3 document.

    The report and snapshot are required to belong to each other. The report cites the
    digest of the snapshot it was built from, so the pairing is checked rather than
    trusted — otherwise one cluster's inventory could be scored against another
    cluster's insights and every digest in the result would still look valid.
    """
    actual = snapshot_digest(snapshot)
    if actual != report.snapshot_digest:
        raise EvidenceMismatch(
            "The report does not belong to this snapshot. Report cites "
            f"{report.snapshot_digest}, snapshot is {actual}."
        )

    return GapLedger(
        cluster=report.cluster,
        kubernetes_version=report.kubernetes_version,
        report_digest=report.digest(),
        snapshot_digest=report.snapshot_digest,
        store_digest=store.digest(),
        ruleset_digest=ruleset_digest(),
        entries=detect_gaps(report, snapshot, store),
        store_errors=tuple(getattr(store, "load_errors", ())),
    )
