"""A quote read off the wrong PAGE of one paginated list verifies, re-cited to the right page.

THE FALSE REJECTION THIS FIXES, observed live. The research agent extracted karpenter release
`1.0.9` with the quote `>v1.0.9<` and cited
`https://github.com/kubernetes-sigs/karpenter/releases?page=3`. The bytes of that quote were
actually on `?page=2` of the same list -- also fetched in the same run -- because GitHub shifts
releases between its paginated list pages as new ones are cut. `verify.py` called it WRONG_PAGE,
which failed QUOTE_MATCHES_SOURCE and VALUES_TRACE_TO_QUOTES and rejected the whole candidate
with 23 of 24 values verified. The citation was stale, not fabricated.

The fix: when a quote is absent from the cited page but present verbatim in another FETCHED page
whose URL differs ONLY in its query string (same scheme, host and path), it is VERIFIED and the
citation is re-attributed to the page the bytes are on. Everything else that is WRONG_PAGE today
-- a different path, a different host, a quote present only on an unrelated page -- stays
rejected exactly as before.

These tests build PageSet/FetchedPage fixtures inline with small bodies, and cover the four
cases named in the fix: (1) sibling query-page re-attributes and verifies; (2) a different path
stays WRONG_PAGE; (3) a different host stays rejected; (4) a quote on the cited page is
untouched.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eks_addon_readiness.models import Confidence
from eks_addon_readiness.research.boundary import (
    Assessment,
    BoundKind,
    CompatibilityStatementInput,
    EvidenceTier,
    InstalledVersionStatus,
    ProposedConfidence,
    RecommendationAction,
    ReleaseSightingInput,
    ResearchExtraction,
    SourceRef,
)
from eks_addon_readiness.research.judge import judge
from eks_addon_readiness.research.pages import FetchedPage, PageSet
from eks_addon_readiness.research.receipt import AcceptanceDecision
from eks_addon_readiness.research.verify import QuoteCheck, check_quote

_CITED = "https://github.com/kubernetes-sigs/karpenter/releases?page=3"
_SIBLING = "https://github.com/kubernetes-sigs/karpenter/releases?page=2"
_DIFFERENT_PATH = "https://github.com/kubernetes-sigs/karpenter/tags?page=2"
_DIFFERENT_HOST = "https://example.com/kubernetes-sigs/karpenter/releases?page=2"
_QUOTE = ">v1.0.9<"


def _page(url: str, body: str, revision: str = "") -> FetchedPage:
    return FetchedPage(url=url, revision=revision, body=body, status=200)


# --------------------------------------------------------------------------- #
# check_quote: the four cases of the fix, at the unit the fix lives in.
# --------------------------------------------------------------------------- #


def test_a_sibling_query_page_verifies_and_re_attributes() -> None:
    pages = PageSet()
    pages.record(_page(_CITED, "no releases of interest here"))
    pages.record(_page(_SIBLING, f"<a>{_QUOTE}</a> karpenter 1.0.9 release"))

    check = check_quote(_QUOTE, _CITED, "", pages)

    assert check.verdict == QuoteCheck.VERIFIED_SIBLING_PAGE
    assert check.verified is True
    assert check.reattributed is True
    # The citation points at the page whose bytes carry the quote, not the cited one.
    assert check.attributed_url == _SIBLING
    assert _CITED in check.detail and _SIBLING in check.detail


def test_a_quote_on_a_different_path_stays_wrong_page() -> None:
    pages = PageSet()
    pages.record(_page(_CITED, "nothing"))
    pages.record(_page(_DIFFERENT_PATH, f"{_QUOTE} on the tags page"))

    check = check_quote(_QUOTE, _CITED, "", pages)

    assert check.verdict == QuoteCheck.WRONG_PAGE
    assert check.verified is False
    assert check.reattributed is False
    assert check.attributed_url == _CITED


def test_a_quote_on_a_different_host_stays_rejected() -> None:
    pages = PageSet()
    pages.record(_page(_CITED, "nothing"))
    pages.record(_page(_DIFFERENT_HOST, f"{_QUOTE} on another host entirely"))

    check = check_quote(_QUOTE, _CITED, "", pages)

    assert check.verdict == QuoteCheck.WRONG_PAGE
    assert check.verified is False
    assert check.reattributed is False


def test_a_quote_present_on_the_cited_page_is_unchanged() -> None:
    pages = PageSet()
    pages.record(_page(_CITED, f"the release list, {_QUOTE}, right where it was cited"))
    pages.record(_page(_SIBLING, "a different page of the same list"))

    check = check_quote(_QUOTE, _CITED, "", pages)

    assert check.verdict == QuoteCheck.PRESENT
    assert check.verified is True
    assert check.reattributed is False
    assert check.attributed_url == _CITED


# --------------------------------------------------------------------------- #
# judge: the re-attributed URL is what lands on the assembled fact's evidence.
# --------------------------------------------------------------------------- #

_STATEMENT_QUOTE = "Supported Kubernetes versions: 1.35 - 1.36"
_SUPPORT_PAGE = "https://karpenter.sh/docs/upgrading/compatibility/"


def _supported_body() -> str:
    return (
        "## Compatibility\n\n"
        f"{_STATEMENT_QUOTE}\n"
    )


def _extraction() -> ResearchExtraction:
    """A karpenter-shaped extraction: one supported-range statement, one release.

    The release quote is cited to `?page=3` but its bytes are on `?page=2`. The statement
    is cited to a page present as itself, so it is not re-attributed -- the point is that
    only the stale-page release moves, and the fact still assembles and is accepted
    (prose evidence caps it at MEDIUM) instead of being rejected on the stale citation.
    """
    stmt_source = SourceRef(url=_SUPPORT_PAGE, revision="v1.0.9")
    release_source = SourceRef(url=_CITED, revision="v1.0.9")
    return ResearchExtraction(
        found=True,
        component_id="karpenter",
        version_series="1.0.x",
        canonical_repository="https://github.com/kubernetes-sigs/karpenter",
        official_documentation=("https://karpenter.sh",),
        statements=(
            CompatibilityStatementInput(
                version_spec="1.0.x",
                kind=BoundKind.SUPPORTED_RANGE,
                stated_minimum="1.35",
                stated_maximum_inclusive="1.36",
                quote=_STATEMENT_QUOTE,
                source=stmt_source,
            ),
        ),
        releases=(
            ReleaseSightingInput(version="1.0.9", quote=_QUOTE, source=release_source),
        ),
        assessment=Assessment(
            installed_version="1.0.9",
            current_kubernetes="1.35",
            target_kubernetes="1.36",
            installed_version_status=InstalledVersionStatus.COVERS_TARGET,
            action=RecommendationAction.KEEP,
            recommended_version="1.0.9",
            recommended_series="1.0.x",
            covers_current_kubernetes=True,
            covers_target_kubernetes=True,
            evidence_tier=EvidenceTier.MATRIX_IN_OFFICIAL_DOCS,
            proposed_confidence=ProposedConfidence.HIGH,
            confidence_rationale="supported range in the docs covers the target",
        ),
    )


def _pages() -> PageSet:
    pages = PageSet()
    # The statement's own page, present as cited.
    pages.record(_page(_SUPPORT_PAGE, _supported_body(), revision="v1.0.9"))
    # The release was cited to page 3, which does NOT carry the quote...
    pages.record(_page(_CITED, "releases 1.1.0, 1.0.11, 1.0.10 -- newer ones only", "v1.0.9"))
    # ...its bytes are on page 2 of the same list, also fetched this run.
    pages.record(
        _page(_SIBLING, f"<a href='/releases/tag/v1.0.9'>{_QUOTE}</a> and older", "v1.0.9")
    )
    return pages


def test_the_stale_page_release_is_accepted_and_cites_the_page_it_was_read_from() -> None:
    candidate = judge(_extraction(), _pages(), retrieved_at=datetime(2026, 1, 1, tzinfo=UTC))

    # The whole candidate is no longer rejected on a stale citation.
    assert candidate.receipt.decision is AcceptanceDecision.PENDING_APPROVAL
    # Every value traces -- the release among them -- so the quote signals pass.
    assert candidate.receipt.values_verified == candidate.receipt.values_total
    assert candidate.receipt.confidence in (Confidence.HIGH, Confidence.MEDIUM)

    knowledge = candidate.knowledge
    assert knowledge is not None
    (release,) = knowledge.releases
    assert release.version == "1.0.9"
    # THE RE-ATTRIBUTION IS WHAT MATTERS: the stored fact cites the page whose bytes carry
    # the quote (page 2), not the stale page number the model wrote (page 3).
    (evidence,) = release.evidence
    assert str(evidence.url) == _SIBLING
    assert str(evidence.url) != _CITED


def test_a_release_quote_on_an_unrelated_page_still_sinks_the_fact() -> None:
    """Re-attribution is narrow: a release quote present only on a different PATH rejects.

    Same shape as the accepted case, but the quote's bytes are on `/tags`, a different
    document, not another page of the release list. That stays WRONG_PAGE, the release
    value does not trace, and the candidate is rejected -- the behaviour the fix must not
    loosen.
    """
    ex = _extraction()
    pages = PageSet()
    pages.record(_page(_SUPPORT_PAGE, _supported_body(), revision="v1.0.9"))
    pages.record(_page(_CITED, "releases 1.1.0, 1.0.11 -- newer ones only", "v1.0.9"))
    pages.record(_page(_DIFFERENT_PATH, f"tag list: {_QUOTE}", "v1.0.9"))

    candidate = judge(ex, pages, retrieved_at=datetime(2026, 1, 1, tzinfo=UTC))

    assert candidate.receipt.decision is AcceptanceDecision.REJECTED
    assert candidate.knowledge is None
