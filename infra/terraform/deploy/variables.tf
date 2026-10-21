# ---------------------------------------------------------------------------
# Where and what
# ---------------------------------------------------------------------------
variable "target_cluster" {
  description = <<-EOT
    The EKS cluster this deployment assesses. Required. It must be onboarded with
    `infra/terraform/cluster-access` before a run can scan it.

    The release gate reads THIS cluster's access entries to decide who may authorise a
    release: every principal with cluster-admin on it is an approver, and nobody else is.
    With `upgrade_stage_enabled = true` it is also the ONE cluster the upgrade lanes may
    write to.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[0-9A-Za-z][A-Za-z0-9_-]{0,99}$", var.target_cluster))
    error_message = "target_cluster must be an EKS cluster name (letters, digits, - and _), not a wildcard or ARN."
  }
}

variable "region" {
  description = "AWS region to deploy into. Every resource in this sample lives in this one region."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name. Change it to run two copies in one account."
  type        = string
  default     = "eks-addon-upgrades"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.name_prefix))
    error_message = "name_prefix must be 3-24 characters: lowercase letters, digits and hyphens, starting with a letter."
  }
}

variable "tags" {
  description = "Tags applied to every resource through the provider's default_tags."
  type        = map(string)
  default = {
    Project = "eks-addon-upgrades"
  }
}

variable "target_kubernetes_version" {
  description = "Default target Kubernetes version for a run. Overridable per execution in the Step Functions input."
  type        = string
  default     = "1.36"
}

# ---------------------------------------------------------------------------
# People: who is asked, and who may decide
# ---------------------------------------------------------------------------
variable "approval_email_from" {
  description = <<-EOT
    The address approval requests are mailed from. Terraform creates an SES email identity for
    it, and SES mails that address a verification link you must click before any mail is sent.
    Empty disables mail: runs still pause for approval and can be resumed from the console.
  EOT
  type        = string
  default     = ""
}

variable "approval_email_to" {
  description = <<-EOT
    Who receives approval requests. THIS IS AN AUTHORISATION BOUNDARY, not a notification
    preference: the mailed links are single-use approval capabilities. While SES is in sandbox
    mode every recipient must also be a verified identity.
  EOT
  type        = list(string)
  default     = []
}

variable "release_approval_timeout_seconds" {
  description = "How long the release gate waits for an authorisation before failing the run."
  type        = number
  default     = 7200
}

variable "github_token_secret_arn" {
  description = <<-EOT
    Optional ARN of a Secrets Manager secret holding a GitHub token. Used by the freshness sweep
    and the upgrade brief's issue search. Without it both run unauthenticated against GitHub's
    much lower rate limit and report "could not look" where they could have answered.
  EOT
  type        = string
  default     = null
}

# ---------------------------------------------------------------------------
# The upgrade stage: the part that changes a cluster
# ---------------------------------------------------------------------------
variable "upgrade_stage_enabled" {
  description = <<-EOT
    Whether a run may proceed past the readiness report into upgrade approval, the release
    gate and the upgrade lanes. FALSE BY DEFAULT: the run ends on a readiness report and
    nothing can write to a cluster. When true, the lanes may write to `target_cluster`.
  EOT
  type        = bool
  default     = false
}


variable "upgrade_lane_groups" {
  description = "Kubernetes groups a lane's temporary access entry may name."
  type        = list(string)
  default     = ["upgrade-agent"]
}

variable "upgrade_health_deadline_seconds" {
  description = "How long a lane waits for an upgraded component to become healthy."
  type        = number
  default     = 900
}

variable "upgrade_health_checkpoints" {
  description = "Seconds after the upgrade at which a lane checks health."
  type        = list(number)
  default     = [180, 420, 660, 900]
}

variable "upgrade_revalidation_checkpoints" {
  description = "Seconds after a healthy verdict at which a lane re-checks."
  type        = list(number)
  default     = [30, 60]
}

variable "upgrade_build_timeout_minutes" {
  description = "CodeBuild timeout for one upgrade lane."
  type        = number
  default     = 60
}

variable "upgrade_helm_version" {
  description = "Helm release the lane downloads. Pinned together with its SHA-256."
  type        = string
  default     = "v3.16.4"
}

variable "upgrade_helm_sha256" {
  description = "SHA-256 of the Helm linux-amd64 tarball for `upgrade_helm_version`."
  type        = string
  default     = "fc307327959aa38ed8f9f7e66d45492bb022a66c3e5da6063958254b9767d179"
}

variable "upgrade_kubectl_version" {
  description = "kubectl release the lane downloads. Pinned together with its SHA-256."
  type        = string
  default     = "v1.35.0"
}

variable "upgrade_kubectl_sha256" {
  description = "SHA-256 of the kubectl linux-amd64 binary for `upgrade_kubectl_version`."
  type        = string
  default     = "a2e984a18a0c063279d692533031c1eff93a262afcc0afdc517375432d060989"
}
