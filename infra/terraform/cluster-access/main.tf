terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
  }

  # No backend block: state is local. Use one Terraform workspace per cluster
  # (`terraform workspace new <cluster>`) so each cluster keeps its own state.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = var.tags
  }
}

data "aws_eks_cluster" "target" { name = var.cluster_name }
data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  role_name = "${var.name_prefix}-read-${var.cluster_name}"

  # The deploy root's state machine and bucket, derived the same way the platform module names
  # them, so `start_a_run` prints a complete command with nothing to fill in.
  account_id        = data.aws_caller_identity.current.account_id
  state_machine_arn = "arn:${data.aws_partition.current.partition}:states:${var.region}:${local.account_id}:stateMachine:${var.name_prefix}"
  document_bucket   = "${var.name_prefix}-${local.account_id}-${var.region}-an"
}

# ---------------------------------------------------------------------------
# 1. This cluster's read-only IAM role.
#
# One role per cluster, not one for the fleet. The scan stage assumes the role for
# the cluster it was asked to scan, so the blast radius of the platform's cluster
# reach is exactly the set of clusters onboarded -- and revoking one cluster is
# deleting one role, not editing a policy shared with the others.
#
# Least privilege is the point: this role must be INCAPABLE of writing, so Stage
# 1's read-only guarantee is enforced by IAM rather than by application code.
# AmazonEKSMCPReadOnlyAccess grants eks-mcp:InvokeMcp and CallReadOnlyTool, and
# does NOT grant CallPrivilegedTool. That absence is the write guarantee.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    sid     = "PlatformScanStageAssumes"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [var.platform_scan_role_arn]
    }
  }

}

resource "aws_iam_role" "cluster_read" {
  name        = local.role_name
  description = "Read-only identity for EKS add-on upgrade readiness on ${var.cluster_name}. No write permissions by design."

  assume_role_policy   = data.aws_iam_policy_document.assume.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "mcp_read_only" {
  role       = aws_iam_role.cluster_read.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSMCPReadOnlyAccess"
}

# describe-addon-versions is an EKS control-plane call with no MCP tool, so Stage 1
# makes it directly. It is a global catalogue read: no cluster, no ARNs, no account
# ids in the response.
data "aws_iam_policy_document" "addon_catalogue" {
  statement {
    sid    = "ReadTheGlobalAddonCatalogue"
    effect = "Allow"
    actions = [
      "eks:DescribeAddonVersions",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ReadThisClusterOnly"
    effect = "Allow"
    actions = [
      "eks:DescribeCluster",
      "eks:ListAddons",
      "eks:DescribeAddon",
      "eks:ListInsights",
      "eks:DescribeInsight",
      "eks:AccessKubernetesApi",
    ]
    # Scoped to this cluster. The role cannot describe a cluster it was not
    # onboarded for, even though it could reach the API.
    resources = [
      data.aws_eks_cluster.target.arn,
      "${data.aws_eks_cluster.target.arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "addon_catalogue" {
  name   = "read-cluster-and-catalogue"
  role   = aws_iam_role.cluster_read.id
  policy = data.aws_iam_policy_document.addon_catalogue.json
}

# ---------------------------------------------------------------------------
# 2. The EKS access entry.
#
# IAM permission (eks:AccessKubernetesApi) is necessary but NOT sufficient:
# Kubernetes RBAC must also authorise the principal, which is what an access entry
# does. Skipping it produces Kubernetes RBAC denials that look like IAM failures.
#
# AmazonEKSViewPolicy maps to the built-in Kubernetes `view` ClusterRole, which
# EXCLUDES Secrets. That is deliberate: Helm release state lives in a Secret, and
# this system reads chart provenance labels instead. The alternatives were all
# rejected -- AmazonEKSAdminViewPolicy grants get/list/watch on every resource
# including Secrets, and the Edit/Admin/ClusterAdmin policies grant write.
# ---------------------------------------------------------------------------
resource "aws_eks_access_entry" "cluster_read" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.cluster_read.arn
  type          = "STANDARD"

  # Maps the principal into the Kubernetes group the supplementary ClusterRole
  # binds, so no IAM ARN appears in any manifest and the binding survives a
  # principal rename.
  kubernetes_groups = [var.kubernetes_group]
}

resource "aws_eks_access_policy_association" "view" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.cluster_read.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSViewPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.cluster_read]
}
