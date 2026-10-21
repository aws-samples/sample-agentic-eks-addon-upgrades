"""The model boundary: everything a model is allowed to say, and nothing more.

Stage 6 is the one place a model runs. This module is the contract it returns, and the
contract is the boundary — not a prompt instruction, not a review step.

**The model returns an `Assessment`.** It reports whether the installed version covers the
target Kubernetes minor, which version should run instead, in what order to upgrade, and a
*proposed* confidence with its rationale. Handing the model the target risks a model told
the answer you want reaching for it — the assessment is survivable because nothing in it is
taken on trust:

  * every version number in the assessment must be a task input or appear in a quote the
    model also returned — `judge.py` re-checks the recommendation against the `releases`
    it saw and the windows `bounds.py` derived;
  * an N-2 policy derivation is **recomputed** from its stated anchor and window size,
    and a mismatch is a failed check on the receipt, not a trusted number;
  * `proposed_confidence` can only LOWER the final level. The machine computes its own
    level from checkable signals, caps it by the evidence tier, and takes the minimum.

So the model proposes a conclusion and code re-derives it, rather than letting the
conclusion reach the store unexamined.

Two fields are deliberately absent, each because asking the model for it produced a wrong
fact:

**No `maximum_exclusive` on statements.** Asking the model to compute the first Kubernetes
minor ABOVE the top of a range invites perfect arithmetic on the wrong question. Here the
model reports the number literally in the source — `stated_maximum_inclusive` — and
`bounds.py` does the half-open conversion.

**No `from_official_matrix`.** A model-set boolean that on its own lifts a single source to
HIGH confidence puts the authority decision in the wrong place. Source authority is
determined in `authority.py`, from the source's identity and the shape of its bytes —
both facts code can establish without asking.

What the model supplies on every statement is the one thing only a reader of prose can:
**which kind of statement the source made.** `BoundKind` is that report. It is checkable
— the quote must support it, and `verify.py` checks the quote against the fetched page
byte-for-byte.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from ..models import ContractModel


class BoundKind(StrEnum):
    """What kind of compatibility statement a source made about one version spec.

    The distinction this enum draws is the whole point of Stage 6's contract: collapsing
    it loses real information. Upstream projects publish genuinely different things:

    `PUBLISHED_MINIMUM`
        A floor and no ceiling: `0.6.x | 1.25+`, "requires Kubernetes 1.22 or later".
        The project states where support starts and says nothing about where it ends.

    `SUPPORTED_RANGE`
        A window the project states it *supports*: "supported Kubernetes versions:
        1.33 - 1.36". Both edges are claims about support.

    `TESTED_RANGE`
        A window the project states it *tested*, routinely disclaimed in the sentence
        above the table. `bounds.py` derives a window from it, because a published tested
        matrix is the cleanest evidence most CNCF projects offer and a platform engineer
        treats it as *the* matrix. What is kept is the WORD: the derived window carries
        basis TESTED, the qualifier travels on the receipt, and the report shows "tested"
        next to the number. The claim is preserved; it is not suppressed.

    `EXPLICIT_MAXIMUM`
        A stated ceiling with no stated floor: "does not support Kubernetes above 1.32",
        "up to 1.29".

    `ENUMERATED_MINORS`
        A discrete list of Kubernetes minors, typically a table keyed the other way
        round -- one row per Kubernetes version naming the component version to use.
        Bounded by what is listed, because a source that lists discrete minors has not
        stated support for a minor it does not list.

    `SUPPORT_POLICY`
        A RELATIVE rule with no absolute numbers in it: "we support the latest three
        Kubernetes minors", "N-2". The row itself yields no window — the numbers, if the
        model derived any, live in `Assessment.derivation`, where `judge.py` recomputes
        the arithmetic from the stated anchor and window size.

    `UNSTATED`
        The source names the component but makes no compatibility statement. A real,
        useful answer: it is why a component can stay an honest gap instead of acquiring
        a fabricated window.
    """

    PUBLISHED_MINIMUM = "PUBLISHED_MINIMUM"
    SUPPORTED_RANGE = "SUPPORTED_RANGE"
    TESTED_RANGE = "TESTED_RANGE"
    EXPLICIT_MAXIMUM = "EXPLICIT_MAXIMUM"
    ENUMERATED_MINORS = "ENUMERATED_MINORS"
    SUPPORT_POLICY = "SUPPORT_POLICY"
    UNSTATED = "UNSTATED"


class SourceRef(ContractModel):
    """Where a statement was read, pinned so the bytes can be fetched again.

    `revision` is a commit sha or tag, never a date. A `source_revision` like
    `"2025-01-09"` against an unversioned documentation URL cannot be re-fetched at the
    revision claimed, so the quote cannot be checked afterwards. A date is not a revision;
    the validator says so.

    (The component is deliberately not named here: this docstring is pulled into the
    generated JSON Schema and therefore into the model's prompt, where a component name
    would breach the invariant that no component name reaches anything the code acts on.)
    """

    url: str = Field(min_length=1)
    revision: str = Field(min_length=1)

    @model_validator(mode="after")
    def revision_is_not_a_date(self) -> SourceRef:
        """Reject an ISO date masquerading as a revision.

        Narrow on purpose -- only the exact `YYYY-MM-DD` shape. A tag like `v2.8.3` or a
        sha is unaffected.
        """
        parts = self.revision.split("-")
        if len(parts) == 3 and len(parts[0]) == 4 and all(p.isdigit() for p in parts):
            raise ValueError(
                f"revision {self.revision!r} is a date, not a revision. A citation must "
                "pin a commit sha or tag so the quote can be re-checked against the "
                "bytes that were read."
            )
        return self


class ExtractedClaimInput(ContractModel):
    """One value COPIED out of a source, with that source and the verbatim quote.

    `field` names what the value is about (`kubernetes_minimum for 0.6.x`) so two sources
    can be compared like with like. `quote` must contain the value, and the quote must
    itself appear in the bytes of the page at `source`, which `verify.py` checks. Comparing
    the value against the quote alone — both written by the model in the same turn — is not
    enough: a six-line quote spliced from two different commits can pass that weaker check.

    **COPIED, NOT SUMMARISED, AND THE DIFFERENCE IS LOAD-BEARING.** `verify.py` requires
    `value` to appear character-for-character inside `quote`. That is what stops a version
    number being invented on the back of a genuine quote. It follows that a value the model
    puts in its own words CANNOT pass, however faithful the paraphrase: the check asks
    whether the text was copied, not whether the meaning survived.

    So prose does not belong here. A breaking change, a disclaimer, an upgrade note is a
    sentence being summarised, and it belongs in `Assessment.caveats`, which carries no
    such check because it was never meant to hold copied values. A summarised sentence
    placed in `claims` fails the copy check and caps the whole component at LOW however
    good the rest of the evidence is.
    """

    field: str = Field(min_length=1)
    value: str = Field(min_length=1)
    source: SourceRef
    quote: str = Field(min_length=1)
    # True when the value was parsed out of a machine-readable file (a releases payload,
    # `Chart.yaml`, a `kubeVersion` constraint) rather than read out of prose. This buys no
    # confidence on its own -- `authority.py` re-derives it from the source path and the
    # fetched bytes, and disagreement is reported rather than trusted.
    claimed_structured: bool = False


class CompatibilityStatementInput(ContractModel):
    """One compatibility statement about one version spec, as the source phrased it.

    Every field is a transcription, not an inference. The model copies the numbers that
    appear in the source and names the kind of statement they form; it performs no
    arithmetic on them. `bounds.py` derives the windows, deterministically.
    """

    # A series (`0.6.x`) or an exact release (`3.2.0`), matching the stored contract's
    # widened spec pattern.
    version_spec: str = Field(pattern=r"^(?:\d+\.\d+\.x|\d+\.\d+\.\d+)$")
    kind: BoundKind
    # The floor exactly as the source writes it: `1.25` from `1.25+`, `1.30` from
    # `1.30 - 1.32`. None when the source states no floor.
    stated_minimum: str | None = Field(default=None, pattern=r"^\d+\.\d+$")
    # The top of the window exactly as the source writes it -- INCLUSIVE, because that is
    # how projects publish: `1.30 - 1.32` means 1.32 works. The exclusive conversion is
    # `bounds.py`'s, and only for kinds where a support ceiling may be derived at all.
    stated_maximum_inclusive: str | None = Field(default=None, pattern=r"^\d+\.\d+$")
    # For ENUMERATED_MINORS: every Kubernetes minor the source lists for this spec.
    stated_minors: tuple[str, ...] = ()
    served_api_versions: tuple[str, ...] = ()
    # The verbatim text containing the numbers above, and where it was read.
    quote: str = Field(min_length=1)
    source: SourceRef
    # The sentence qualifying the statement, when the source carries one -- "there is no
    # guarantee", "tested against N-2". Not consumed by any rule: it exists so the
    # receipt can show a reader the disclaimer that made a kind what it is, in the
    # source's own words.
    qualifier_quote: str | None = None

    @model_validator(mode="after")
    def numbers_match_the_kind(self) -> CompatibilityStatementInput:
        """Each kind requires the numbers that kind is made of.

        Structural, so a mis-typed statement is a parse failure at the boundary rather
        than a silently empty window three layers down. `SUPPORT_POLICY` and `UNSTATED`
        require none: the first is a relative rule whose numbers live in the assessment's
        derivation, the second states nothing.
        """
        if self.kind in (
            BoundKind.SUPPORTED_RANGE,
            BoundKind.TESTED_RANGE,
        ) and (self.stated_minimum is None or self.stated_maximum_inclusive is None):
            raise ValueError(
                f"{self.kind} states a window and requires both stated_minimum and "
                "stated_maximum_inclusive, copied from the source"
            )
        if self.kind is BoundKind.PUBLISHED_MINIMUM and self.stated_minimum is None:
            raise ValueError("PUBLISHED_MINIMUM requires stated_minimum")
        if self.kind is BoundKind.EXPLICIT_MAXIMUM and self.stated_maximum_inclusive is None:
            raise ValueError("EXPLICIT_MAXIMUM requires stated_maximum_inclusive")
        if self.kind is BoundKind.ENUMERATED_MINORS and not self.stated_minors:
            raise ValueError("ENUMERATED_MINORS requires the minors the source lists")
        for minor in self.stated_minors:
            parts = minor.split(".")
            if len(parts) != 2 or not all(p.isdigit() for p in parts):
                raise ValueError(f"stated_minors entries must look like '1.33', got {minor!r}")
        return self


class ReleaseSightingInput(ContractModel):
    """One released version the model saw, and the quote showing it.

    A release carried as a bare string with no quote and no source traces to nothing. A
    release drives the concrete version the plan recommends installing, so it is a
    verdict-affecting value and carries evidence like every other.
    """

    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    quote: str = Field(min_length=1)
    source: SourceRef


# ---------------------------------------------------------------------------
# The assessment: the model's conclusion, every piece of which code re-checks.
# ---------------------------------------------------------------------------


class InstalledVersionStatus(StrEnum):
    """What the evidence says about the version that is installed today."""

    COVERS_TARGET = "COVERS_TARGET"
    OUTSIDE_STATED_WINDOW = "OUTSIDE_STATED_WINDOW"
    NOT_STATED = "NOT_STATED"


class RecommendationAction(StrEnum):
    """What the model proposes doing, each with a defined evidence bar.

    `KEEP` — installed covers the target. `UPGRADE` — a newer release has a first-party
    statement covering the target. `LATEST_UNVERIFIED` — nothing states compatibility;
    the latest stable release is named and confidence is forced LOW. `INSUFFICIENT_EVIDENCE`
    — no release could be named at all.
    """

    KEEP = "KEEP"
    UPGRADE = "UPGRADE"
    LATEST_UNVERIFIED = "LATEST_UNVERIFIED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class UpgradeOrdering(StrEnum):
    """Whether the add-on moves before or after the cluster.

    During an EKS upgrade the control plane moves first, then nodes; the safest path is a
    component version that spans both minors, upgraded first. That is what
    `BEFORE_CLUSTER_UPGRADE` asserts, and `judge.py` checks it against the windows.
    """

    BEFORE_CLUSTER_UPGRADE = "BEFORE_CLUSTER_UPGRADE"
    AFTER_CLUSTER_UPGRADE = "AFTER_CLUSTER_UPGRADE"
    EITHER = "EITHER"
    UNKNOWN = "UNKNOWN"


class EvidenceTier(StrEnum):
    """The best class of evidence behind the recommendation. Caps the final confidence.

    The caps live in `signals.py`: a matrix (in the repository or the official docs) or a
    recomputed policy derivation can reach HIGH; a chart constraint or a bare floor caps
    at MEDIUM; no evidence at all caps at LOW.
    """

    MATRIX_IN_REPOSITORY = "MATRIX_IN_REPOSITORY"
    MATRIX_IN_OFFICIAL_DOCS = "MATRIX_IN_OFFICIAL_DOCS"
    POLICY_DERIVED = "POLICY_DERIVED"
    CHART_CONSTRAINT_ONLY = "CHART_CONSTRAINT_ONLY"
    FLOOR_ONLY = "FLOOR_ONLY"
    NONE = "NONE"


class ProposedConfidence(StrEnum):
    """The model's proposed level. A cap on the final confidence, never a floor."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AnchorBasis(StrEnum):
    """Where a policy derivation's anchor minor came from.

    `SOURCE_STATED` — a source names the newest minor the policy counts down from.
    `TASK_LATEST_UPSTREAM` — the anchor is the task input's latest upstream minor, which
    no source confirmed; `signals.py` caps such a derivation at MEDIUM.
    """

    SOURCE_STATED = "SOURCE_STATED"
    TASK_LATEST_UPSTREAM = "TASK_LATEST_UPSTREAM"


class CitedSource(ContractModel):
    """One document the assessment actually relied on, and what it was used for.

    The pinpoint list a reader opens to check the rationale: url + revision + a phrase
    like "compatibility matrix" or "release list". Only pages that were fetched — the
    judge holds the recorded bytes for every one.
    """

    url: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    used_for: str = Field(min_length=1)


class ChartProvenanceInput(ContractModel):
    """Where the Helm chart for this component is published, COPIED out of a source.

    The upgrade lane runs `helm repo add <repository_url>` with this value, so it is held to
    the standard of a version number: it must appear in `quote`, and `quote` must appear in
    the bytes of the page at `source`. The install page's `helm repo add <alias> <url>` line
    is the usual source; quote that one line. Only the URL is asked for -- the chart's name
    is already known from the release installed on the cluster, and which chart version
    ships the approved application version is asked of the repository itself at upgrade
    time, so neither needs transcribing.

    A chart published only as an OCI reference carries that reference here instead
    (`oci://public.ecr.aws/karpenter/karpenter`), quoted from the line that shows it -- the
    lane installs it directly with no `helm repo add`. Give the reference up to and including
    the chart name only: NO `:tag` and no `--version`. A project that prints its OCI install
    line with the version pinned (`oci://quay.io/jetstack/charts/cert-manager:v1.21.2`, or
    `... cert-manager --version v1.21.2`) still records the untagged location
    (`oci://quay.io/jetstack/charts/cert-manager`); the version is resolved against the
    registry at upgrade time.
    """

    # `http(s)://...` OR an `oci://` reference whose path segments carry NO version tag. The
    # final alternative forbids a `:` in any path segment after the host, so a pinned reference
    # like `oci://quay.io/jetstack/charts/cert-manager:v1.21.2` is refused at the boundary: the
    # version is resolved against the registry at upgrade time and passed to `helm --version`,
    # and a tag baked into the reference breaks the lane, which appends the chart name
    # `http(s)://...` OR an `oci://` reference whose path segments carry NO version tag. The
    # final alternative forbids a `:` in any path segment after the host, so a pinned reference
    # like `oci://quay.io/jetstack/charts/cert-manager:v1.21.2` is refused at the boundary: the
    # version is resolved against the registry at upgrade time and passed to `helm --version`,
    # and a tag baked into the reference breaks the lane, which appends the chart name
    # (`.../cert-manager:v1.21.2/cert-manager` is un-pullable). The host may still carry a
    # `:port`, so the first path step after `oci://host[:port]/` is where the ban begins.
    # `knowledge.py` enforces the same rule on the stored fact as a backstop.
    repository_url: str = Field(
        min_length=1,
        pattern=r"^(?:https?://.+|oci://[^/]+(?:/[^/:]+)+/?)$",
    )
    source: SourceRef
    quote: str = Field(min_length=1)


class PolicyDerivation(ContractModel):
    """The model's N-2 style arithmetic, shown so code can redo it.

    `judge.py` recomputes `derived_minimum`/`derived_maximum_inclusive` from the anchor
    and the window size, checks that `policy_statement_index` points at a
    `SUPPORT_POLICY` statement, and fails the check on any mismatch. An anchor the task
    supplied rather than a source (`TASK_LATEST_UPSTREAM`) caps confidence at MEDIUM.
    """

    policy_statement_index: int = Field(ge=0)
    window_size: int = Field(ge=1)
    anchor_kubernetes_minor: str = Field(pattern=r"^\d+\.\d+$")
    anchor_basis: AnchorBasis
    anchor_quote: str | None = None
    anchor_source: SourceRef | None = None
    derived_minimum: str = Field(pattern=r"^\d+\.\d+$")
    derived_maximum_inclusive: str = Field(pattern=r"^\d+\.\d+$")
    applies_to_series: str = Field(pattern=r"^\d+\.\d+\.x$")


class Assessment(ContractModel):
    """What the evidence means for THIS upgrade. Proposed by the model, re-checked by code.

    Every version number here must be a task input or present in a quote the extraction
    also carries — `judge.py` enforces the checkable parts: the recommended version must
    be among the `releases` seen, the recommended series must have a derived window
    covering the target for the coverage claims to pass, and a derivation is recomputed.
    The `*_refs` fields are indices into `statements`/`claims`, so a reader can go from
    the conclusion to the exact quotes it rests on.
    """

    # Echo of the task input. None when the task itself said the installed release is
    # unknown -- the assessment then speaks about the series instead, and says so.
    installed_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    current_kubernetes: str = Field(pattern=r"^\d+\.\d+$")
    target_kubernetes: str = Field(pattern=r"^\d+\.\d+$")
    installed_version_status: InstalledVersionStatus
    installed_version_statement_refs: tuple[int, ...] = ()
    action: RecommendationAction
    recommended_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    recommended_series: str | None = Field(default=None, pattern=r"^\d+\.\d+\.x$")
    recommended_chart_version: str | None = None
    recommendation_statement_refs: tuple[int, ...] = ()
    covers_current_kubernetes: bool | None = None
    covers_target_kubernetes: bool | None = None
    upgrade_ordering: UpgradeOrdering = UpgradeOrdering.UNKNOWN
    upgrade_path_claim_refs: tuple[int, ...] = ()
    evidence_tier: EvidenceTier = EvidenceTier.NONE
    derivation: PolicyDerivation | None = None
    proposed_confidence: ProposedConfidence = ProposedConfidence.LOW
    confidence_rationale: str = Field(min_length=1)
    cited_sources: tuple[CitedSource, ...] = ()
    caveats: tuple[str, ...] = ()
    not_found: tuple[str, ...] = ()


class ResearchExtraction(ContractModel):
    """The model's entire output for one component: transcriptions, and an assessment.

    Every transcription field is re-checked against bytes the tools fetched; the
    assessment is re-checked against the transcriptions. See the module docstring for
    why the assessment exists and what keeps it honest.
    """

    found: bool
    component_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    version_series: str = Field(pattern=r"^\d+\.\d+\.x$")
    canonical_repository: str | None = None
    official_documentation: tuple[str, ...] = ()
    claims: tuple[ExtractedClaimInput, ...] = ()
    # Every compatibility statement the sources make, for every version spec -- not only
    # the installed series. The whole table is what lets a later stage name the newest
    # spec that reaches a target, and what lets `bounds.py` read a floor
    # table's shape to bound a row that states no ceiling of its own.
    statements: tuple[CompatibilityStatementInput, ...] = ()
    releases: tuple[ReleaseSightingInput, ...] = ()
    # The Helm chart repository, for a component installed by Helm. Verified like every
    # other transcription and copied onto the approved fact, so the upgrade lane can
    # `helm repo add` without a committed catalogue.
    chart: ChartProvenanceInput | None = None
    # SUPERSEDED by Assessment.recommended_version. Kept so a transcription-only prompt and
    # `verify.py`'s cross-check keep working; the assessment prompt never fills it.
    recommended_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    # The conclusion, model-proposed and code-re-checked. None when the prompt asks only
    # for transcription.
    assessment: Assessment | None = None
    # No compatibility statement was found anywhere. An honest, useful answer.
    no_compatibility_found: bool = False
    not_found_reason: str | None = None
