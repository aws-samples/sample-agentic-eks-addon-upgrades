"""The upgrade brief: one advisory document per run, five sections per moved component.

    in    the plan's component-move steps, and one brief-agent run per step
    out   one document, keyed by plan step order: what breaks, what is deprecated, what
          changed that you would notice, what to watch for, and what is worth turning on

**ADVISORY, NEVER BLOCKING, AND THE DOCUMENT SAYS SO IN A FIELD.** `advisory` is a literal on
the contract rather than a property of the code that happens to be true today. The approval
surface reads this beside the plan; if it is missing, partial, or says every section failed, the
approval proceeds and the person decides. Nothing here may make a run fail, which is why the
absence vocabulary below exists instead of an exception.

**EVERY SECTION CARRIES ITS OWN ABSENCE, AND ONLY ONE OF THE THREE IS AN ALL-CLEAR.**

    REPORTED    we looked and there is content
    NONE_FOUND  we looked and the project announced nothing -- readable as reassurance
    NOT_LOOKED  we could not look or could not find the material -- NOT readable as reassurance

The agent supplies which of the last two applies, per section, with a reason and the pages it
checked; `section_of` maps that through unchanged. An absence is only credible if it can show
where it looked, which is why `looked_in` survives into the document.

**BULLETS FIRST, CITATIONS AT THE END.** Each finding carries `quote` and `source` because the
quote is checked byte-for-byte against bytes recorded before the agent read them -- that is what
makes this document evidence rather than assertion. But `BriefSectionView.sources` is a
DEDUPLICATED list derived from those citations, so a renderer shows clean bullets and then one
source list per section. The ask was explicitly for understanding in points and the resources at
the end; both halves are satisfied without either being weakened.

**IT IS A POINT-IN-TIME OBSERVATION, NOT A REPRODUCIBLE DOCUMENT.** `reproducible: false` sits
beside `observed_at` for exactly the reason `research/freshness.py` places the sweep outside the
digest chain: open issues change daily and projects edit release notes, so the same plan
legitimately produces two different briefs an hour apart. A reader who finds two disagreeing has
learned something true. The document still has a `digest()` -- so the payload can cite it and the
key can carry it, as the sweep's does -- but no stage reads it and nothing derives from it.

**IT WRITES NO FACT, EVER.** No candidate, no PutItem. That is what frees every finding to
carry `ChangeBasis.INFERRED`: *"this reads as a breaking change, here is the quote"* is a
legitimate line in an advisory document and an inadmissible one in the knowledge base, whose
gate exists to refuse anything that is not a stated support bound.

**THE JOIN KEY IS THE PLAN STEP ORDER.** A grant authorises named steps (`execute/grant.py`), a
review page renders one row per step, and this document is addressed the same way:
`brief_for(order)`. Keying on the component name would break the moment a plan moved one
component twice, which `PlanStep` explicitly permits.

**TWO CROSS-CHECKS THE AGENT CANNOT TALK ITS WAY PAST.** Coverage: what it says it covered is
compared against the minors the hop actually enters, and the difference is reported. And an
issue section claiming the project announced nothing is DOWNGRADED to NOT_LOOKED when the
recorded tool receipts show a search that never ran -- because a rate-limited query reported as
an all-clear is the single most dangerous output this document could produce.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from ..models import ClusterReference, ContractModel
from ..order.plan import PlanStep, StepKind
from ..research.pages import PageSet
from ..research.verify import QuoteCheck, check_quote
from .boundary import (
    ActionPhase,
    BriefExtraction,
    BriefSection,
    BriefSource,
    ChangeBasis,
    EmptySectionNote,
    PlatformConcern,
)
from .walk import InstalledPrecision, MinorEnumeration, ReleaseWalk

# Step kinds that get a brief. Both component kinds, and that is a decision rather than a
# convenience: `execute/grant.py::EXECUTABLE_STEP_KINDS` authorises both, so both appear as a row
# a person clicks to approve, and a row a person can approve with no brief is exactly the gap
# this document closes. A CLUSTER_UPGRADE is not here -- this system does not move a control
# plane, and there is no installed-to-recommended jump to walk.
#
# Listed explicitly rather than derived from "everything except CLUSTER_UPGRADE", so adding a
# step kind to the plan does not silently make it briefable and produce an empty brief for it.
BRIEFABLE_STEP_KINDS: frozenset[StepKind] = frozenset(
    {StepKind.COMPONENT_MOVE, StepKind.COMPONENT_UPGRADE_RECOMMENDED}
)


# Install methods whose components this stage does not brief.
#
# AWS PUBLISHES AND QUALIFIES THESE VERSIONS ITSELF, so there is nothing for an agent to research:
# the version to move to comes from the EKS add-on catalogue rather than from a project's release
# notes, AWS has already decided the version is compatible with the cluster's Kubernetes minor, and
# there is no Helm chart for an operator to reconfigure. Briefing them spent a multi-minute model
# loop per component to report on repositories the fact store does not even record -- four of the
# seven components in a live run, each one arriving with `repository: None`.
#
# MATCHED ON THE INSTALL METHOD, NEVER ON A NAME. `tests/stage6_checks.py` asserts that no
# component name appears in any value or identifier this code acts on, and it is right to: a list
# of add-on names here would be a list to keep in step with AWS's catalogue forever, and it would
# silently stop excluding the moment AWS added one. The inventory already records how each
# component was installed, which is the durable signal.
#
# The string is the one `inventory/resolve.py` writes, not `models.InstallationMethod.EKS_ADDON`.
# Those are two different vocabularies for the same idea and the report carries the former; using
# the enum here would match nothing and quietly exclude no component at all.
EXCLUDED_INSTALL_METHODS: frozenset[str] = frozenset({"EKS_MANAGED_ADDON"})


def excluded_reason(install_method: str | None) -> str | None:
    """Why this component is not briefed at all, or None if it should be.

    Distinct from `unbriefable_reason`, which is about a step this stage COULD brief and cannot.
    This is a step it deliberately does not, and the two must not be conflated: one is a gap in
    what we know and the other is a decision about what is worth knowing.
    """
    if install_method and install_method.strip().upper() in EXCLUDED_INSTALL_METHODS:
        return (
            f"installed as {install_method}, so AWS publishes and qualifies this version itself: "
            "the target comes from the EKS add-on catalogue rather than a project's release notes, "
            "AWS has already established it works on this Kubernetes minor, and there is no Helm "
            "chart to reconfigure. Nothing here needs researching, so no brief was requested."
        )
    return None


def unbriefable_reason(step: PlanStep) -> str | None:
    """Why this step cannot be briefed, or None if it can.

    Checked before the agent is invoked, so a step that could never carry a brief costs no
    model call and appears in `unbriefed` with a reason instead of as a brief with five empty
    sections.
    """
    if step.kind not in BRIEFABLE_STEP_KINDS:
        return (
            f"a {step.kind} step moves no component version, so there is no installed-to-"
            "recommended jump to read release notes across"
        )
    if not (step.component and step.to_series and step.at_kubernetes):
        return (
            "the step names no component, no target series or no Kubernetes version, so there "
            "is nothing to brief against"
        )
    return None


class SectionState(StrEnum):
    """Whether a section has content, has nothing, or could not be produced."""

    REPORTED = "REPORTED"
    NONE_FOUND = "NONE_FOUND"
    NOT_LOOKED = "NOT_LOOKED"


class QuoteVerdict(StrEnum):
    """How one quote fared against the bytes recorded before the agent read them.

    The same verdicts `research/verify.py::QuoteCheck` produces, restated as a closed enum
    because this one goes into a document: a verdict field that could hold an unrecognised
    string is a field a reader cannot act on. `_verdict_of` is the only mapping point, and
    `tests/test_one_brief_failing_does_not_drop_the_others.py` fails if the two lists drift.
    """

    PRESENT = "PRESENT"
    PRESENT_WHITESPACE = "PRESENT_WHITESPACE"
    VERIFIED_SIBLING_PAGE = "VERIFIED_SIBLING_PAGE"
    WRONG_PAGE = "WRONG_PAGE"
    ABSENT = "ABSENT"
    NOT_FETCHED = "NOT_FETCHED"

    @property
    def verified(self) -> bool:
        """Whether a reader may treat the quote as the source's own words.

        `VERIFIED_SIBLING_PAGE` counts, as it does in `QuoteCheck.verified`: the quote is in
        fetched bytes, on a sibling page of the one cited.
        """
        return self in (
            QuoteVerdict.PRESENT,
            QuoteVerdict.PRESENT_WHITESPACE,
            QuoteVerdict.VERIFIED_SIBLING_PAGE,
        )


def _verdict_of(check: QuoteCheck) -> QuoteVerdict:
    """The document's verdict for one quote check.

    A verdict this enum does not know is reported as `NOT_FETCHED` (unverified) rather than
    raised: a new verdict in `verify.py` must cost one quote its "verified" mark, never the
    whole brief.
    """
    try:
        return QuoteVerdict(check.verdict)
    except ValueError:
        return QuoteVerdict.NOT_FETCHED


class Citation(ContractModel):
    """One source, as it appears in a section's list at the end. Deduplicated by url+revision."""

    url: str = Field(min_length=1)
    revision: str = ""
    label: str = Field(min_length=1)


class BriefItem(ContractModel):
    """One bullet: the reader's half, then the evidence half.

    The first four fields are what a renderer shows. The rest is what makes it checkable, and it
    is carried on every item rather than summarised, because an item whose quote failed must be
    identifiable individually -- that is the difference between "this brief has one unverified
    claim" and "this brief is unverified".
    """

    title: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    release: str | None = None
    concern: PlatformConcern
    action: str | None = None
    action_phase: ActionPhase = ActionPhase.NO_ACTION
    basis: ChangeBasis

    # The issue-specific fields. Present only on a KNOWN_ISSUES item; None elsewhere rather
    # than a separate model, so one renderer walks every section.
    issue_url: str | None = None
    issue_number: int | None = None
    trail: str | None = None
    affects: str | None = None
    workaround: str | None = None

    # The feature-specific fields, present only on a NICE_TO_HAVES item.
    why_it_helps: str | None = None
    helm_values: str | None = None
    helm_set: str | None = None
    enablement: str | None = None

    quote: str = Field(min_length=1)
    source: Citation
    quote_verdict: QuoteVerdict
    quote_detail: str = Field(min_length=1)
    page_digest: str | None = None
    # Whether `release` is one the walk actually crossed. False flags a finding attributed to a
    # release outside the hop, which is either a mis-attribution or notes the agent read from
    # somewhere it should not have.
    in_walk: bool = True


class BriefSectionView(ContractModel):
    """One section: its state, its bullets, and its citations at the end.

    `sources` is derived from the items' own citations (or from the agent's `looked_in` when the
    section is empty), deduplicated in stable order. A renderer prints `items` then `sources`
    and satisfies the whole ask without any link appearing mid-bullet.
    """

    section: BriefSection
    state: SectionState
    reason: str = Field(min_length=1)
    items: tuple[BriefItem, ...] = ()
    sources: tuple[Citation, ...] = ()

    @model_validator(mode="after")
    def state_matches_contents(self) -> BriefSectionView:
        """Refuse a section whose state disagrees with its own contents.

        The discipline `SweepDocument` applies to its headline and `DispatchDocument` to its
        empty queue: an inconsistent evidence document must not be constructible, because a
        reader who cannot trust the summary reads the detail every time and will not.
        """
        if self.state is SectionState.REPORTED and not self.items:
            raise ValueError(
                f"a REPORTED {self.section} section carries no items. Use NONE_FOUND for "
                "'the project announced nothing' or NOT_LOOKED for 'we could not look'."
            )
        if self.state is not SectionState.REPORTED and self.items:
            raise ValueError(
                f"a {self.state} {self.section} section carries {len(self.items)} item(s). "
                "The state and the contents must be the same claim."
            )
        return self

    @property
    def unverified(self) -> int:
        return sum(1 for item in self.items if not item.quote_verdict.verified)


class SequencingView(ContractModel):
    """Whether the hop may be taken directly, or must pass through named releases.

    Its own field on the brief rather than a section, because it is not a list of things to read
    -- it is a constraint on the plan, and an operator who finds it after the fact has already
    broken it.
    """

    must_install_in_order: tuple[str, ...] = ()
    detail: str = Field(min_length=1)
    basis: ChangeBasis
    quote: str = Field(min_length=1)
    source: Citation
    quote_verdict: QuoteVerdict
    quote_detail: str = Field(min_length=1)

    @property
    def direct_hop_allowed(self) -> bool:
        """Whether anything must be installed on the way. Derived, never stored."""
        return not self.must_install_in_order


class CoverageView(ContractModel):
    """What the hop crosses, against what the agent says it read.

    **THE MOST LOAD-BEARING FIELD HERE IS `minors_unread`.** metrics-server at 0.6.4 moving to
    0.9.0 has its breaking changes in 0.7's, 0.8's and 0.9's notes. A brief assembled from 0.7
    and 0.9 that does not say 0.8 was unread reads as complete and is not, and absence of a
    warning is read as absence of a hazard.
    """

    installed: str = Field(min_length=1)
    recommended: str = Field(min_length=1)
    precision: InstalledPrecision
    enumeration: MinorEnumeration
    releases_on_path: tuple[str, ...] = ()
    releases_read: tuple[str, ...] = ()
    minors_entered: tuple[str, ...] = ()
    minors_covered: tuple[str, ...] = ()
    minors_unread: tuple[str, ...] = ()
    note: str = Field(min_length=1)

    @property
    def complete(self) -> bool:
        """Whether every minor the hop enters was accounted for."""
        return not self.minors_unread


class ComponentBrief(ContractModel):
    """One plan step's brief. Addressed by step order, as a grant is.

    Every field above the sections is copied from the `PlanStep`, so the brief shown beside a
    step can be checked against the step it claims to describe -- the same reasoning that put
    `StepFingerprint` on the execution grant.
    """

    step_order: int = Field(ge=1)
    step_kind: StepKind
    component: str = Field(min_length=1)
    from_series: str | None = None
    to_series: str = Field(min_length=1)
    recommended_version: str | None = None
    at_kubernetes: str = Field(pattern=r"^\d+\.\d+$")
    # The CHART version that carries the recommended application version, when the agent found
    # the mapping. Operators upgrade charts; the plan names an application version, and for most
    # CNCF add-ons those are different numbers.
    chart_version: str | None = None

    coverage: CoverageView | None = None
    sequencing: SequencingView | None = None
    sections: tuple[BriefSectionView, ...] = Field(min_length=1)

    # WHEN THIS COMPONENT'S BRIEF WAS PRODUCED, which is not necessarily when the run happened.
    # A brief is stored under the upgrade it describes and served again for up to a week
    # (`brief/reuse.py`), so a reader comparing this against the run's own `observed_at` can see
    # that they are looking at a five-day-old reading of an issue tracker. Required rather than
    # optional: a cached answer indistinguishable from a fresh one is the whole hazard of reuse.
    observed_at: str = Field(min_length=1)

    # Whether this brief was served from storage or researched now, in words. Carried into the
    # document rather than derived from the two timestamps, because "served from a brief written 3
    # days ago" is what a reader needs and subtracting two ISO strings is not something a renderer
    # should be doing.
    reuse_note: str = Field(min_length=1)

    # The `research-evidence/session=<id>/` prefix the bytes behind every quote were recorded
    # under, before anything read them. None when nothing was fetched for this step.
    evidence_session: str | None = None

    @model_validator(mode="after")
    def one_view_per_section(self) -> ComponentBrief:
        """Five sections, each exactly once. A page rendering one must not find two."""
        seen = [s.section for s in self.sections]
        missing = [s for s in BriefSection if s not in seen]
        if missing:
            raise ValueError(
                f"sections is missing {[str(s) for s in missing]}. Every section appears, even "
                "when it could not be produced -- an omitted section reads as 'nothing to say' "
                "and that is the one thing this document must never imply."
            )
        if len(seen) != len(set(seen)):
            raise ValueError("a section appears more than once")
        return self

    def section(self, section: BriefSection) -> BriefSectionView:
        """One section by name. Total, because `one_view_per_section` guarantees it."""
        return next(s for s in self.sections if s.section is section)

    def digest_of_self(self) -> str:
        """Content digest of this one component's brief.

        Named unlike the run document's `digest()` on purpose: this is not a stage document and
        nothing cites it in a chain. It exists so `component_brief_key` can put it in the leaf, so
        two briefs of the same upgrade that DISAGREE are two objects rather than one overwriting
        the other -- `sweep_key`'s reasoning, applied to an artifact that reads an issue tracker.
        """
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    @property
    def unverified(self) -> int:
        """How many bullets did not trace to the recorded bytes. Derived, never stored."""
        return sum(s.unverified for s in self.sections)

    @property
    def out_of_walk(self) -> int:
        """How many bullets were attributed to a release outside the hop."""
        return sum(1 for s in self.sections for i in s.items if not i.in_walk)

    @property
    def actions(self) -> tuple[BriefItem, ...]:
        """Every bullet that needs doing, ordered as a runbook: before, during, then after.

        The reader's real question is not "what changed" but "what do I do and when". Derived
        rather than stored so it can never disagree with the sections it comes from.
        """
        order = {
            ActionPhase.BEFORE_UPGRADE: 0,
            ActionPhase.DURING_UPGRADE: 1,
            ActionPhase.AFTER_UPGRADE: 2,
        }
        acting = [
            item
            for s in self.sections
            for item in s.items
            if item.action_phase is not ActionPhase.NO_ACTION
        ]
        return tuple(sorted(acting, key=lambda i: (order[i.action_phase], i.title)))

    @property
    def all_sections_unavailable(self) -> bool:
        """Whether nothing at all could be produced for this step.

        The approval surface needs this to say "no brief is available, decide without it" rather
        than rendering five empty panels that read as five all-clears.
        """
        return all(s.state is SectionState.NOT_LOOKED for s in self.sections)


class UnbriefedStep(ContractModel):
    """A step that got no brief at all, named rather than omitted.

    An omission reads as "nothing to say about this step". A component whose sources were
    unreachable is exactly the case where a reader most needs to know the silence is ignorance.
    """

    step_order: int = Field(ge=1)
    component: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ExcludedStep(ContractModel):
    """A step this stage deliberately did not brief, and why.

    A THIRD CATEGORY, NOT A KIND OF `UnbriefedStep`, and the separation is the point. `unbriefed`
    means "we could not tell you" -- its whole job is to stop silence reading as safety. An EKS
    managed add-on is not silence: AWS decided the version and qualified it, so there is nothing to
    report. Filing it under `unbriefed` would dilute the one signal that matters there, and a reader
    scanning for gaps would find four entries that are not gaps.
    """

    step_order: int = Field(ge=1)
    component: str = Field(min_length=1)
    install_method: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class UpgradeBrief(ContractModel):
    """The run's advisory document: one brief per moved component, plus what got none.

    Cites the plan it was written against and the fact store that resolved the components, so a
    reader can tell whether a changed brief came from a changed plan, changed knowledge, changed
    rules, or from upstream simply being different today. It cites no snapshot, ledger or
    assessment digest: it derives nothing from them, and citing a digest a document does not
    depend on is a reproducibility claim it cannot keep.
    """

    schema_version: Literal["1.0"] = "1.0"
    # Not a number, for the reason `SweepDocument` is not one: the numbered stages are the
    # pipeline's document chain, and this document is not in it.
    stage: Literal["brief"] = "brief"
    advisory: Literal[True] = True
    reproducible: Literal[False] = False
    observed_at: str = Field(min_length=1)

    cluster: ClusterReference
    target_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    store_digest: str = Field(min_length=1)
    ruleset_digest: str = Field(min_length=1)

    briefs: tuple[ComponentBrief, ...] = ()
    unbriefed: tuple[UnbriefedStep, ...] = ()
    excluded: tuple[ExcludedStep, ...] = ()

    @model_validator(mode="after")
    def every_step_appears_once(self) -> UpgradeBrief:
        """One step, one entry. Briefed, unbriefed or excluded -- never two of them, never twice."""
        orders = (
            [b.step_order for b in self.briefs]
            + [u.step_order for u in self.unbriefed]
            + [e.step_order for e in self.excluded]
        )
        duplicates = sorted({o for o in orders if orders.count(o) > 1})
        if duplicates:
            raise ValueError(
                f"steps {duplicates} appear more than once across briefs and unbriefed. A page "
                "rendering one step must not have two documents to choose between."
            )
        return self

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    def brief_for(self, order: int) -> ComponentBrief | None:
        """The brief for one plan step, or None. The join an approval page makes."""
        return next((b for b in self.briefs if b.step_order == order), None)

    def absence_for(self, order: int) -> UnbriefedStep | None:
        """Why one plan step has no brief, or None if it has one."""
        return next((u for u in self.unbriefed if u.step_order == order), None)

    @property
    def reused(self) -> tuple[ComponentBrief, ...]:
        """Briefs served from storage rather than researched during this run.

        Decided by comparing each brief's own `observed_at` against the run's -- derived rather
        than stored, so it cannot disagree with the timestamps it comes from.
        """
        return tuple(b for b in self.briefs if b.observed_at != self.observed_at)

    def counts(self) -> dict[str, int]:
        per_section = {
            f"{str(section).lower()}": sum(
                len(b.section(section).items) for b in self.briefs
            )
            for section in BriefSection
        }
        return {
            "briefed": len(self.briefs),
            "unbriefed": len(self.unbriefed),
            "excluded": len(self.excluded),
            **per_section,
            "actions": sum(len(b.actions) for b in self.briefs),
            "unverified_quotes": sum(b.unverified for b in self.briefs),
            "items_outside_the_walk": sum(b.out_of_walk for b in self.briefs),
            "sections_not_looked": sum(
                1
                for b in self.briefs
                for s in b.sections
                if s.state is SectionState.NOT_LOOKED
            ),
            "steps_with_no_section_at_all": sum(
                1 for b in self.briefs if b.all_sections_unavailable
            ),
            "reused_from_storage": len(self.reused),
            "steps_with_unread_minors": sum(
                1 for b in self.briefs if b.coverage is not None and not b.coverage.complete
            ),
        }

    def summary(self) -> str:
        """One line for the state payload and the top of a page."""
        c = self.counts()
        return (
            f"briefed {c['briefed']} step(s), {c['unbriefed']} unbriefed, "
            f"{c['excluded']} excluded as AWS-managed; "
            f"{c['breaking_changes']} breaking, {c['deprecations']} deprecated, "
            f"{c['known_issues']} known issue(s), {c['nice_to_haves']} nice-to-have(s), "
            f"{c['actions']} action(s); {c['reused_from_storage']} served from storage, "
            f"{c['sections_not_looked']} section(s) could not be produced. "
            "Advisory only -- the upgrade approval does not wait on this."
        )


# ---------------------------------------------------------------------------
# BUILDING ONE BRIEF FROM ONE EXTRACTION
# ---------------------------------------------------------------------------


def _citation(source: BriefSource) -> Citation:
    return Citation(url=source.url, revision=source.revision, label=source.label)


def _dedup(citations: tuple[Citation, ...]) -> tuple[Citation, ...]:
    """Deduplicate citations by url and revision, first label winning, order preserved.

    Order preserved rather than sorted: the agent cites in the order it read, which is roughly
    the order of the hop, and that is more useful to a reader than alphabetical.

    >>> a = Citation(url="u", revision="v1", label="v1 notes")
    >>> b = Citation(url="u", revision="v1", label="release v1")
    >>> [c.label for c in _dedup((a, b))]
    ['v1 notes']
    """
    seen: dict[tuple[str, str], Citation] = {}
    for citation in citations:
        seen.setdefault((citation.url, citation.revision), citation)
    return tuple(seen.values())


def _normalised(release: str) -> str:
    """A release as the walk spells it, so `v0.7.0` and `0.7.0` compare equal.

    >>> _normalised("v0.7.0"), _normalised(" 0.7.0 ")
    ('0.7.0', '0.7.0')
    """
    return release.strip().lstrip("vV")


def _verified(
    quote: str, source: BriefSource, pages: PageSet
) -> tuple[QuoteVerdict, str, str | None]:
    check = check_quote(quote, source.url, source.revision, pages)
    return _verdict_of(check), check.detail, check.page_digest


def _item(
    *,
    title: str,
    detail: str,
    concern: PlatformConcern,
    basis: ChangeBasis,
    quote: str,
    source: BriefSource,
    pages: PageSet,
    walked: frozenset[str],
    release: str | None = None,
    action: str | None = None,
    action_phase: ActionPhase = ActionPhase.NO_ACTION,
    **extra: object,
) -> BriefItem:
    verdict, verdict_detail, page_digest = _verified(quote, source, pages)
    return BriefItem(
        title=title,
        detail=detail,
        release=release,
        concern=concern,
        action=action,
        action_phase=action_phase,
        basis=basis,
        quote=quote,
        source=_citation(source),
        quote_verdict=verdict,
        quote_detail=verdict_detail,
        page_digest=page_digest,
        # A finding with no release attributed cannot be outside the walk -- an issue is about a
        # version range rather than one release -- so it is not flagged.
        in_walk=(release is None) or (_normalised(release) in walked),
        **extra,  # type: ignore[arg-type]
    )


def _empty_view(section: BriefSection, note: EmptySectionNote) -> BriefSectionView:
    """An empty section, mapped from the agent's own account of why.

    NOTHING_ANNOUNCED becomes NONE_FOUND, which a reader may treat as reassurance. NOT_FOUND
    becomes NOT_LOOKED, which they may not. Nothing else decides this: the agent is the only
    thing that knows whether it found the material, so a second opinion here would just be a
    guess overriding an observation.
    """
    state = (
        SectionState.NONE_FOUND
        if note.outcome == "NOTHING_ANNOUNCED"
        else SectionState.NOT_LOOKED
    )
    return BriefSectionView(
        section=section,
        state=state,
        reason=note.reason,
        sources=_dedup(tuple(_citation(s) for s in note.looked_in)),
    )


def _not_looked_view(section: BriefSection, reason: str) -> BriefSectionView:
    return BriefSectionView(section=section, state=SectionState.NOT_LOOKED, reason=reason)


def unavailable_sections(reason: str) -> tuple[BriefSectionView, ...]:
    """All five sections as NOT_LOOKED with one reason. The honest total failure.

    Five NOT_LOOKED sections say "we know nothing here". Five empty REPORTED sections would say
    "we checked and it is clean", which is the lie this function exists to avoid.
    """
    return tuple(_not_looked_view(section, reason) for section in BriefSection)


def coverage_view(walk: ReleaseWalk, extraction: BriefExtraction | None) -> CoverageView:
    """The hop's coverage, from the walk and the agent's own account of what it read.

    `minors_unread` is the difference between the minors the hop ENTERS and the minors the agent
    says it covered -- intersected with what a release listing could show, so a minor that
    published no release at all is not held against it. When there is no extraction, nothing was
    covered and every entered minor is unread, which is the honest reading of a failed run.
    """
    covered = {
        _normalised(m) for m in (extraction.minors_covered if extraction else ())
    }
    read = tuple(r for r in (extraction.releases_read if extraction else ()))
    unread = tuple(m for m in walk.minors_entered if _normalised(m) not in covered)
    note = walk.note
    if unread:
        note += (
            f" The brief accounts for {sorted(covered) or 'no'} minor(s), so anything "
            f"introduced in {', '.join(unread)} is NOT in it."
        )
    return CoverageView(
        installed=walk.installed,
        recommended=walk.recommended,
        precision=walk.precision,
        enumeration=walk.enumeration,
        releases_on_path=walk.hops,
        releases_read=read,
        minors_entered=walk.minors_entered,
        minors_covered=tuple(sorted(covered)),
        minors_unread=unread,
        note=note,
    )


def _sequencing_view(
    extraction: BriefExtraction, pages: PageSet
) -> SequencingView | None:
    note = extraction.sequencing
    if note is None:
        return None
    verdict, detail, _ = _verified(note.quote, note.source, pages)
    return SequencingView(
        must_install_in_order=note.must_install_in_order,
        detail=note.detail,
        basis=note.basis,
        quote=note.quote,
        source=_citation(note.source),
        quote_verdict=verdict,
        quote_detail=detail,
    )


def _findings_view(
    section: BriefSection,
    extraction: BriefExtraction,
    pages: PageSet,
    walked: frozenset[str],
) -> BriefSectionView:
    """One populated section, with every quote checked and the citations gathered at the end."""
    items: list[BriefItem] = []

    if section is BriefSection.KNOWN_ISSUES:
        for issue in extraction.known_issues:
            items.append(
                _item(
                    title=issue.title,
                    detail=issue.detail,
                    concern=issue.concern,
                    basis=issue.basis,
                    quote=issue.quote,
                    source=issue.source,
                    pages=pages,
                    walked=walked,
                    issue_url=issue.issue_url,
                    issue_number=issue.issue_number,
                    trail=issue.trail,
                    affects=issue.affects,
                    workaround=issue.workaround,
                )
            )
    elif section is BriefSection.NICE_TO_HAVES:
        for feature in extraction.nice_to_haves:
            items.append(
                _item(
                    title=feature.title,
                    detail=feature.detail,
                    release=feature.release,
                    concern=feature.concern,
                    basis=feature.basis,
                    quote=feature.quote,
                    source=feature.source,
                    pages=pages,
                    walked=walked,
                    why_it_helps=feature.why_it_helps,
                    helm_values=feature.helm_values,
                    helm_set=feature.helm_set,
                    enablement=feature.enablement,
                )
            )
    else:
        findings = {
            BriefSection.BREAKING_CHANGES: extraction.breaking_changes,
            BriefSection.DEPRECATIONS: extraction.deprecations,
            BriefSection.RELEASE_NOTES: extraction.release_notes,
        }[section]
        for finding in findings:
            items.append(
                _item(
                    title=finding.title,
                    detail=finding.detail,
                    release=finding.release,
                    concern=finding.concern,
                    basis=finding.basis,
                    quote=finding.quote,
                    source=finding.source,
                    pages=pages,
                    walked=walked,
                    action=finding.action,
                    action_phase=finding.action_phase,
                )
            )

    stated = sum(1 for i in items if i.basis is ChangeBasis.STATED)
    unverified = sum(1 for i in items if not i.quote_verdict.verified)
    reason = (
        f"{len(items)} item(s), {stated} stated by the source and {len(items) - stated} "
        f"judged from what it describes"
    )
    if unverified:
        reason += (
            f". {unverified} could not be traced to the recorded bytes and is marked as such "
            "rather than removed"
        )
    return BriefSectionView(
        section=section,
        state=SectionState.REPORTED,
        reason=reason,
        items=tuple(items),
        sources=_dedup(tuple(i.source for i in items)),
    )


def _cross_check_issues(
    view: BriefSectionView, searches_ran: bool | None
) -> BriefSectionView:
    """Downgrade a "nothing announced" issue section when no search actually ran.

    **THE ONE PLACE THE DETERMINISTIC LAYER OVERRIDES THE AGENT, and it is worth being explicit
    about why.** Everywhere else the agent is the only thing that knows whether it found the
    material, so a second opinion would be a guess overriding an observation. Here it is not a
    guess: `search_issues` records a receipt per lookup naming its own outcome, so whether a
    query ever ran is a fact on disk. An agent that was rate-limited and reported "no known
    issues" would produce the single most dangerous output this document can carry -- a false
    all-clear, in the section an operator is most likely to act on.

    `None` means the receipts could not be read, which is not evidence either way and changes
    nothing. `False` means receipts exist and every one of them says the search did not run.
    """
    if view.state is not SectionState.NONE_FOUND or searches_ran is not False:
        return view
    return BriefSectionView(
        section=view.section,
        state=SectionState.NOT_LOOKED,
        reason=(
            "the brief reported no known issues, but the recorded tool receipts show that no "
            "issue search actually completed -- every attempt was refused, rate-limited or "
            "unreadable. Downgraded to not-looked: this component's issues are UNKNOWN, not "
            f"absent. The reported reason was: {view.reason}"
        ),
        sources=view.sources,
    )


def component_brief(
    step: PlanStep,
    *,
    walk: ReleaseWalk | None,
    extraction: BriefExtraction | None,
    pages: PageSet,
    session_id: str | None,
    observed_at: str,
    reuse_note: str = "researched fresh",
    failure_reason: str = "",
    issue_searches_ran: bool | None = None,
) -> ComponentBrief:
    """One step's brief. Never reports an absence as an all-clear.

    `extraction is None` means the agent could not be reached or would not answer, and
    `failure_reason` says which -- all five sections then read NOT_LOOKED with that reason.

    PRECONDITION: `unbriefable_reason(step)` is None. The caller checks that first, which is why
    the step's component, target series and Kubernetes version are read directly here.
    """
    walked = frozenset(_normalised(v) for v in (walk.hops if walk else ()))

    if extraction is None:
        sections = unavailable_sections(
            failure_reason or "no brief was produced for this step"
        )
        sequencing = None
        chart_version = None
    else:
        explained = {note.section: note for note in extraction.empty_sections}
        views: list[BriefSectionView] = []
        for section in BriefSection:
            note = explained.get(section)
            view = (
                _empty_view(section, note)
                if note is not None
                else _findings_view(section, extraction, pages, walked)
            )
            if section is BriefSection.KNOWN_ISSUES:
                view = _cross_check_issues(view, issue_searches_ran)
            views.append(view)
        sections = tuple(views)
        sequencing = _sequencing_view(extraction, pages)
        chart_version = extraction.chart_version

    return ComponentBrief(
        step_order=step.order,
        step_kind=step.kind,
        component=str(step.component),
        from_series=step.from_series,
        to_series=str(step.to_series),
        recommended_version=step.recommended_version,
        at_kubernetes=str(step.at_kubernetes),
        chart_version=chart_version,
        coverage=coverage_view(walk, extraction) if walk is not None else None,
        sequencing=sequencing,
        sections=sections,
        observed_at=observed_at,
        reuse_note=reuse_note,
        evidence_session=session_id,
    )
