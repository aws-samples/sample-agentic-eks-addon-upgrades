# 3. Supplementary cluster-scoped read access.
#
# WHY IT IS NEEDED
# AmazonEKSViewPolicy does not cover apiextensions.k8s.io or apiregistration.k8s.io,
# so CustomResourceDefinition and APIService reads return 403. Those are how
# operator-style components -- cert-manager, Kyverno, anything owning a CRD -- are
# detected and correlated to a workload. The only managed alternative,
# AmazonEKSAdminViewPolicy, grants get/list/watch on EVERY resource including
# Secrets, so it was rejected: this ClusterRole names specific resource types and
# nothing else.
#
# WHY IT IS OPTIONAL
# Applying it needs the cluster's Kubernetes API server to be reachable from where
# Terraform runs. `apply_supplementary_rbac` defaults to true; set it false when it
# is not reachable (see variables.tf).
#
# WHAT HAPPENS WHEN IT IS NOT APPLIED, AND WHY THAT IS ACCEPTABLE
# The scan still attempts the reads, they are denied, and the denial becomes a
# typed CLUSTER_SCOPED_READ_FORBIDDEN gap on the snapshot. The pipeline completes
# and reports honestly that CRD-derived evidence is unavailable and must not be
# treated as absent. That is a degraded answer, not a wrong one -- which is exactly
# what scan/ports.py's (data, error) convention exists to produce.
#
# When Terraform cannot reach the API server, the right path is to commit this manifest to
# whatever GitOps the cluster already runs, which leaves no drift. That is the one
# step this module cannot take for you, and it is called out rather than hidden.

locals {
  apply_rbac = var.apply_supplementary_rbac
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.target.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.target.certificate_authority[0].data)

  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", var.cluster_name, "--region", var.region]
  }
}

resource "kubernetes_cluster_role_v1" "cluster_scoped_read" {
  count = local.apply_rbac ? 1 : 0

  metadata {
    name = "${var.name_prefix}-cluster-scoped-read"

    labels = {
      "app.kubernetes.io/name"       = "eks-addon-upgrade-readiness"
      "app.kubernetes.io/component"  = "rbac"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  # Read-only verbs only. No write verb appears anywhere in this resource.
  #
  # API surface extension: which components add APIs to the cluster. Also feeds
  # removed-API analysis from spec.versions and status.storedVersions.
  rule {
    api_groups = ["apiextensions.k8s.io"]
    resources  = ["customresourcedefinitions"]
    verbs      = ["get", "list", "watch"]
  }

  # Aggregated API registration, for example metrics-server's
  # v1beta1.metrics.k8s.io.
  rule {
    api_groups = ["apiregistration.k8s.io"]
    resources  = ["apiservices"]
    verbs      = ["get", "list", "watch"]
  }

  # API consumption -- the causal signal. What actually breaks on a Kubernetes
  # upgrade is API *usage*, and a ClusterRole states exactly which apiGroups a
  # component consumes. This identifies components the other rules miss entirely:
  # external-dns, kube-state-metrics and cluster-autoscaler own no CRD, serve no
  # APIService, and are not DaemonSets -- yet all break when an API they use is
  # removed.
  #
  # Reading a ClusterRole does not confer the permissions it describes. A role that
  # says "delete everything" is, to this identity, a document about permissions
  # someone else holds.
  #
  # Disclosure this DOES accept: ClusterRoleBindings reveal which identities hold
  # which permissions. That is reconnaissance-useful and normal for posture
  # tooling, but it is a real widening and is called out rather than buried.
  rule {
    api_groups = ["rbac.authorization.k8s.io"]
    resources  = ["clusterroles", "clusterrolebindings"]
    verbs      = ["get", "list", "watch"]
  }

  # Admission webhooks: a strong operator signal, and an upgrade hazard in
  # themselves -- one whose backing service is unavailable can block writes
  # cluster-wide.
  rule {
    api_groups = ["admissionregistration.k8s.io"]
    resources  = ["validatingwebhookconfigurations", "mutatingwebhookconfigurations"]
    verbs      = ["get", "list", "watch"]
  }

  # Plugin registration: storage and ingress implementations.
  # StorageClass.provisioner names the CSI driver; IngressClass.spec.controller
  # names the ingress implementation.
  rule {
    api_groups = ["storage.k8s.io"]
    resources  = ["csidrivers", "storageclasses"]
    verbs      = ["get", "list", "watch"]
  }

  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["ingressclasses"]
    verbs      = ["get", "list", "watch"]
  }

  # Nothing here grants access to Secrets, ConfigMaps, Pod logs, Pod exec, or any
  # namespaced workload data beyond what AmazonEKSViewPolicy already allows. The
  # scanner additionally refuses to request Secrets in code (FORBIDDEN_KINDS in
  # scan/adapters/eks_mcp.py), so that refusal survives a future widening here.
}

resource "kubernetes_cluster_role_binding_v1" "cluster_scoped_read" {
  count = local.apply_rbac ? 1 : 0

  metadata {
    name = "${var.name_prefix}-cluster-scoped-read"

    labels = {
      "app.kubernetes.io/name"       = "eks-addon-upgrade-readiness"
      "app.kubernetes.io/component"  = "rbac"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.cluster_scoped_read[0].metadata[0].name
  }

  # Bound to the group, not to the IAM ARN. The access entry maps the principal
  # into this group, so the binding survives a principal rename and no ARN is
  # hardcoded in cluster state.
  subject {
    kind      = "Group"
    name      = var.kubernetes_group
    api_group = "rbac.authorization.k8s.io"
  }
}
