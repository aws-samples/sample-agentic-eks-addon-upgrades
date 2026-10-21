"""The presentation stage: one digested report document, three views of it.

    in    inventory, ledger, assessment, plan, fact store
    out   a digested report document, plus its HTML and Markdown views

WHY THIS IS A STAGE AND NOT A RENDERER BOLTED ONTO ONE
-----------------------------------------------------
Rendering the report inside the research package (Stage 6), which does not run
on every assessment, means the report cannot be produced without also running
research -- and research has no business owning Stage 4 and Stage 5 presentation.
Worse, a report produced strictly downstream of every document with no digest
covering it leaves `verify` blind to the numbers a human reads.

Making presentation a stage fixes both. Its output is a document like any other:
clock-free, citing every upstream digest, carrying its own two fingerprints, and
digesting the rendered bytes so replay reaches the report.

WHAT IS DELIBERATELY NOT NUMBERED
--------------------------------
The stage is called `present`, not "Stage 6". In this project's stage vocabulary 6
is research and 7 is the resolve loop; giving a number a second meaning is exactly
the homonym defect the resolve/resolve rename exists to remove.
"""

from .build import (
    DocumentMismatch,
    approvals_from_research,
    build_document,
    build_report_view,
)
from .model import (
    ABSENT,
    ApprovalControls,
    ComponentGroup,
    ComponentRow,
    PendingApproval,
    PresentationDocument,
    Provenance,
    RecommendedRow,
    ReportTotals,
    ReportView,
    SourceRow,
    StepRow,
    UnresolvedOrigin,
    UnresolvedRow,
)
from .render import to_html, to_markdown
from .ruleset import RENDERER_MODULES, RULESET_MODULES, renderer_digest, ruleset_digest
from .verify import ReportVerification, verify_document

__all__ = [
    "ABSENT",
    "RENDERER_MODULES",
    "RULESET_MODULES",
    "ApprovalControls",
    "ComponentGroup",
    "ComponentRow",
    "DocumentMismatch",
    "PendingApproval",
    "PresentationDocument",
    "Provenance",
    "RecommendedRow",
    "ReportTotals",
    "ReportVerification",
    "ReportView",
    "SourceRow",
    "StepRow",
    "UnresolvedOrigin",
    "UnresolvedRow",
    "approvals_from_research",
    "build_document",
    "build_report_view",
    "renderer_digest",
    "ruleset_digest",
    "to_html",
    "to_markdown",
    "verify_document",
]
