"""What the brief agent may say. The extraction contract, written for platform engineers.

**WHY THIS IS NOT `research/boundary.py` PLUS `research/acceptance.py`.** The research stage's
gate exists to refuse any claim that is not a *stated support bound* -- that refusal is why
keda's candidate is REJECTED seven times and why the fact store is worth trusting. A breaking
change is not a support bound. Neither is a known issue, a deprecation or a nice-to-have.
Running them through that gate has exactly two outcomes and both are bad: everything is
rejected because none of it is a bound, or the gate is loosened until it stops protecting the
facts it was built for. So this is a separate contract, judged separately, and it writes no
fact.

**WHAT THIS CONTRACT CARRIES THAT A FACT MAY NOT.** A fact must be a stated bound or nothing.
A brief may legitimately say *"this reads as a breaking change, here is the quote"* -- so
`ChangeBasis` is required on every finding. `STATED` means the source itself says it;
`INFERRED` means the reader judged it from what the entry describes. Collapsing the two would
either drop real hazards or dress a judgement up as an upstream statement.

**THE AUDIENCE IS IN THE SCHEMA, NOT ONLY IN THE PROMPT.** The reader of this document is a
platform, DevOps or SRE engineer about to run `helm upgrade` against a live cluster. Two
fields make that concrete and are required rather than optional:

- `concern` -- which of the things that actually break a cluster this touches. A closed enum,
  so an item cannot be filed as "general". A model that wants to report a Go refactor has
  nowhere to put it, which is the same shape-not-trust argument `research/schema.py` makes.
- `title` and `detail` -- the bullet and its explanation, in the reader's terms. There is no
  field for a raw changelog line, because pasting one is the failure mode: *"fix: handle nil
  pointer in scraper.go"* is not something an operator can act on.

**LINKS DO NOT GO IN THE BULLETS.** Every finding carries `quote` and `source` because the
quote is checked byte-for-byte against bytes recorded before the agent read them -- that is
non-negotiable and it is what makes the document evidence. But the *rendered* brief shows
bullets first and a deduplicated source list at the end, which is what a person reading
before a maintenance window actually wants. `document.py` derives that list; the citations
stay here so nothing is unverifiable.

**AN EMPTY SECTION MUST SAY WHY IT IS EMPTY.** `empty_sections` is required to name every
section that came back with nothing, with the reason. A validator refuses an extraction where
a section is empty and unexplained, or explained and non-empty. "No breaking changes were
announced across these releases" and "the release notes could not be found" are different
answers and the reader acts differently on each.

**NO MODEL LIVES IN THIS PACKAGE.** `prompt.py` holds the instruction and the tool
definitions; the Harness invocation is transport and lives in `stages/brief_harness.py`. Every
rule here is checkable with no SDK and no network.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from ..models import ContractModel

# An ISO date, which is not a revision. Same refusal `research/boundary.py::SourceRef` makes,
# and for the same reason: a `source_revision` like `"2025-01-09"` against an
# unversioned URL cannot be re-fetched and the quote cannot be checked.
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class BriefSource(ContractModel):
    """Where one finding was read, pinned so the bytes can be fetched and re-hashed.

    **DELIBERATELY NOT `research/boundary.py::SourceRef`, and the difference is one field's
    optionality.** That type requires a revision, which is right for a compatibility fact:
    every such fact is read from a versioned file in a repository. This brief legitimately
    reads pages that have no revision -- a project's "Known issues" documentation page, an
    upgrade guide served from a docs site -- and requiring a revision there would make the
    agent invent one, which is the exact defect the date refusal below exists to prevent.

    So `revision` may be empty, and the empty string is meaningful: it means an unpinned page.
    `PageSet.get(url, revision)` is exact on both parts and the fetch tool records with the
    revision it was called with, so an unpinned citation verifies against an unpinned
    recording and never silently against a pinned one.
    """

    url: str = Field(min_length=1)
    # A commit sha, a tag, or `issue-<number>` for a recorded issue. Empty for an unpinned page.
    revision: str = ""
    # What the page is, in the reader's terms: "v0.7.0 release notes", "upgrade guide",
    # "issue 1600". Rendered in the source list at the end of the brief, so a reader scanning
    # citations can tell a release note from a forum-shaped issue without opening each link.
    label: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def revision_is_not_a_date(self) -> BriefSource:
        if _ISO_DATE.match(self.revision):
            raise ValueError(
                f"revision={self.revision!r} is a date, not a revision. A date cannot be "
                "re-fetched, so the quote it supports could never be checked again. Use the "
                "commit sha or the tag, or leave it empty for a page that has neither."
            )
        return self


class ChangeBasis(StrEnum):
    """Whether the source said it, or the reader judged it. Required on every finding.

    See the module docstring: this is the uncertainty a brief may carry and a fact may not,
    and it is only worth anything if it is never optional.
    """

    # The source says it: a BREAKING CHANGES heading, "removed", "no longer", "you must".
    STATED = "STATED"
    # The reader judged it from what the entry describes. Weaker, and labelled so.
    INFERRED = "INFERRED"


class PlatformConcern(StrEnum):
    """What a platform engineer would actually worry about. A closed set, on every finding.

    **THIS ENUM IS THE RELEVANCE FILTER.** The brief exists because a release-notes dump is
    useless to an operator: a Go refactor, a dependency bump, a CI fix and a docs typo are all
    real changes and none of them affect a cluster. There is no enum value for any of them, so
    an item that is only one of those has nowhere to be filed -- which is a stronger constraint
    than a prompt asking for relevance, for the reason `research/schema.py` gives about fields
    that cannot express a wrong answer.

    Every value is something that has taken a production cluster down during an add-on
    upgrade. `OTHER` exists because a closed set that cannot express a real hazard would push
    the agent into mislabelling, but the prompt tells it that `OTHER` needs justifying in
    `detail`.
    """

    # A CRD added, changed or with a bumped stored version. THE canonical add-on footgun:
    # `helm upgrade` does not apply CRD changes, so this is a manual step and often one-way.
    CRD_OR_SCHEMA = "CRD_OR_SCHEMA"
    # A Helm values key renamed, removed, restructured, or a chart default changed. What an
    # operator hits first, and never visible in the application's own changelog.
    HELM_VALUES = "HELM_VALUES"
    # Behaviour changes with no configuration change: a default replica count, resource
    # request, probe timing, retention or sampling rate.
    DEFAULT_CHANGED = "DEFAULT_CHANGED"
    # New RBAC rules, a new ServiceAccount, or a changed IRSA / EKS Pod Identity expectation.
    RBAC_OR_IDENTITY = "RBAC_OR_IDENTITY"
    # A Kubernetes API version depended on, removed or newly required -- or a raised minimum
    # Kubernetes version. Decides whether this add-on can ride the cluster hop at all.
    KUBERNETES_API = "KUBERNETES_API"
    # A metric renamed or removed. Dashboards and alerts break silently, which is the worst
    # way for an SRE to find out.
    METRICS_OR_ALERTS = "METRICS_OR_ALERTS"
    # Ports, probes, Service or Ingress shape, webhook endpoints, network policy expectations.
    NETWORKING = "NETWORKING"
    # Requests, limits, replica counts, HPA behaviour, or anything that changes footprint.
    RESOURCES_OR_SCALING = "RESOURCES_OR_SCALING"
    # securityContext, TLS, certificate handling, authentication or authorisation.
    SECURITY = "SECURITY"
    # A CLI flag or argument added, renamed or removed.
    FLAGS_OR_ARGS = "FLAGS_OR_ARGS"
    # On-disk or in-cluster state format, PVCs, retention, or a migration on start-up.
    STORAGE_OR_STATE = "STORAGE_OR_STATE"
    # Restart, drain, downtime or ordering behaviour during the upgrade itself.
    AVAILABILITY = "AVAILABILITY"
    # Real, operator-relevant, and none of the above. Needs justifying in `detail`.
    OTHER = "OTHER"


class ActionPhase(StrEnum):
    """When an action has to happen relative to the upgrade. Ordering is the whole value.

    An operator does not want a set of changes; they want a runbook. A CRD apply that must
    happen BEFORE `helm upgrade` and a dashboard fix that can happen AFTER are both actions
    and confusing them costs an outage.
    """

    BEFORE_UPGRADE = "BEFORE_UPGRADE"
    DURING_UPGRADE = "DURING_UPGRADE"
    AFTER_UPGRADE = "AFTER_UPGRADE"
    # Nothing to do. Reported so that "no action" is an answer rather than an omission.
    NO_ACTION = "NO_ACTION"


class BriefSection(StrEnum):
    """The sections of the brief. Named so an empty one can be explained by name."""

    BREAKING_CHANGES = "BREAKING_CHANGES"
    DEPRECATIONS = "DEPRECATIONS"
    RELEASE_NOTES = "RELEASE_NOTES"
    KNOWN_ISSUES = "KNOWN_ISSUES"
    NICE_TO_HAVES = "NICE_TO_HAVES"


class Finding(ContractModel):
    """One bullet in the brief: what changes, what it means, and what to do about it.

    `title` is the bullet a reader scans. `detail` is why they should care, in their terms --
    not a restatement of the changelog line, which is what `quote` already holds verbatim.
    """

    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=1200)
    # Which release introduced it. Compared against the walk, so a finding attributed to a
    # release whose notes were never read is visible rather than assumed.
    release: str = Field(min_length=1)
    concern: PlatformConcern
    basis: ChangeBasis
    action: str | None = Field(default=None, max_length=800)
    action_phase: ActionPhase = ActionPhase.NO_ACTION
    quote: str = Field(min_length=1)
    source: BriefSource

    @model_validator(mode="after")
    def an_action_has_a_phase(self) -> Finding:
        """An action with no phase is a to-do nobody can schedule, so refuse the pair.

        And a phase with no action claims work that is not described. Both directions,
        because an operator reading a runbook must not find either half alone.
        """
        has_action = bool((self.action or "").strip())
        if has_action and self.action_phase is ActionPhase.NO_ACTION:
            raise ValueError(
                "an action was given with action_phase=NO_ACTION. Say when it has to "
                "happen: BEFORE_UPGRADE, DURING_UPGRADE or AFTER_UPGRADE. An unscheduled "
                "action is one an operator cannot put in a runbook."
            )
        if not has_action and self.action_phase is not ActionPhase.NO_ACTION:
            raise ValueError(
                f"action_phase={self.action_phase} claims work to do but no action was "
                "described. Either describe it or use NO_ACTION."
            )
        return self


class IssueFinding(ContractModel):
    """One open issue worth watching for after the upgrade, and the evidence it is widespread.

    `trail` is required and it is the point. The ask was not "list open bugs" -- every project
    has hundreds -- it was the ones **many people hit after upgrading**. So a finding must say
    what makes this one of those: the comment count, the participant count, the reactions, or
    a maintainer confirming it. An issue with one comment and no reactions is one person's
    environment and does not belong in a brief that a maintenance window will be planned
    against.
    """

    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=1200)
    concern: PlatformConcern
    # The issue itself. Separate from `source` because the source may be a docs "Known issues"
    # page rather than the issue tracker, and a reader still wants the tracker link.
    issue_url: str | None = None
    issue_number: int | None = Field(default=None, ge=1)
    # Why this counts as widely hit: "63 comments, 21 reactions, 30 participants, open since
    # v0.7.0". Numbers rather than adjectives -- `search_issues` returns them, so there is no
    # reason for this to be a judgement.
    trail: str = Field(min_length=1, max_length=400)
    # Which versions it affects, when the issue says. Absent when it does not.
    affects: str | None = Field(default=None, max_length=200)
    workaround: str | None = Field(default=None, max_length=800)
    basis: ChangeBasis
    quote: str = Field(min_length=1)
    source: BriefSource


class FeatureFinding(ContractModel):
    """One new capability worth having, and exactly how to turn it on with Helm.

    **`helm_values` IS THE DELIVERABLE, NOT THE FEATURE NAME.** A brief that says "0.9 added
    request-level metrics" has told an operator nothing they can use. The ask was explicitly
    *via Helm flags only*, so the field holds the YAML fragment to add to a values file, and
    `helm_set` holds the same thing as `--set` for a one-off. Either may be absent -- some
    capabilities are on by default, some need no chart change -- and `enablement` then says
    which, so absence is never mistaken for "we could not work it out".
    """

    title: str = Field(min_length=1, max_length=160)
    detail: str = Field(min_length=1, max_length=1200)
    release: str = Field(min_length=1)
    concern: PlatformConcern
    # Why it is worth having on an EKS cluster specifically, in one or two sentences. The
    # filter that keeps this section from becoming a feature list.
    why_it_helps: str = Field(min_length=1, max_length=600)
    # The values-file fragment, as YAML. Preferred over `helm_set`, because a values file is
    # what a reviewed change looks like.
    helm_values: str | None = Field(default=None, max_length=1200)
    helm_set: str | None = Field(default=None, max_length=600)
    # ON_BY_DEFAULT, NEEDS_VALUES, NEEDS_MORE_THAN_HELM (a CRD apply, an IAM role, a
    # dependency) or UNKNOWN. Free text so a real answer is not forced into a wrong enum, but
    # required so absence of `helm_values` is always explained.
    enablement: str = Field(min_length=1, max_length=300)
    basis: ChangeBasis
    quote: str = Field(min_length=1)
    source: BriefSource


class SequencingNote(ContractModel):
    """A constraint on how the hop may be taken. Often the difference between a clean upgrade
    and a broken one.

    Some projects forbid skipping minors -- you must go through 0.8 to reach 0.9, because 0.8
    runs a migration 0.9 assumes has happened. The plan orders component moves from
    compatibility evidence and knows nothing about that, so if it is stated anywhere it is
    stated in the release notes, and this is where it lands. An operator who reads it after
    the fact has already skipped it.
    """

    # The releases that must be installed on the way, in order. Empty with a reason means the
    # notes state the hop may be taken directly.
    must_install_in_order: tuple[str, ...] = ()
    detail: str = Field(min_length=1, max_length=1000)
    basis: ChangeBasis
    quote: str = Field(min_length=1)
    source: BriefSource


class EmptySectionNote(ContractModel):
    """One section that came back with nothing, and why. Required for every empty section.

    "No breaking changes were announced in 0.7, 0.8 or 0.9" and "the release notes for 0.8
    could not be found" are different answers. An operator declines an upgrade over the
    second and proceeds on the first, so a schema that let both be an empty list would be
    hiding the only thing that distinguishes them.
    """

    section: BriefSection
    # Which of the two it is. `NOTHING_ANNOUNCED` may be read as reassurance; `NOT_FOUND` may
    # not. Free text follows in `reason`.
    outcome: str = Field(pattern=r"^(NOTHING_ANNOUNCED|NOT_FOUND)$")
    reason: str = Field(min_length=1, max_length=800)
    # Where it looked. Present even on NOT_FOUND -- especially on NOT_FOUND, because "we
    # checked the GitHub releases and the docs upgrade guide" is what makes the absence
    # credible rather than merely asserted.
    looked_in: tuple[BriefSource, ...] = ()


class BriefExtraction(ContractModel):
    """Everything the brief agent may return about one component's hop. One JSON object.

    `releases_read` and `minors_covered` are the agent's own account of its coverage. They are
    compared against the walk by `document.py`, so a brief that rested on fewer releases than
    the hop crosses says so in the document instead of reading as complete. That comparison is
    the reason both fields are required.
    """

    component_id: str = Field(min_length=1)
    installed_version: str = Field(min_length=1)
    recommended_version: str = Field(min_length=1)

    # Which release notes were actually read, and which minors they account for.
    releases_read: tuple[str, ...] = ()
    minors_covered: tuple[str, ...] = ()
    # The chart version that carries the recommended application version, when the component
    # ships a Helm chart and the mapping was found. Operators upgrade CHARTS; the plan names
    # an application version, and the two are not the same number for most CNCF add-ons.
    # THE VALUE AN OPERATOR WOULD PASS TO `helm upgrade --version`, and nothing else.
    #
    # IT WAS FREE TEXT AND CAME BACK IN THREE SHAPES ACROSS THREE COMPONENTS in one live run:
    # `v1.19.6` (the app version, v-prefixed), `metrics-server-helm-chart-3.14.0` (a git tag) and
    # `1.23.1` (bare). A reader cannot use two of those, and a renderer cannot repair them: strip
    # a `v` and you might be right, strip `metrics-server-helm-chart-` and you are guessing at a
    # tag convention, and guessing wrong prints a `--version` that fails.
    #
    # The pattern is the fix rather than the prompt alone, for this package's usual reason: an
    # unconstrained field is one the model fills with whatever it read, and no amount of asking
    # changes that. A tag or a v-prefix now fails validation, the proxy re-asks with the error,
    # and the value that reaches a reader is one they can paste.
    #
    # Null stays legitimate and means "no chart, or the mapping was not found" -- correct for an
    # EKS managed add-on, which has no chart at all.
    chart_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")

    breaking_changes: tuple[Finding, ...] = ()
    deprecations: tuple[Finding, ...] = ()
    release_notes: tuple[Finding, ...] = ()
    known_issues: tuple[IssueFinding, ...] = ()
    nice_to_haves: tuple[FeatureFinding, ...] = ()

    sequencing: SequencingNote | None = None
    empty_sections: tuple[EmptySectionNote, ...] = ()

    def items_for(self, section: BriefSection) -> tuple[Any, ...]:
        """The items in one section, by name. The join `empty_sections` is validated against."""
        return {
            BriefSection.BREAKING_CHANGES: self.breaking_changes,
            BriefSection.DEPRECATIONS: self.deprecations,
            BriefSection.RELEASE_NOTES: self.release_notes,
            BriefSection.KNOWN_ISSUES: self.known_issues,
            BriefSection.NICE_TO_HAVES: self.nice_to_haves,
        }[section]

    @model_validator(mode="after")
    def every_empty_section_is_explained(self) -> BriefExtraction:
        """An empty section must be named in `empty_sections`, and a named one must be empty.

        Both directions, and refused at construction rather than reconciled later -- the
        discipline `SweepDocument` applies to a headline that disagrees with its findings. An
        evidence document whose summary can contradict its own contents makes a reader check
        the detail every time, and they will not.
        """
        explained = {note.section for note in self.empty_sections}
        if len(explained) != len(self.empty_sections):
            raise ValueError(
                "empty_sections names the same section twice; one section has one reason"
            )

        for section in BriefSection:
            empty = not self.items_for(section)
            named = section in explained
            if empty and not named:
                raise ValueError(
                    f"{section} came back empty and is not in empty_sections. Say which it "
                    "is: NOTHING_ANNOUNCED, which a reader may treat as reassurance, or "
                    "NOT_FOUND, which they may not. An unexplained empty section is the one "
                    "thing this brief must never produce."
                )
            if named and not empty:
                raise ValueError(
                    f"{section} is named in empty_sections but carries "
                    f"{len(self.items_for(section))} item(s). The summary and the contents "
                    "must be the same claim."
                )
        return self
