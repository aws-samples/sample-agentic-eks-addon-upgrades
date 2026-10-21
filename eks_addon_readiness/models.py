"""Strict, versioned contracts for the single Analyzer agent."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    """Immutable contract that rejects undeclared input."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    def canonical_json(self) -> str:
        """Return stable JSON for hashing, replay, and contract comparison."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNRESOLVED = "UNRESOLVED"


class CompatibilityVerdict(StrEnum):
    COMPATIBLE = "COMPATIBLE"
    REQUIRES_COMPONENT_UPGRADE = "REQUIRES_COMPONENT_UPGRADE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNRESOLVED = "UNRESOLVED"


class InstallationMethod(StrEnum):
    HELM = "HELM"
    MANIFEST = "MANIFEST"
    EKS_ADDON = "EKS_ADDON"
    UNKNOWN = "UNKNOWN"


class EvidenceKind(StrEnum):
    HELM_RELEASE = "HELM_RELEASE"
    APPLICATION_METADATA = "APPLICATION_METADATA"
    WORKLOAD_IMAGE = "WORKLOAD_IMAGE"
    CRD_METADATA = "CRD_METADATA"
    OFFICIAL_COMPATIBILITY = "OFFICIAL_COMPATIBILITY"
    OFFICIAL_RELEASE = "OFFICIAL_RELEASE"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    BLOCKER = "BLOCKER"


class Citation(ContractModel):
    url: AnyHttpUrl
    title: str = Field(min_length=1)
    source_revision: str | None = None
    retrieved_at: datetime


class Evidence(ContractModel):
    kind: EvidenceKind
    source: str = Field(min_length=1)
    observed_value: str = Field(min_length=1)
    confidence: Confidence
    citation: Citation | None = None


class UnresolvedFinding(ContractModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    missing_evidence: tuple[str, ...] = Field(min_length=1)
    resolution_steps: tuple[str, ...] = Field(min_length=1)


class ImageReference(ContractModel):
    repository: str = Field(min_length=1)
    tag: str | None = None
    digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")


class ComponentInventory(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    canonical_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    namespace: str = Field(min_length=1)
    chart_name: str | None = None
    chart_version: str | None = None
    application_version: str | None = None
    images: tuple[ImageReference, ...] = ()
    installation_method: InstallationMethod
    detection_evidence: tuple[Evidence, ...] = Field(min_length=1)
    version_resolution_confidence: Confidence
    unresolved: tuple[UnresolvedFinding, ...] = ()

    @model_validator(mode="after")
    def unresolved_confidence_has_details(self) -> ComponentInventory:
        if self.version_resolution_confidence is Confidence.UNRESOLVED and not self.unresolved:
            raise ValueError("UNRESOLVED confidence requires an unresolved finding")
        return self


class KubernetesHop(ContractModel):
    current_version: str = Field(pattern=r"^\d+\.\d+$")
    target_version: str = Field(pattern=r"^\d+\.\d+$")


class Finding(ContractModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    severity: Severity
    summary: str = Field(min_length=1)
    details: str = Field(min_length=1)
    citations: tuple[Citation, ...] = Field(min_length=1)


class HopAnalysis(ContractModel):
    hop: KubernetesHop
    verdict: CompatibilityVerdict
    confidence: Confidence
    recommended_chart_version: str | None = None
    recommended_application_version: str | None = None
    findings: tuple[Finding, ...] = ()
    unresolved: tuple[UnresolvedFinding, ...] = ()

    @model_validator(mode="after")
    def unresolved_verdict_has_details(self) -> HopAnalysis:
        if self.verdict is CompatibilityVerdict.UNRESOLVED and not self.unresolved:
            raise ValueError("UNRESOLVED verdict requires an unresolved finding")
        return self


class ClusterReference(ContractModel):
    name: str = Field(min_length=1)
    account_alias: str | None = None
    region: str | None = None


class AnalysisReport(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    created_at: datetime
    cluster: ClusterReference
    snapshot_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    input_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    current_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    target_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    knowledge_base_revision: str = Field(min_length=1)
    inventory: ComponentInventory
    hop_analyses: tuple[HopAnalysis, ...] = Field(min_length=1)
    unresolved: tuple[UnresolvedFinding, ...] = ()


class EvidenceManifest(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: UUID
    snapshot_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    knowledge_base_revision: str = Field(min_length=1)
    citations: tuple[Citation, ...] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)