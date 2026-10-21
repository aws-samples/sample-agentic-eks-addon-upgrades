"""When the agent finds no compatibility statement and says so, the judge stores a LOW fact.

THE PROMISE THAT WAS NEVER KEPT. The research prompt has always said: no compatibility
statement anywhere -> the latest stable release, LOW, with a plain sentence saying no
documentation was found. The judge could not deliver it: a fact needs a compatibility rule and
no source gave one, so the honest answer was REJECTED every time. For a component like
argo-rollouts, the honest "no statement found" reading was rejected on every attempt, and the
answer was accepted only when the agent happened
to find a chart README's "Kubernetes 1.7+" and attributed it to the installed series.

WHAT IS STORED NOW, AND WHAT IS NOT CLAIMED. One rule for the installed series whose window is
the Kubernetes minor the series is OBSERVED running on, with no ceiling -- a fact about this
cluster, not about the project -- titled so. The releases, each with a verified quote. LOW. The
decider then says COMPATIBLE at the target with the honest reason ("from 1.35 with no recorded
ceiling"), and the planner recommends the newest recorded release marked unverified
(`order/latest.py`). `TARGET_COVERED_BY_EVIDENCE` still fails and still shows.

THE FIXTURE IS A REAL REJECTED EXTRACTION, with its pages cut down to the bytes
the quotes need. Every quote still verifies against them; that is the condition the fallback
requires, and the test that a composed quote still rejects is below.
"""

from __future__ import annotations

import json
from pathlib import Path

from eks_addon_readiness.facts.ledger import FactStatus, GapEntry
from eks_addon_readiness.models import Confidence
from eks_addon_readiness.order.latest import Basis, fact_recommendation
from eks_addon_readiness.research.boundary import ResearchExtraction
from eks_addon_readiness.research.judge import OBSERVED_WINDOW_TITLE, judge
from eks_addon_readiness.research.pages import FetchedPage, PageSet
from eks_addon_readiness.research.receipt import AcceptanceDecision
from eks_addon_readiness.research.report import bases_for, build_report, quoted_claims_for

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parent
        / "fixtures"
        / "research"
        / "argo-rollouts-no-statement.json"
    ).read_text()
)


def _extraction() -> ResearchExtraction:
    return ResearchExtraction.model_validate(FIXTURE["extraction"])


def _pages() -> PageSet:
    ps = PageSet()
    for p in FIXTURE["pages"]:
        ps.record(FetchedPage(p["url"], p["revision"], p["body"], p["status"]))
    return ps


def test_the_fixture_is_the_honest_case() -> None:
    ex = _extraction()
    assert ex.no_compatibility_found is True
    assert ex.assessment is not None and ex.assessment.recommended_version == "1.10.0"
    assert all(s.kind.value == "UNSTATED" for s in ex.statements)


def test_it_is_stored_as_a_low_fact_with_an_observed_window() -> None:
    candidate = judge(_extraction(), _pages())
    assert candidate.receipt.decision is AcceptanceDecision.PENDING_APPROVAL
    assert candidate.receipt.confidence is Confidence.LOW
    knowledge = candidate.knowledge
    assert knowledge is not None
    (rule,) = knowledge.compatibility_rules
    assert rule.component_version_series == "1.6.x"
    assert rule.kubernetes_versions.minimum_inclusive == "1.35", "the observed minor"
    assert rule.kubernetes_versions.maximum_exclusive is None, "the ceiling nobody published"
    assert rule.evidence[0].title == OBSERVED_WINDOW_TITLE
    assert [r.version for r in knowledge.releases][:3] == ["1.10.0", "1.9.1", "1.8.4"]


def test_the_decider_and_the_planner_read_it_the_intended_way() -> None:
    from eks_addon_readiness.decide.rules import _verdict_from_knowledge

    knowledge = judge(_extraction(), _pages()).knowledge
    assert knowledge is not None
    gap = GapEntry.model_construct(
        workload_ref="HelmRelease/argo-rollouts/argo-rollouts",
        identity="argo-rollouts",
        version="1.6.6",
        version_series="1.6.x",
        status=FactStatus.FACT_AVAILABLE,
        reason="fixture",
    )
    verdict, _valid, _findings, reason = _verdict_from_knowledge(gap, knowledge, "1.36")
    assert verdict.value == "COMPATIBLE"
    assert "no recorded ceiling" in reason
    assert fact_recommendation(knowledge, "1.6.6", ("1.35", "1.36")) == (
        "1.10.0",
        Basis.LATEST_UNVERIFIED,
    )


def test_the_approval_page_says_so_first() -> None:
    ex, pages = _extraction(), _pages()
    candidate = judge(ex, pages)
    report = build_report(
        candidate,
        current=None,
        quoted_claims=quoted_claims_for(ex, pages),
        proposed_bases=bases_for(ex.statements),
        researched_at=candidate.knowledge.refreshed_at,  # type: ignore[union-attr]
        installed_version="1.6.6",
        advisory="advice",
        assessment=ex.assessment,
    )
    assert report.caveats[0].startswith("No first-party compatibility statement was found")
    assert report.recommended_version == "1.10.0"
    assert report.action == "LATEST_UNVERIFIED"


def test_a_composed_quote_still_rejects_even_when_nothing_is_published() -> None:
    """The fallback needs every quote verified. 'Nothing published' is not a licence to invent."""
    ex = _extraction()
    broken = ex.model_copy(
        update={
            "releases": (
                ex.releases[0].model_copy(update={"quote": "v1.10.0 (composed from memory)"}),
                *ex.releases[1:],
            )
        }
    )
    candidate = judge(broken, _pages())
    assert candidate.receipt.decision is AcceptanceDecision.REJECTED
    assert candidate.knowledge is None


def test_a_found_statement_never_takes_the_fallback() -> None:
    """With `no_compatibility_found` false the assembler behaves exactly as before."""
    ex = _extraction().model_copy(update={"no_compatibility_found": False})
    candidate = judge(ex, _pages())
    assert candidate.receipt.decision is AcceptanceDecision.REJECTED
    assert candidate.knowledge is None
