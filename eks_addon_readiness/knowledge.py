"""Versioned, evidence-backed compatibility knowledge contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AnyHttpUrl, AnyUrl, Field, field_validator, model_validator

from .models import Citation, Confidence, ContractModel


class UpgradeStrategy(StrEnum):
    IN_PLACE = "IN_PLACE"
    SEQUENTIAL_MINOR = "SEQUENTIAL_MINOR"
    REINSTALL = "REINSTALL"
    MANUAL = "MANUAL"


class Reversibility(StrEnum):
    """Whether a component version move can be undone by rolling the version back.

    Closes GAP-4. The point is a safety asymmetry: a `REVERSIBLE` step can be tried and
    rolled back, while a `ONE_WAY` step cannot, so the two warrant different approval
    handling downstream.

    `ONE_WAY` is more than "there was a breaking change". Helm rollback reverts workload
    manifests but **not** CRD schema migrations or a stored-version bump — once a custom
    resource is rewritten to a new stored version, reinstalling the old chart does not
    restore it. That is the canonical irreversible move.

    `UNKNOWN` is the honest default when the knowledge base has not classified a change,
    and it is deliberately **not** treated as reversible. A move we cannot confirm is
    safe to undo must be handled as if it cannot be, because the cost of wrongly assuming
    reversible (an unrecoverable cluster) dwarfs the cost of wrongly assuming one-way (an
    unnecessary approval).
    """

    REVERSIBLE = "REVERSIBLE"
    ONE_WAY = "ONE_WAY"
    UNKNOWN = "UNKNOWN"


class VersionRange(ContractModel):
    """A half-open Kubernetes version window: `[minimum_inclusive, maximum_exclusive)`.

    Half-open deliberately, and it is the most common place to introduce an off-by-one.
    Upstream projects publish *inclusive* bounds — cert-manager's table says
    "1.33 – 1.36" — so converting one costs a bump: `1.33 – 1.36` inclusive becomes
    `minimum_inclusive="1.33", maximum_exclusive="1.37"`. Writing `1.36` there would
    silently drop a supported version.

    `maximum_exclusive=None` means "and every later version", which is what a project
    with no announced ceiling is actually claiming.
    """

    minimum_inclusive: str = Field(pattern=r"^\d+\.\d+$")
    maximum_exclusive: str | None = Field(default=None, pattern=r"^\d+\.\d+$")

    @model_validator(mode="after")
    def ceiling_is_above_floor(self) -> VersionRange:
        """A window that permits nothing is a data error, not a strict rule.

        Caught here because an inverted window silently makes every version
        incompatible, which reads as a real finding rather than a bad fact.
        """
        if self.maximum_exclusive is None:
            return self
        floor = tuple(int(p) for p in self.minimum_inclusive.split("."))
        ceiling = tuple(int(p) for p in self.maximum_exclusive.split("."))
        if ceiling <= floor:
            raise ValueError(
                f"maximum_exclusive {self.maximum_exclusive} must be above "
                f"minimum_inclusive {self.minimum_inclusive}; as written the window "
                "permits no Kubernetes version"
            )
        return self


class ApiRemoval(ContractModel):
    """An API version a component depends on that Kubernetes has removed.

    Closes GAP-3. Its predecessor was `removed_api_versions: tuple[str, ...]` — bare
    strings with no removal version, so nothing could be evaluated against an upgrade
    target: knowing that `policy/v1beta1` is gone is useless without knowing it went
    in 1.25.

    `affected_component_series` empty means every series of the component is affected.
    That is the honest default for a dependency the project has never dropped, and it
    keeps a partially-researched fact from silently exempting the series nobody checked.
    """

    api_version: str = Field(min_length=1)
    removed_in_kubernetes: str = Field(pattern=r"^\d+\.\d+$")
    # Which series still depend on the removed API. Empty means all of them.
    affected_component_series: tuple[str, ...] = ()
    replacement_api_version: str | None = None
    evidence: tuple[Citation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def series_are_well_formed(self) -> ApiRemoval:
        for series in self.affected_component_series:
            if not series.endswith(".x") or series.count(".") != 2:
                raise ValueError(
                    f"affected_component_series entries must look like '1.14.x', got {series!r}"
                )
        return self


# A compatibility rule is keyed on a version *spec*, which is one of two shapes:
#
#   series        `0.6.x`   — every patch in this minor shares this compatibility
#   exact release `3.2.0`   — this specific release, when the project publishes per-release
#
# The field keeps its historical name `component_version_series` so existing series
# entries serialise byte-identically and their store digest does not move — the change is
# purely a widened pattern. Whether a given value is a series or an exact release is
# decided by `is_series_spec` / `is_exact_spec` below, and the Stage 4 matcher branches on
# it. This is the contract half of Stage 6.5 (concrete-version compatibility).
_SERIES_SPEC = r"\d+\.\d+\.x"
_EXACT_SPEC = r"\d+\.\d+\.\d+"


def is_series_spec(spec: str) -> bool:
    """True when a compatibility spec covers a whole minor series (`0.6.x`)."""
    return spec.endswith(".x") and spec.count(".") == 2


def is_exact_spec(spec: str) -> bool:
    """True when a compatibility spec names one exact release (`3.2.0`)."""
    return not spec.endswith(".x") and spec.count(".") == 2


class CompatibilityRule(ContractModel):
    # Series (`0.6.x`) OR exact release (`3.2.0`). Widened from series-only in Stage 6.5.
    # Existing series values are unaffected; the name is retained for serialisation
    # stability rather than accuracy — read it as "version spec".
    component_version_series: str = Field(pattern=rf"^(?:{_SERIES_SPEC}|{_EXACT_SPEC})$")
    kubernetes_versions: VersionRange
    # Descriptive metadata: the aggregated API versions this series serves, when it serves
    # one. Not every component extends the Kubernetes API — KEDA's operator and aws-node
    # publish a compatibility matrix with no API-version column — so this is optional. It
    # is not consumed by any verdict; requiring it once rejected valid matrix facts.
    served_api_versions: tuple[str, ...] = ()
    evidence: tuple[Citation, ...] = Field(min_length=1)

    @property
    def is_series(self) -> bool:
        return is_series_spec(self.component_version_series)

    @property
    def is_exact(self) -> bool:
        return is_exact_spec(self.component_version_series)


class ChartApplicationMapping(ContractModel):
    chart_version: str = Field(min_length=1)
    application_version: str = Field(min_length=1)
    evidence: tuple[Citation, ...] = Field(min_length=1)


class ReleaseRecord(ContractModel):
    version: str = Field(min_length=1)
    released_at: datetime
    source_revision: str = Field(min_length=1)
    evidence: tuple[Citation, ...] = Field(min_length=1)


class ChangeRecord(ContractModel):
    introduced_in: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    # Whether crossing this change can be undone by a version rollback. Defaults to
    # UNKNOWN (GAP-4) so an existing entry validates unchanged and an unclassified
    # change is treated as one-way, not as safe. A CRD schema migration or a
    # stored-version bump is ONE_WAY even though Helm reports a successful rollback.
    reversibility: Reversibility = Reversibility.UNKNOWN
    evidence: tuple[Citation, ...] = Field(min_length=1)


class EvidenceBackedStatement(ContractModel):
    statement_id: str = Field(pattern=r"^[a-z][a-z0-9._-]+$")
    statement: str = Field(min_length=1)
    evidence: tuple[Citation, ...] = Field(min_length=1)


class VerificationCheck(ContractModel):
    check_id: str = Field(pattern=r"^[a-z][a-z0-9._-]+$")
    description: str = Field(min_length=1)
    required: bool = True
    evidence: tuple[Citation, ...] = Field(min_length=1)


class ComponentKnowledge(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    knowledge_base_revision: str = Field(min_length=1)
    canonical_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    aliases: tuple[str, ...] = ()
    canonical_repository: AnyHttpUrl
    official_documentation: tuple[AnyHttpUrl, ...] = Field(min_length=1)
    chart_repository: AnyUrl | None = None
    chart_name: str | None = None
    chart_mappings: tuple[ChartApplicationMapping, ...] = ()
    compatibility_rules: tuple[CompatibilityRule, ...] = Field(min_length=1)
    releases: tuple[ReleaseRecord, ...] = Field(min_length=1)
    breaking_changes: tuple[ChangeRecord, ...] = ()
    # Typed as of Stage 4 (GAP-3). Was `tuple[str, ...]`, which could not be evaluated
    # against an upgrade target. An empty tuple validates under either shape, so the
    # existing metrics-server entry needed no edit.
    removed_api_versions: tuple[ApiRemoval, ...] = ()
    dependencies: tuple[str, ...] = ()
    preflight_checks: tuple[EvidenceBackedStatement, ...] = ()
    verification_checks: tuple[VerificationCheck, ...] = Field(min_length=1)
    default_upgrade_strategy: UpgradeStrategy
    refreshed_at: datetime
    evidence_confidence: Confidence

    @field_validator("chart_repository")
    @classmethod
    def chart_repository_is_helm_or_oci(cls, value: AnyUrl | None) -> AnyUrl | None:
        # A `helm repo add` URL (http/https) or an OCI chart reference (`oci://`). Widened from
        # `AnyHttpUrl` so OCI-only charts (karpenter on public ECR) can carry their location;
        # any other scheme is still refused, so the lane is never handed a `file://` or `ftp://`.
        if value is None:
            return value
        if value.scheme not in ("http", "https", "oci"):
            raise ValueError(f"chart_repository must be http(s) or oci://, not {value.scheme}://")
        # AN `oci://` REFERENCE MUST NOT CARRY A `:tag`. The version is resolved at upgrade time
        # and passed to `helm --version`; a tag baked into the reference is both redundant and,
        # when the lane appends the chart name, un-pullable: cert-manager's
        # `oci://quay.io/jetstack/charts/cert-manager:v1.21.2` became
        # `.../cert-manager:v1.21.2/cert-manager`, which no registry serves. A tag sits on the
        # last path segment (never on the host:port), so that is the only segment checked.
        if value.scheme == "oci":
            last_segment = str(value).rstrip("/").rsplit("/", 1)[-1]
            if ":" in last_segment:
                raise ValueError(
                    f"chart_repository {str(value)!r} is an oci:// reference carrying a version "
                    f"tag ({last_segment!r}); give the chart location with no ':tag' (e.g. "
                    "oci://registry.example/charts/<chart>). The version is resolved and "
                    "passed to helm at upgrade time."
                )
        return value

    @model_validator(mode="after")
    def high_confidence_requires_chart_mapping(self) -> ComponentKnowledge:
        if (
            self.evidence_confidence is Confidence.HIGH
            and self.chart_name
            and not self.chart_mappings
        ):
            raise ValueError("HIGH confidence chart knowledge requires a chart mapping")
        return self
