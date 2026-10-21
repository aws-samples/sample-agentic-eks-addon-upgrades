"""A Helm component answered in the chart numbering, not the installed numbering, is rejected.

AFTER THE CHART-VERSION-IN-APP-LABEL FIX. The inventory now records a Helm component by its
APPLICATION version (kyverno installs as 1.13.6, argo-rollouts as 1.6.6), not its chart
version. Decide matches a research fact against that installed version, so an answer whose
compatibility rules and releases are entirely in the CHART numbering -- kyverno chart 3.x,
argo-rollouts chart 2.x -- can never describe the install: Decide finds no rule for the
installed major and scores the component INCOMPATIBLE with no brief.

The judge REJECTS that answer -- a Helm component whose installed MAJOR appears in neither
any rule's series nor any release -- with a scheme-neutral reason the harness re-asks on,
telling the agent to restate the fact in the installed (application) version's numbering.
Both real cases are modelled here: kyverno installed 1.13.6 answered in 3.x, and
argo-rollouts installed 1.6.6 answered in 2.x.
"""

from __future__ import annotations

from eks_addon_readiness.research.boundary import (
    BoundKind,
    CompatibilityStatementInput,
    ReleaseSightingInput,
    ResearchExtraction,
    SourceRef,
)
from eks_addon_readiness.research.judge import judge
from eks_addon_readiness.research.pages import PageSet
from eks_addon_readiness.research.receipt import AcceptanceDecision


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


def test_the_kyverno_chart_version_answer_is_rejected() -> None:
    """kyverno installed 1.13.6 (application), answered in chart versions 3.9.x -> REJECT."""
    ex = _extraction("kyverno", "3.9.x", "3.9.x", ("3.9.0", "3.9.1"))
    candidate = judge(ex, PageSet(), installed_version="1.13.6", helm_release="kyverno")
    assert candidate.receipt.decision is AcceptanceDecision.REJECTED
    assert candidate.knowledge is None


def test_the_argo_rollouts_chart_version_answer_is_rejected() -> None:
    """argo-rollouts installed 1.6.6 (application), answered in chart versions 2.43.x -> REJECT."""
    ex = _extraction("argo-rollouts", "2.43.x", "2.43.x", ("2.43.6", "2.43.0"))
    candidate = judge(ex, PageSet(), installed_version="1.6.6", helm_release="argo-rollouts")
    assert candidate.receipt.decision is AcceptanceDecision.REJECTED
    assert candidate.knowledge is None


def test_the_rejection_reason_is_scheme_neutral_and_actionable() -> None:
    """The re-ask text names the installed version and says to restate in its numbering.

    It must NOT assert that the installed version is the chart version -- that was the old,
    now-wrong framing. The installed version is the application version the cluster runs.
    """
    ex = _extraction("kyverno", "3.9.x", "3.9.x", ("3.9.0", "3.9.1"))
    summary = judge(ex, PageSet(), installed_version="1.13.6", helm_release="kyverno").receipt.summary
    assert "1.13.6" in summary
    assert "application version the cluster runs" in summary
    assert "SAME numbering" in summary
    # The old chart-centric wording must be gone.
    assert "is the Helm CHART version" not in summary
    assert "chart-to-application" not in summary


def test_a_managed_addon_with_no_helm_release_is_not_touched() -> None:
    """The guard only runs for a Helm component. Without helm_release it never fires."""
    ex = _extraction("kyverno", "3.9.x", "3.9.x", ("3.9.0", "3.9.1"))
    reasonless = judge(ex, PageSet(), installed_version="1.13.6", helm_release=None)
    # No helm_release -> the scheme guard is skipped and the extraction follows the ordinary
    # path (which rejects here for its own reason: an empty PageSet verifies no quote). The
    # point is only that the version-scheme reason is NOT what rejected it.
    assert "application version the cluster runs" not in reasonless.receipt.summary
