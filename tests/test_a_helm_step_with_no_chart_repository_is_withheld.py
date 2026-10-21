"""A Helm upgrade step whose approved fact has no chart repository gets no Approve button.

This is the run8 failure's prevention on the UPGRADE-APPROVAL page: the lane planner skips a
Helm step with no chart repository (`stages/upgrade_report.py`), so offering the control for
it lets a person approve a step certain to be skipped. The page withholds it, using the SAME
provenance the release record hands the lane -- so the page and the lane cannot disagree about
which Helm steps can run.
"""

from __future__ import annotations

from types import SimpleNamespace

from eks_addon_readiness.stages.release_prep import StepProvenance
from eks_addon_readiness.stages.upgrade_approval import (
    _components_without_chart,
    _withheld_until_the_cluster_moves,
)


class _Fact:
    def __init__(self, repo: str | None) -> None:
        self.chart_repository = repo
        self.chart_name = None
        self.releases = ()


class _Store:
    def __init__(self, facts: dict[str, _Fact]) -> None:
        self._facts = facts

    def find(self, identity):
        return self._facts.get(identity or "")


def _component(identity: str, method: str) -> SimpleNamespace:
    # `provenance` reads workload_ref as `HelmRelease/<ns>/<release>` for a Helm component.
    ref = f"HelmRelease/{identity}/{identity}" if method == "HELM" else f"Deployment/kube-system/{identity}"
    return SimpleNamespace(
        identity=identity,
        workload_ref=ref,
        install_method=method,
        namespace=identity if method == "HELM" else "kube-system",
        labels={"k8s-app": identity},
    )


def _inventory(*components: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(components=components)


def test_components_without_chart_names_only_helm_facts_with_no_repository() -> None:
    inventory = _inventory(
        _component("argo-rollouts", "HELM"),
        _component("cert-manager", "HELM"),
        _component("coredns", "EKS_MANAGED_ADDON"),
    )
    store = _Store(
        {
            "argo-rollouts": _Fact(None),
            "cert-manager": _Fact("https://charts.jetstack.io"),
            # A managed add-on needs no chart; even a None fact must not flag it.
            "coredns": _Fact(None),
        }
    )
    assert _components_without_chart(inventory, store) == frozenset({"argo-rollouts"})


def test_the_step_for_a_charts_missing_component_is_withheld_with_a_reason() -> None:
    move = SimpleNamespace(
        order=1,
        kind="COMPONENT_MOVE",
        aws_managed=False,
        description="Move argo-rollouts: 1.6.x → 1.8.5 (install 1.8.5) at Kubernetes 1.35",
    )
    other = SimpleNamespace(
        order=2,
        kind="COMPONENT_MOVE",
        aws_managed=False,
        description="Move cert-manager: 1.20.x → 1.21.2 (install 1.21.2) at Kubernetes 1.35",
    )
    view = SimpleNamespace(
        steps=(move, other),
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
    )
    withheld = _withheld_until_the_cluster_moves(
        view, {}, frozenset({"argo-rollouts"})
    )
    assert 1 in withheld and "chart repository" in withheld[1]
    assert 2 not in withheld


def test_provenance_fields_round_trip_through_step_provenance() -> None:
    # Guard that the attribute `_components_without_chart` reads is the real field name.
    p = StepProvenance(chart_repository=None)
    assert p.chart_repository is None
