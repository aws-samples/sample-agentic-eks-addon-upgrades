"""A chart whose chart version is stamped into `app.kubernetes.io/version` resolves to the image.

THE KYVERNO BUG. The kyverno chart stamps its CHART version into
`app.kubernetes.io/version`: on the reference cluster the label and the `helm.sh/chart`
suffix both read 3.3.9, while all four workloads run `ghcr.io/kyverno/*:v1.13.6`. Stage 2
resolves compatibility in APPLICATION versions, so recording 3.3.9 as the installed version
makes every later stage look up a version space kyverno does not publish a matrix for. The
fix: when the `app.kubernetes.io/version` label DISAGREES with the image tag AND EQUALS the
chart version parsed from `helm.sh/chart`, the image tag (normalized) is the installed
version, with a `LABEL_IS_CHART_VERSION` signal and an honest `IMAGE_TAG` source.

The correction fires in BOTH paths: the per-workload rung (`_resolve_version`) and the
Helm-release fold (`_merge_release`), which is what produced the real
`HelmRelease/kyverno/kyverno` record. argo-rollouts is the control: its label agrees with
its image (v1.6.6) and only the chart differs (2.34.3), so nothing moves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from eks_addon_readiness.inventory.confidence import VersionSource
from eks_addon_readiness.inventory.resolve import _resolve_version, resolve_inventory
from eks_addon_readiness.models import ClusterReference, ImageReference
from eks_addon_readiness.snapshot import (
    ClusterSnapshot,
    SnapshotSource,
    WorkloadContainer,
    WorkloadSnapshot,
)


def _workload(
    name: str,
    *,
    chart: str,
    app_label: str,
    images: tuple[tuple[str, str], ...],
    release: str | None = None,
    namespace: str = "kyverno",
) -> WorkloadSnapshot:
    labels = {
        "app.kubernetes.io/version": app_label,
        "helm.sh/chart": chart,
        "app.kubernetes.io/managed-by": "Helm",
        "app.kubernetes.io/name": name,
    }
    annotations = (
        {
            "meta.helm.sh/release-name": release,
            "meta.helm.sh/release-namespace": namespace,
        }
        if release
        else {}
    )
    return WorkloadSnapshot(
        kind="Deployment",
        namespace=namespace,
        name=name,
        labels=labels,
        helm_annotations=annotations,
        containers=tuple(
            WorkloadContainer(name=f"{name}-{i}", image=ImageReference(repository=repo, tag=tag))
            for i, (repo, tag) in enumerate(images)
        ),
        replicas=1,
        ready_replicas=1,
    )


def _snapshot(workloads: list[WorkloadSnapshot]) -> ClusterSnapshot:
    return ClusterSnapshot(
        snapshot_id=uuid4(),
        captured_at=datetime(2026, 10, 12, tzinfo=UTC),
        source=SnapshotSource.FROZEN_FIXTURE,
        sanitized=True,
        cluster=ClusterReference(name="my-eks-cluster", region="us-east-1"),
        kubernetes_version="1.33",
        workloads=tuple(workloads),
    )


def _record(snapshot: ClusterSnapshot, identity: str):
    return next(r for r in resolve_inventory(snapshot) if r.identity == identity)


# --- the four real kyverno workloads: label/chart 3.3.9, images v1.13.6 ------------------

_KYVERNO_WORKLOADS = [
    _workload(
        wl,
        chart="kyverno-3.3.9",
        app_label="3.3.9",
        images=((f"ghcr.io/kyverno/{wl.split('kyverno-')[-1]}", "v1.13.6"),),
        release="kyverno",
    )
    for wl in (
        "kyverno-admission-controller",
        "kyverno-background-controller",
        "kyverno-cleanup-controller",
        "kyverno-reports-controller",
    )
]


class TestKyverno:
    def test_per_workload_rung_prefers_the_image_tag(self) -> None:
        """Rung 2 on one kyverno workload returns the image version, not the chart label."""
        version, source, signals = _resolve_version(_KYVERNO_WORKLOADS[0], None)
        assert version == "1.13.6"
        assert source is VersionSource.IMAGE_TAG
        assert "LABEL_IS_CHART_VERSION" in {s.code for s in signals}

    def test_the_folded_release_records_the_application_version(self) -> None:
        """The real `HelmRelease/kyverno/kyverno` record: 1.13.6, not the chart 3.3.9."""
        record = _record(_snapshot(_KYVERNO_WORKLOADS), "kyverno")
        assert record.workload_ref == "HelmRelease/kyverno/kyverno"
        assert record.version == "1.13.6"
        assert record.version_source is VersionSource.IMAGE_TAG
        assert "LABEL_IS_CHART_VERSION" in {s.code for s in record.signals}
        # The signal is not duplicated when it already came up the per-workload rung.
        assert [s.code for s in record.signals].count("LABEL_IS_CHART_VERSION") == 1


class TestArgoRolloutsUnchanged:
    def test_a_label_that_agrees_with_the_image_is_untouched(self) -> None:
        """argo-rollouts: label v1.6.6 == image v1.6.6, chart 2.34.3. Stays 1.6.6, no new signal."""
        workload = _workload(
            "argo-rollouts",
            chart="argo-rollouts-2.34.3",
            app_label="v1.6.6",
            images=(("quay.io/argoproj/argo-rollouts", "v1.6.6"),),
            release="argo-rollouts",
            namespace="argo-rollouts",
        )
        record = _record(_snapshot([workload]), "argo-rollouts")
        assert record.version == "1.6.6"
        assert record.version_source is VersionSource.HELM_CHART_LABEL
        assert "LABEL_IS_CHART_VERSION" not in {s.code for s in record.signals}


class TestBehaviourIsUnchangedOutsideTheKyvernoShape:
    def test_label_differs_from_both_image_and_chart_keeps_the_label(self) -> None:
        """label != image AND label != chart: ordinary disagreement, keep the label."""
        workload = _workload(
            "foo",
            chart="foo-9.9.9",
            app_label="2.0.0",
            images=(("repo/foo", "3.0.0"),),
            release="foo",
            namespace="x",
        )
        record = _record(_snapshot([workload]), "foo")
        assert record.version == "2.0.0"
        assert record.version_source is VersionSource.HELM_CHART_LABEL
        assert "LABEL_IS_CHART_VERSION" not in {s.code for s in record.signals}
        assert "LABEL_IMAGE_DISAGREE" in {s.code for s in record.signals}

    def test_an_unusable_image_tag_keeps_the_label(self) -> None:
        """label == chart but the image is `latest`: no usable version to adopt, keep label."""
        workload = _workload(
            "bar",
            chart="bar-3.3.9",
            app_label="3.3.9",
            images=(("repo/bar", "latest"),),
            release="bar",
            namespace="x",
        )
        record = _record(_snapshot([workload]), "bar")
        assert record.version == "3.3.9"
        assert record.version_source is VersionSource.HELM_CHART_LABEL
        assert "LABEL_IS_CHART_VERSION" not in {s.code for s in record.signals}

    def test_containers_that_disagree_keep_the_label(self) -> None:
        """label == chart but two containers carry different versions: ambiguous, keep label."""
        workload = _workload(
            "multi",
            chart="multi-3.3.9",
            app_label="3.3.9",
            images=(("repo/a", "v1.13.6"), ("repo/b", "v1.12.0")),
            release="multi",
            namespace="x",
        )
        version, source, signals = _resolve_version(workload, None)
        assert version == "3.3.9"
        assert source is VersionSource.HELM_CHART_LABEL
        assert "LABEL_IS_CHART_VERSION" not in {s.code for s in signals}
