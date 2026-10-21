"""The chart a Helm component's lane installs from: found, re-asked for, or OCI.

Three fixes for the run8 failure, where argo-rollouts' fact carried no chart repository and
its lane was skipped:

  * the research task names the INSTALLED chart and chart version (leads), read from the
    run's inventory, so the agent looks for that chart;
  * an accepted Helm answer with no chart is RE-ASKED once, on the same session, and the
    compatibility fact is never lost over the chart;
  * an OCI-only chart (karpenter) is a valid `chart_repository`, and the lane installs it
    directly with no `helm repo add`, refusing a chart whose appVersion is not the approved one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from eks_addon_readiness.research.boundary import ChartProvenanceInput, SourceRef
from eks_addon_readiness.stages import research_harness
from eks_addon_readiness.stages.upgrade_report import _oci_chart_ref
from eks_addon_readiness.upgrade_lane.chart_version import (
    ChartVersionUnresolved,
    resolve_oci_chart_version,
)

# ---------------------------------------------------------------------------
# Task leads
# ---------------------------------------------------------------------------


def _inventory(*components):
    return SimpleNamespace(components=components)


def _helm(release: str, ns: str, identity: str, chart_label: str | None):
    return SimpleNamespace(
        workload_ref=f"HelmRelease/{ns}/{release}",
        identity=identity,
        labels={"helm.sh/chart": chart_label} if chart_label else {},
    )


def test_the_task_names_the_installed_chart_and_chart_version(monkeypatch) -> None:
    inventory = _inventory(
        _helm("argo-rollouts", "argo-rollouts", "argo-rollouts", "argo-rollouts-2.34.3"),
        _helm("keda", "keda", "keda", "keda-2.17.2"),
    )
    monkeypatch.setattr("eks_addon_readiness.storage.read_document", lambda uri, model: inventory)
    event = {"helm_release": "argo-rollouts", "inventory_uri": "s3://b/inventories/x.json"}
    assert research_harness._chart_leads(event) == ("argo-rollouts", "2.34.3")

    prompt = research_harness._prompt(
        {
            "component_id": "argo-rollouts",
            "version_series": "1.6.x",
            "installed_version": "1.6.6",
            **event,
        },
        "s" * 40,
    )
    assert "chart_name: argo-rollouts" in prompt
    assert "chart_version: 2.34.3" in prompt
    assert "application version is 1.6.6" in prompt


def test_no_inventory_means_no_leads_and_research_runs_as_before(monkeypatch) -> None:
    def broken(uri, model):
        raise OSError("denied")

    monkeypatch.setattr("eks_addon_readiness.storage.read_document", broken)
    assert research_harness._chart_leads({"helm_release": "x", "inventory_uri": "s3://b/k"}) == (
        None,
        None,
    )
    assert research_harness._chart_leads({"helm_release": "x"}) == (None, None)
    assert research_harness._chart_leads({"inventory_uri": "s3://b/k"}) == (None, None)


# ---------------------------------------------------------------------------
# The chart re-ask
# ---------------------------------------------------------------------------


class _Calls:
    def __init__(self, second_judge: dict) -> None:
        self.harness = 0
        self.second_judge = second_judge
        self.messages: list = []

    def run_harness(self, session_id, messages):
        self.harness += 1
        self.messages = messages
        return {"second": True}

    def run_judge(self, session_id, event, extraction):
        return self.second_judge


def _accepted_without_chart() -> dict:
    return {"decision": "PENDING_APPROVAL", "pending_approval": True, "chart_repository": None}


def test_an_accepted_helm_answer_with_no_chart_is_re_asked_once(monkeypatch) -> None:
    calls = _Calls(
        {
            "decision": "PENDING_APPROVAL",
            "pending_approval": True,
            "chart_repository": "https://argoproj.github.io/argo-helm",
        }
    )
    monkeypatch.setattr(research_harness, "_run_harness", calls.run_harness)
    monkeypatch.setattr(research_harness, "_run_judge", calls.run_judge)
    monkeypatch.setattr(research_harness, "_chart_leads", lambda event: ("argo-rollouts", None))

    result = research_harness._with_chart(
        {"helm_release": "argo-rollouts"}, "s", [], {"first": True}, _accepted_without_chart()
    )
    assert calls.harness == 1
    assert result["chart_repository"] == "https://argoproj.github.io/argo-helm"
    assert result["chart_reask"] == "found"
    asked = calls.messages[-1]["content"][0]["text"]
    assert "charts repository on GitHub" in asked and "oci://" in asked


def test_a_rejected_second_answer_keeps_the_first_fact(monkeypatch) -> None:
    calls = _Calls({"decision": "REJECTED", "summary": "quote not on page"})
    monkeypatch.setattr(research_harness, "_run_harness", calls.run_harness)
    monkeypatch.setattr(research_harness, "_run_judge", calls.run_judge)
    monkeypatch.setattr(research_harness, "_chart_leads", lambda event: (None, None))

    first = _accepted_without_chart()
    result = research_harness._with_chart({"helm_release": "x"}, "s", [], {}, first)
    assert result["decision"] == "PENDING_APPROVAL", "the fact is never lost over its chart"
    assert result["chart_reask"].startswith("rejected")


@pytest.mark.parametrize(
    "event, result",
    [
        ({}, _accepted_without_chart()),  # not Helm
        ({"helm_release": "x"}, {**_accepted_without_chart(), "chart_repository": "https://c"}),
        ({"helm_release": "x"}, {"decision": "REJECTED", "pending_approval": False}),
    ],
)
def test_no_re_ask_when_not_needed(monkeypatch, event, result) -> None:
    def must_not_run(*_args, **_kwargs):
        raise AssertionError("re-asked when no chart re-ask was needed")

    monkeypatch.setattr(research_harness, "_run_harness", must_not_run)
    assert research_harness._with_chart(event, "s", [], {}, result) == result


# ---------------------------------------------------------------------------
# OCI charts
# ---------------------------------------------------------------------------


def test_an_oci_reference_is_a_valid_chart_location_and_other_schemes_are_not() -> None:
    source = SourceRef(url="https://karpenter.sh/docs/getting-started/", revision="main")
    oci = "oci://public.ecr.aws/karpenter/karpenter"
    assert (
        ChartProvenanceInput(
            repository_url=oci, source=source, quote=f"helm install x {oci}"
        ).repository_url
        == oci
    )
    with pytest.raises(ValidationError):
        ChartProvenanceInput(repository_url="ftp://x/y", source=source, quote="ftp://x/y")


def test_the_fact_accepts_oci_and_refuses_other_schemes() -> None:
    from eks_addon_readiness.knowledge import ComponentKnowledge

    with pytest.raises(ValueError, match="http\\(s\\) or oci"):
        ComponentKnowledge.chart_repository_is_helm_or_oci(  # type: ignore[attr-defined]
            SimpleNamespace(scheme="file")
        )
    assert (
        ComponentKnowledge.chart_repository_is_helm_or_oci(  # type: ignore[attr-defined]
            SimpleNamespace(scheme="oci")
        ).scheme
        == "oci"
    )


def test_the_fact_refuses_an_oci_reference_carrying_a_version_tag() -> None:
    """cert-manager's run-11 fact `oci://quay.io/jetstack/charts/cert-manager:v1.21.2` is refused:
    the lane appends the chart name and `.../cert-manager:v1.21.2/cert-manager` is un-pullable."""
    from pydantic import TypeAdapter
    from pydantic_core import Url

    from eks_addon_readiness.knowledge import ComponentKnowledge

    tagged = TypeAdapter(Url).validate_python("oci://quay.io/jetstack/charts/cert-manager:v1.21.2")
    with pytest.raises(ValueError, match="version tag"):
        ComponentKnowledge.chart_repository_is_helm_or_oci(tagged)  # type: ignore[attr-defined]

    # The untagged location is accepted, and a tag on the host:port is not mistaken for a chart tag.
    untagged = TypeAdapter(Url).validate_python("oci://quay.io/jetstack/charts/cert-manager")
    assert (
        str(ComponentKnowledge.chart_repository_is_helm_or_oci(untagged))  # type: ignore[attr-defined]
        == "oci://quay.io/jetstack/charts/cert-manager"
    )


def test_the_research_boundary_refuses_a_tagged_oci_reference() -> None:
    """The same rule at the model boundary, so the agent gets a schema error, not a silent store.
    Enforced by the `repository_url` pattern (no `:` in an oci path segment), not a validator,
    so `ChartProvenanceInput` does not defer `ResearchExtraction`'s schema build."""
    source = SourceRef(url="https://cert-manager.io/docs/installation/", revision="v1.21.2")
    with pytest.raises(ValidationError):
        ChartProvenanceInput(
            repository_url="oci://quay.io/jetstack/charts/cert-manager:v1.21.2",
            source=source,
            quote="helm install cert-manager oci://quay.io/jetstack/charts/cert-manager:v1.21.2",
        )
    ok = ChartProvenanceInput(
        repository_url="oci://quay.io/jetstack/charts/cert-manager",
        source=source,
        quote="helm install cert-manager oci://quay.io/jetstack/charts/cert-manager --version v1.21.2",
    )
    assert ok.repository_url == "oci://quay.io/jetstack/charts/cert-manager"
    # A `:port` on the host is still accepted -- the ban is on path segments, not the authority.
    with_port = ChartProvenanceInput(
        repository_url="oci://registry.example:5000/charts/keda",
        source=source,
        quote="helm install keda oci://registry.example:5000/charts/keda",
    )
    assert with_port.repository_url == "oci://registry.example:5000/charts/keda"


def test_the_oci_chart_reference_names_the_chart_once() -> None:
    assert (
        _oci_chart_ref("oci://public.ecr.aws/karpenter/karpenter", "karpenter")
        == "oci://public.ecr.aws/karpenter/karpenter"
    )
    assert _oci_chart_ref("oci://r.example/charts/", "keda") == "oci://r.example/charts/keda"


def test_an_oci_chart_must_ship_the_approved_application_version() -> None:
    ok = "apiVersion: v2\nname: karpenter\nversion: 1.15.0\nappVersion: 1.15.0\n"
    assert (
        resolve_oci_chart_version(chart_ref="oci://k", application_version="v1.15.0", chart_yaml=ok)
        == "1.15.0"
    )
    wrong = "name: k\nversion: 1.15.0\nappVersion: 1.14.0\n"
    with pytest.raises(ChartVersionUnresolved, match="not the approved"):
        resolve_oci_chart_version(
            chart_ref="oci://k", application_version="1.15.0", chart_yaml=wrong
        )
    with pytest.raises(ChartVersionUnresolved, match="could be pulled"):
        resolve_oci_chart_version(chart_ref="oci://k", application_version="1.15.0", chart_yaml="")
