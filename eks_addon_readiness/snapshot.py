"""Sanitized, component-neutral cluster snapshot contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from .models import ClusterReference, ContractModel, ImageReference, UnresolvedFinding


class SnapshotSource(StrEnum):
    FROZEN_FIXTURE = "FROZEN_FIXTURE"
    LIVE_READ_ONLY = "LIVE_READ_ONLY"


class EksManagedAddonSnapshot(ContractModel):
    """An EKS-managed add-on as reported by the EKS API.

    Managed add-ons are not Helm releases and therefore cannot be represented by
    `HelmReleaseSnapshot`. Their version is authoritative because it is supplied
    by the EKS control plane rather than inferred.
    """

    addon_name: str = Field(min_length=1)
    addon_version: str | None = None
    status: str | None = None
    namespace: str | None = None
    health_issue_codes: tuple[str, ...] = ()


class EksAddonVersionCompatibility(ContractModel):
    """One published EKS-managed add-on build and the Kubernetes versions it supports.

    A faithful, sanitized copy of one `describe-addon-versions` `addonVersions[]` entry:
    the add-on version string (`v1.14.3-eksbuild.16`) and the Kubernetes minor versions
    AWS lists it as compatible with (`compatibilities[].clusterVersion`). This is the
    **authoritative control-plane catalogue**, unlike an EKS Insight — it is a stable
    published fact keyed by add-on version, and it spans every Kubernetes version AWS
    offers, so it answers multi-hop planning where the single-hop insight cannot.

    Nothing account-specific is captured: `describe-addon-versions` is a global catalogue
    call with no ARNs or account IDs in its response, so there is nothing to strip.
    """

    addon_name: str = Field(min_length=1)
    addon_version: str = Field(min_length=1)
    # The Kubernetes minor versions AWS lists this add-on version as compatible with,
    # sorted for determinism. `compatibilities[].clusterVersion` in the API response.
    compatible_kubernetes_versions: tuple[str, ...] = ()


class ClusterInsightSnapshot(ContractModel):
    """One EKS Insight, as AWS assessed it at a moment in time.

    Captured into the snapshot rather than queried during assessment, and the
    distinction is the whole point. An insight is **AWS's computed opinion, not a
    stable fact**: it carries `computed_at` because AWS recalculates it, and the
    answer can change without the cluster changing. Calling it at verdict time would
    make a verdict irreproducible — replaying the same snapshot months later could
    yield a different result, and `verify` would begin reporting DEFECT on reports
    that were correct when written.

    Recording it here keeps every stage after Stage 1 a pure function over evidence,
    which is what makes replay work at all.

    `kubernetes_version` is the target AWS assessed against, always the *next* minor;
    the tool accepts no target parameter. So this does not answer multi-hop planning,
    and Stage 5 will need something else.

    **Resource ARNs are deliberately not captured.** The underlying response lists
    affected resources as ARNs, which embed the AWS account ID. The add-on names they
    identify are already present in `addon_compatible_versions`, so the ARNs add
    nothing except an identifier the snapshot contract exists to keep out.
    """

    insight_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    # The target AWS assessed against — the next Kubernetes minor.
    kubernetes_version: str | None = None
    # PASSING, ERROR, WARNING or UNKNOWN, as reported.
    status: str | None = None
    reason: str | None = None
    recommendation: str | None = None
    # Documentation links AWS attaches to the insight.
    #
    # Not reliably parseable as URLs. The EKS MCP Server applies its own sensitive-
    # value redaction to tool responses and does so over-eagerly: on the reference
    # cluster it returned
    # `https://docs.aws.amazon.[HIDDEN_FOR_SECURITY_REASONS].html`, mangling a public
    # AWS documentation link. Retained verbatim rather than dropped or repaired —
    # inventing the missing segment would fabricate evidence, and discarding the entry
    # would lose the label, which still tells a reader what the link was for. A
    # consumer must not assume these values parse.
    additional_info: dict[str, str] = Field(default_factory=dict)
    # When AWS last computed this insight, which is not when we read it.
    computed_at: datetime | None = None
    # add-on name -> versions AWS considers compatible with `kubernetes_version`.
    # Narrower than the full `describe-addon-versions` catalogue: on the reference
    # cluster this reported 1 version for coredns where the catalogue listed 16. It
    # answers "what can I move to from here", not "everything that would work".
    addon_compatible_versions: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    # Deliberately a count, not the payload. `deprecationDetails` was empty on every
    # cluster available when this was written, so its populated shape has never been
    # observed. Modelling a contract against an empty array would be guessing, and
    # storing it as an unvalidated blob would break the no-arbitrary-payload rule the
    # snapshot rests on. Recording the count means the data is never silently dropped:
    # a non-zero value raises a typed collection gap telling the reader to inspect the
    # insight directly, and the contract gets written once the real shape is known.
    deprecation_detail_count: int = Field(default=0, ge=0)


class WorkloadContainer(ContractModel):
    """One container in a workload's pod template."""

    name: str = Field(min_length=1)
    image: ImageReference


class WorkloadSnapshot(ContractModel):
    """A workload with the evidence needed to identify and version it.

    Labels are retained in full: identity resolution reads
    `app.kubernetes.io/name` and `helm.sh/chart`, and a workload that cannot be
    identified is reported with every label present so an operator can recognise
    what the tool could not.

    Annotations are **allowlisted**, not retained in full. Annotations such as
    `kubectl.kubernetes.io/last-applied-configuration` embed an entire manifest,
    which can include environment variables and other sensitive values. Only the
    annotations required to establish Helm release identity are kept.
    """

    kind: str = Field(min_length=1)
    namespace: str | None = None
    name: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)
    helm_annotations: dict[str, str] = Field(default_factory=dict)
    containers: tuple[WorkloadContainer, ...] = ()
    replicas: int | None = None
    ready_replicas: int | None = None
    # The pod's ServiceAccount, when it declares a dedicated one. `None` means the
    # workload did not name one and therefore runs as `default`.
    #
    # Retained because it is the cheapest available evidence that a workload talks
    # to the Kubernetes API: a component that needs permissions is given its own
    # identity, whereas an application that only serves traffic is usually left on
    # `default`. It costs nothing to collect — it is already in the detail read
    # performed for container images.
    service_account_name: str | None = None
    # True when detail could not be read, so an empty `containers` tuple is not
    # mistaken for a workload that has no containers.
    detail_unavailable: bool = False


class HelmReleaseSnapshot(ContractModel):
    """A Helm release as identified from chart provenance labels.

    Carries no image references. Images belong to a workload, not to a release:
    a release can span several workloads and a workload can run several
    containers, so an image list at release level cannot express which image
    belongs to what. Images live on `WorkloadSnapshot.containers`, and
    `derived_from_workloads` links a release to the workloads that evidence it.
    """

    release_name: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    chart_name: str = Field(min_length=1)
    chart_version: str | None = None
    application_version: str | None = None
    # Workloads that carried the chart provenance labels this release was
    # derived from. Retained so a version claim can be traced to its evidence.
    derived_from_workloads: tuple[str, ...] = ()


class KubernetesResourceReference(ContractModel):
    """A reference to a cluster resource, with labels where they matter.

    Labels are retained for cluster-scoped kinds so ownership can be correlated:
    a CustomResourceDefinition carrying `app.kubernetes.io/name: keda-operator`
    evidences that the `keda-operator` component extends the cluster API, which is
    the strongest available signal that it is platform infrastructure rather than
    an application.

    **Completeness differs by kind and is not uniform.** For cluster-scoped kinds
    the collector performs two reads and merges them: an unfiltered enumeration,
    which the transport may cap silently, and a read filtered to resources
    carrying `app.kubernetes.io/name`, which is complete because only labelled
    resources match. So every labelled resource of a collected kind is present,
    while unlabelled ones may not be. `ClusterSnapshot.collection_gaps` records
    which applies via `API_GROUP_ENUMERATION_INCOMPLETE`.
    """

    api_version: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    namespace: str | None = None
    name: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_secret_resources(self) -> KubernetesResourceReference:
        if self.kind.casefold() == "secret":
            raise ValueError("Secret resources must not enter snapshots")
        return self


class ClusterSnapshot(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    snapshot_id: UUID
    captured_at: datetime
    source: SnapshotSource
    sanitized: Literal[True]
    cluster: ClusterReference
    kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    platform_version: str | None = None
    eks_addons: tuple[EksManagedAddonSnapshot, ...] = ()
    # The authoritative EKS add-on version catalogue (describe-addon-versions), one entry
    # per published add-on build with the Kubernetes versions it supports. Unlike an
    # insight, this spans every Kubernetes version AWS offers, so managed add-ons can be
    # decided deterministically across every hop AWS publishes — no research needed.
    eks_addon_catalogue: tuple[EksAddonVersionCompatibility, ...] = ()
    helm_releases: tuple[HelmReleaseSnapshot, ...] = ()
    # Full workload evidence — labels, containers, replicas. Stage 2 identity,
    # version resolution and confidence rating are pure functions over this, so
    # no stage after collection needs cluster access.
    workloads: tuple[WorkloadSnapshot, ...] = ()
    # Cluster-scoped references. Read `KubernetesResourceReference` before
    # treating absence as evidence: completeness is guaranteed for resources
    # carrying an ownership label and not guaranteed otherwise.
    resources: tuple[KubernetesResourceReference, ...] = ()
    served_api_versions: tuple[str, ...] = ()
    # AWS's own upgrade-readiness assessment, frozen at capture time. See
    # `ClusterInsightSnapshot`: these are computed opinions that AWS recalculates,
    # so they are evidence rather than facts and must not be re-fetched during
    # assessment.
    insights: tuple[ClusterInsightSnapshot, ...] = ()
    # Reads that were attempted and failed, typed rather than omitted. A snapshot
    # with an empty collection where access was denied would be indistinguishable
    # from a cluster that genuinely has nothing, which is the failure mode this
    # field exists to prevent.
    collection_gaps: tuple[UnresolvedFinding, ...] = ()