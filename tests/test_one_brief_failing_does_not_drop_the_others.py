"""One component's brief failing must cost that component its brief, never the others'.

Observed live (run 10): a quote that verified on a sibling page produced the verdict
`VERIFIED_SIBLING_PAGE`, which `brief/document.py::QuoteVerdict` did not list. Converting it
raised, the iteration failed, the Map failed with it, and every move on the upgrade approval page
showed "No brief". Three guards, one per link in that chain.
"""

from __future__ import annotations

import re
from pathlib import Path

from eks_addon_readiness.brief.document import QuoteVerdict, _verdict_of
from eks_addon_readiness.research.verify import QuoteCheck

WORKFLOW = Path(__file__).resolve().parents[1] / "infra/terraform/modules/platform/workflow.tf"


def _check_verdicts() -> set[str]:
    return {
        value
        for name, value in vars(QuoteCheck).items()
        if name.isupper() and isinstance(value, str)
    }


def test_the_brief_knows_every_verdict_the_quote_check_can_produce() -> None:
    assert {v.value for v in QuoteVerdict} == _check_verdicts()


def test_the_brief_and_the_quote_check_agree_on_what_counts_as_verified() -> None:
    for verdict in _check_verdicts():
        check = QuoteCheck("q", "https://example.com/", "main", verdict, "detail")
        assert _verdict_of(check).verified == check.verified, verdict


def test_a_sibling_page_verdict_is_verified_in_the_brief() -> None:
    check = QuoteCheck(
        "q", "https://example.com/?page=1", "main", QuoteCheck.VERIFIED_SIBLING_PAGE, "found"
    )
    assert _verdict_of(check) is QuoteVerdict.VERIFIED_SIBLING_PAGE
    assert _verdict_of(check).verified


def test_an_unknown_verdict_marks_one_quote_unverified_instead_of_raising() -> None:
    check = QuoteCheck("q", "https://example.com/", "main", "A_VERDICT_ADDED_LATER", "x")
    assert _verdict_of(check) is QuoteVerdict.NOT_FETCHED
    assert not _verdict_of(check).verified


def test_each_brief_iteration_catches_its_own_failure() -> None:
    text = WORKFLOW.read_text()
    iterator = text[text.index('"Brief research" = {') :]
    iterator = iterator[: iterator.index('"Brief assemble" = {')]
    task = iterator[iterator.index('"Brief one component" = {') : iterator.index('"Brief failed" = {')]
    assert re.search(r'Next\s*=\s*"Brief failed"', task), "the iteration's task has no Catch"
    failed = iterator[iterator.index('"Brief failed" = {') :]
    # The assembler records a result with no `brief_uri` as unbriefed, keyed on these fields.
    for field in ('"step_order.$"', '"component.$"', '"reason.$"'):
        assert field in failed, field
    assert "brief_uri" not in failed
