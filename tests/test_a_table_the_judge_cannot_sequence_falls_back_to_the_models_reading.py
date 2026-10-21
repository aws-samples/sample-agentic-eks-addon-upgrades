"""When the judge's ceiling inference leaves no series spanning the path, the model's reading wins.

TWO TABLES THAT LOOK ALIKE AND MEAN OPPOSITE THINGS.

metrics-server (keyed by COMPONENT series):   0.6.x | 1.25+   0.7.x | 1.27+   0.9.x | 1.34+
  "each row stops where the next starts" is right; 0.6.4 is not fit for 1.36. The judge's
  inference handles it and this fallback never fires.

karpenter (keyed by KUBERNETES minor):        1.35 | >= 1.9   1.36 | >= 1.13
  "1.9 stops at 1.36" is wrong; `>= 1.9` says nothing about where 1.9 stops, and 1.13 runs on
  1.35 too. Read the judge's way no series spans 1.35 and 1.36: karpenter was unsequenceable,
  the plan infeasible, and the component got neither a step nor a brief.

THE FALLBACK FIRES ON THE FAILURE, NOT ON A GUESS ABOUT SHAPE: only when no derived series
covers every minor from current to target, and the model says its recommendation covers the
target. Then the highest verified series covering the target is widened down to the current
minor. Everything else is untouched; the receipt shows the basis; never HIGH.

Both fixtures are the real extractions from the live runs, pages cut to the bytes the quotes
need.
"""

from __future__ import annotations

import json
from pathlib import Path

from eks_addon_readiness.facts.ledger import FactStatus, GapEntry
from eks_addon_readiness.models import Confidence
from eks_addon_readiness.order.moves import plan_moves
from eks_addon_readiness.research.boundary import ResearchExtraction
from eks_addon_readiness.research.judge import judge
from eks_addon_readiness.research.pages import FetchedPage, PageSet

FIX = Path(__file__).resolve().parent / "fixtures" / "research"


def _load(name: str) -> tuple[ResearchExtraction, PageSet]:
    d = json.loads((FIX / f"{name}.json").read_text())
    ps = PageSet()
    for p in d["pages"]:
        ps.record(FetchedPage(p["url"], p["revision"], p["body"], p["status"]))
    return ResearchExtraction.model_validate(d["extraction"]), ps


def _windows(knowledge) -> dict[str, tuple[str, str | None]]:
    return {
        r.component_version_series: (
            r.kubernetes_versions.minimum_inclusive,
            r.kubernetes_versions.maximum_exclusive,
        )
        for r in knowledge.compatibility_rules
    }


def test_karpenters_table_is_transcribed_as_one_minor_slices() -> None:
    ex, _ = _load("karpenter-minimum-per-minor")
    assert all(
        s.kind.value == "ENUMERATED_MINORS" and len(s.stated_minors) == 1 for s in ex.statements
    )
    assert ex.assessment is not None and ex.assessment.covers_target_kubernetes is True


def test_karpenter_becomes_sequenceable_by_the_models_reading() -> None:
    ex, pages = _load("karpenter-minimum-per-minor")
    candidate = judge(ex, pages)
    knowledge = candidate.knowledge
    assert knowledge is not None
    windows = _windows(knowledge)
    assert windows["1.13.x"] == ("1.35", "1.37"), "widened down to the current minor"
    assert windows["1.9.x"] == ("1.35", "1.36"), "every other row untouched"
    widened = next(
        r for r in knowledge.compatibility_rules if r.component_version_series == "1.13.x"
    )
    assert widened.kubernetes_versions.minimum_inclusive == "1.35"
    plan = plan_moves(knowledge, "1.0.5", ("1.35", "1.36"))
    assert plan.feasible is True
    assert plan.moves[-1].to_series == "1.13.x" and plan.moves[-1].at_kubernetes == "1.35"
    assert candidate.receipt.confidence is not Confidence.HIGH


def test_the_decider_now_offers_the_carrying_series_at_the_current_minor() -> None:
    from eks_addon_readiness.decide.rules import _verdict_from_knowledge

    ex, pages = _load("karpenter-minimum-per-minor")
    knowledge = judge(ex, pages).knowledge
    gap = GapEntry.model_construct(
        workload_ref="HelmRelease/karpenter/karpenter",
        identity="karpenter",
        version="1.0.5",
        version_series="1.0.x",
        status=FactStatus.FACT_AVAILABLE,
        reason="fixture",
    )
    verdict, valid, _f, _r = _verdict_from_knowledge(gap, knowledge, "1.35")
    assert verdict.value == "REQUIRES_COMPONENT_UPGRADE"
    assert "1.13.x" in valid


def test_metrics_servers_rising_floors_are_byte_identical_to_before() -> None:
    """The fallback must not fire where the inference already sequences: 0.9.x spans the path."""
    ex, pages = _load("metrics-server-rising-floors")
    windows = _windows(judge(ex, pages).knowledge)
    assert windows["0.6.x"] == ("1.25", "1.27"), "0.6.4 still not fit for 1.36"
    assert windows["0.9.x"] == ("1.34", None)


def test_without_the_models_covers_target_claim_nothing_is_widened() -> None:
    ex, pages = _load("karpenter-minimum-per-minor")
    assert ex.assessment is not None
    quiet = ex.model_copy(
        update={"assessment": ex.assessment.model_copy(update={"covers_target_kubernetes": None})}
    )
    windows = _windows(judge(quiet, pages).knowledge)
    assert windows["1.13.x"] == ("1.36", "1.37"), "the judge's own inference stands"
