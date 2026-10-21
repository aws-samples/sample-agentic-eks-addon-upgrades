"""Stage 7 — the resolve loop: from a knowledge gap to a resolved plan.

Two modules, and the split is the boundary:

    resolve_loop.py   the deterministic rules -- which gaps to research and whether
                      the enlarged store warrants a re-run. Pure functions of
                      documents. No model, ever, and no clock either: whether a
                      stored fact still answers is decided outside the pipeline by
                      the drift sweep in `research/freshness.py`.
    dispatch.py       the ENTIRE interface to the research stage: a request that
                      has nowhere to put a verdict, an outcome the loop treats as
                      reporting, and one Protocol method.

Named `resolve_loop` rather than `resolve`, to avoid a homonym with Stage 2's
`inventory/resolve.py` -- one resolves a knowledge gap, the other resolves a
version. Both are live and share no symbols, so the name is the only thing that
distinguishes them.
"""

from .dispatch import (
    DispatchReason,
    NullDispatcher,
    ResearchDispatcher,
    ResearchOutcome,
    ResearchRequest,
)
from .resolve_loop import (
    DispatchDocument,
    NotDispatchedReason,
    ResumeDecision,
    dispatch_queue,
    resume_decision,
)
from .ruleset import RULESET_MODULES, ruleset_digest

__all__ = [
    "RULESET_MODULES",
    "DispatchDocument",
    "DispatchReason",
    "NotDispatchedReason",
    "NullDispatcher",
    "ResearchDispatcher",
    "ResearchOutcome",
    "ResearchRequest",
    "ResumeDecision",
    "dispatch_queue",
    "resume_decision",
    "ruleset_digest",
]
