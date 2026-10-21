"""An application-version answer, and every legitimate component, pass the version-scheme guard.

AFTER THE CHART-VERSION-IN-APP-LABEL FIX. A Helm component's installed version is now its
APPLICATION version (kyverno 1.13.6, argo-rollouts 1.6.6). The scheme guard must reject ONLY
an answer in a different numbering (the chart's) and nothing sound. Two properties here:

1. A Helm component answered in the SAME numbering as its installed application version
   (installed major present in the rules and releases) is not rejected by the guard -- it is
   judged exactly as it would be with no installed/helm context at all. This covers both
   kyverno 1.13.6 answered in 1.x and argo-rollouts 1.6.6 answered in 1.x.

2. The other components from run first-run-20261012-164414 are unaffected. Their real
   approved facts are keyed to the same major as their installed application version:
   cert-manager 1.13.2 / 1.x, karpenter 1.0.5 / 1.x, keda 2.21.0 / 2.x,
   metrics-server 0.9.0 / 0.x. The guard sees the installed major in the answer and does not
   fire, so passing the installed/helm context changes nothing about the verdict.
"""

from __future__ import annotations

import pytest

from eks_addon_readiness.research.boundary import (
    BoundKind,
    CompatibilityStatementInput,
    ReleaseSightingInput,
    ResearchExtraction,
    SourceRef,
)
from eks_addon_readiness.research.judge import _chart_scheme_mismatch_reason, judge
from eks_addon_readiness.research.pages import PageSet


def _extraction(component: str, series: str, rule_series: str, releases: tuple[str, ...]):
    src = SourceRef(url=f"https://github.com/{component}/{component}", revision="v1")
    return ResearchExtraction(
        found=True,
        component_id=component,
        version_series=series,
        statements=(
            CompatibilityStatementInput(
                version_spec=rule_series,
                kind=BoundKind.PUBLISHED_MINIMUM,
                stated_minimum="1.30",
                quote=f"{component} {rule_series} requires Kubernetes 1.30 or higher",
                source=src,
            ),
        ),
        releases=tuple(
            ReleaseSightingInput(version=v, quote=f"v{v}", source=src) for v in releases
        ),
    )


def test_the_kyverno_application_version_answer_is_not_rejected_by_the_guard() -> None:
    """kyverno installed 1.13.6, answered in application versions 1.x (matching the install)."""
    ex = _extraction("kyverno", "1.13.x", "1.13.x", ("1.13.6", "1.14.0"))
    assert (
        _chart_scheme_mismatch_reason(ex, installed_version="1.13.6", helm_release="kyverno")
        is None
    )
    with_ctx = judge(ex, PageSet(), installed_version="1.13.6", helm_release="kyverno")
    without_ctx = judge(ex, PageSet())
    # The guard did not fire, so the verdict is identical to the context-free judgement.
    assert with_ctx.receipt.decision is without_ctx.receipt.decision
    assert "application version the cluster runs" not in with_ctx.receipt.summary


def test_the_argo_rollouts_application_version_answer_is_not_rejected_by_the_guard() -> None:
    """argo-rollouts installed 1.6.6, answered in application versions 1.x."""
    ex = _extraction("argo-rollouts", "1.6.x", "1.6.x", ("1.6.6", "1.8.0"))
    assert (
        _chart_scheme_mismatch_reason(
            ex, installed_version="1.6.6", helm_release="argo-rollouts"
        )
        is None
    )
    with_ctx = judge(ex, PageSet(), installed_version="1.6.6", helm_release="argo-rollouts")
    without_ctx = judge(ex, PageSet())
    assert with_ctx.receipt.decision is without_ctx.receipt.decision
    assert "application version the cluster runs" not in with_ctx.receipt.summary


# (component, installed_version, rule_series, releases) -- the real shape of each approved
# fact from run first-run-20261012-164414, now keyed on the installed APPLICATION version.
_LEGIT = [
    ("argo-rollouts", "1.6.6", "1.6.x", ("1.6.6", "1.8.0")),
    ("cert-manager", "1.13.2", "1.13.x", ("1.13.2", "1.21.2")),
    ("karpenter", "1.0.5", "1.0.5", ("1.0.5", "1.15.0")),
    ("keda", "2.21.0", "2.21.x", ("2.21.0", "2.20.2")),
    ("kyverno", "1.13.6", "1.13.x", ("1.13.6", "1.14.0")),
    ("metrics-server", "0.9.0", "0.9.x", ("0.9.0",)),
]


@pytest.mark.parametrize("component, installed, rule_series, releases", _LEGIT)
def test_the_legitimate_components_are_unaffected(
    component: str, installed: str, rule_series: str, releases: tuple[str, ...]
) -> None:
    series = installed.rsplit(".", 1)[0] + ".x"
    ex = _extraction(component, series, rule_series, releases)
    # The guard sees the installed major in the rules/releases and does not fire.
    assert (
        _chart_scheme_mismatch_reason(ex, installed_version=installed, helm_release=component)
        is None
    )
    with_ctx = judge(ex, PageSet(), installed_version=installed, helm_release=component)
    without_ctx = judge(ex, PageSet())
    assert with_ctx.receipt.decision is without_ctx.receipt.decision


def test_the_guard_needs_both_a_helm_release_and_an_installed_version() -> None:
    """Either missing means the component is not a Helm install with a known version."""
    # A chart-numbering answer that WOULD be rejected if both inputs were present.
    ex = _extraction("kyverno", "3.9.x", "3.9.x", ("3.9.0", "3.9.1"))
    assert _chart_scheme_mismatch_reason(ex, installed_version="1.13.6", helm_release=None) is None
    assert _chart_scheme_mismatch_reason(ex, installed_version=None, helm_release="kyverno") is None


def test_an_empty_answer_leaves_the_nothing_found_path_alone() -> None:
    """No statements and no releases -> nothing to disagree with, so the guard stays out."""
    ex = ResearchExtraction(
        found=False,
        component_id="kyverno",
        version_series="1.13.x",
        no_compatibility_found=True,
        not_found_reason="no first-party compatibility documentation found",
    )
    assert (
        _chart_scheme_mismatch_reason(ex, installed_version="1.13.6", helm_release="kyverno")
        is None
    )
