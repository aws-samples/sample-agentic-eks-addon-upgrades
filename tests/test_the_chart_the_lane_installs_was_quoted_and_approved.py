"""The Helm chart a lane installs comes from an approved fact, not a committed catalogue.

THE COMMITTED `upgrade_lane_catalogue` IS GONE, AND THIS FILE IS WHAT REPLACES ITS GUARANTEES.
That Terraform map carried, per component, the Helm release name, the chart repository and an
app->chart version table that somebody typed in after reading `helm search`. What that costs:
a component with an approved fact and no map entry has its step reported
LANE_NOT_OPENED for a reason no page upstream could have warned about.

Now the chain is the one every other value already follows:

  1. the research model TRANSCRIBES where the chart is published -- the URL on the project's
     `helm repo add` line -- with a quote and a pinned source (`boundary.ChartProvenanceInput`);
  2. `research/verify.py` checks it against the recorded bytes OUTSIDE the signals that accept a
     compatibility fact, and `judge._assemble` puts it on the fact only if it verified;
  3. the approver sees the repository beside the recommended version and approves both;
  4. `release_record.py` copies the fact's repository and the inventory's release, namespace and
     chart onto each `ReleaseStep`, and the planner reads nothing else;
  5. the lane asks the repository itself -- `helm search repo --versions` -- which chart version
     ships the approved application version, and refuses before anything moves when none does.

NOT TRANSCRIBED, DELIBERATELY: the chart name (the inventory knows the chart that is installed)
and the chart version (the repository knows which one ships the approved release). A first cut
asked the model to copy `appVersion`/`version` pairs out of `index.yaml`; a 20-line span lost one
line, the byte check called it composed, and a cert-manager fact with 36 of 38 compatibility values
verified was REJECTED over it. Provenance and compatibility answer different questions, and the
chart version is a fact about the repository today, not about any document.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eks_addon_readiness.knowledge import (
    CompatibilityRule,
    ComponentKnowledge,
    ReleaseRecord,
    Reversibility,
    UpgradeStrategy,
    VerificationCheck,
    VersionRange,
)
from eks_addon_readiness.models import Citation, ClusterReference, Confidence
from eks_addon_readiness.order.plan import PlanStep, StepKind, UpgradePlan
from eks_addon_readiness.report import ComponentReport, InventoryReport
from eks_addon_readiness.research.boundary import (
    BoundKind,
    ChartProvenanceInput,
    CompatibilityStatementInput,
    ReleaseSightingInput,
    ResearchExtraction,
    SourceRef,
)
from eks_addon_readiness.research.extract import SYSTEM_PROMPT
from eks_addon_readiness.research.judge import judge
from eks_addon_readiness.research.pages import FetchedPage, PageSet
from eks_addon_readiness.research.report import chart_missing_note, quoted_claims_for
from eks_addon_readiness.research.verify import literal_in_quote, verify_extraction
from eks_addon_readiness.stages import release_prep, release_record
from eks_addon_readiness.upgrade_lane import __main__ as cli
from eks_addon_readiness.upgrade_lane.chart_version import (
    ChartVersionUnresolved,
    resolve_chart_version,
)

ROOT = Path(__file__).resolve().parent.parent
BUILDSPEC = ROOT / "infra" / "terraform" / "modules" / "platform" / "upgrade_buildspec.yml"
STAMP = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

# A component that is deliberately NOT one on the cluster: the research package may name no
# component, and neither may a test that would pass only for the ones it names.
COMPONENT = "example-operator"
REPO = f"https://github.com/example-org/{COMPONENT}"
README = f"{REPO}/blob/v2.4.0/README.md"
INSTALL = f"https://example-org.github.io/{COMPONENT}-docs/install/"
CHART_REPO = f"https://example-org.github.io/{COMPONENT}/"

README_BODY = f"""# {COMPONENT}

## Compatibility

{COMPONENT}  Kubernetes
2.4.x  1.30+
2.3.x  1.28+

## Releases

- 2.4.0
- 2.3.0
"""

INSTALL_BODY = f"""# Installing

helm repo add example {CHART_REPO}
helm install {COMPONENT} example/{COMPONENT} --namespace {COMPONENT}
"""


def _pages() -> PageSet:
    ps = PageSet()
    ps.record(FetchedPage(README, "v2.4.0", README_BODY, 200))
    ps.record(FetchedPage(INSTALL, "main", INSTALL_BODY, 200))
    return ps


def _row(series: str) -> str:
    return next(line for line in README_BODY.splitlines() if line.startswith(f"{series}  "))


def _extraction(*, chart: ChartProvenanceInput | None) -> ResearchExtraction:
    readme = SourceRef(url=README, revision="v2.4.0")
    return ResearchExtraction(
        found=True,
        component_id=COMPONENT,
        version_series="2.3.x",
        canonical_repository=REPO,
        official_documentation=(REPO, INSTALL),
        statements=tuple(
            CompatibilityStatementInput(
                version_spec=spec,
                kind=BoundKind.PUBLISHED_MINIMUM,
                stated_minimum=low,
                quote=_row(spec),
                source=readme,
            )
            for spec, low in (("2.4.x", "1.30"), ("2.3.x", "1.28"))
        ),
        releases=(
            ReleaseSightingInput(version="2.4.0", quote="- 2.4.0", source=readme),
            ReleaseSightingInput(version="2.3.0", quote="- 2.3.0", source=readme),
        ),
        chart=chart,
    )


GOOD_CHART = ChartProvenanceInput(
    repository_url=CHART_REPO,
    source=SourceRef(url=INSTALL, revision="main"),
    quote=f"helm repo add example {CHART_REPO}",
)
# The quote is composed: this line is on no fetched page.
COMPOSED_CHART = ChartProvenanceInput(
    repository_url=CHART_REPO,
    source=SourceRef(url=INSTALL, revision="main"),
    quote=f"helm repo add example {CHART_REPO} --force-update",
)


# ---------------------------------------------------------------------------
# 1 + 2. Transcribed, verified, and only the verified part reaches the fact.
# ---------------------------------------------------------------------------


def test_the_repository_is_checked_like_a_value_but_counted_apart() -> None:
    report = verify_extraction(_extraction(chart=COMPOSED_CHART), _pages())
    assert [c.verified for c in report.chart_checks] == [False]
    assert report.unverified_values == (), (
        "a composed chart quote must not appear among the compatibility values: it is checked "
        "in its own bucket so it cannot reject the fact"
    )
    assert not report.fabricated_quotes


def test_a_verified_repository_reaches_the_fact() -> None:
    candidate = judge(_extraction(chart=GOOD_CHART), _pages(), retrieved_at=STAMP)
    fact = candidate.knowledge
    assert fact is not None
    assert str(fact.chart_repository) == CHART_REPO
    assert fact.chart_name is None and fact.chart_mappings == (), (
        "the name comes from the inventory and the version from the repository; the fact "
        "carries neither, which also keeps the frozen HIGH-needs-a-mapping validator quiet"
    )


def test_a_composed_repository_quote_costs_the_lane_and_nothing_else() -> None:
    good = judge(_extraction(chart=GOOD_CHART), _pages(), retrieved_at=STAMP)
    bad = judge(_extraction(chart=COMPOSED_CHART), _pages(), retrieved_at=STAMP)
    assert bad.knowledge is not None and bad.knowledge.chart_repository is None
    assert bad.receipt.confidence == good.receipt.confidence, (
        "the compatibility fact is judged on compatibility evidence; a chart quote that did "
        "not verify drops the repository, it does not move the confidence"
    )
    assert bad.receipt.decision == good.receipt.decision


def test_a_chart_repository_url_is_matched_as_text_not_as_a_version() -> None:
    """`value_in_quote` would compare version tokens for a URL containing `/v2/`; a URL is text."""
    assert literal_in_quote(
        "https://charts.example.io/v2/", "helm repo add x https://charts.example.io/v2"
    )
    assert not literal_in_quote(
        "https://charts.example.io/v2/", "helm repo add x https://other.io/v2"
    )


# ---------------------------------------------------------------------------
# 3. The approver sees it.
# ---------------------------------------------------------------------------


def test_the_approval_report_shows_the_repository_with_its_verdict() -> None:
    by_field = {r.field: r for r in quoted_claims_for(_extraction(chart=GOOD_CHART), _pages())}
    assert by_field["chart_repository"].verified is True
    composed = {r.field: r for r in quoted_claims_for(_extraction(chart=COMPOSED_CHART), _pages())}
    assert composed["chart_repository"].verified is False
    assert by_field["chart_repository"].source_authority.startswith("FIRST_PARTY"), (
        "the chart repository host vouches for the chart claims, so a claim read from the "
        "component's own install page is first-party"
    )


def test_the_prompt_asks_for_the_chart_and_carries_its_schema() -> None:
    assert "helm_release:" in SYSTEM_PROMPT
    assert '"ChartProvenanceInput"' in SYSTEM_PROMPT and "ChartVersionInput" not in SYSTEM_PROMPT
    manifest = json.loads((ROOT / "agents" / "readiness_research" / "harness.json").read_text())
    assert manifest["systemPrompt"] == SYSTEM_PROMPT, (
        "agents/readiness_research/harness.json is stale; "
        "run `uv run python -m tools.research.generate`"
    )


def test_the_prompt_tells_a_helm_task_where_to_look_when_the_install_page_has_no_chart() -> None:
    """The prompt no longer tells a Helm task to give up when the install page shows no chart.

    This is the argo-rollouts failure: the install page links to a separate charts repo, and
    the old prompt said "no `helm repo add` line -> leave `chart` out". It must now send the
    agent to the project's own charts repository, and require a not_found note when it still
    finds nothing.
    """
    assert "charts repository" in SYSTEM_PROMPT
    assert "chart repository:" in SYSTEM_PROMPT
    # The old unconditional give-up wording is gone.
    assert "the install\n  page shows no `helm repo add` line, leave `chart` out" not in SYSTEM_PROMPT


def test_a_helm_proposal_with_no_verified_chart_gets_a_visible_not_found_note() -> None:
    """A Helm release whose fact has no repository gains a `chart repository:` not_found entry.

    This is the entry the fact-approval page lifts into the Recommended column, so a reviewer
    sees the gap when approving the fact rather than two stages later when the lane skips it.
    """
    note = chart_missing_note("argo-rollouts", _fact_without_chart(), [])
    assert any(g.startswith("chart repository:") for g in note)
    # A managed add-on has no helm_release and gets nothing.
    assert chart_missing_note(None, _fact_without_chart(), []) == []
    # A verified repository gets nothing, and an agent's own note is not duplicated.
    assert chart_missing_note("argo-rollouts", _fact(), []) == []
    pre = ["chart repository: tried docs, README, nothing found"]
    assert chart_missing_note("argo-rollouts", _fact_without_chart(), pre) == pre


def _fact_without_chart() -> ComponentKnowledge:
    fact = _fact()
    return fact.model_copy(update={"chart_repository": None})


# ---------------------------------------------------------------------------
# 4. The record carries what the lane needs, from the fact and the inventory.
# ---------------------------------------------------------------------------


def _fact() -> ComponentKnowledge:
    cite = Citation(url=INSTALL, title="install page", source_revision="main", retrieved_at=STAMP)
    return ComponentKnowledge(
        knowledge_base_revision="test",
        canonical_id=COMPONENT,
        canonical_repository=REPO,
        official_documentation=(REPO,),
        chart_repository=CHART_REPO,
        compatibility_rules=(
            CompatibilityRule(
                component_version_series="2.4.x",
                kubernetes_versions=VersionRange(
                    minimum_inclusive="1.30", maximum_exclusive="1.37"
                ),
                evidence=(cite,),
            ),
        ),
        releases=(
            ReleaseRecord(
                version="2.4.0", released_at=STAMP, source_revision="v2.4.0", evidence=(cite,)
            ),
        ),
        verification_checks=(
            VerificationCheck(
                check_id="serves-api", description="serves", required=True, evidence=(cite,)
            ),
        ),
        default_upgrade_strategy=UpgradeStrategy.SEQUENTIAL_MINOR,
        refreshed_at=STAMP,
        evidence_confidence=Confidence.MEDIUM,
    )


class _Store:
    def __init__(self, facts: dict[str, ComponentKnowledge]) -> None:
        self._facts = facts

    def find(self, identity: str) -> ComponentKnowledge | None:
        return self._facts.get(identity)

    def digest(self) -> str:
        return "sha256:" + "0" * 64


def _component(**overrides: object) -> ComponentReport:
    base: dict[str, object] = {
        "identity": COMPONENT,
        "identity_source": "helm",
        "identity_detail": "Helm release",
        "classification": "CNCF",
        "classification_reason": "test",
        "assessment_priority": "HIGH",
        "version": "2.3.0",
        "version_source": "image tag",
        "confidence": Confidence.HIGH,
        "workload_ref": f"HelmRelease/{COMPONENT}/{COMPONENT}-release",
        "namespace": COMPONENT,
        "install_method": "HELM",
        "labels": {"app.kubernetes.io/instance": f"{COMPONENT}-release"},
    }
    base.update(overrides)
    return ComponentReport(**base)


def _inventory(*components: ComponentReport) -> InventoryReport:
    return InventoryReport(
        cluster=ClusterReference(name="my-eks-cluster", region="us-east-1"),
        kubernetes_version="1.35",
        snapshot_digest="sha256:" + "a" * 64,
        snapshot_captured_at=STAMP.isoformat(),
        ruleset_digest="sha256:" + "b" * 64,
        components=components,
    )


def test_provenance_takes_the_release_from_the_inventory_and_the_chart_from_the_fact() -> None:
    addon = _component(
        identity="coredns",
        identity_source="eks",
        identity_detail="EKS-managed add-on coredns, correlated to Deployment/coredns",
        workload_ref="Deployment/kube-system/coredns",
        namespace="kube-system",
        install_method="EKS_MANAGED_ADDON",
        labels={"k8s-app": "kube-dns", "eks.amazonaws.com/component": "coredns"},
    )
    found = release_record.provenance(_inventory(_component(), addon), _Store({COMPONENT: _fact()}))

    helm = found[COMPONENT]
    assert helm.release_name == f"{COMPONENT}-release", "the name after HelmRelease/<ns>/"
    assert helm.namespace == COMPONENT
    assert helm.pod_selector is None, (
        "Helm pods are found by the instance label the capture already uses"
    )
    assert helm.chart_repository == CHART_REPO, "from the approved fact"
    assert helm.chart_name == COMPONENT, "from the inventory: the chart that is installed"

    managed = found["coredns"]
    assert managed.release_name == "coredns"
    assert managed.pod_selector == "k8s-app=kube-dns", (
        "read off the correlated workload's own labels"
    )
    assert managed.chart_repository is None and managed.chart_name is None


def _plan() -> UpgradePlan:
    digest = "sha256:" + "a" * 64
    return UpgradePlan(
        cluster=ClusterReference(name="my-eks-cluster", region="us-east-1"),
        current_kubernetes_version="1.35",
        target_kubernetes_version="1.36",
        steps=(
            PlanStep(
                order=1,
                kind=StepKind.COMPONENT_MOVE,
                description=f"Move {COMPONENT}: 2.3.x -> 2.4.x (install 2.4.0)",
                component=COMPONENT,
                from_series="2.3.x",
                to_series="2.4.x",
                recommended_version="2.4.0",
                at_kubernetes="1.35",
                reversibility=Reversibility.REVERSIBLE,
            ),
        ),
        snapshot_digest=digest,
        report_digest=digest,
        ledger_digest=digest,
        assessment_digest=digest,
        store_digest="sha256:" + "c" * 64,
        ruleset_digest="sha256:" + "b" * 64,
    )


def test_the_release_step_carries_the_repository_the_release_and_the_chart() -> None:
    (step,) = release_prep.approved_steps(
        _plan(),
        [1],
        install_methods={COMPONENT: "HELM"},
        provenance=release_record.provenance(
            _inventory(_component()), _Store({COMPONENT: _fact()})
        ),
    )
    assert step.to_version == "2.4.0"
    assert step.chart_repository == CHART_REPO and step.chart_name == COMPONENT
    assert step.release_name == f"{COMPONENT}-release" and step.namespace == COMPONENT
    assert not hasattr(step, "chart_version"), "the lane resolves it from the repository"


def test_a_fact_without_a_chart_leaves_the_step_without_one_rather_than_guessing() -> None:
    bare = _fact().model_copy(update={"chart_repository": None})
    (step,) = release_prep.approved_steps(
        _plan(),
        [1],
        install_methods={COMPONENT: "HELM"},
        provenance=release_record.provenance(_inventory(_component()), _Store({COMPONENT: bare})),
    )
    assert step.chart_repository is None
    # The planner then skips it with that reason -- the words the report will show.
    planner = (ROOT / "eks_addon_readiness" / "stages" / "upgrade_report.py").read_text()
    assert "carries no chart repository" in planner
    assert "UPGRADE_LANE_CATALOGUE" not in planner, "the committed catalogue is gone"


def test_the_catalogue_is_gone_from_terraform_too() -> None:
    platform = ROOT / "infra" / "terraform" / "modules" / "platform"
    files = [*platform.glob("*.tf"), *platform.glob("*.tfvars")]
    offenders = [p.name for p in files if "upgrade_lane_catalogue" in p.read_text()]
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# 5. The lane asks the repository before it moves.
# ---------------------------------------------------------------------------

SEARCH = [
    {"name": f"example/{COMPONENT}", "version": "5.1.0", "app_version": "2.4.0"},
    {"name": f"example/{COMPONENT}", "version": "5.0.0", "app_version": "2.3.0"},
    {"name": f"example/{COMPONENT}-crds", "version": "5.1.0", "app_version": "2.4.0"},
]


def test_the_newest_chart_shipping_the_approved_release_is_chosen() -> None:
    ref = f"example/{COMPONENT}"
    assert (
        resolve_chart_version(chart_ref=ref, application_version="2.4.0", search=SEARCH) == "5.1.0"
    )
    assert (
        resolve_chart_version(chart_ref=ref, application_version="v2.4.0", search=SEARCH) == "5.1.0"
    )
    two = [*SEARCH, {"name": ref, "version": "5.1.1", "app_version": "2.4.0"}]
    assert resolve_chart_version(chart_ref=ref, application_version="2.4.0", search=two) == "5.1.1"


@pytest.mark.parametrize(
    ("application_version", "said"),
    [
        ("2.5.0", "no chart version of"),
        ("", "no target version reached the lane"),
    ],
)
def test_a_release_the_repository_does_not_ship_is_refused(
    application_version: str, said: str
) -> None:
    with pytest.raises(ChartVersionUnresolved, match=re.escape(said)):
        resolve_chart_version(
            chart_ref=f"example/{COMPONENT}", application_version=application_version, search=SEARCH
        )


def test_another_chart_in_the_same_repository_is_not_mistaken_for_this_one() -> None:
    with pytest.raises(ChartVersionUnresolved, match="lists no chart named"):
        resolve_chart_version(
            chart_ref=f"example/{COMPONENT}-crds", application_version="2.4.0", search=SEARCH[:2]
        )


def test_the_cli_prints_the_version_or_exits_with_the_refusal_code(tmp_path: Path, capsys) -> None:
    search = tmp_path / "helm-search.json"
    search.write_text(json.dumps(SEARCH))
    args = ["resolve-chart-version", "--chart-ref", f"example/{COMPONENT}", "--search", str(search)]
    assert cli.main([*args, "--application-version", "2.4.0"]) == 0
    assert capsys.readouterr().out.strip() == "5.1.0"
    assert cli.main([*args, "--application-version", "2.5.0"]) == cli.REFUSAL


def test_the_buildspec_confirms_before_it_renders_and_installs_only_what_it_confirmed() -> None:
    text = BUILDSPEC.read_text()
    assert "helm search repo" in text and "resolve-chart-version" in text
    pre_build = text[text.index("pre_build:") : text.index("build:", text.index("pre_build:") + 10)]
    assert pre_build.index("resolve-chart-version") < pre_build.index(
        'helm template "${RELEASE}"'
    ), "the confirmation must run before anything is rendered, in the phase with `set -e`"
    assert "TARGET_CHART_VERSION" not in text, (
        "no chart version reaches the lane from outside; it is resolved from the repository"
    )
    assert "/tmp/target-chart-version" in text


# ---------------------------------------------------------------------------
# The approver's source list cannot be empty while there are quotes.
# ---------------------------------------------------------------------------


def test_an_empty_cited_sources_is_derived_from_the_quotes() -> None:
    """A fact that comes back HIGH with 18 verified statements and `cited_sources: []`.

    The list is display, but it is the display an approver reads. Derived from the quotes it
    can neither be empty nor name a page nothing was quoted from.
    """
    from eks_addon_readiness.research.report import cited_sources_from_claims

    rows = cited_sources_from_claims(quoted_claims_for(_extraction(chart=GOOD_CHART), _pages()))
    by_url = {r.url: r for r in rows}
    assert set(by_url) == {README, INSTALL}, "exactly the pages quoted from, no more"
    assert by_url[README].revision == "v2.4.0"
    assert "compatibility rows for 2.4.x, 2.3.x" in by_url[README].used_for
    assert "release list" in by_url[README].used_for
    assert by_url[INSTALL].used_for == "chart repository"


def test_the_models_own_list_wins_when_it_gave_one() -> None:
    from eks_addon_readiness.research.boundary import (
        Assessment,
        CitedSource,
        EvidenceTier,
        InstalledVersionStatus,
        RecommendationAction,
    )
    from eks_addon_readiness.research.report import bases_for, build_report

    extraction = _extraction(chart=GOOD_CHART)
    pages = _pages()
    candidate = judge(extraction, pages, retrieved_at=STAMP)
    claims = quoted_claims_for(extraction, pages)
    theirs = Assessment(
        current_kubernetes="1.35",
        target_kubernetes="1.36",
        installed_version_status=InstalledVersionStatus.NOT_STATED,
        action=RecommendationAction.UPGRADE,
        recommended_version="2.4.0",
        evidence_tier=EvidenceTier.MATRIX_IN_REPOSITORY,
        confidence_rationale="the README table covers the target",
        cited_sources=(CitedSource(url=README, revision="v2.4.0", used_for="the matrix"),),
    )

    def report(assessment):
        return build_report(
            candidate,
            current=None,
            quoted_claims=claims,
            proposed_bases=bases_for(extraction.statements),
            researched_at=STAMP,
            advisory="advice",
            assessment=assessment,
        )

    assert [r.used_for for r in report(theirs).cited_sources] == ["the matrix"]
    derived = report(theirs.model_copy(update={"cited_sources": ()})).cited_sources
    assert {r.url for r in derived} == {README, INSTALL}
