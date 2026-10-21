"""Rank how strongly a workload looks like platform infrastructure.

**This module never excludes anything from assessment.** It produces a priority
and the evidence behind it; every component is checked regardless. See
`AssessmentPriority` for why that matters.

Signals, all checkable from evidence the cluster already reports:

    is an EKS-managed add-on         AWS declares it one
    owns a CustomResourceDefinition  it extends the cluster API
    serves an aggregated APIService  it extends the cluster API
    is a DaemonSet                   a copy on every node is node-level
                                     infrastructure, not a business application
    lives in kube-system             the only namespace Kubernetes itself defines

Every signal that fires is recorded, rather than returning on the first match, so
the reason states all the evidence and the priority reflects its combined
strength.

Correlating a workload to an API it extends is the hard part, because Kubernetes
records no link between the two. Two routes, strongest first:

    1. Ownership label. A CRD carrying `app.kubernetes.io/name: keda-operator`
       names its owner outright. Sparsely available: on the reference cluster only
       10 of 271 CRDs carry any label, though KEDA's do.

    2. API group correlation. A CRD named `scaledobjects.keda.sh` and an
       APIService named `v1alpha1.keda.sh` both belong to group `keda.sh`, whose
       leading label is `keda`. A workload declaring `keda` in any of its own
       standard names is evidence of the same project. Kubernetes' own API groups
       are excluded, since the apiserver serves those and no workload does.

Known incompleteness, stated because it shapes how the output should be read:
these signals recognise components that *extend* the Kubernetes API, while what
actually breaks on an upgrade is API *usage*. A component can consume the API
without extending it — `external-dns`, `kube-state-metrics` — and a
Deployment-shaped one outside `kube-system` will rank low. That is survivable only
because ranking does not gate. The stronger signal would be a ClusterRole, which
proves API access and names the `apiGroups` used, but reading those needs RBAC
beyond `AmazonEKSViewPolicy`.

An operator override is supported for the cases the rules get wrong.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import NamedTuple

from ..snapshot import ClusterSnapshot, KubernetesResourceReference, WorkloadSnapshot
from .identity import CHART_LABEL, NAME_LABEL, IdentitySource
from .versions import strip_chart_version

# The standard labels by which a workload declares what project it belongs to.
# `part-of` and `instance` matter because a project's workloads are often named
# per-component (`keda-operator`, `keda-admission-webhooks`) while only these
# carry the project name.
PART_OF_LABEL = "app.kubernetes.io/part-of"
INSTANCE_LABEL = "app.kubernetes.io/instance"

# The only namespace defined by Kubernetes itself. Deliberately not a curated
# list of conventional platform namespaces: those vary by installation and
# encoding them would be the hardcoding this design avoids.
STANDARD_SYSTEM_NAMESPACES = frozenset({"kube-system"})

# API groups owned by Kubernetes. Served by the apiserver, so their presence is
# never evidence that a workload extends anything. This is a fixed list defined
# by Kubernetes upstream, not a curated list of components.
KUBERNETES_API_GROUPS = frozenset(
    {
        "admissionregistration.k8s.io",
        "apiextensions.k8s.io",
        "apiregistration.k8s.io",
        "apps",
        "authentication.k8s.io",
        "authorization.k8s.io",
        "autoscaling",
        "batch",
        "certificates.k8s.io",
        "coordination.k8s.io",
        "discovery.k8s.io",
        "events.k8s.io",
        "flowcontrol.apiserver.k8s.io",
        "internal.apiserver.k8s.io",
        "networking.k8s.io",
        "node.k8s.io",
        "policy",
        "rbac.authorization.k8s.io",
        "resource.k8s.io",
        "scheduling.k8s.io",
        "storage.k8s.io",
        "storagemigration.k8s.io",
    }
)

# Generic words that appear as an API group's leading label but identify no
# project, so matching one proves nothing.
GENERIC_GROUP_TOKENS = frozenset(
    {
        "addon",
        "addons",
        "api",
        "apis",
        "cloud",
        "cluster",
        "config",
        "core",
        "internal",
        "kubernetes",
        "node",
        "nodes",
        "resource",
        "resources",
        "services",
        "system",
    }
)

# Below this length a token is too common to be evidence: `s3` would match an
# application called `s3-uploader` against the `s3.services.k8s.aws` group.
MIN_TOKEN_LENGTH = 4

# An APIService is named `<version>.<group>`, so the version prefix is stripped
# to recover the group.
_API_VERSION_SEGMENT = re.compile(r"^v\d+(?:(?:alpha|beta)\d+)?$")

_TOKEN_SEPARATORS = re.compile(r"[-._/]+")

# Collection gaps that make a "no platform signal" verdict unreliable, because the
# API a workload extends may simply be absent from the snapshot. Deliberately a
# set rather than one code: a denied read undermines the verdict exactly as much
# as a capped one, and only listing truncation would have missed that.
#
# `WORKLOAD_LIST_INCOMPLETE` is *not* here. That gap means some workloads are
# missing from the snapshot, which is a completeness problem for the inventory as
# a whole — it says nothing about the workloads that are present.
EVIDENCE_GAP_CODES = frozenset(
    {
        "API_GROUP_ENUMERATION_INCOMPLETE",
        "CLUSTER_SCOPED_READ_FORBIDDEN",
        "OWNERSHIP_LABEL_READ_UNAVAILABLE",
    }
)


class Classification(StrEnum):
    RECOGNIZED = "recognized"
    UNRECOGNIZED = "unrecognized"
    CUSTOM_WORKLOAD = "custom-workload"


class AssessmentPriority(StrEnum):
    """How strongly this workload should be checked against a target version.

    **Nothing is ever excluded from assessment.** Priority orders and explains; it
    does not gate.

    That is a deliberate correction. Classification's two errors are wildly
    asymmetric: an application checked unnecessarily produces noise, while a
    platform component wrongly filed as an application is never checked, and you
    discover it during the upgrade. An imperfect rule is acceptable when its worst
    outcome is a misordered list. It is not acceptable when its worst outcome is a
    silent missing blocker — and the rule *is* imperfect, because it cannot see
    components that consume the Kubernetes API without extending it, such as a log
    forwarder or `external-dns` running outside `kube-system`.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ClassificationResult(NamedTuple):
    classification: Classification
    is_platform_component: bool
    reason: str
    priority: AssessmentPriority = AssessmentPriority.LOW
    # Every platform signal that fired, so the ordering is explainable rather than
    # an opaque score.
    evidence: tuple[str, ...] = ()
    # True when the verdict rests on not having found evidence, and the evidence
    # is known to be incompletely collected. Only a `custom-workload` verdict can
    # be provisional: every platform verdict is reached by finding something,
    # which incomplete collection cannot invent.
    provisional: bool = False


class Overrides(NamedTuple):
    """Operator-supplied corrections.

    `platform_namespaces` adds namespaces to be treated as platform.
    `platform_identities` and `application_identities` force a decision for a
    named identity.
    """

    platform_namespaces: frozenset[str] = frozenset()
    platform_identities: frozenset[str] = frozenset()
    application_identities: frozenset[str] = frozenset()


def tokenize(name: str) -> set[str]:
    """Split a name into lowercase tokens.

    >>> sorted(tokenize("keda-operator-metrics-apiserver"))
    ['apiserver', 'keda', 'metrics', 'operator']
    >>> sorted(tokenize("cognitoidentity-chart"))
    ['chart', 'cognitoidentity']
    """
    return {part for part in _TOKEN_SEPARATORS.split(name.strip().casefold()) if part}


def api_group_of(resource: KubernetesResourceReference) -> str | None:
    """The API group a cluster-scoped resource belongs to, or None.

    A CustomResourceDefinition is named `<plural>.<group>`; an APIService is
    named `<version>.<group>`.

    >>> ref = lambda k, n: KubernetesResourceReference(
    ...     api_version="v1", kind=k, name=n)
    >>> api_group_of(ref("CustomResourceDefinition", "scaledobjects.keda.sh"))
    'keda.sh'
    >>> api_group_of(ref("APIService", "v1alpha1.keda.sh"))
    'keda.sh'
    >>> api_group_of(ref("APIService", "v1.")) is None
    True
    """
    if resource.kind == "CustomResourceDefinition":
        _, _, group = resource.name.partition(".")
        return group or None

    if resource.kind == "APIService":
        head, _, group = resource.name.partition(".")
        if not _API_VERSION_SEGMENT.match(head):
            return None
        return group or None

    return None


def extension_api_tokens(resources: tuple[KubernetesResourceReference, ...]) -> set[str]:
    """Leading labels of every non-Kubernetes API group in the cluster.

    These are the tokens a workload can be correlated against. Generic and short
    labels are dropped because matching them is not evidence.
    """
    tokens: set[str] = set()
    for resource in resources:
        group = api_group_of(resource)
        if not group or group in KUBERNETES_API_GROUPS:
            continue
        leading = group.split(".", 1)[0].casefold()
        if len(leading) < MIN_TOKEN_LENGTH or leading in GENERIC_GROUP_TOKENS:
            continue
        tokens.add(leading)
    return tokens


def api_extension_owners(resources: tuple[KubernetesResourceReference, ...]) -> set[str]:
    """Identities that declare CRD or APIService ownership by label.

    The strongest correlation available, and unused on clusters whose projects do
    not label their CRDs.
    """
    return _labelled_owners(resources, {"CustomResourceDefinition", "APIService"})


def webhook_owners(resources: tuple[KubernetesResourceReference, ...]) -> set[str]:
    """Identities that declare ownership of an admission webhook configuration.

    Registering a webhook means intercepting writes to the Kubernetes API, which
    only a controller has a reason to do. Like CRD ownership this depends on the
    project labelling its own objects — on the reference cluster 1 of 4 webhook
    configurations does.
    """
    return _labelled_owners(
        resources,
        {"ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"},
    )


def _labelled_owners(
    resources: tuple[KubernetesResourceReference, ...], kinds: set[str]
) -> set[str]:
    owners: set[str] = set()
    for resource in resources:
        if resource.kind not in kinds:
            continue
        owner = resource.labels.get(NAME_LABEL, "").strip()
        if owner:
            owners.add(owner)
    return owners


def declared_names(workload: WorkloadSnapshot, identity: str | None) -> set[str]:
    """Every name this workload declares about itself.

    Only standard `app.kubernetes.io/*` labels and the Helm chart name are read.
    Non-standard labels such as `app` are ignored: they carry no agreed meaning,
    so treating them as project declarations would invite false matches.
    """
    names: set[str] = set()
    if identity:
        names.add(identity)
    for key in (NAME_LABEL, PART_OF_LABEL, INSTANCE_LABEL):
        value = workload.labels.get(key, "").strip()
        if value:
            names.add(value)
    chart = workload.labels.get(CHART_LABEL, "").strip()
    if chart:
        chart_name, _ = strip_chart_version(chart)
        if chart_name:
            names.add(chart_name)
    return names


def classify(
    workload: WorkloadSnapshot,
    identity: str | None,
    snapshot: ClusterSnapshot,
    *,
    identity_source: IdentitySource = IdentitySource.NONE,
    overrides: Overrides | None = None,
    eks_addon_names: frozenset[str] | None = None,
) -> ClassificationResult:
    """Decide whether a workload is a platform component, and how to label it."""
    overrides = overrides or Overrides()
    addon_names = eks_addon_names or frozenset(a.addon_name for a in snapshot.eks_addons)

    # An identity taken from an image repository names the image, not the
    # project: `nginx:alpine` yields `nginx`, which says nothing about what the
    # workload is. Such a workload can still be a platform component, but it is
    # not an identified one.
    identity_is_declared = identity is not None and identity_source in {
        IdentitySource.EKS_ADDON_API,
        IdentitySource.NAME_LABEL,
        IdentitySource.CHART_LABEL,
    }

    def bucket_for(is_component: bool) -> Classification:
        if not is_component:
            return Classification.CUSTOM_WORKLOAD
        return (
            Classification.RECOGNIZED
            if identity_is_declared
            else Classification.UNRECOGNIZED
        )

    names = declared_names(workload, identity)

    # Explicit overrides win outright, so an operator can correct a wrong call once
    # rather than argue with it on every run.
    if identity and identity in overrides.application_identities:
        return ClassificationResult(
            Classification.CUSTOM_WORKLOAD,
            False,
            f"override: {identity} declared an application",
            AssessmentPriority.LOW,
            ("OVERRIDE_APPLICATION",),
        )
    if identity and identity in overrides.platform_identities:
        return ClassificationResult(
            bucket_for(True),
            True,
            f"override: {identity} declared a platform component",
            AssessmentPriority.HIGH,
            ("OVERRIDE_PLATFORM",),
        )

    # Every signal is gathered rather than short-circuited on the first hit, so the
    # reason states all the evidence and the priority reflects its strength.
    evidence: list[str] = []
    reasons: list[str] = []

    if names & addon_names:
        matched = sorted(names & addon_names)[0]
        evidence.append("EKS_MANAGED_ADDON")
        reasons.append(f"{matched} is an EKS-managed add-on")

    api_owners = names & api_extension_owners(snapshot.resources)
    if api_owners:
        evidence.append("LABELLED_API_OWNER")
        reasons.append(f"{sorted(api_owners)[0]} is labelled owner of a CRD or APIService")

    hook_owners = names & webhook_owners(snapshot.resources)
    if hook_owners:
        evidence.append("OWNS_ADMISSION_WEBHOOK")
        reasons.append(
            f"{sorted(hook_owners)[0]} registers an admission webhook, so it "
            "intercepts writes to the Kubernetes API"
        )

    workload_tokens = {token for name in names for token in tokenize(name)}
    shared = workload_tokens & extension_api_tokens(snapshot.resources)
    if shared:
        matched = sorted(shared)[0]
        evidence.append("API_GROUP_TOKEN_MATCH")
        reasons.append(
            f"declares {matched!r}, the API group of a CustomResourceDefinition "
            "or APIService in this cluster"
        )

    # A DaemonSet runs a copy on every node. That is what node-level infrastructure
    # does — CNI, kube-proxy, log forwarders, monitoring agents — and almost never
    # what a business application does. Free signal: already in the snapshot, and
    # it catches the API-consuming agents the API-extension rules miss.
    if workload.kind == "DaemonSet":
        evidence.append("IS_DAEMONSET")
        reasons.append("is a DaemonSet, so it runs node-level infrastructure")

    # A workload given its own ServiceAccount was given it for a reason: something
    # needs permissions against the Kubernetes API. That is *consuming* the API,
    # which is the actual upgrade risk, and it is the only signal here that reaches
    # the components the API-extension rules structurally miss — a controller that
    # owns no CRD and is not a DaemonSet.
    #
    # Suggestive rather than conclusive, so it ranks MEDIUM on its own: an
    # application using IRSA for S3 access also has a dedicated account. The
    # conclusive version would read the ClusterRoleBindings naming this account, at
    # the cost of one call per binding; see the note in `collector.py`.
    if workload.service_account_name:
        evidence.append("DEDICATED_SERVICE_ACCOUNT")
        reasons.append(
            f"runs as ServiceAccount {workload.service_account_name!r} rather than "
            "default, so it was given an identity to use the Kubernetes API"
        )

    namespace = workload.namespace or ""
    if namespace in STANDARD_SYSTEM_NAMESPACES:
        evidence.append("SYSTEM_NAMESPACE")
        reasons.append(f"runs in {namespace}")
    if namespace in overrides.platform_namespaces:
        evidence.append("OVERRIDE_PLATFORM_NAMESPACE")
        reasons.append(f"override: {namespace} declared a platform namespace")

    if evidence:
        return ClassificationResult(
            bucket_for(True),
            True,
            "; ".join(reasons),
            _priority_for(tuple(evidence)),
            tuple(evidence),
        )

    # Reached only by absence of evidence, so it is the one verdict incomplete
    # collection can get wrong — and the reason says so when that applies.
    incomplete = sorted(
        {
            gap.code
            for gap in snapshot.collection_gaps
            if gap.code in EVIDENCE_GAP_CODES
        }
    )
    reason = (
        "no cluster API in this snapshot matches anything it declares, but "
        "classification evidence was incompletely collected ("
        + ", ".join(incomplete)
        + "), so an API it extends may be missing"
        if incomplete
        else "extends no cluster API, is not an EKS-managed add-on, is not a "
        "DaemonSet, and does not run in a system namespace"
    )
    truncated = bool(incomplete)
    return ClassificationResult(
        Classification.CUSTOM_WORKLOAD,
        False,
        reason,
        # Still assessed. No platform signal only means it sorts last.
        AssessmentPriority.LOW,
        (),
        provisional=truncated,
    )


# Signals that identify infrastructure on their own. The rest are positional or
# circumstantial: running in `kube-system` is suggestive, but people do put other
# things there.
_STRONG_SIGNALS = frozenset(
    {
        "EKS_MANAGED_ADDON",
        "LABELLED_API_OWNER",
        "OWNS_ADMISSION_WEBHOOK",
        "API_GROUP_TOKEN_MATCH",
        "IS_DAEMONSET",
        "OVERRIDE_PLATFORM",
    }
)


def _priority_for(evidence: tuple[str, ...]) -> AssessmentPriority:
    """Rank by the strength of the evidence, never to exclude anything.

    >>> _priority_for(("EKS_MANAGED_ADDON",))
    <AssessmentPriority.HIGH: 'high'>
    >>> _priority_for(("SYSTEM_NAMESPACE",))
    <AssessmentPriority.MEDIUM: 'medium'>
    >>> _priority_for(())
    <AssessmentPriority.LOW: 'low'>
    """
    if set(evidence) & _STRONG_SIGNALS:
        return AssessmentPriority.HIGH
    if evidence:
        return AssessmentPriority.MEDIUM
    return AssessmentPriority.LOW
