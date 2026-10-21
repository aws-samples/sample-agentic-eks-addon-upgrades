"""Stage 6 -- research an unknown component into a judged, cited compatibility fact.

The one place a model runs, and the tight box it runs in. The split is the package's own
structure, not a convention:

    model side                      deterministic side
    ----------------------------    ----------------------------------------------
    boundary.py  what a model may   bounds.py      what a source's words imply
                 say                verify.py      quote vs fetched bytes
    extract.py   the model loop     authority.py   how much a source is worth
    sources/     fetching           signals.py     confidence
                                    acceptance.py  accept / hold / reject
                                    judge.py       the whole judgement
                                    freshness.py   when a fact stops answering

Nothing on the deterministic side imports `extract.py` or `sources/`, and `judge.py` takes
its evidence as an argument rather than fetching any -- so the judging half runs, and is
tested, with no model and no network.
"""

from __future__ import annotations

from .acceptance import advisory_note, classify_direction, decide, summarize
from .approval import ApprovalRefused, approve, is_pending
from .authority import SourceAuthority, classify_source
from .boundary import (
    BoundKind,
    CompatibilityStatementInput,
    ExtractedClaimInput,
    ReleaseSightingInput,
    ResearchExtraction,
    SourceRef,
)
from .bounds import derive_windows, next_minor, tested_windows
from .candidate import CandidateFact
from .freshness import (
    DriftResult,
    GitHubUpstreamResolver,
    StaticUpstreamResolver,
    SweepDocument,
    SweepFinding,
    SweepMode,
    SweepOutcome,
    UpstreamRelease,
    cited_urls,
    github_repository,
    is_stale,
    sweep,
    sweep_document,
)
from .judge import judge
from .marks import FactMark, MarkKind, MarkLedger, MarkRetirement
from .pages import FetchedPage, PageSet
from .receipt import (
    AcceptanceDecision,
    ApprovalRecord,
    ChangeDirection,
    Contradiction,
    ResearchReceipt,
    SignalName,
    SignalResult,
)
from .render_html import render_approval_html
from .report import ApprovalReport, QuotedClaim, build_report, verdict_delta
from .ruleset import RULESET_MODULES, ruleset_digest
from .verify import verify_extraction

__all__ = [
    "RULESET_MODULES",
    "AcceptanceDecision",
    "ApprovalRecord",
    "ApprovalRefused",
    "ApprovalReport",
    "BoundKind",
    "CandidateFact",
    "ChangeDirection",
    "CompatibilityStatementInput",
    "Contradiction",
    "DriftResult",
    "ExtractedClaimInput",
    "FactMark",
    "FetchedPage",
    "GitHubUpstreamResolver",
    "MarkKind",
    "MarkLedger",
    "MarkRetirement",
    "PageSet",
    "QuotedClaim",
    "ReleaseSightingInput",
    "ResearchExtraction",
    "ResearchReceipt",
    "SignalName",
    "SignalResult",
    "SourceAuthority",
    "SourceRef",
    "StaticUpstreamResolver",
    "SweepDocument",
    "SweepFinding",
    "SweepMode",
    "SweepOutcome",
    "UpstreamRelease",
    "advisory_note",
    "approve",
    "build_report",
    "cited_urls",
    "classify_direction",
    "classify_source",
    "decide",
    "derive_windows",
    "github_repository",
    "is_pending",
    "is_stale",
    "judge",
    "next_minor",
    "render_approval_html",
    "ruleset_digest",
    "summarize",
    "sweep",
    "sweep_document",
    "tested_windows",
    "verdict_delta",
    "verify_extraction",
]
