"""THE BOUNDARY between the deterministic loop and the research stage.

This module is the whole interface. Stage 7 knows these three types and nothing
else about research; a research stage knows these three types and nothing else
about the loop. Keeping it this narrow is deliberate: the two are being built
separately, and a wide interface would have to be negotiated rather than merged.

WHAT THE SHAPE ENFORCES, AND WHY IT IS SHAPE RATHER THAN TRUST
------------------------------------------------------------
`ResearchRequest` is the ENTIRE input a research stage receives from the loop. It
carries identifiers and a reason. It has **no field for a verdict, a confidence, a
priority ceiling or an acceptance decision**, so the loop cannot tell a model what
to conclude, and a model reading the request finds nothing to agree with. This is
the same mechanism used on the other side of the boundary, where
`ResearchExtraction` has nowhere to put a confidence:
*the boundary is enforced by shape, not by trust.*

`ResearchOutcome` carries a `disposition` that the research stage COMPUTED
deterministically from checkable signals -- not one a model asserted. The loop
treats it as opaque reporting: it decides whether to resume from **the fact store's
digest changing**, never from this field. So even a research stage that returned
`ACCEPTED` for everything could not make the loop re-run over knowledge that was not
actually written. That is the one property worth being paranoid about, because it is
the only path by which a model could influence a verdict.

WHAT IS DELIBERATELY ABSENT
--------------------------
No method to ask a model *whether* to research. Making Stage 7 an
agent that decides to invoke research was considered and rejected explicitly:
*"if the assessment pipeline were an agent that decides
to invoke research, a model would re-enter the verdict path. Gap detection, dispatch
and resume are all deterministic. The research it dispatches remains fully
agentic."* The queue is computed by `resolve_loop.dispatch_queue`, a pure function
of the gap ledger. There is no hook here for anything else.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import Field

from ..models import ContractModel


class DispatchReason(StrEnum):
    """Why the loop is asking. Reported to research; never an input to its answer.

    ONE reason, decided by deterministic rules over documents:

    - `FACT_MISSING` -- the ledger says no source holds a fact for this component's
      installed series, and both a name and a usable version are known. This is
      Stage 3's `FACT_MISSING`, which is the researchable set by definition.

    THERE USED TO BE A SECOND, `FACT_STALE`, and it went out with the 24-hour expiry
    the project rejected. Staleness here means the upstream source moved, which a sweep
    outside the pipeline decides; it marks the fact, the marked fact stops answering,
    and the ledger then reports the component as FACT_MISSING through the path it
    already had. So a re-research caused by drift arrives with this reason, honestly:
    from Stage 3's position no source holds a usable fact, which is true.

    Keeping the member would have left a value nothing can produce -- a reason a reader
    could filter for and never find, which is worse than one fewer distinction. The
    distinction is not lost either: the sweep's own document names the fact it withheld,
    the revision it was pinned at and the revision upstream now reports.

    A `NOT_ASKABLE` gap is never dispatched under any reason: there is no usable
    version, so there is nothing to look up, and sending it would be asking a
    question nobody can answer.
    """

    FACT_MISSING = "FACT_MISSING"


class ResearchRequest(ContractModel):
    """One component to research. The entire input the research stage receives."""

    component_id: str = Field(min_length=1)
    # The installed series the question is about, e.g. `0.6.x`. Guaranteed present:
    # a gap with no usable series routes to NOT_ASKABLE and is never dispatched.
    version_series: str = Field(min_length=1)
    # The exact installed version the ledger carried, e.g. `0.6.4`. The research
    # assessment answers "does THIS work on the target", which needs the release, not
    # only its series. None when the ledger knew the series but not the version --
    # serialized as null, so the Map's ItemSelector always finds the field.
    installed_version: str | None = None
    # The Helm release name when the component is installed by Helm, read off the
    # ledger's `HelmRelease/<namespace>/<release>` workload ref. It switches the research
    # prompt's chart section on: the model is asked where the chart is published and which
    # chart version ships which application version, because the upgrade lane runs `helm`
    # on exactly those values. None for a managed add-on or a manifest, and serialized as
    # null so the Map's ItemSelector always finds the field.
    helm_release: str | None = None
    reason: DispatchReason
    # Stage 2's assessment priority, carried so a capped dispatch takes the most
    # important gaps first. Ordering information, not a ceiling on the answer.
    priority: str = Field(min_length=1)
    # The ledger this request was derived from. A research result can therefore be
    # tied back to the exact evidence that asked for it, which is what makes the
    # loop auditable after the fact.
    ledger_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ResearchOutcome(ContractModel):
    """What the research stage reports back for one request.

    `disposition` is a value the research stage COMPUTED from checkable signals. The
    loop reads it to report honestly on what happened; it does not act on it. See
    the module docstring.
    """

    component_id: str = Field(min_length=1)
    version_series: str = Field(min_length=1)
    # ACCEPTED -- a fact was written and can drive verdicts.
    # HELD      -- a candidate was produced but did not earn acceptance, so it is
    #              invisible to the store's find() and moves no verdict.
    # FAILED    -- no candidate could be produced at all.
    disposition: Literal["ACCEPTED", "HELD", "FAILED"]
    # The computed confidence label, for reporting. Never an input to a verdict.
    confidence: str | None = None
    # Digest of the candidate fact, so what was written is citable.
    candidate_digest: str | None = None
    detail: str = ""


@runtime_checkable
class ResearchDispatcher(Protocol):
    """The one call the loop makes into research.

    A batch rather than one call per component, so the research stage owns its own
    concurrency, retry and rate limiting -- the loop has no business deciding how
    many model calls happen at once, only *which questions* are legitimate to ask.

    Implementations are expected to be side-effecting on the fact store: an accepted
    candidate is written there. The loop does not pass the store in, and could not write
    it if it wanted to -- the dispatch stage's own role has no `PutItem` on the fact
    table.

    Corrected from "no stage role has `PutItem`": the `-research` role does hold it, and
    never uses it. See `stages/resolve.py`'s first bullet for the full picture.
    """

    def research(
        self, requests: tuple[ResearchRequest, ...]
    ) -> tuple[ResearchOutcome, ...]:
        """Research each request and report what happened. Order need not be kept."""
        ...


class NullDispatcher:
    """A dispatcher that researches nothing, and says so.

    Not a test double -- it is the production dispatcher until the research stage is
    wired in, and it is what makes the loop deployable and observable on its own. The
    loop still computes and publishes its queue, so *what would have been researched*
    is inspectable before any model exists to research it.

    It is also the honest answer for a deployment that has deliberately not enabled
    research: gaps stay gaps, reported as gaps.
    """

    def research(
        self, requests: tuple[ResearchRequest, ...]
    ) -> tuple[ResearchOutcome, ...]:
        return tuple(
            ResearchOutcome(
                component_id=r.component_id,
                version_series=r.version_series,
                disposition="FAILED",
                detail=(
                    "No research stage is wired in, so this gap was queued and not "
                    "researched. It remains an honest gap rather than a guess."
                ),
            )
            for r in requests
        )
