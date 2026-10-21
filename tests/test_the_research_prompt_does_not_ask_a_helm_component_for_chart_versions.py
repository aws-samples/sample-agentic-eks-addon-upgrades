"""The research prompt does NOT tell a Helm component to answer in chart versions.

THE REVERT. An earlier change told the research agent that a Helm component's INSTALLED
version is the Helm CHART version and that the whole answer (rules, releases, recommended
version) must be returned in chart versions. That was wrong given how the inventory now
resolves: the chart-version-in-app-label fix means a Helm component's installed version is
the APPLICATION version (kyverno 1.13.6, not chart 3.3.9), so a chart-version answer could
never match it. The pipeline resolves compatibility in application versions throughout, so
the prompt must not steer the agent into the chart-version space.

What the prompt STILL does for a Helm component is ask for the chart REPOSITORY URL (the
value the upgrade lane runs `helm repo add` with). That is unrelated to the version scheme
and is unchanged.
"""

from __future__ import annotations

from eks_addon_readiness.research.extract import SYSTEM_PROMPT
from eks_addon_readiness.stages.research_harness import _prompt


def test_the_prompt_does_not_say_installed_is_the_chart_version() -> None:
    assert "installed_version` is then the Helm CHART version" not in SYSTEM_PROMPT
    assert "INSTALLED version you are given is then the Helm CHART version" not in SYSTEM_PROMPT


def test_the_prompt_does_not_require_the_answer_in_chart_versions() -> None:
    # None of the chart-version-scheme instruction may survive.
    assert "answer the whole task in CHART" not in SYSTEM_PROMPT
    assert "chart-to-application mapping" not in SYSTEM_PROMPT
    assert "must be chart versions" not in SYSTEM_PROMPT


def test_the_prompt_still_asks_a_helm_component_for_the_chart_repository_url() -> None:
    # The one chart value the lane needs: the `helm repo add` URL. Unrelated to the scheme.
    assert "THE CHART REPOSITORY (WHEN THE TASK SAYS `helm_release:`)" in SYSTEM_PROMPT
    assert "helm repo add" in SYSTEM_PROMPT


def test_the_task_message_turns_on_the_chart_repository_for_a_helm_component() -> None:
    event = {
        "component_id": "kyverno",
        "version_series": "1.13.x",
        "installed_version": "1.13.6",
        "helm_release": "kyverno",
        "current_kubernetes_version": "1.33",
        "target_kubernetes_version": "1.34",
    }
    message = _prompt(event, "research-kyverno-session-0000000000000")
    assert "helm_release: kyverno" in message
    assert "installed_version: 1.13.6" in message
    # The chart-repository instruction is appended only for a Helm component.
    assert "helm repo add" in message


def test_a_non_helm_component_task_message_has_no_chart_section() -> None:
    event = {
        "component_id": "coredns",
        "version_series": "1.11.x",
        "installed_version": "1.11.1",
        "current_kubernetes_version": "1.33",
        "target_kubernetes_version": "1.34",
    }
    message = _prompt(event, "research-coredns-session-000000000000")
    assert "helm_release:" not in message
    assert "helm repo add" not in message
