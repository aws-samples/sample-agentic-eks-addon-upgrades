"""Replay verification for the report itself.

THE GAP THIS CLOSES. A `verify_report` that re-derives only the Stage 2 *document* and
compares digests leaves rendering -- which happens strictly downstream of every document
and is covered by no digest -- structurally invisible to `verify`: a wrong count on the
report passes on the day the Unresolved tile and the truth disagree. The replay guarantee
the whole design exists to provide then stops one step short of the report.

Here the report is a digested document whose rendered bytes are digested inside
it, so re-deriving it from the same upstream documents checks both halves: the
values, and the bytes a human sees.

Deliberately NOT in the presentation ruleset (`view/ruleset.py` lists `model` and
`build` only). Verification is a check on the report, not part of what the report
says, so editing it must not move any report digest.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ..decide import ReadinessAssessment
from ..facts import FactStore, GapLedger
from ..models import ContractModel
from ..order import UpgradePlan
from ..report import InventoryReport
from .build import _sha256, build_document
from .model import PresentationDocument


class ReportVerification(ContractModel):
    """Outcome of re-deriving a stored report from the documents it cites."""

    schema_version: Literal["1.0"] = "1.0"
    reproduced: bool
    # Split three ways, because "the stylesheet changed" and "a number changed"
    # are not the same finding and must not present as one.
    values_reproduced: bool
    html_reproduced: bool
    markdown_reproduced: bool
    stored_digest: str = Field(min_length=1)
    recomputed_digest: str = Field(min_length=1)
    stored_ruleset: str = Field(min_length=1)
    current_ruleset: str = Field(min_length=1)
    stored_renderer: str = Field(min_length=1)
    current_renderer: str = Field(min_length=1)
    detail: str = Field(min_length=1)

    @property
    def ruleset_changed(self) -> bool:
        return self.stored_ruleset != self.current_ruleset

    @property
    def renderer_changed(self) -> bool:
        return self.stored_renderer != self.current_renderer

def verify_document(
    stored: PresentationDocument,
    assessment: ReadinessAssessment,
    plan: UpgradePlan,
    inventory: InventoryReport,
    ledger: GapLedger,
    store: FactStore | None = None,
) -> ReportVerification:
    """Re-derive a stored report from its upstream documents and compare.

    A `verify_report` that re-derives only the Stage 2
    *document* leaves rendering -- strictly downstream of every document, with no
    digest covering it -- a place a wrong count on the report is invisible to verify by
    construction. Here the report is a document, its rendered bytes are digested
    inside it, and both halves are re-checked.

    The two ways a report can fail to reproduce mean very different things, so
    they are distinguished rather than merged:

      the rules changed    expected after a presentation edit; the stored report
                           remains a true record of what was displayed
      the rules are the    a defect. The same values and the same renderers must
      same but the bytes   produce the same bytes
      differ

    THE APPROVALS BLOCK IS RE-RENDERED FROM THE STORED DOCUMENT, not re-derived
    from a research result. It has to be: it is a carried input, and the research
    stage that produced it does not run on every execution, so there is nothing
    upstream to re-derive it from. What that buys is still exactly what is needed
    -- rendering remains provably a pure function of (values, provenance, carried
    controls), which is the property the html and markdown digests exist to check.

    What it does NOT claim is that the approvals block itself is authentic, and it is
    worth being exact about how far the endpoint's own checks make up for that,
    because an earlier version of this note overstated it.

    The endpoint re-derives the candidate digest and the report digest and refuses a
    mismatch, so a tampered block cannot approve a fact whose digests it invents. But
    digests are not human-readable, and the earlier claim -- that tampering "can only
    cause a refused approval" -- was false: swapping one candidate's PROSE for another
    candidate's consistent triple leaves every digest check passing while a reader
    approves a component they never read about. The block is therefore also required
    to name the component and version series, and the endpoint checks both against the
    candidate it loaded, which is what actually closes that gap.

    The remaining honest limit: this document's own digest covers the approvals block,
    so tampering is detectable by comparing digests -- it is not prevented here.
    """
    document, html, markdown = build_document(
        assessment, plan, inventory, ledger, store, stored.approvals
    )
    stored_digest, recomputed_digest = stored.digest(), document.digest()
    values_match = stored.view_digest == document.view_digest
    html_match = stored.html_digest == _sha256(html)
    markdown_match = stored.markdown_digest == _sha256(markdown)
    rules_changed = stored.ruleset_digest != document.ruleset_digest
    renderers_changed = stored.renderer_digest != document.renderer_digest

    if stored_digest == recomputed_digest:
        detail = (
            "Reproduced exactly. The same documents through the same presentation "
            "rules and renderers produced a byte-identical report."
        )
    elif not values_match and rules_changed:
        detail = (
            "Did not reproduce: the presentation rules changed, so the displayed "
            "values moved. The stored report remains a valid record of what was "
            "displayed at the time."
        )
    elif values_match and not (html_match and markdown_match) and renderers_changed:
        detail = (
            "The numbers are identical; only the rendering changed. This is a "
            "presentation edit, not a change to what the report says."
        )
    elif not values_match:
        detail = (
            "DEFECT: the presentation rules are unchanged but the displayed values "
            "differ. The same documents must produce the same numbers."
        )
    else:
        detail = (
            "DEFECT: the values and the renderers are unchanged but the rendered "
            "bytes differ. Rendering must be a pure function of the value set."
        )

    return ReportVerification(
        reproduced=stored_digest == recomputed_digest,
        values_reproduced=values_match,
        html_reproduced=html_match,
        markdown_reproduced=markdown_match,
        stored_digest=stored_digest,
        recomputed_digest=recomputed_digest,
        stored_ruleset=stored.ruleset_digest,
        current_ruleset=document.ruleset_digest,
        stored_renderer=stored.renderer_digest,
        current_renderer=document.renderer_digest,
        detail=detail,
    )
