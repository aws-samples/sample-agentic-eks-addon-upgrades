"""The presentation contracts: every number a human reads, computed exactly once.

WHY THIS MODULE EXISTS AT ALL
----------------------------
Without a presentation layer, three renderers each re-derive facts from the stage
documents and one number comes out three different ways:

    HTML tile       de-duplicated
    HTML section    de-duplicated AGAIN, independently
    Markdown        not de-duplicated
    JSON            absent entirely

The consequence a user sees is an Unresolved tile reading one count, a section pill
reading another, and neither matching the truth. Fixing one renderer leaves the defect
live in the others. A function meant to prevent exactly this -- the value set every format
renders, computed once so they cannot diverge -- does not help if it does not compute the
number at all.

The shape here makes the class of defect impossible rather than fixing the
instance:

1. There is exactly ONE list of unresolved components (`ReportView.unresolved`),
   built once by `build.py`, keyed on `workload_ref` -- the identifier both the
   Stage 4 and Stage 5 documents carry and neither derives, so the join cannot
   drift with a display name the way name-keyed de-duplication could.
2. Every headline number lives on `ReportTotals`, and a `model_validator`
   RECOMPUTES each one from the lists and refuses to construct the document if
   they disagree. A report carrying a wrong count cannot exist.
3. Renderers take a `ReportView` and may not import a stage document. Three
   formats cannot disagree because there is only one set of numbers.
4. `PresentationDocument` is itself digested and cites every upstream digest, so
   `verify` reaches the report. If rendering happens strictly downstream of
   every document with no digest covering it, replay -- the property
   the whole design exists to provide -- stops short of the numbers a human
   actually reads.

NO CLOCK, NO RUN IDENTIFIER. Same discipline as every stage document since
Stage 2: content is a function of the upstream documents and these rules, nothing
else, so two runs over the same inputs render byte-identical bytes.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from ..models import ClusterReference, ContractModel

# The one placeholder every view uses for a value that does not exist. Defined
# once here rather than per renderer: three renderers each choosing their own
# dash is how "the formats disagree" starts.
ABSENT = "—"

# The `install_method` the Stage 2 inventory writes for an add-on AWS installs, publishes and
# qualifies itself. NAMED ON THE VALUE SET rather than only in `build.py`, because a renderer
# needs the same answer -- an AWS-managed step is deliberately not researched, so the page has
# to say why there is no brief -- and a renderer may not import `build`.
#
# The string is the one `inventory/resolve.py` writes, not `models.InstallationMethod.EKS_ADDON`:
# two vocabularies for one idea, and the report carries the former. Matched on the install
# method and never on a component name, for `brief/document.py`'s reason -- a list of add-on
# names here would be a list to keep in step with AWS's catalogue forever, and it would stop
# matching the moment AWS added one.
MANAGED_ADDON_INSTALL_METHOD = "EKS_MANAGED_ADDON"

# The `StepRow.kind` for a component NO recorded version of which reaches the target
# Kubernetes minor.
#
# NOT A `StepKind`, AND THE PLAN DOCUMENT IS RIGHT NOT TO HAVE ONE. `order/plan.py` puts such
# a component in `unsequenceable` and leaves `feasible` false, because that is the true
# statement for a document to make: no ordering of moves carries this component to the target.
# Minting a plan step would make the plan assert a move that does not exist, and the plan is
# digest-cited -- a reader who checked it would find it claiming something untrue.
#
# WHAT IT FIXES IS THE ABSENCE OF A DECISION. Before this kind, a component in
# `unsequenceable` reached the approval page only inside the could-not-be-sequenced table,
# which carries no Approve and no Reject. So the run offered a person no way to say anything
# about it: not "accept it, this cluster can live with keda unsupported at 1.36", not "refuse,
# do not touch this cluster". The only paths were to abandon the upgrade or to proceed with no
# record that anybody had weighed the risk -- and an unrecorded acceptance is the one outcome
# this whole pipeline exists to prevent. The consent is per cluster, so it belongs on the
# per-cluster approval, which is the surface these rows are added to.
#
# IT IS NOT PROGRESS TOWARD THE TARGET AND MUST NEVER BE WORDED AS ANY. Approving one of these
# rows records that this cluster accepts running the component unsupported; `feasible` stays
# false and the plan still does not reach the target for that component.
#
# NAMED ON THE VALUE SET, for `MANAGED_ADDON_INSTALL_METHOD`'s reason: `build.py` mints the
# rows, `render/html.py` words them, `stages/upgrade_approval.py` decides they are
# authorisable, and a renderer may not import `build`. One spelling, in one place.
UNSUPPORTED_AT_TARGET_KIND = "COMPONENT_UNSUPPORTED_AT_TARGET"


class ComponentGroup(StrEnum):
    """Which presentation bucket a component belongs to.

    Presentation-only: it changes nothing a verdict depends on. Derived from the
    Stage 2 inventory's `install_method` and workload reference rather than from a
    curated list of component names -- per-component behaviour is data in the
    knowledge store, never a name in this package.
    """

    MANAGED_ADDON = "MANAGED_ADDON"
    HELM_RELEASE = "HELM_RELEASE"
    OTHER_WORKLOAD = "OTHER_WORKLOAD"


class UnresolvedOrigin(StrEnum):
    """Where an unresolved row came from, so the overlap is visible not implied.

    A component Stage 4 left UNRESOLVED is *necessarily* also unsequenceable in
    Stage 5, so the two collections overlap. That overlap is the whole reason a
    naive count is wrong. Recording the origin per row means the de-duplication is
    an inspectable property of the document rather than arithmetic buried in a
    renderer.
    """

    # UNRESOLVED verdict in Stage 4, and therefore also unsequenceable in Stage 5.
    BOTH = "BOTH"
    # UNRESOLVED in Stage 4 but not named in the plan's `unsequenceable`.
    ASSESSMENT_ONLY = "ASSESSMENT_ONLY"
    # Decided in Stage 4 yet still unsequenceable -- blocked for a DIFFERENT
    # reason (no feasible window). The row an HTML view would call "could not be
    # sequenced", and the only one that is genuinely additional.
    PLAN_ONLY = "PLAN_ONLY"


class Provenance(ContractModel):
    """The digest chain this report rests on, carried verbatim from the plan.

    Displayed, not merely stored: the chain from the rendered report back to the
    cluster reading is the claim the whole pipeline makes, and a report that does
    not show it asks to be trusted rather than checked. Passed to the renderers
    alongside the value set for that reason.

    It is not a field on `ReportView` because `ReportView` is the *derived* value
    set and these are carried inputs; keeping them apart means a renderer cannot
    confuse "a number I was given" with "a number someone computed".
    """

    snapshot_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    report_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ledger_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    assessment_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    store_digest: str = Field(min_length=1)


class CitedSource(ContractModel):
    """One document the recommendation rests on, and what it was used for.

    Shown as a list an approver can open. Only pages the fetch tool actually
    recorded reach this list, so every row has bytes behind it.
    """

    url: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    used_for: str = Field(min_length=1)


class PendingApproval(ContractModel):
    """One researched fact awaiting a person, carried verbatim from Stage 6.

    Every field is copied from the research stage's own result. Nothing here is
    derived, and deliberately so: these values are the terms of a decision another
    stage already framed, and a presentation rule that recomputed one of them could
    show an approver a digest that does not match the fact they would store.

    `candidate_digest` and `report_digest` are the two that matter. The approval
    endpoint refuses an approval whose candidate digest no longer matches the
    stored candidate, and refuses one whose report digest it cannot re-derive. So
    these are not decoration: they are what makes an approval submitted from this
    page provably an approval of what this page displayed.
    """

    component: str = Field(min_length=1)
    version_series: str = Field(min_length=1)
    candidate_uri: str = Field(min_length=1)
    candidate_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    report_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    # The per-candidate evidence receipt, when one was written. A link, not a
    # replacement: the run report carries the decision, that page carries the
    # quotes and the fetched page bytes behind it.
    report_html_uri: str | None = None
    confidence: str = Field(min_length=1)
    change_direction: str = Field(min_length=1)
    # The consequence sentence Stage 6 computed. Displayed verbatim so the approver
    # reads the same words the approval report states.
    headline: str = Field(min_length=1)
    values_verified: int = Field(default=0, ge=0)
    values_total: int = Field(default=0, ge=0)

    # ------------------------------------------------------------------
    # THE NUMBERS THE DECISION IS ACTUALLY ABOUT.
    #
    # Before these, the row offered Approve and Reject beside a component
    # name, a confidence word and a headline -- and nothing an approver
    # could weigh. The version to install, whether the installed one is
    # already outside its stated window, and whether the recommendation
    # spans the cluster upgrade were all computed by Stage 6 and then left
    # on the floor.
    #
    # Copied verbatim from the research result, like every other field
    # here: a presentation rule that recomputed one of them could show an
    # approver a number the candidate does not contain.
    #
    # ALL OPTIONAL. An extraction from the transcribe-only prompt carries
    # no assessment, and its row must still build -- a missing
    # recommendation is a thing to display, not a reason to fail the
    # report.
    # ------------------------------------------------------------------
    installed_version: str | None = None
    installed_version_status: str | None = None
    recommended_version: str | None = None
    recommended_series: str | None = None
    action: str | None = None
    upgrade_ordering: str | None = None
    covers_current_kubernetes: bool | None = None
    covers_target_kubernetes: bool | None = None
    # The two confidences shown TOGETHER. Seeing that the model proposed
    # HIGH while the signals allowed only LOW is the most useful single
    # thing on the row, and it is invisible if only the final level shows.
    machine_confidence: str | None = None
    proposed_confidence: str | None = None
    evidence_tier: str | None = None
    confidence_rationale: str | None = None
    # Where the model's own answer was stored, so a reader can go from the
    # row to the raw extraction without re-running anything.
    extraction_uri: str | None = None
    cited_sources: tuple[CitedSource, ...] = ()
    caveats: tuple[str, ...] = ()
    not_found: tuple[str, ...] = ()
    # THE CHART REPOSITORY THE UPGRADE LANE WILL `helm repo add`, as the fact will carry it:
    # only if its quote verified. Shown under the recommended version; its absence on a Helm
    # component means the lane will not open for it.
    chart_repository: str | None = None


class ApprovalControls(ContractModel):
    """The approve / reject surface: a CARRIED INPUT, like `Provenance`.

    WHY THIS IS NOT PART OF `ReportView`. `ReportView` is the derived value set,
    clock-free and a pure function of the four stage documents. These values are
    none of those things -- they are carried from a stage that does not run on
    every execution, and a candidate URI embeds the instant the research ran.
    Putting them on `ReportView` would make the report's own value set depend on
    whether research happened, which is exactly the coupling `present` exists to
    remove.

    It IS recorded on `PresentationDocument`, though, because the rendered bytes
    depend on it and `verify` re-renders to check them. A carried input that
    changed the bytes without being recorded would put `verify` back to being
    blind to part of what a human reads.

    WHAT THIS DELIBERATELY DOES NOT CARRY. No credential, no signature, no
    presigned URL, no token. The page is a static object in S3 that anyone with
    read access can open -- including any agent that renders report links. Baking
    a usable approval capability into it would hand
    the agent the one power the whole design denies it. So the control is a command
    the human signs with their own credentials, and the recorded approver is
    therefore the human rather than whatever role rendered the page.
    """

    # The approval Lambda's function URL. None on an archived copy or a local
    # render, and then the page shows no command at all -- a control that cannot
    # work is worse than none.
    endpoint: str | None = None
    # The region the SigV4 command signs for. Carried rather than parsed out of the
    # endpoint host, because a wrong guess produces a signature mismatch whose
    # error message names neither the guess nor the endpoint.
    region: str = Field(default="us-east-1", min_length=1)
    pending: tuple[PendingApproval, ...] = ()

    @model_validator(mode="after")
    def pending_is_ordered_and_unique(self) -> ApprovalControls:
        """Deterministic order, one row per candidate.

        The research stage is a Step Functions Map, and Map result order is not a
        guarantee this code should rest on. Sorting in `build.py` and asserting it
        here means the rendered bytes cannot depend on the order three Lambda
        invocations happened to return in -- which would otherwise make the report
        non-reproducible for a reason no reader could ever diagnose.
        """
        keys = [(p.component, p.version_series) for p in self.pending]
        if keys != sorted(keys):
            raise ValueError(
                "pending approvals are out of order, so two runs over the same "
                f"research results would render different bytes: {keys}. Sort in "
                "view/build.py."
            )
        if len(keys) != len(set(keys)):
            duplicated = sorted({k for k in keys if keys.count(k) > 1})
            raise ValueError(
                f"the same candidate is listed more than once: {duplicated}. One "
                "row per component and series, or the page offers a human two "
                "controls for one decision."
            )
        return self


class SourceRow(ContractModel):
    """One cited source behind a component's compatibility fact.

    `url` is None for a source that is not a document -- the EKS Insight AWS
    computed for this cluster is a cluster fact, so it carries a label and a
    synopsis and nothing to click.
    """

    title: str = Field(min_length=1)
    url: str | None = None
    # Whether the title marks an authoritative compatibility matrix. Computed in
    # `build.py`, not in a renderer, so the star means the same thing everywhere.
    official: bool = False
    synopsis: str = ""


class ComponentRow(ContractModel):
    """One component, flattened for display. No verdict is formed here.

    Every field is either carried verbatim from a stage document or derived by a
    rule in `build.py`. Nothing is computed at render time.
    """

    # Display name: the resolved identity when there is one, else the workload
    # reference. Computed once here; every view uses it.
    component: str = Field(min_length=1)
    identity: str | None = None
    workload_ref: str = Field(min_length=1)
    namespace: str | None = None
    installed_version: str | None = None
    # The concrete version this component should end up on, or None when no
    # honest recommendation exists (an UNRESOLVED component gets none).
    recommended_version: str | None = None
    # True when the recommendation is the newest recorded release and NO compatibility
    # statement covers it -- the fact is silent above the installed series. The renderer
    # says so beside the number (`order/latest.py`, LATEST_UNVERIFIED).
    recommendation_unverified: bool = False
    # True when the recommendation IS the installed version: "stay put".
    stays: bool = False
    verdict: str = Field(min_length=1)
    # Evidence confidence from the stored fact, or ABSENT when no fact backs it.
    confidence: str = Field(min_length=1)
    priority: str = Field(min_length=1)
    fact_status: str = Field(min_length=1)
    group: ComponentGroup
    install_method: str = Field(min_length=1)
    # The evidence classes behind this component's hops, de-duplicated in hop
    # order: `stored-fact`, `eks-insight`, `none`.
    decided_from: tuple[str, ...] = ()
    sources: tuple[SourceRow, ...] = ()


class UnresolvedRow(ContractModel):
    """One component that could not be decided or could not be sequenced.

    Exactly one row per component, whichever way it arrived. This is the list
    whose length a renderer could otherwise compute several different ways.
    """

    component: str = Field(min_length=1)
    workload_ref: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    origin: UnresolvedOrigin
    reason: str = Field(min_length=1)


class RecommendedRow(ContractModel):
    """An advisory upgrade: already compatible, but a newer supported version exists."""

    component: str = Field(min_length=1)
    from_series: str = Field(min_length=1)
    recommended_version: str = Field(min_length=1)
    at_kubernetes: str = Field(min_length=1)


class StepRow(ContractModel):
    """One ordered step, with its display label resolved once."""

    order: int = Field(ge=1)
    kind: str = Field(min_length=1)
    # CLUSTER / REQUIRED / OPTIONAL. The three-way distinction a reader needs:
    # advance the cluster, a move you must make, a move you may make.
    label: str = Field(min_length=1)
    description: str = Field(min_length=1)
    reversibility: str | None = None
    # A step a version rollback will not undo. Surfaced per step because knowing
    # which ones those are before starting is the difference between a
    # maintenance window and an incident.
    one_way: bool = False
    unordered_with: tuple[int, ...] = ()
    # How the component this step moves was installed, joined from the Stage 2 inventory in
    # `build.py`. None for a cluster hop, which moves no component, and None when the join
    # finds nothing -- an unknown install method is not a claim that it is Helm.
    #
    # CARRIED SO A RENDERER NEVER HAS TO GUESS IT. The page has to explain why an AWS-managed
    # step has no brief, and the only signal in reach was the `-eksbuild.N` suffix on a version
    # string -- which is a guess about a naming convention, not a fact the pipeline recorded.
    # The inventory already knows, so the answer travels with the row.
    install_method: str | None = None

    @property
    def aws_managed(self) -> bool:
        """Whether AWS installs, publishes and qualifies this component's versions.

        The one question the brief's absence turns on: an AWS-managed add-on is deliberately
        not researched (`brief/document.py::excluded_reason`), so "no brief" is a decision
        about what is worth knowing rather than a gap in what could be found -- and the page
        must not report the first as though it were the second.
        """
        return self.install_method == MANAGED_ADDON_INSTALL_METHOD

    @property
    def unsupported_at_target(self) -> bool:
        """Whether this row is a component that cannot reach the target at all.

        Asked as a property for `aws_managed`'s reason: three surfaces need the same answer
        about the same string -- the renderer words the row, the approval stage decides it is
        authorisable, and `build.py` mints it -- and a `== "COMPONENT_UNSUPPORTED_AT_TARGET"`
        spelled out at each of them is three places for a typo to read as False and silently
        drop the row back into the state where nobody could decide anything about it.
        """
        return self.kind == UNSUPPORTED_AT_TARGET_KIND


class ReportTotals(ContractModel):
    """Every headline number, in one place, cross-checked against the lists.

    Storing derivable counts is normally a smell -- it creates somewhere for two
    values to disagree. Here it is the opposite: the counts are what a human
    reads, so they are made explicit, digested, and then VALIDATED against the
    lists they summarise (see `ReportView.totals_agree_with_the_lists`). The smell
    is answered by the check, and what is gained is that a renderer can never
    compute its own total.
    """

    detected: int = Field(ge=0)
    compatible: int = Field(ge=0)
    # INCOMPATIBLE + REQUIRES_COMPONENT_UPGRADE. Folded because a reader acts the
    # same way on both: this component has to move.
    upgrade_needed: int = Field(ge=0)
    upgrade_recommended: int = Field(ge=0)
    # THE number a naive renderer gets wrong. One count, one list.
    unresolved: int = Field(ge=0)
    # The split, so the de-duplication is legible rather than asserted.
    unresolved_undecided: int = Field(ge=0)
    unresolved_unsequenceable_only: int = Field(ge=0)
    steps: int = Field(ge=0)
    cluster_upgrades: int = Field(ge=0)
    component_moves: int = Field(ge=0)
    one_way_moves: int = Field(ge=0)
    managed_addons: int = Field(ge=0)
    helm_releases: int = Field(ge=0)
    other_workloads: int = Field(ge=0)
    # Carried from the Stage 3 ledger: how much of the knowledge question is
    # answered, and how much a research pass could still close.
    answered_gaps: int = Field(ge=0)
    researchable_gaps: int = Field(ge=0)


class ReportView(ContractModel):
    """The value set every view renders. Clock-free, digest-citable, validated.

    A renderer receives this and nothing else. It may not import a stage
    document, so "this format computed its own total" becomes an import
    violation a test can see rather than a helper call nobody notices.
    """

    schema_version: Literal["1.0"] = "1.0"
    cluster: ClusterReference
    current_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    target_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    # The single-minor sequence actually evaluated, as display strings. Present
    # because "1.35 to 1.37" hides that two hops were checked.
    hops: tuple[str, ...] = Field(min_length=1)
    feasible: bool = True

    components: tuple[ComponentRow, ...] = ()
    unresolved: tuple[UnresolvedRow, ...] = ()
    recommended_upgrades: tuple[RecommendedRow, ...] = ()
    steps: tuple[StepRow, ...] = ()
    # Fact-store entries that would not parse, carried from the Stage 3 ledger. A
    # degraded store makes components look like they have no fact; a reader has
    # to be able to see the difference.
    store_errors: tuple[str, ...] = ()

    totals: ReportTotals

    @model_validator(mode="after")
    def totals_agree_with_the_lists(self) -> ReportView:
        """Recompute every total from the lists and refuse a document that lies.

        This is the structural half of the fix. `build.py` computes the totals
        once; this validator proves it. If a future edit adds a fifth way to
        count unresolved components, the document fails to construct rather than
        rendering a wrong number on a conference screen.
        """
        groups = [r.group for r in self.components]
        verdicts = [r.verdict for r in self.components]
        expected = {
            "detected": len(self.components),
            "compatible": verdicts.count("COMPATIBLE"),
            "upgrade_needed": (
                verdicts.count("INCOMPATIBLE")
                + verdicts.count("REQUIRES_COMPONENT_UPGRADE")
            ),
            "upgrade_recommended": len(self.recommended_upgrades),
            "unresolved": len(self.unresolved),
            "unresolved_undecided": sum(
                1
                for u in self.unresolved
                if u.origin
                in (UnresolvedOrigin.BOTH, UnresolvedOrigin.ASSESSMENT_ONLY)
            ),
            "unresolved_unsequenceable_only": sum(
                1 for u in self.unresolved if u.origin is UnresolvedOrigin.PLAN_ONLY
            ),
            "steps": len(self.steps),
            "cluster_upgrades": sum(
                1 for s in self.steps if s.kind == "CLUSTER_UPGRADE"
            ),
            "component_moves": sum(
                1 for s in self.steps if s.kind == "COMPONENT_MOVE"
            ),
            "one_way_moves": sum(1 for s in self.steps if s.one_way),
            "managed_addons": groups.count(ComponentGroup.MANAGED_ADDON),
            "helm_releases": groups.count(ComponentGroup.HELM_RELEASE),
            "other_workloads": groups.count(ComponentGroup.OTHER_WORKLOAD),
        }
        wrong = {
            name: (getattr(self.totals, name), value)
            for name, value in expected.items()
            if getattr(self.totals, name) != value
        }
        if wrong:
            detail = ", ".join(
                f"{name}: totals say {stated}, the list holds {actual}"
                for name, (stated, actual) in sorted(wrong.items())
            )
            raise ValueError(
                "a headline total disagrees with the list it summarises, so this "
                f"report would display a number that is not true: {detail}. Every "
                "displayed value must be computed once, in view/build.py."
            )
        # `unresolved` must be the sum of its two disjoint parts, or the
        # de-duplication has gone wrong in a way the per-part checks would miss.
        parts = (
            self.totals.unresolved_undecided
            + self.totals.unresolved_unsequenceable_only
        )
        if parts != self.totals.unresolved:
            raise ValueError(
                f"unresolved total {self.totals.unresolved} is not the sum of its "
                f"parts ({parts}). The undecided and unsequenceable-only sets must "
                "partition it exactly -- an overlap counted twice is the "
                "double-count this guards against."
            )
        # One row per component, whichever collection it arrived from.
        refs = [u.workload_ref for u in self.unresolved]
        if len(refs) != len(set(refs)):
            duplicated = sorted({r for r in refs if refs.count(r) > 1})
            raise ValueError(
                "the unresolved list names the same component more than once: "
                f"{duplicated}. Stage 4's undecided set and Stage 5's "
                "unsequenceable set overlap by construction and must be joined on "
                "workload_ref, not concatenated."
            )
        return self

    def digest(self) -> str:
        """Content digest of the whole value set."""
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()


class PresentationDocument(ContractModel):
    """The presentation stage's output document: the report, made verifiable.

    Cites seven digests. Six are inherited so the chain from the rendered report
    back to the cluster reading stays walkable; the seventh pair is this stage's
    own -- `ruleset_digest` over the modules that compute the values, and
    `renderer_digest` over the modules that turn them into bytes. Two fields
    rather than one for the same reason there are four per-stage ruleset digests
    and not one: a reader who finds two differing reports must be able to tell
    whether the numbers changed or only the stylesheet did.

    `html_digest` and `markdown_digest` are digests of the rendered bytes. They
    are what finally brings rendering under the replay guarantee: re-derive the
    view from the upstream documents, re-render it, and the bytes must match.
    There is no circularity -- the rendered bytes are a function of `view`, which
    does not contain them.
    """

    schema_version: Literal["1.0"] = "1.0"
    # Deliberately not an ordinal. In this project's stage vocabulary 6 is
    # research and 7 is the resolve loop; minting a second meaning for a number
    # is the homonym defect this package was written to remove.
    stage: Literal["present"] = "present"
    cluster: ClusterReference

    snapshot_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    report_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ledger_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    assessment_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    store_digest: str = Field(min_length=1)
    ruleset_digest: str = Field(min_length=1)
    renderer_digest: str = Field(min_length=1)

    view: ReportView
    # The approve / reject surface. A carried input, recorded here rather than on
    # `view` for the reasons in `ApprovalControls`, and recorded AT ALL because the
    # rendered bytes depend on it: `verify` re-renders from this document, so an
    # input that changed the page without being written down would put replay back
    # to being blind to part of what a human reads.
    approvals: ApprovalControls = ApprovalControls()
    # Redundant by construction and checked below. Present so a reader can cite
    # the value set without recomputing it, and so a mismatch is caught here
    # rather than surviving into a report someone trusts.
    view_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    html_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    markdown_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def cited_view_digest_is_the_view(self) -> PresentationDocument:
        actual = self.view.digest()
        if self.view_digest != actual:
            raise ValueError(
                f"view_digest cites {self.view_digest} but the embedded view "
                f"digests to {actual}. The document would misattribute its own "
                "numbers."
            )
        return self

    def digest(self) -> str:
        """Content digest of the whole document, values and rendered bytes alike."""
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    def counts(self) -> dict[str, int]:
        """Headline numbers for a summary line, taken from the validated totals."""
        return self.view.totals.model_dump()

    def provenance(self) -> Provenance:
        """The digest chain, in the shape the renderers are handed.

        Reassembled from this document's own fields rather than stored twice, so
        there is nowhere for the displayed chain and the cited chain to disagree.
        """
        return Provenance(
            snapshot_digest=self.snapshot_digest,
            report_digest=self.report_digest,
            ledger_digest=self.ledger_digest,
            assessment_digest=self.assessment_digest,
            plan_digest=self.plan_digest,
            store_digest=self.store_digest,
        )


# ---------------------------------------------------------------------------
# THE UPGRADE ADVICE, AS THE APPROVAL PAGE READS IT
#
# A THIRD CARRIED INPUT, NOT PART OF `ReportView`, and the reason is `Provenance`'s
# reason one step further on. `ReportView` states its own determinism: no clock, no
# run identifier, so two runs over the same inputs render byte-identical bytes. An
# upgrade brief cannot keep that promise and says so -- it carries `observed_at`, a
# reuse note, and `reproducible: false`, because it reads an issue tracker that
# changes daily. Folding it into `ReportView` would quietly retract that document's
# central claim.
#
# So it arrives beside the value set, exactly as the digest chain does, and for the
# same stated reason: keeping them apart means a renderer cannot confuse a number it
# was given with a number someone computed.
#
# WHY THESE TYPES EXIST AT ALL rather than the renderer reading `brief/document.py`.
# `tests/view/test_render_boundary.py` admits three relative imports into
# `view/render/` -- `model`, `command`, `html` -- and nothing else. A renderer reaching
# `..brief` is refused by name. That guard is what stops a page re-deriving a count,
# and the brief is exactly the kind of document it was written about: it has sections
# whose lengths a renderer could total for itself. So the mapping happens once, in
# `view/advice.py`, and the page sees only what it is allowed to draw.
#
# NAMED `Advice...` DELIBERATELY. `brief/document.py` already owns `BriefItem`,
# `BriefSectionView` and `UnbriefedStep`; a second set of those names in a module that
# sits next to it in an import list is the homonym defect this project renamed
# `resolve` to remove. "Advice" is the document's own word for itself.


class AdviceLink(ContractModel):
    """One page a section was read from, as the page prints it at the end of that section.

    NOT `CitedSource`, which requires `used_for` and a non-empty `revision`. A brief
    legitimately reads pages that have neither -- a project's known-issues page, an
    upgrade guide on a docs site -- and requiring them here would mean inventing them.
    """

    url: str = Field(min_length=1)
    label: str = Field(min_length=1)


class AdviceFinding(ContractModel):
    """One row in one section's table.

    Carries what the row draws and nothing that would let a renderer compute a verdict.
    `stated` is the whole of `ChangeBasis` reduced to the one bit the page shows: whether
    the release notes said it, or whether it follows from what they did say.
    """

    title: str = Field(min_length=1)
    detail: str = ""
    # What a platform engineer would worry about, as words: "helm values", "rbac".
    concern: str = ""
    stated: bool = True
    # The release that introduced it. Empty on a known-issue row, which carries the
    # issue instead.
    release: str = ""
    issue_url: str | None = None
    issue_number: int | None = None
    # BEFORE_UPGRADE / DURING_UPGRADE / AFTER_UPGRADE / NO_ACTION. The page groups the
    # runbook by this and shows nothing for NO_ACTION -- a badge on the majority of rows
    # saying "nothing to do" is noise.
    action: str | None = None
    action_phase: str = "NO_ACTION"
    # Known-issue extras.
    affects: str | None = None
    workaround: str | None = None
    # Worth-enabling extras. `helm_values` is a YAML fragment, `helm_set` a --set line.
    why_it_helps: str | None = None
    enablement: str | None = None
    helm_values: str | None = None
    helm_set: str | None = None


class AdvicePanel(ContractModel):
    """One of the brief's five sections, with its own account of being empty.

    THREE STATES AND ONLY ONE IS AN ALL-CLEAR, carried through unchanged because the
    distinction is the document's central honesty and a renderer must not be able to
    collapse it:

        REPORTED    we looked and there is content
        NONE_FOUND  we looked and the project announced nothing -- reassurance
        NOT_LOOKED  we could not look -- NOT reassurance

    `sources` is where it looked, which is what makes an empty section credible.
    """

    section: str = Field(min_length=1)
    state: str = Field(min_length=1)
    reason: str = ""
    findings: tuple[AdviceFinding, ...] = ()
    sources: tuple[AdviceLink, ...] = ()

    @model_validator(mode="after")
    def state_matches_contents(self) -> AdvicePanel:
        """Refuse a panel whose state disagrees with its own rows.

        The same discipline `brief/document.py::BriefSectionView` applies at the document
        boundary, restated here because this type is constructed separately and a mapping
        bug that produced an empty REPORTED panel would render as a silent all-clear.
        """
        if self.state == "REPORTED" and not self.findings:
            raise ValueError(
                f"a REPORTED {self.section} panel carries no findings. Use NONE_FOUND for "
                "'the project announced nothing' or NOT_LOOKED for 'we could not look'."
            )
        if self.state != "REPORTED" and self.findings:
            raise ValueError(
                f"a {self.state} {self.section} panel carries {len(self.findings)} "
                "finding(s). The state and the contents must be the same claim."
            )
        return self


class AdviceCoverage(ContractModel):
    """How much of the jump was actually read.

    `minors_unread` is the load-bearing field. A brief assembled from some of the minors
    a hop crosses, that does not say which it missed, reads as complete and is not --
    and absence of a warning is read as absence of a hazard.
    """

    releases_on_path: int = Field(ge=0)
    releases_read: tuple[str, ...] = ()
    minors_entered: tuple[str, ...] = ()
    minors_unread: tuple[str, ...] = ()


class AdvicePath(ContractModel):
    """Whether the hop may be taken directly, or must stop on the way.

    Its own type rather than a section, because it is a constraint on the plan rather
    than a list of things to read -- an operator who finds it afterwards has already
    broken it.
    """

    must_install_in_order: tuple[str, ...] = ()
    detail: str = ""

    @property
    def direct(self) -> bool:
        return not self.must_install_in_order


class StepAdvice(ContractModel):
    """One plan step's brief, addressed the way a grant addresses a step.

    KEYED ON `step_order`, which is `brief/document.py`'s own stated join key: a plan may
    move one component twice -- cert-manager does -- so keying on the component name
    would collapse two different walks with two different findings into one row.
    """

    step_order: int = Field(ge=1)
    component: str = Field(min_length=1)
    # The CHART version carrying the recommended application version, when the brief
    # found the mapping. Operators upgrade charts; the plan names an application version,
    # and for most CNCF add-ons those are different numbers.
    chart_version: str | None = None
    coverage: AdviceCoverage | None = None
    path: AdvicePath | None = None
    panels: tuple[AdvicePanel, ...] = ()
    # When THIS component's brief was read, which is not necessarily when the run
    # happened: a brief is stored under the upgrade it describes and served again for up
    # to a week. A cached answer indistinguishable from a fresh one is the whole hazard of
    # reuse, so both this and the note below are required rather than optional.
    observed_at: str = Field(min_length=1)
    reuse_note: str = Field(min_length=1)

    def panel(self, section: str) -> AdvicePanel | None:
        return next((p for p in self.panels if p.section == section), None)

    @property
    def runbook(self) -> tuple[AdviceFinding, ...]:
        """Every finding that needs doing, ordered before then during then after.

        Derived rather than stored so it cannot disagree with the panels it comes from.
        The reader's question is not "what changed" but "what do I do and when".
        """
        order = {"BEFORE_UPGRADE": 0, "DURING_UPGRADE": 1, "AFTER_UPGRADE": 2}
        acting = [
            f
            for p in self.panels
            for f in p.findings
            if f.action_phase in order
        ]
        return tuple(sorted(acting, key=lambda f: (order[f.action_phase], f.title)))

    @property
    def sources(self) -> tuple[AdviceLink, ...]:
        """Every page this step's brief was read from, deduplicated, first label winning."""
        seen: dict[str, AdviceLink] = {}
        for panel in self.panels:
            for link in panel.sources:
                seen.setdefault(link.url, link)
        return tuple(seen.values())

    @property
    def nothing_was_produced(self) -> bool:
        """Whether every panel came back NOT_LOOKED.

        The page needs this to say "no brief is available, decide without it" rather than
        drawing five empty panels that read as five all-clears.
        """
        return bool(self.panels) and all(p.state == "NOT_LOOKED" for p in self.panels)


class UnadvisedStep(ContractModel):
    """A step that got no brief at all, named rather than omitted.

    An omission reads as "nothing to say about this step". A component whose sources were
    unreachable is exactly the case where a reader most needs to know the silence is
    ignorance.
    """

    step_order: int = Field(ge=1)
    component: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class AdviceView(ContractModel):
    """Every step's brief, as the approval page reads them.

    ADVISORY IS A FIELD, not a property of the code that happens to be true today. The
    page states it because a reader who has just been shown a wall of breaking changes
    needs to know in the same breath that none of it gated the approval, and that reading
    the same upgrade again may legitimately give a different answer.
    """

    advisory: Literal[True] = True
    reproducible: Literal[False] = False
    observed_at: str = Field(min_length=1)
    steps: tuple[StepAdvice, ...] = ()
    unadvised: tuple[UnadvisedStep, ...] = ()

    def for_step(self, order: int) -> StepAdvice | None:
        """One step's brief, or None. The join the approval page makes."""
        return next((s for s in self.steps if s.step_order == order), None)

    def absence_for(self, order: int) -> UnadvisedStep | None:
        """Why one step has no brief, or None if it has one."""
        return next((u for u in self.unadvised if u.step_order == order), None)

    @model_validator(mode="after")
    def one_entry_per_step(self) -> AdviceView:
        """One step, one entry. Advised or not, never both and never twice."""
        orders = [s.step_order for s in self.steps] + [u.step_order for u in self.unadvised]
        duplicates = sorted({o for o in orders if orders.count(o) > 1})
        if duplicates:
            raise ValueError(
                f"steps {duplicates} appear more than once across steps and unadvised. A "
                "page rendering one step must not have two briefs to choose between."
            )
        return self
