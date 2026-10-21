variable "cluster_name" {
  description = "The EKS cluster to onboard. One apply per cluster."
  type        = string
}

variable "region" {
  description = "Region the cluster is in."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for the IAM role and the Kubernetes RBAC objects. Match the platform module's prefix."
  type        = string
  default     = "eks-addon-upgrades"
}

variable "platform_scan_role_arn" {
  description = <<-EOT
    ARN of the platform's scan-stage execution role, from the platform module's
    `scan_role_arn` output. This is the only principal that may assume the
    cluster read role.
  EOT
  type        = string
}

variable "kubernetes_group" {
  description = <<-EOT
    Kubernetes group the access entry maps the principal into, and which the
    supplementary ClusterRoleBinding binds. Binding to a group rather than an IAM
    ARN keeps ARNs out of cluster state.
  EOT
  type        = string
  default     = "eks-addon-readiness-readers"
}

variable "apply_supplementary_rbac" {
  description = <<-EOT
    Whether Terraform should create the supplementary ClusterRole and binding
    that permit CRD and APIService reads.

    TRUE BY DEFAULT. AmazonEKSViewPolicy does not cover these cluster-scoped types,
    and the scan needs them to find add-ons, so the read role should always have them.

    Applying it needs the cluster's Kubernetes API server to be reachable from where
    Terraform runs, or the apply fails. If it is not, set false and deliver the
    ClusterRole through the cluster's own GitOps. Without it the scan still
    completes and records a typed CLUSTER_SCOPED_READ_FORBIDDEN gap.
  EOT
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags applied to every AWS resource that supports them."
  type        = map(string)
  default = {
    Project   = "eks-addon-upgrades"
    ManagedBy = "terraform"
  }
}
