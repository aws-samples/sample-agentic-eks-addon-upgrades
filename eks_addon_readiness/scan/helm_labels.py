"""Derive Helm release identity from chart provenance labels on workloads.

Helm 3 records release state in a Kubernetes Secret, which this system
deliberately never reads: `AmazonEKSViewPolicy` denies Secrets, and the EKS MCP
Server redacts sensitive values regardless. Helm also stamps standard labels and
annotations onto every resource it renders, and those are readable, unredacted,
and sufficient to identify a release and its versions.

Everything in this module is a pure function over already-fetched data, so it is
testable without a cluster.

Labels and annotations used, all part of the documented Helm/Kubernetes
conventions:

    annotation  meta.helm.sh/release-name       release identity
    annotation  meta.helm.sh/release-namespace  release identity
    label       helm.sh/chart                   "<chart-name>-<chart-version>"
    label       app.kubernetes.io/version       application (appVersion)
    label       app.kubernetes.io/managed-by    "Helm" (corroboration only)
"""

from __future__ import annotations

import re
from typing import Any

from ..snapshot import HelmReleaseSnapshot

RELEASE_NAME_ANNOTATION = "meta.helm.sh/release-name"
RELEASE_NAMESPACE_ANNOTATION = "meta.helm.sh/release-namespace"
CHART_LABEL = "helm.sh/chart"
APP_VERSION_LABEL = "app.kubernetes.io/version"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"

# Helm's chart label is "<name>-<version>". The name may itself contain hyphens
# and the version may carry a prerelease suffix that also contains hyphens
# (for example "cognitoidentity-chart-0.0.0-non-release-version"), so the split
# anchors on the first segment that looks like the start of a version.
#
# `v?\d` and not `\d`. A leading `v` on a chart version is ordinary -- cert-manager
# publishes every chart as `cert-manager-v1.13.2` -- and anchoring on a bare digit
# left the whole label as the NAME. That name becomes the component's `identity`
# (`inventory/resolve.py` `_merge_release`), and `identity` is the fact-store lookup
# key, so cert-manager resolved as `cert-manager-v1.13.2` and a fact authored for
# `cert-manager` could never match it. Approving one would have changed nothing,
# which is worse than an honest gap because it looks like it should have worked.
#
# The `\d` after the optional `v` is what keeps this from over-correcting: a name
# ending in `v`, or a segment like `velero`, has no digit and so does not split.
#
# NOTE the asymmetry. `inventory/versions.py:strip_chart_version` carries the same
# blind pattern and is DIGEST-FROZEN -- its source is hashed into Stage 2's ruleset
# digest, so changing it would break byte-identical replay against every document
# produced so far. It feeds the per-workload identity rung, which the release-level
# fold overwrites for any Helm-managed workload, so the residual exposure is a
# workload carrying a chart label but no Helm release annotation. Pinned in
# tests/scan/test_chart_label_v_prefix.py rather than left implicit.
_CHART_LABEL_PATTERN = re.compile(r"^(?P<name>.+?)-(?P<version>v?\d[^\s]*)$")


def split_chart_label(chart_label: str) -> tuple[str, str | None]:
    """Split a `helm.sh/chart` value into chart name and chart version.

    Returns the whole value as the name and `None` as the version when no
    version-shaped suffix is present, rather than guessing.

    >>> split_chart_label("cert-manager-v1.20.0")
    ('cert-manager', 'v1.20.0')
    >>> split_chart_label("cert-manager-1.20.0")
    ('cert-manager', '1.20.0')
    >>> split_chart_label("cognitoidentity-chart-0.0.0-non-release-version")
    ('cognitoidentity-chart', '0.0.0-non-release-version')
    >>> split_chart_label("standalone")
    ('standalone', None)
    >>> split_chart_label("backup-velero")
    ('backup-velero', None)
    """
    match = _CHART_LABEL_PATTERN.match(chart_label.strip())
    if not match:
        return chart_label.strip(), None
    return match.group("name"), match.group("version")


def _workload_ref(workload: dict[str, Any]) -> str:
    kind = workload.get("_kind") or "Workload"
    namespace = workload.get("namespace") or "-"
    name = workload.get("name") or "-"
    return f"{kind}/{namespace}/{name}"


def derive_helm_releases(
    workloads: list[dict[str, Any]],
) -> tuple[HelmReleaseSnapshot, ...]:
    """Group Helm-managed workloads into one release per (name, namespace).

    Workloads without Helm release annotations are ignored: they were not
    installed by Helm and no version claim can honestly be made about them from
    chart provenance.

    Ordering is deterministic (by namespace then release name) so that a snapshot
    built twice from the same input is byte-identical.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for workload in workloads:
        annotations = workload.get("annotations") or {}
        labels = workload.get("labels") or {}

        release_name = annotations.get(RELEASE_NAME_ANNOTATION)
        release_namespace = (
            annotations.get(RELEASE_NAMESPACE_ANNOTATION) or workload.get("namespace")
        )
        if not release_name or not release_namespace:
            continue

        key = (release_namespace, release_name)
        entry = grouped.setdefault(
            key,
            {
                "chart_name": None,
                "chart_version": None,
                "application_version": None,
                "workloads": [],
                "managed_by_helm": False,
            },
        )

        entry["workloads"].append(_workload_ref(workload))

        if labels.get(MANAGED_BY_LABEL) == "Helm":
            entry["managed_by_helm"] = True

        chart_label = labels.get(CHART_LABEL)
        if chart_label and entry["chart_name"] is None:
            chart_name, chart_version = split_chart_label(chart_label)
            entry["chart_name"] = chart_name
            entry["chart_version"] = chart_version

        app_version = labels.get(APP_VERSION_LABEL)
        if app_version and entry["application_version"] is None:
            entry["application_version"] = app_version

    releases: list[HelmReleaseSnapshot] = []
    for (namespace, release_name), entry in sorted(grouped.items()):
        releases.append(
            HelmReleaseSnapshot(
                release_name=release_name,
                namespace=namespace,
                # Fall back to the release name when no chart label was found:
                # the release provably exists, only its chart name is unknown.
                chart_name=entry["chart_name"] or release_name,
                chart_version=entry["chart_version"],
                application_version=entry["application_version"],
                derived_from_workloads=tuple(sorted(entry["workloads"])),
            )
        )
    return tuple(releases)
