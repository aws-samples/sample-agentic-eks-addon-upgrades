"""Stage 7 carries no clock, no window and no notion of a stale fact.

THE DECISION THIS PINS. Staleness means the upstream source moved, not that N hours
elapsed -- `research/freshness.py`'s docstring records that decision and the evidence for
it. The drift implementation shipped; the rejected 24-hour clock is what stayed live in
Stage 7. This file is the red test for taking it out.

WHY STAGE 7 NEEDS NO FRESHNESS CONCEPT AT ALL, which is the part that makes the removal
safe rather than merely tidy: a marked fact returns `None` from `FactStore.find()`, so the
gap ledger already reports its component through the FACT_MISSING path it has always had
for a component with no fact. Stage 7 dispatches that entry because the ledger called it
researchable. Nothing about the age of anything is consulted, and nothing needs to be.

Every assertion below fails against the implementation that read
`READINESS_FACT_MAX_AGE_HOURS` and queued a `FACT_STALE` request.
"""

from __future__ import annotations

import ast
import inspect
from types import ModuleType

import pytest

from eks_addon_readiness import loop
from eks_addon_readiness.facts import GapLedger
from eks_addon_readiness.loop import resolve_loop
from eks_addon_readiness.loop.dispatch import DispatchReason
from eks_addon_readiness.stages import resolve as resolve_stage
from eks_addon_readiness.storage import dispatch_key

DIGEST = "sha256:" + "ab" * 32
CAPTURED_AT = "2026-09-12T06:29:43+00:00"

# Every spelling the removed mechanism used, matched as a substring against identifiers and
# string literals. `max_age` and `cutoff` are deliberately in: `fact_max_age_hours` and
# `_stale_requests`'s `cutoff_seconds` are both this mechanism wearing a different name.
CLOCK_WORDS = ("fresh", "stale", "max_age", "cutoff", "ttl", "expiry")


def _code_words(module: ModuleType) -> set[str]:
    """Every identifier and string literal in a module's CODE, docstrings excluded.

    Asserted over the parse tree rather than the raw text on purpose. A text search cannot
    tell a live symbol from a docstring that explains why the symbol is gone -- and this
    change deletes a mechanism whose absence needs explaining, so the prose must be allowed
    to name it while the code must not.
    """
    tree = ast.parse(inspect.getsource(module))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, ast.alias):
            # BOTH names, not just the bound one: `from x import DEFAULT_FRESHNESS_HOURS as
            # w` must be caught by what it imports, not only by what it calls it.
            found.update({node.name, node.asname} - {None})
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            found.add(node.name)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            found.add(node.value)
    return found - {""}


def _ledger(entry: dict[str, object]) -> GapLedger:
    """A one-entry Stage 3 ledger, built directly so no snapshot fixture is needed."""
    return GapLedger.model_validate(
        {
            "cluster": {"name": "clock-test"},
            "kubernetes_version": "1.35",
            "report_digest": DIGEST,
            "snapshot_digest": DIGEST,
            "store_digest": DIGEST,
            "ruleset_digest": DIGEST,
            "entries": [entry],
        }
    )


ANSWERED_BY_AN_ANCIENT_FACT = {
    "workload_ref": "deploy/metrics-server",
    "identity": "metrics-server",
    "version": "0.8.1",
    "version_series": "0.8.x",
    "assessment_priority": "high",
    "status": "FACT_AVAILABLE",
    "detail_code": "RULE_MATCH",
    "reason": (
        "the fact store holds a compatibility rule for this series, researched 56 days "
        "ago and pinned to a source revision that has not moved since"
    ),
    "fact_knowledge_base_revision": "metrics-server-researched-2026-07-18",
    "fact_series": "0.8.x",
    "fact_kubernetes_versions": {
        "minimum_inclusive": "1.30",
        "maximum_exclusive": "1.40",
    },
}


class TestNoFreshnessSymbolSurvivesInTheLoop:
    """The rules module: no window, no reference instant, no staleness rule."""

    def test_dispatch_queue_takes_no_freshness_parameter(self) -> None:
        parameters = inspect.signature(resolve_loop.dispatch_queue).parameters
        assert "freshness_reference" not in parameters
        assert "freshness_hours" not in parameters

    def test_dispatch_queue_no_longer_reads_the_fact_store(self) -> None:
        """The store was an argument only so a fact's age could be looked up.

        With staleness gone the ledger is the whole input: it already encodes which
        components the store answered. Keeping the parameter would leave a reader
        believing Stage 7 consults knowledge it does not consult.
        """
        assert "store" not in inspect.signature(resolve_loop.dispatch_queue).parameters

    def test_the_dispatch_document_records_no_window(self) -> None:
        fields = set(resolve_loop.DispatchDocument.model_fields)
        assert not fields & {
            "freshness_hours",
            "freshness_reference",
            "freshness_checked",
        }

    def test_no_default_window_constant_survives(self) -> None:
        assert not hasattr(resolve_loop, "DEFAULT_FRESHNESS_HOURS")
        assert not hasattr(loop, "DEFAULT_FRESHNESS_HOURS")
        assert "DEFAULT_FRESHNESS_HOURS" not in loop.__all__

    def test_no_staleness_rule_survives(self) -> None:
        assert not hasattr(resolve_loop, "_stale_requests")

    def test_no_clock_word_survives_anywhere_in_the_rules_code(self) -> None:
        """Not just the four named symbols: no identifier or literal of that class at all."""
        offenders = {
            word
            for word in _code_words(resolve_loop)
            if any(clock in word.casefold() for clock in CLOCK_WORDS)
        }
        assert offenders == set(), f"clock vocabulary still live in resolve_loop.py: {offenders}"


class TestNoFreshnessSymbolSurvivesInTheStage:
    """The handler: no environment window, no reference instant, no clock at all."""

    def test_the_stage_reads_no_age_environment_variable(self) -> None:
        assert "READINESS_FACT_MAX_AGE_HOURS" not in _code_words(resolve_stage)

    def test_the_stage_has_no_freshness_reference_helper(self) -> None:
        assert not hasattr(resolve_stage, "_freshness_reference")

    def test_the_stage_imports_no_default_window(self) -> None:
        assert not hasattr(resolve_stage, "DEFAULT_FRESHNESS_HOURS")

    def test_no_clock_word_survives_anywhere_in_the_stage_code(self) -> None:
        offenders = {
            word
            for word in _code_words(resolve_stage)
            if any(clock in word.casefold() for clock in CLOCK_WORDS)
        }
        assert offenders == set(), f"clock vocabulary still live in stages/resolve.py: {offenders}"


class TestTheKeyAndTheReasonBothDropTheWindow:
    def test_the_dispatch_key_carries_no_window(self) -> None:
        key = dispatch_key("p1", DIGEST, DIGEST, DIGEST, CAPTURED_AT)
        assert "fresh-" not in key
        assert key.endswith(".max-3.round-1.json")

    def test_the_dispatch_key_takes_no_window_argument(self) -> None:
        assert "freshness_hours" not in inspect.signature(dispatch_key).parameters

    def test_stale_is_no_longer_a_dispatch_reason(self) -> None:
        """`FACT_STALE` was the only reason a clock could produce, so it goes too.

        Nothing is lost from the report: a fact the sweep marked is withheld, so the
        ledger reports FACT_MISSING and the request carries that reason honestly. WHY it
        was withheld is answered by the sweep's own document, which names the pinned
        revision and the upstream one -- a better answer than the word "stale".
        """
        assert [member.value for member in DispatchReason] == ["FACT_MISSING"]


class TestAgeAloneNeverQueuesAnything:
    """The behaviour, not just the symbols."""

    def test_an_answered_component_is_not_queued_however_old_its_fact(self) -> None:
        document = resolve_loop.dispatch_queue(_ledger(ANSWERED_BY_AN_ANCIENT_FACT))

        assert document.requests == ()
        assert document.not_dispatched is resolve_loop.NotDispatchedReason.NOTHING_TO_RESEARCH

    def test_a_withheld_fact_is_queued_as_missing_not_as_stale(self) -> None:
        """A marked fact reaches Stage 7 as FACT_MISSING, through the path that existed."""
        withheld = {
            **ANSWERED_BY_AN_ANCIENT_FACT,
            "status": "FACT_MISSING",
            "detail_code": "NO_ENTRY",
            "reason": (
                "no source holds a fact for this series: the sweep marked the stored one "
                "because its pinned source moved, so it does not answer"
            ),
            "fact_knowledge_base_revision": None,
            "fact_series": None,
            "fact_kubernetes_versions": None,
        }

        document = resolve_loop.dispatch_queue(_ledger(withheld))

        assert len(document.requests) == 1
        assert document.requests[0].reason is DispatchReason.FACT_MISSING

    def test_two_calls_over_one_ledger_are_byte_identical(self) -> None:
        """With no clock anywhere, repeated invocation cannot produce two documents."""
        ledger = _ledger(ANSWERED_BY_AN_ANCIENT_FACT)

        first = resolve_loop.dispatch_queue(ledger)
        second = resolve_loop.dispatch_queue(ledger)

        assert first.canonical_json() == second.canonical_json()
        assert first.digest() == second.digest()


def test_the_handler_needs_no_reference_instant_on_the_payload() -> None:
    """`freshness_reference` was a payload field the stage read. Nothing may consult it.

    Asserted over the code rather than by invoking the handler, which would need S3: the
    point is that no code path reads the field, so supplying it changes nothing.
    """
    assert "freshness_reference" not in _code_words(resolve_stage)


@pytest.mark.parametrize("symbol", ["DEFAULT_FRESHNESS_HOURS", "_stale_requests"])
def test_removed_symbols_are_not_re_exported_anywhere(symbol: str) -> None:
    """A symbol deleted from its module and left in a package __init__ is still live."""
    assert symbol not in dir(loop)
    assert symbol not in dir(resolve_loop)
