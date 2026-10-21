"""The upgrade brief -- what a platform engineer needs to know before approving a component move.

    in    one plan component-move step, and the project's own sources
    out   five sections: what BREAKS, what is DEPRECATED, what CHANGED that you would notice,
          what to WATCH FOR after installing, and what is worth TURNING ON -- with the Helm
          values to do it

**IT IS ADVISORY AND IT CANNOT BLOCK.** If a brief cannot be produced the document says so
plainly, per section, and the upgrade approval proceeds anyway. Nothing in this package raises for
a network condition, a rate limit or a model failure; every one of those becomes a `NOT_LOOKED`
section with a reason. The person reading the approval decides whether to upgrade -- this document
informs that decision and never gates it. That is the opposite property from the approval wait,
where a run legitimately fails because a signature did not arrive, and the difference is
deliberate.

**IT HAS ITS OWN AGENT, and that is the load-bearing decision.** Not the research agent, and not
a single-shot call. Release notes are not in one predictable place -- GitHub Releases, a
CHANGELOG.md, an UPGRADING.md, a per-minor upgrade guide on a docs site, the body of a release
pull request -- and the same project may move them halfway through the version range one hop
covers. So this agent searches, with its own prompt, its own tools and its own Harness.
`prompt.py` records why sharing the research agent would have been wrong: its instruction is
"transcribe, do not reason past the page", this one's is "read the CHANGELOG and judge what an
operator would care about", and one prompt cannot say both.

**IT REUSES THE RESEARCH SUBSTRATE AND NOT THE RESEARCH GATE.** Kept: the fetch-and-record path,
where a fetched body is written to `research-evidence/` BEFORE anything reads it, and quote
verification against exactly those bytes (`research/verify.py`, `research/pages.py`,
`stages/fetch_source.py`). Not kept: the acceptance gate in `research/bounds.py` and
`research/acceptance.py`, which exists to refuse any claim that is not a stated support bound.
Breaking changes, known issues and nice-to-haves are not support bounds; that gate would either
reject all of them or have to be opened, and opening it is how the fact store stops being worth
trusting.

**IT WRITES NO FACT.** No PutItem, no candidate under `facts-candidates/`. Its output is an
advisory document, which is what frees it to carry uncertainty a fact may not: *"this reads as a
breaking change, here is the quote"* is a legitimate line in a brief and an inadmissible one in
the knowledge base.

**IT IS A POINT-IN-TIME OBSERVATION.** Open issues change daily and projects edit release notes,
so two briefs of the same plan may legitimately disagree -- and the document says
`reproducible: false` rather than leaving a reader to assume the digest discipline of the
assessment and the plan applies here. Same reasoning that places the freshness sweep outside the
digest chain.

The split within the package:

    agent side                     deterministic side
    ---------------------------    ------------------------------------------------------
    boundary.py  what the agent    walk.py      which releases the hop crosses, and which
                 may say                        minors it must therefore account for
    prompt.py    its instruction,  issues.py    the issue query: FOUND, NONE_OPEN or
                 its tools, its                 NOT_LOOKED, never a silent empty list
                 output schema     document.py  the document, every quote checked, and the
                                                two cross-checks the agent cannot talk past

No module here imports a model client or boto3. The Harness invocation is transport and lives in
`stages/brief_harness.py`; the two tools are `stages/fetch_source.py` (shared with the research
agent, unchanged) and `stages/fetch_issues.py`.
"""

from __future__ import annotations

from .boundary import (
    ActionPhase,
    BriefExtraction,
    BriefSection,
    BriefSource,
    ChangeBasis,
    EmptySectionNote,
    FeatureFinding,
    Finding,
    IssueFinding,
    PlatformConcern,
    SequencingNote,
)
from .document import (
    BRIEFABLE_STEP_KINDS,
    BriefItem,
    BriefSectionView,
    Citation,
    ComponentBrief,
    CoverageView,
    QuoteVerdict,
    SectionState,
    SequencingView,
    UnbriefedStep,
    UpgradeBrief,
    component_brief,
    coverage_view,
    unavailable_sections,
    unbriefable_reason,
)
from .issues import (
    DEFAULT_LIMIT,
    IssueLookup,
    IssueLookupOutcome,
    IssueSearch,
    IssueSighting,
    StaticIssueSearch,
    build_query,
    classify_response,
    not_looked,
    repository_slug,
    search_url,
    sightings_from_payload,
)
from .prompt import REASK, SYSTEM_PROMPT, extraction_schema, tools, user_prompt
from .reuse import MAX_REUSE_AGE_DAYS, StoredBrief, expiry_note, serve, written_on
from .ruleset import RULESET_MODULES, ruleset_digest
from .walk import (
    InstalledPrecision,
    MinorEnumeration,
    ReleaseNote,
    ReleaseWalk,
    plan_walk,
    releases_from_listing,
)

__all__ = [
    "BRIEFABLE_STEP_KINDS",
    "DEFAULT_LIMIT",
    "MAX_REUSE_AGE_DAYS",
    "REASK",
    "RULESET_MODULES",
    "SYSTEM_PROMPT",
    "ActionPhase",
    "BriefExtraction",
    "BriefItem",
    "BriefSection",
    "BriefSectionView",
    "BriefSource",
    "ChangeBasis",
    "Citation",
    "ComponentBrief",
    "CoverageView",
    "EmptySectionNote",
    "FeatureFinding",
    "Finding",
    "InstalledPrecision",
    "IssueFinding",
    "IssueLookup",
    "IssueLookupOutcome",
    "IssueSearch",
    "IssueSighting",
    "MinorEnumeration",
    "PlatformConcern",
    "QuoteVerdict",
    "ReleaseNote",
    "ReleaseWalk",
    "SectionState",
    "SequencingNote",
    "SequencingView",
    "StaticIssueSearch",
    "StoredBrief",
    "UnbriefedStep",
    "UpgradeBrief",
    "build_query",
    "classify_response",
    "component_brief",
    "coverage_view",
    "expiry_note",
    "extraction_schema",
    "not_looked",
    "plan_walk",
    "releases_from_listing",
    "repository_slug",
    "ruleset_digest",
    "search_url",
    "serve",
    "sightings_from_payload",
    "tools",
    "unavailable_sections",
    "unbriefable_reason",
    "user_prompt",
    "written_on",
]
