"""Establish a component's project identity from what the cluster declares.

No registry, no hardcoded list of known components. Identity comes from the
component's own self-declaration, in descending order of authority:

    1. `app.kubernetes.io/name`   the standard Kubernetes label, set by the project
    2. `helm.sh/chart`            chart name with its version suffix removed
    3. image repository basename  last path segment of the container image
    4. otherwise                  unidentified

Rung 3 is a weak signal and is marked as such: `nginx:alpine` yields the identity
`nginx`, which describes the image rather than the component. Whether that is
useful is left to the caller, which records the rung that answered.

Everything here is a pure function over already-collected snapshot data.
"""

from __future__ import annotations

from enum import StrEnum

from ..snapshot import WorkloadSnapshot
from .versions import strip_chart_version

NAME_LABEL = "app.kubernetes.io/name"
CHART_LABEL = "helm.sh/chart"


class IdentitySource(StrEnum):
    """Which rung established the identity. Ordered most to least authoritative.

    `EKS_ADDON_API` is not one of the rungs below: it applies to a record derived
    from the EKS API rather than from a workload, where AWS supplies the name.
    """

    EKS_ADDON_API = "EKS_ADDON_API"
    NAME_LABEL = "NAME_LABEL"
    CHART_LABEL = "CHART_LABEL"
    IMAGE_REPOSITORY = "IMAGE_REPOSITORY"
    NONE = "NONE"


class ResolvedIdentity:
    """A component identity and the evidence that produced it."""

    __slots__ = ("name", "source", "detail")

    def __init__(self, name: str | None, source: IdentitySource, detail: str) -> None:
        self.name = name
        self.source = source
        self.detail = detail

    @property
    def identified(self) -> bool:
        return self.name is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ResolvedIdentity(name={self.name!r}, source={self.source})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ResolvedIdentity):
            return NotImplemented
        return (self.name, self.source, self.detail) == (other.name, other.source, other.detail)


def image_repository_basename(repository: str) -> str | None:
    """Last path segment of an image repository.

    >>> image_repository_basename("registry.k8s.io/metrics-server/metrics-server")
    'metrics-server'
    >>> image_repository_basename("nginx")
    'nginx'
    >>> image_repository_basename("") is None
    True
    """
    segment = repository.strip().rstrip("/").rpartition("/")[2]
    return segment or None


def resolve_identity(workload: WorkloadSnapshot) -> ResolvedIdentity:
    """Establish the project identity for one workload."""
    name = workload.labels.get(NAME_LABEL, "").strip()
    if name:
        return ResolvedIdentity(name, IdentitySource.NAME_LABEL, f"label {NAME_LABEL}={name}")

    chart = workload.labels.get(CHART_LABEL, "").strip()
    if chart:
        chart_name, _ = strip_chart_version(chart)
        if chart_name:
            return ResolvedIdentity(
                chart_name, IdentitySource.CHART_LABEL, f"label {CHART_LABEL}={chart}"
            )

    # Only a single-container workload can yield an unambiguous identity from an
    # image. With several containers there is no basis for choosing one.
    if len(workload.containers) == 1:
        repository = workload.containers[0].image.repository
        basename = image_repository_basename(repository)
        if basename:
            return ResolvedIdentity(
                basename, IdentitySource.IMAGE_REPOSITORY, f"image repository {repository}"
            )

    reason = (
        f"{len(workload.containers)} containers, no identifying labels"
        if len(workload.containers) > 1
        else "no identifying labels and no usable image repository"
    )
    return ResolvedIdentity(None, IdentitySource.NONE, reason)
