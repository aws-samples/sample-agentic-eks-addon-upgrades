"""Turn a snapshot into one record per component: identify, resolve, rate, classify.

A pure function over `ClusterSnapshot`. No cluster access, no network, no model,
no clock. The same snapshot always produces the same inventory, which is what
makes the result replayable and testable without credentials.
"""

from __future__ import annotations

from typing import NamedTuple

from ..models import Confidence
from ..snapshot import ClusterSnapshot, EksManagedAddonSnapshot, WorkloadSnapshot
from .classify import (
    AssessmentPriority,
    Classification,
    Overrides,
    classify,
    declared_names,
)
from .confidence import (
    Signal,
    VersionSource,
    addon_mid_transition,
    addon_workload_not_correlated,
    ambiguous_containers,
    authoritative_source,
    combine,
    digest_pinned_without_tag,
    label_image_disagree,
    label_is_chart_version,
    label_uncorroborated,
    no_version_found,
    sources_agree,
    version_is_placeholder,
    workload_detail_unavailable,
)
from .identity import IdentitySource, ResolvedIdentity, resolve_identity
from .versions import normalize_version, strip_chart_version, versions_agree

VERSION_LABEL = "app.kubernetes.io/version"
CHART_LABEL = "helm.sh/chart"


class ComponentRecord(NamedTuple):
    """One component, with its version, confidence and the reasoning behind both."""

    identity: str | None
    identity_source: IdentitySource
    identity_detail: str
    classification: Classification
    classification_reason: str
    classification_provisional: bool
    assessment_priority: AssessmentPriority
    platform_evidence: tuple[str, ...]
    version: str | None
    version_source: VersionSource
    confidence: Confidence
    signals: tuple[Signal, ...]
    workload_ref: str
    namespace: str | None
    install_method: str
    replicas: int | None
    ready_replicas: int | None
    container_images: tuple[str, ...]
    labels: dict[str, str]
    # Operational state, carried as data rather than folded into `confidence`.
    # Whether a component is healthy is an upgrade-readiness question for a later
    # stage; it is not evidence about which version is installed.
    addon_status: str | None = None
    health_issue_codes: tuple[str, ...] = ()

    @property
    def is_healthy(self) -> bool:
        """True when nothing is reported wrong with this component right now.

        Deliberately separate from `confidence`. Confidence rates the evidence for
        the version number; this reports whether the thing is running properly.
        Conflating them made every degraded add-on look like a weakly-evidenced
        version, which was wrong in both directions.
        """
        return not self.health_issue_codes


def _label_version(workload: WorkloadSnapshot) -> str | None:
    raw = workload.labels.get(VERSION_LABEL, "").strip()
    return raw or None


def _image_tags(workload: WorkloadSnapshot) -> tuple[str, ...]:
    return tuple(c.image.tag for c in workload.containers if c.image.tag)


def _chart_version_label(workload: WorkloadSnapshot) -> str | None:
    """The chart version parsed from the workload's own `helm.sh/chart` label, if any."""
    raw = workload.labels.get(CHART_LABEL, "").strip()
    if not raw:
        return None
    _name, version = strip_chart_version(raw)
    return version


def _image_tag_when_label_is_chart_version(
    label_raw: str,
    tags: tuple[str, ...],
    chart_version: str | None,
) -> str | None:
    """The normalized image tag to use when the chart label was stamped as the app version.

    Returns the usable image version to install INSTEAD of the label, or None to keep the
    label (today's behaviour). All of these must hold:

      * a chart version was parsed from `helm.sh/chart` and the label EQUALS it (so the
        label is the chart version, not the application version — kyverno's 3.3.9 case);
      * the label DISAGREES with the image tag(s) (an agreeing label is already the right
        answer and nothing needs to change);
      * the image tag normalizes to a usable version (not `latest`, not digest-only);
      * for a multi-container workload every tag that carries a version agrees, so there is
        a single unambiguous image version to adopt.

    Argo-rollouts (label v1.6.6, image v1.6.6, chart 2.34.3) never triggers this: its label
    agrees with the image, so the first guard already returns None.
    """
    if chart_version is None:
        return None
    if not versions_agree(label_raw, chart_version):
        return None
    # The label must be the chart version AND differ from what is running.
    usable = [t for t in tags if normalize_version(t) is not None]
    if not usable:
        return None
    normalized = {normalize_version(t) for t in usable}
    if len(normalized) != 1:
        # Containers disagree on their version — no single image version to adopt.
        return None
    image_version = next(iter(normalized))
    assert image_version is not None  # guaranteed by the usable filter
    if versions_agree(label_raw, image_version):
        # Label agrees with the image after all; this is not the chart-in-label case.
        return None
    return image_version


# EKS-managed add-ons whose in-cluster workload is named differently from the add-on
# itself. Kubernetes records no link from a workload back to the add-on that installed it,
# and name-equality alone misses these: the `vpc-cni` add-on runs as the `aws-node`
# DaemonSet, so without this map aws-node is treated as an unknown project and dispatched
# to source research under the wrong identity (and the wrong image-tag version), while the
# real add-on is only recovered as an uncorrelated orphan. Mapping the workload name to the
# add-on name folds the two into one record decided from AWS's catalogue. The other managed
# add-ons (`coredns`, `kube-proxy`, `eks-pod-identity-agent`) already match by name.
#
# Keys are the names a workload declares about itself (identity / app.kubernetes.io labels);
# values are the EKS add-on name AWS uses in `describe-addon-versions`.
_ADDON_WORKLOAD_ALIASES: dict[str, str] = {
    "aws-node": "vpc-cni",
    "amazon-vpc-cni": "vpc-cni",
    "aws-vpc-cni": "vpc-cni",
}


def _matching_addon(
    workload: WorkloadSnapshot,
    identity: str | None,
    snapshot: ClusterSnapshot,
) -> EksManagedAddonSnapshot | None:
    """Find the EKS-managed add-on a workload belongs to, if any.

    Kubernetes records no link from a workload back to the add-on that installed
    it, so the workload's own declared names are matched against add-on names —
    directly, and through `_ADDON_WORKLOAD_ALIASES` for add-ons whose workload
    carries a different name (notably `vpc-cni`, which runs as `aws-node`).
    """
    names = declared_names(workload, identity)
    if not names:
        return None
    # Direct match first: the add-on name appears among the workload's own names.
    direct = next((a for a in snapshot.eks_addons if a.addon_name in names), None)
    if direct is not None:
        return direct
    # Aliased match: a known workload name maps to a differently-named add-on.
    aliased_targets = {
        _ADDON_WORKLOAD_ALIASES[n] for n in names if n in _ADDON_WORKLOAD_ALIASES
    }
    if not aliased_targets:
        return None
    return next((a for a in snapshot.eks_addons if a.addon_name in aliased_targets), None)


def _resolve_version(
    workload: WorkloadSnapshot,
    addon: EksManagedAddonSnapshot | None,
) -> tuple[str | None, VersionSource, list[Signal]]:
    """Pick the version from the most authoritative usable source.

    Signals are accumulated rather than short-circuited: a placeholder in a
    lower-authority source is still worth reporting even when a better source
    answered.
    """
    signals: list[Signal] = []

    label_raw = _label_version(workload)
    tags = _image_tags(workload)
    image_raw = tags[0] if len(tags) == 1 else None

    # Rung 1 — the EKS control plane.
    if addon is not None and addon.addon_version:
        version = normalize_version(addon.addon_version)
        if version:
            signals.append(authoritative_source(VersionSource.EKS_ADDON_API, version))
            # Corroborate against an image tag if one matches.
            for tag in tags:
                if versions_agree(addon.addon_version, tag):
                    signals.append(
                        sources_agree(
                            VersionSource.EKS_ADDON_API, VersionSource.IMAGE_TAG, version
                        )
                    )
                    break
            return version, VersionSource.EKS_ADDON_API, signals
        signals.append(
            version_is_placeholder(addon.addon_version, VersionSource.EKS_ADDON_API)
        )

    # Rung 2 — the Helm chart label.
    if label_raw:
        version = normalize_version(label_raw)
        if version:
            chart_version = _chart_version_label(workload)
            image_version = _image_tag_when_label_is_chart_version(
                label_raw, tags, chart_version
            )
            if image_version is not None:
                # The label carried the CHART version, not the application version. The
                # pipeline resolves in application versions, so the image tag is the honest
                # installed version. kyverno: label/chart 3.3.9, image v1.13.6 -> 1.13.6.
                signals.append(
                    label_is_chart_version(label_raw, chart_version or label_raw, image_version)
                )
                return image_version, VersionSource.IMAGE_TAG, signals
            if image_raw and versions_agree(label_raw, image_raw):
                signals.append(
                    sources_agree(
                        VersionSource.HELM_CHART_LABEL, VersionSource.IMAGE_TAG, version
                    )
                )
            elif image_raw:
                signals.append(label_image_disagree(label_raw, image_raw))
            elif any(versions_agree(label_raw, tag) for tag in tags):
                # Several containers, one of which matches. Enough to rule out
                # drift in the container that carries the component itself.
                signals.append(
                    sources_agree(
                        VersionSource.HELM_CHART_LABEL, VersionSource.IMAGE_TAG, version
                    )
                )
            elif workload.detail_unavailable:
                signals.append(workload_detail_unavailable())
            else:
                signals.append(
                    label_uncorroborated(label_raw, _no_corroboration_reason(workload, tags))
                )
            return version, VersionSource.HELM_CHART_LABEL, signals
        signals.append(version_is_placeholder(label_raw, VersionSource.HELM_CHART_LABEL))

    # Rung 3 — the container image tag, last resort.
    if len(workload.containers) > 1 and len({t for t in tags}) > 1:
        signals.append(ambiguous_containers(len(workload.containers)))
        return None, VersionSource.NONE, signals

    if image_raw:
        version = normalize_version(image_raw)
        if version:
            return version, VersionSource.IMAGE_TAG, signals
        signals.append(version_is_placeholder(image_raw, VersionSource.IMAGE_TAG))
        return None, VersionSource.NONE, signals

    single_digest_only = (
        len(workload.containers) == 1
        and workload.containers[0].image.tag is None
        and workload.containers[0].image.digest is not None
    )
    if single_digest_only:
        signals.append(
            digest_pinned_without_tag(workload.containers[0].image.repository)
        )
        return None, VersionSource.NONE, signals

    if workload.detail_unavailable:
        signals.append(workload_detail_unavailable())

    signals.append(no_version_found())
    return None, VersionSource.NONE, signals


def _no_corroboration_reason(workload: WorkloadSnapshot, tags: tuple[str, ...]) -> str:
    """Why no image tag could confirm a chart label."""
    if not tags:
        return "no container carries a tag"
    if len(tags) > 1:
        return f"{len(workload.containers)} containers with differing tags: " + ", ".join(
            sorted(set(tags))
        )
    return f"the only image tag is {tags[0]}"


# EKS add-on statuses during which pods may be running a mix of versions. Note
# that DEGRADED is deliberately absent: a degraded add-on is not mid-change, it is
# running the version EKS reports and simply has fewer replicas than it wants.
TRANSITION_STATUSES = frozenset({"CREATING", "UPDATING", "DELETING"})


def _is_mid_transition(addon: EksManagedAddonSnapshot) -> bool:
    return (addon.status or "").strip().upper() in TRANSITION_STATUSES


def _install_method(workload: WorkloadSnapshot, addon: EksManagedAddonSnapshot | None) -> str:
    if addon is not None:
        return "EKS_MANAGED_ADDON"
    if workload.helm_annotations.get("meta.helm.sh/release-name"):
        return "HELM"
    return "MANIFEST"


def _release_key(workload: WorkloadSnapshot) -> tuple[str, str] | None:
    """The (namespace, release-name) a workload belongs to, or None if not Helm-managed.

    Read from the standard Helm provenance annotations. A workload with no release
    annotation is not Helm-managed and is reported on its own.
    """
    name = workload.helm_annotations.get("meta.helm.sh/release-name")
    if not name:
        return None
    namespace = (
        workload.helm_annotations.get("meta.helm.sh/release-namespace")
        or workload.namespace
    )
    if not namespace:
        return None
    return (namespace, name)


def resolve_inventory(
    snapshot: ClusterSnapshot,
    *,
    overrides: Overrides | None = None,
) -> tuple[ComponentRecord, ...]:
    """Produce one record per component: one per EKS add-on, one per Helm release, and
    one per bare (non-Helm) workload.

    A Helm release is a single component even when it renders several workloads — KEDA's
    operator, admission webhook and metrics apiserver are three Deployments of one chart,
    and the thing you upgrade is the chart, not each Deployment. Per-workload records are
    resolved first (reusing all the identity/version/classification logic), then the ones
    sharing a Helm release are folded into one record keyed on the chart. EKS-managed
    add-ons are already one object each and are untouched.

    Ordering is deterministic so two runs over the same snapshot are identical.
    """
    records: list[ComponentRecord] = []
    correlated_addons: set[str] = set()
    # Per-workload record -> its Helm release key, for the fold step below. A record built
    # from an EKS-add-on-correlated workload is left ungrouped: the add-on is the unit.
    record_release: dict[int, tuple[str, str]] = {}

    for workload in snapshot.workloads:
        identity = resolve_identity(workload)
        addon = _matching_addon(workload, identity.name, snapshot)
        if addon is not None:
            correlated_addons.add(addon.addon_name)
            # The EKS API outranks every label rung: AWS names the add-on it
            # installed. This matters for `coredns` and `kube-proxy`, which carry
            # no `app.kubernetes.io/name` label at all, so without this their
            # identity would rest on an image repository basename.
            identity = ResolvedIdentity(
                addon.addon_name,
                IdentitySource.EKS_ADDON_API,
                f"EKS-managed add-on {addon.addon_name}, correlated to "
                f"{workload.kind}/{workload.name}",
            )

        version, source, signals = _resolve_version(workload, addon)

        # Only a transition makes the reported version ambiguous. A health issue
        # does not; see `addon_mid_transition`.
        if addon is not None and _is_mid_transition(addon):
            signals.append(addon_mid_transition(addon.status or "UNKNOWN"))

        if version is None and not any(
            s.code in {"NO_VERSION_FOUND", "VERSION_IS_PLACEHOLDER",
                       "IMAGE_DIGEST_ONLY", "MULTIPLE_CONTAINERS_AMBIGUOUS"}
            for s in signals
        ):
            signals.append(no_version_found())

        classification = classify(
            workload,
            identity.name,
            snapshot,
            identity_source=identity.source,
            overrides=overrides,
        )

        record = ComponentRecord(
            identity=identity.name,
            identity_source=identity.source,
            identity_detail=identity.detail,
            classification=classification.classification,
            classification_reason=classification.reason,
            classification_provisional=classification.provisional,
            assessment_priority=classification.priority,
            platform_evidence=classification.evidence,
            version=version,
            version_source=source,
            confidence=combine(source, tuple(signals)),
            signals=tuple(signals),
            workload_ref=f"{workload.kind}/{workload.namespace or '-'}/{workload.name}",
            namespace=workload.namespace,
            install_method=_install_method(workload, addon),
            replicas=workload.replicas,
            ready_replicas=workload.ready_replicas,
            container_images=tuple(
                sorted(
                    c.image.repository + (f":{c.image.tag}" if c.image.tag else "")
                    for c in workload.containers
                )
            ),
            labels=dict(sorted(workload.labels.items())),
            addon_status=addon.status if addon is not None else None,
            health_issue_codes=(
                addon.health_issue_codes if addon is not None else ()
            ),
        )
        records.append(record)
        # Only fold non-add-on workloads by Helm release; an add-on is its own unit.
        if addon is None:
            key = _release_key(workload)
            if key is not None:
                record_release[id(record)] = key

    records = _fold_helm_releases(records, record_release, snapshot)
    records.extend(_uncorrelated_addon_records(snapshot, correlated_addons))

    return tuple(
        sorted(records, key=lambda r: (r.classification, r.identity or "~", r.workload_ref))
    )


def _fold_helm_releases(
    records: list[ComponentRecord],
    record_release: dict[int, tuple[str, str]],
    snapshot: ClusterSnapshot,
) -> list[ComponentRecord]:
    """Merge per-workload records that belong to the same Helm release into one.

    The chart is the component. A release's several workloads (KEDA's operator, admission
    webhook, metrics apiserver) become one record: identity is the chart name, version is
    the chart version, and the member workloads' containers and health are aggregated. A
    record with no release key (bare workload) passes through unchanged.

    The release's chart name/version come from `derive_helm_releases`, which reads the
    `helm.sh/chart` provenance label — the same source the per-workload rung used, now
    read once per release rather than per workload.
    """
    releases_by_key = {
        (r.namespace, r.release_name): r for r in _derived_releases(snapshot)
    }

    passthrough: list[ComponentRecord] = []
    grouped: dict[tuple[str, str], list[ComponentRecord]] = {}
    for record in records:
        key = record_release.get(id(record))
        if key is None or key not in releases_by_key:
            passthrough.append(record)
            continue
        grouped.setdefault(key, []).append(record)

    folded: list[ComponentRecord] = []
    for key, members in grouped.items():
        folded.append(_merge_release(releases_by_key[key], members))

    return passthrough + folded


def _derived_releases(snapshot: ClusterSnapshot):
    """The Helm releases in this snapshot, grouped by (namespace, release_name).

    Prefers releases already on the snapshot; falls back to deriving them from the
    workloads' provenance labels when a snapshot did not carry them.
    """
    if snapshot.helm_releases:
        return snapshot.helm_releases
    from ..scan.helm_labels import derive_helm_releases

    workloads = [
        {
            "_kind": w.kind,
            "name": w.name,
            "namespace": w.namespace,
            "labels": dict(w.labels),
            "annotations": dict(w.helm_annotations),
        }
        for w in snapshot.workloads
    ]
    return derive_helm_releases(workloads)


class _ChartInAppLabel(NamedTuple):
    """The usable application version to adopt when the app-version label is the chart version."""

    version: str


def _member_image_versions(members: list[ComponentRecord]) -> set[str]:
    """Every usable normalized image version across a release's member workloads.

    Read off `container_images` ("repo:tag"), so it reflects what is actually running
    regardless of which rung each member resolved on. Placeholder and digest-only images
    contribute nothing.
    """
    versions: set[str] = set()
    for member in members:
        for image in member.container_images:
            _repo, sep, tag = image.rpartition(":")
            if not sep or not tag:
                continue
            normalized = normalize_version(tag)
            if normalized is not None:
                versions.add(normalized)
    return versions


def _release_app_label_is_chart_version(
    app_version: str | None,
    chart_version: str | None,
    members: list[ComponentRecord],
) -> _ChartInAppLabel | None:
    """Detect a release whose `app.kubernetes.io/version` label carried the CHART version.

    kyverno is the case: the chart stamps its chart version (3.3.9) into
    `app.kubernetes.io/version`, so `derive_helm_releases` reads `application_version` ==
    `chart_version` == 3.3.9, while every workload image is v1.13.6. Returns the single
    usable image version to install instead, or None to keep today's behaviour. All must
    hold:

      * both an app version and a chart version are present and EQUAL (the app-version
        label is the chart version, not the application version);
      * the member images carry exactly one usable version, so there is an unambiguous
        application version to adopt;
      * that image version DISAGREES with the chart version (an agreeing one is already
        the right answer, as with argo-rollouts: label v1.6.6, image v1.6.6, chart 2.34.3).
    """
    if not app_version or not chart_version:
        return None
    if not versions_agree(app_version, chart_version):
        return None
    image_versions = _member_image_versions(members)
    if len(image_versions) != 1:
        return None
    image_version = next(iter(image_versions))
    if versions_agree(chart_version, image_version):
        return None
    return _ChartInAppLabel(image_version)


def _merge_release(release, members: list[ComponentRecord]) -> ComponentRecord:
    """Fold a release's member records into one, keyed on the chart.

    Identity is the chart name; version is the chart version (what you upgrade). The
    strongest classification/priority across members is kept (a release is as
    platform-critical as its most critical workload), health issues are unioned, and
    container images are merged. Confidence carries over from the member the chart
    version was evidenced on — the chart label is one release-level fact.
    """
    # Deterministic member order for stable aggregation.
    members = sorted(members, key=lambda r: r.workload_ref)
    priority = min((m.assessment_priority for m in members), key=_priority_rank)
    classification = min((m.classification for m in members), key=_classification_rank)
    # The representative member: highest priority, then first by ref. Its confidence and
    # signals describe the version evidence we keep.
    representative = min(
        members, key=lambda m: (_priority_rank(m.assessment_priority), m.workload_ref)
    )

    # Version for COMPATIBILITY is the application version (what runs against the
    # Kubernetes API and what the component's compatibility matrix is keyed on), not the
    # Helm chart version. For metrics-server the chart is 3.11.0 but the app is 0.6.4, and
    # the matrix speaks of 0.6.x — using the chart version would look up a series that has
    # no compatibility matrix. Fall back to the chart version, then to the representative
    # workload's resolved version, when no app version is recorded.
    app_version = getattr(release, "application_version", None)
    chart_version = release.chart_version

    # CHART VERSION STAMPED INTO THE APP-VERSION LABEL. Some charts (kyverno) put their
    # chart version into `app.kubernetes.io/version`, so `application_version` IS the chart
    # version, not the application version. Here `app_version == chart_version` and the
    # running images disagree with both, while the images themselves agree on one usable
    # version. The pipeline resolves in application versions, so the image version is the
    # honest installed version — the same correction the per-workload rung makes, applied
    # at the release level that produced kyverno's HelmRelease/kyverno/kyverno record.
    chart_in_app_label = _release_app_label_is_chart_version(
        app_version, chart_version, members
    )
    if chart_in_app_label is not None:
        version = chart_in_app_label.version
        source = VersionSource.IMAGE_TAG
        chart_signal: tuple[Signal, ...] = (
            label_is_chart_version(
                app_version or "",
                chart_version or "",
                chart_in_app_label.version,
            ),
        )
    else:
        chosen_raw = app_version or chart_version
        version = normalize_version(chosen_raw) if chosen_raw else representative.version
        source = (
            VersionSource.HELM_CHART_LABEL if chosen_raw else representative.version_source
        )
        chart_signal = ()

    images = tuple(sorted({img for m in members for img in m.container_images}))
    health = tuple(sorted({code for m in members for code in m.health_issue_codes}))
    evidence = tuple(sorted({e for m in members for e in m.platform_evidence}))
    ready = sum(m.ready_replicas or 0 for m in members)
    total = sum(m.replicas or 0 for m in members)

    return representative._replace(
        identity=release.chart_name,
        identity_source=IdentitySource.CHART_LABEL,
        identity_detail=(
            f"Helm release {release.release_name} in {release.namespace}, chart "
            f"{release.chart_name}"
            + (f" {chart_version}" if chart_version else "")
            + (
                f" (app {app_version})"
                if app_version and app_version != chart_version and chart_in_app_label is None
                else ""
            )
            + (
                f" (chart version in app label; installed app version {version})"
                if chart_in_app_label is not None
                else ""
            )
            + f", from {len(members)} workload(s)"
        ),
        classification=classification,
        assessment_priority=priority,
        platform_evidence=evidence,
        version=version,
        version_source=source,
        signals=representative.signals
        + tuple(s for s in chart_signal if s.code not in {sig.code for sig in representative.signals}),
        workload_ref=f"HelmRelease/{release.namespace}/{release.release_name}",
        namespace=release.namespace,
        install_method="HELM",
        replicas=total or None,
        ready_replicas=ready or None,
        container_images=images,
        health_issue_codes=health,
    )


def _priority_rank(priority) -> int:
    order = {AssessmentPriority.HIGH: 0, AssessmentPriority.MEDIUM: 1, AssessmentPriority.LOW: 2}
    return order.get(priority, 3)


def _classification_rank(classification) -> int:
    order = {
        Classification.RECOGNIZED: 0,
        Classification.UNRECOGNIZED: 1,
        Classification.CUSTOM_WORKLOAD: 2,
    }
    return order.get(classification, 3)


def _uncorrelated_addon_records(
    snapshot: ClusterSnapshot, correlated: set[str]
) -> list[ComponentRecord]:
    """Records for EKS-managed add-ons that no workload could be matched to.

    An add-on whose workload does not declare the add-on's name — `vpc-cni` runs
    as `aws-node` — would otherwise be dropped, discarding the most authoritative
    version evidence in the whole snapshot. It is reported instead, with the
    failed correlation stated so the reader knows the running workload was not
    identified.
    """
    records: list[ComponentRecord] = []

    for addon in snapshot.eks_addons:
        if addon.addon_name in correlated:
            continue

        signals: list[Signal] = [addon_workload_not_correlated(addon.addon_name)]
        version = normalize_version(addon.addon_version)
        if version:
            signals.append(authoritative_source(VersionSource.EKS_ADDON_API, version))
            source = VersionSource.EKS_ADDON_API
        else:
            if addon.addon_version:
                signals.append(
                    version_is_placeholder(addon.addon_version, VersionSource.EKS_ADDON_API)
                )
            else:
                signals.append(no_version_found())
            source = VersionSource.NONE
        if _is_mid_transition(addon):
            signals.append(addon_mid_transition(addon.status or "UNKNOWN"))

        records.append(
            ComponentRecord(
                identity=addon.addon_name,
                identity_source=IdentitySource.EKS_ADDON_API,
                identity_detail=f"EKS-managed add-on {addon.addon_name}",
                classification=Classification.RECOGNIZED,
                classification_reason=f"{addon.addon_name} is an EKS-managed add-on",
                classification_provisional=False,
                assessment_priority=AssessmentPriority.HIGH,
                platform_evidence=("EKS_MANAGED_ADDON",),
                version=version,
                version_source=source,
                confidence=combine(source, tuple(signals)),
                signals=tuple(signals),
                workload_ref=f"EksAddon/{addon.namespace or '-'}/{addon.addon_name}",
                namespace=addon.namespace,
                install_method="EKS_MANAGED_ADDON",
                replicas=None,
                ready_replicas=None,
                container_images=(),
                labels={},
                addon_status=addon.status,
                health_issue_codes=addon.health_issue_codes,
            )
        )

    return records
