"""Parse container image references and assemble workload evidence.

Pure functions over already-fetched data, so testable without a cluster.

Annotations are deliberately allowlisted rather than copied wholesale:
`kubectl.kubernetes.io/last-applied-configuration` embeds a full manifest, which
can carry environment variables and other sensitive values. Only the annotations
needed to establish Helm release identity are retained.
"""

from __future__ import annotations

import re
from typing import Any

from ..models import ImageReference
from ..snapshot import WorkloadContainer, WorkloadSnapshot

# Annotations retained in the snapshot. Everything else is dropped.
ALLOWED_ANNOTATIONS = frozenset(
    {
        "meta.helm.sh/release-name",
        "meta.helm.sh/release-namespace",
    }
)

_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


def parse_image_reference(reference: str) -> ImageReference | None:
    """Split a container image reference into repository, tag and digest.

    Handles registry hosts with ports, so the final colon is not assumed to
    introduce a tag.

    >>> parse_image_reference("nginx:alpine")
    ImageReference(repository='nginx', tag='alpine', digest=None)
    >>> parse_image_reference("registry.k8s.io/metrics-server/metrics-server:v0.6.4").tag
    'v0.6.4'
    >>> parse_image_reference("repo/app@sha256:" + "a" * 64).tag is None
    True
    >>> parse_image_reference("plain/repo").tag is None
    True
    """
    text = reference.strip()
    if not text:
        return None

    digest: str | None = None
    if "@" in text:
        text, _, candidate = text.rpartition("@")
        if _DIGEST.match(candidate):
            digest = candidate
        if not text:
            return None

    tag: str | None = None
    # A colon introduces a tag only if it appears after the last slash;
    # otherwise it is a registry port, as in "localhost:5000/app".
    last_slash = text.rfind("/")
    last_colon = text.rfind(":")
    if last_colon > last_slash:
        tag = text[last_colon + 1 :] or None
        text = text[:last_colon]

    if not text:
        return None
    return ImageReference(repository=text, tag=tag, digest=digest)


def _containers_from_spec(resource: dict[str, Any]) -> tuple[WorkloadContainer, ...]:
    """Extract containers from a workload's pod template.

    Init containers are excluded: they are setup steps and their versions do not
    represent the component.
    """
    spec = resource.get("spec")
    if not isinstance(spec, dict):
        return ()
    template = spec.get("template")
    if not isinstance(template, dict):
        return ()
    pod_spec = template.get("spec")
    if not isinstance(pod_spec, dict):
        return ()
    raw_containers = pod_spec.get("containers")
    if not isinstance(raw_containers, list):
        return ()

    found: list[WorkloadContainer] = []
    for entry in raw_containers:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        reference = entry.get("image")
        if not isinstance(name, str) or not isinstance(reference, str):
            continue
        image = parse_image_reference(reference)
        if image is None:
            continue
        found.append(WorkloadContainer(name=name, image=image))
    return tuple(sorted(found, key=lambda c: c.name))


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# Where each workload kind reports its ready count. Deployments and StatefulSets use
# `readyReplicas`; DaemonSets use `numberReady`.
_READY_FIELDS = ("readyReplicas", "numberReady")


def _ready_replicas(status: dict[str, Any]) -> int | None:
    """Ready replica count from a workload status.

    Two subtleties, both of which produced wrong answers before this was explicit.

    **Kubernetes omits zero-valued integers from status.** A Deployment with no ready
    pods has no `readyReplicas` key at all — it reports `unavailableReplicas` instead.
    So absence *within a status object that exists* means zero, not unknown, and
    reporting `None` there reads as "we could not tell" when the truth is "none are
    ready". A genuinely unreadable workload is already marked `detail_unavailable`.

    **Zero must not be confused with absent.** The previous implementation chained
    the two field names with `or`, so a present `readyReplicas: 0` fell through to
    `numberReady` and yielded `None`. DaemonSets happened to work only because
    `numberReady` came second.
    """
    for field in _READY_FIELDS:
        if field in status:
            return _int_or_none(status[field])
    # The status exists and names no ready count, so the count is zero.
    return 0 if status else None


# Kubernetes substitutes this when a pod names no ServiceAccount, so it is not a
# dedicated identity and carries no evidence.
DEFAULT_SERVICE_ACCOUNT = "default"


def _service_account_from_spec(resource: dict[str, Any]) -> str | None:
    """The pod template's ServiceAccount, or `None` when it is not a dedicated one.

    Both an absent field and an explicit `default` return `None`: neither means
    the workload was given an identity of its own.

    >>> _service_account_from_spec(
    ...     {"spec": {"template": {"spec": {"serviceAccountName": "keda-operator"}}}})
    'keda-operator'
    >>> _service_account_from_spec(
    ...     {"spec": {"template": {"spec": {"serviceAccountName": "default"}}}}) is None
    True
    >>> _service_account_from_spec({"spec": {"template": {"spec": {}}}}) is None
    True
    """
    spec = resource.get("spec")
    if not isinstance(spec, dict):
        return None
    template = spec.get("template")
    if not isinstance(template, dict):
        return None
    pod_spec = template.get("spec")
    if not isinstance(pod_spec, dict):
        return None
    name = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or name == DEFAULT_SERVICE_ACCOUNT:
        return None
    return name


def build_workload(
    kind: str,
    listed: dict[str, Any],
    detail: dict[str, Any] | None,
) -> WorkloadSnapshot:
    """Combine a list entry with an optional detail read into one record.

    `listed` supplies labels and annotations; `detail` supplies containers and
    replica counts. When `detail` is absent the record is marked
    `detail_unavailable` so an empty container tuple is not mistaken for a
    workload that genuinely has none.
    """
    raw_labels = listed.get("labels")
    labels = (
        {k: v for k, v in raw_labels.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(raw_labels, dict)
        else {}
    )

    raw_annotations = listed.get("annotations")
    helm_annotations = (
        {
            k: v
            for k, v in raw_annotations.items()
            if k in ALLOWED_ANNOTATIONS and isinstance(v, str)
        }
        if isinstance(raw_annotations, dict)
        else {}
    )

    containers: tuple[WorkloadContainer, ...] = ()
    replicas: int | None = None
    ready_replicas: int | None = None
    service_account_name: str | None = None

    if detail is not None:
        containers = _containers_from_spec(detail)
        service_account_name = _service_account_from_spec(detail)
        spec = detail.get("spec")
        if isinstance(spec, dict):
            replicas = _int_or_none(spec.get("replicas"))
        status = detail.get("status")
        if isinstance(status, dict):
            ready_replicas = _ready_replicas(status)

    name = listed.get("name")
    return WorkloadSnapshot(
        kind=kind,
        namespace=listed.get("namespace") or None,
        name=str(name),
        labels=labels,
        helm_annotations=helm_annotations,
        containers=containers,
        replicas=replicas,
        ready_replicas=ready_replicas,
        service_account_name=service_account_name,
        detail_unavailable=detail is None,
    )
