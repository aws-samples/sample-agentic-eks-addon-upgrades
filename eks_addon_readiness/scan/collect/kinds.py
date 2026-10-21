"""What the scan asks the cluster for, and the labels it asks by.

Every constant here is a *shape* of question, never the name of a component. The
scan layer has no knowledge of cert-manager, Istio, KEDA or anything else: it
enumerates kinds, reads provenance labels, and hands whatever it finds to Stage 2.
What differs per component is data in the fact store.
"""

from __future__ import annotations

# Workload kinds that carry Helm chart provenance labels. Namespaced, so readable
# under AmazonEKSViewPolicy.
#
# `CronJob` is included and bare `Job` is not. A CronJob is a durable component —
# a backup or reconciliation operator is often shipped as one. A Job is an
# instance: clusters accumulate hundreds of completed Jobs plus Helm install
# hooks, none of which is a component, and all of which would crowd out real
# workloads against the transport's list cap.
WORKLOAD_KINDS: tuple[tuple[str, str], ...] = (
    ("Deployment", "apps/v1"),
    ("DaemonSet", "apps/v1"),
    ("StatefulSet", "apps/v1"),
    ("CronJob", "batch/v1"),
)

# Cluster-scoped kinds that identify operator-style add-ons and the aggregated
# API surface. These are attempted even when access is expected to be denied:
# recording the denial as a typed gap is the point. Omitting the attempt would
# leave the snapshot silently incomplete, which is indistinguishable from a
# cluster that has no CRDs at all.
CLUSTER_SCOPED_KINDS: tuple[tuple[str, str], ...] = (
    ("CustomResourceDefinition", "apiextensions.k8s.io/v1"),
    ("APIService", "apiregistration.k8s.io/v1"),
    # Admission webhooks. Registering one is a strong operator signal — nothing
    # else has a reason to intercept API writes — and a webhook is itself an
    # upgrade hazard, because one whose backing service is unavailable can block
    # writes cluster-wide.
    ("ValidatingWebhookConfiguration", "admissionregistration.k8s.io/v1"),
    ("MutatingWebhookConfiguration", "admissionregistration.k8s.io/v1"),
)

# ClusterRole and ClusterRoleBinding are deliberately NOT collected, though the
# advisor's RBAC now permits reading them. `list_k8s_resources` returns only
# metadata, so establishing which ServiceAccount a binding grants to needs one
# `read_k8s_resource` per binding — 82 on the reference cluster, hundreds on a
# large one. `WorkloadSnapshot.service_account_name` gives most of the same signal
# for no extra call, and the ClusterRole *rules* matter for Stage 4's
# deprecated-API analysis rather than for ranking here.

# The label by which a project declares ownership of a cluster-scoped resource.
# Used as an existence selector, which is what makes ownership evidence complete:
# only labelled resources match, so the result cannot be crowded out by the
# hundreds of unlabelled CRDs that an unfiltered enumeration would return.
OWNERSHIP_LABEL = "app.kubernetes.io/name"

# Kind used to enumerate namespaces for sharding. Cluster-scoped.
NAMESPACE_KIND = ("Namespace", "v1")
