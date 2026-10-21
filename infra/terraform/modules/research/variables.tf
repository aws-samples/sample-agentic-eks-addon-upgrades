variable "region" {
  description = "AWS region for every resource in this module."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = <<-EOT
    Prefix for every resource name. Must match the platform module's prefix, since
    this module refers to that module's bucket and fact table by derived name.
  EOT
  type        = string
  default     = "eks-addon-upgrades"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,40}$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric with hyphens, 3-41 chars, starting with a letter."
  }
}

variable "document_bucket_name" {
  description = <<-EOT
    The stage-document bucket, which this module READS AND WRITES but does not
    manage. Leave null to derive "<name_prefix>-<account>-<region>-an", matching how
    the platform module derives it. No account id appears in source either way.
  EOT
  type        = string
  default     = null
}

variable "fact_store_table_name" {
  description = <<-EOT
    The fact table, which this module WRITES but does not manage. Leave null to
    derive "<name_prefix>-facts".
  EOT
  type        = string
  default     = null
}

variable "research_model_id" {
  description = <<-EOT
    The Bedrock model Stage 6 uses to read sources. A PARAMETER, deliberately: which
    model transcribes a compatibility table is a deployment decision, and the code
    reads it from the environment rather than hardcoding one.

    The default is the cross-region INFERENCE PROFILE for Claude Sonnet 5, not the
    bare foundation-model id. Bedrock lists `anthropic.claude-sonnet-5` as
    INFERENCE_PROFILE-only, and invoking the bare id returns 400 asking for a profile
    id or ARN. `us.anthropic.claude-sonnet-5` is that profile for the same model.

    Two notes for anyone changing this. The code sets no `temperature`, because this
    model removed that parameter and returns 400 if it is sent -- the SDK omits it
    from the method signature entirely, so a model that requires temperature would
    need a code change, not just a new value here. And the code uses structured
    outputs, which need an SDK new enough to accept `output_config`.
  EOT
  type        = string
  default     = "us.anthropic.claude-sonnet-5"
}

variable "research_effort" {
  description = <<-EOT
    Reasoning effort for the extraction call: "low", "medium" or "high".

    Low by default, and that is a correctness choice rather than a cost one. Stage 6
    transcribes what a source states; reasoning past what the page says is precisely
    the failure that produced the defect this stage was rebuilt to fix. Raise it only
    with a reason.
  EOT
  type        = string
  default     = "low"

  validation {
    condition     = contains(["low", "medium", "high"], var.research_effort)
    error_message = "research_effort must be low, medium or high."
  }
}




# `fact_ttl_hours`, `staleness_mode` and `sweep_schedule_expression` were removed with the
# sweep and its schedule. All three configured a mechanism this project does not have:
#
#   fact_ttl_hours             how old a fact could be before it expired
#   staleness_mode             "upstream_drift" or "ttl", validated to accept both
#   sweep_schedule_expression  how often the whole-store sweep ran
#
# Staleness means the upstream source moved, decided per run by a stage of platform's state
# machine. No code path read any of them -- `research/freshness.py` has been drift-only since
# it was written, so the "ttl" mode was a dial that did nothing and a reader who set it would
# have got drift behaviour anyway. That was reported as F8 of the sweep code review and is
# closed here, because the move took their last consumers with it.

variable "denied_approver_role_names" {
  description = <<-EOT
    Roles that must NEVER be able to approve a fact -- for example the tool roles of an
    agent that reads approval pages.
    An explicit Deny naming them goes into the approver role's trust policy, and an
    explicit deny is final in IAM evaluation -- so it holds even if one of these roles is
    later granted sts:AssumeRole on the approver role by someone else.

    Empty by default: IAM rejects a trust policy that names a role which does not exist, so
    list only roles that are already deployed. Adding one is a variable change, not a code
    change. Names rather than ARNs so no account id appears
    here; the ARNs are composed from the caller identity.
  EOT
  type        = list(string)
  default     = []
}

variable "log_retention_days" {
  description = "CloudWatch log retention for this module's functions."
  type        = number
  default     = 30
}

variable "log_kms_key_arn" {
  description = <<-EOT
    ARN of the customer-managed KMS key that encrypts this module's CloudWatch log groups:
    `module.platform.log_kms_key_arn`, wired in the deploy root. One key encrypts every log
    group across all four modules; the Logs service principal is the only principal in its
    key policy, because CloudWatch Logs is what encrypts the data.

    Null leaves the log groups on the default (AWS-owned) CloudWatch Logs encryption, which is
    a working configuration for a checkout that does not want a CMK.
  EOT
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default = {
    project = "eks-addon-upgrades"
    stage   = "6-research"
  }
}

variable "research_gateway_arn" {
  description = <<-EOT
    An EXTERNALLY MANAGED research Gateway to use instead of the one this module creates.
    Leave null -- the normal case -- and `agentcore.tf` creates the Gateway, its target and
    its role, and every reference resolves to that.

    It exists for a consumer running with `manage_agentcore = false`, pointing at a Gateway
    some other checkout owns.

    HISTORY WORTH KEEPING: this used to be a REQUIRED reference with a hardcoded ARN default,
    because the Gateway was created by the agentcore CDK stack rather than by Terraform. That
    split gave Stage 6 two state systems, and they drifted -- CloudFormation went on recording
    a Gateway target that had been deleted, so the Harness sat READY with no tools and no plan
    anywhere reported it. See the header of agentcore.tf.
  EOT
  type        = string
  default     = null

  validation {
    condition = var.research_gateway_arn == null || can(
      regex("^arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:gateway/", var.research_gateway_arn)
    )
    error_message = "research_gateway_arn must be an AgentCore gateway ARN, or null."
  }
}

variable "manage_agentcore" {
  description = <<-EOT
    Whether this module owns the AgentCore resources -- the research Gateway, its
    source-fetch target, the Harness, and the two execution roles.

    TRUE IS THE INTENDED STATE, and this variable exists to make the other state expressible
    rather than to invite it: a checkout that must not touch the Harness can set it false and
    supply `research_gateway_arn`.

    Setting it false does NOT delete anything on a fresh state; it means this module plans
    nothing for those resources. On a state that already holds them, false is a DESTROY --
    read the plan.
  EOT
  type        = bool
  default     = true
}

variable "research_harness_name" {
  description = <<-EOT
    The Harness's name, which also names the runtime it manages: AgentCore derives
    `harness_<name>` from it.

    RENAMING REPLACES THE HARNESS and issues a new ARN, so anything holding the old one --
    the Stage 6 invoker's IAM grant most of all -- has to move with it.
  EOT
  type        = string
  default     = "readiness_research"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}$", var.research_harness_name))
    error_message = "research_harness_name must start with a letter and contain only letters, digits and underscores."
  }
}

variable "research_max_tokens" {
  description = <<-EOT
    The model's per-response output ceiling.

    A PLATFORM CEILING WITH ITS SOURCE, not a budget chosen here. 128000 is the maximum number
    of output tokens `anthropic.claude-sonnet-5` accepts. The citation is the service's own
    rejection text, probed read-only on `converse_stream` -- the `api_format` this variable
    actually feeds, so the citation and the call shape match:

      ValidationException: The maximum tokens you requested exceeds the model limit of 128000.
      Try again with a maximum tokens value that is lower than 128000.

    and the boundary was bisected rather than assumed -- 128000 is accepted, 128001 is refused.
    (The message's "lower than 128000" is loose; 128000 itself is accepted, which is why the
    boundary was probed rather than read off the wording.) The Messages API on the same model
    reports the identical ceiling in different words -- "max_tokens: 999999 > 128000, which is
    the maximum allowed number of output tokens for anthropic.claude-sonnet-5" -- and the
    `claude-api` skill and the SDK's own 128_000 denominator agree.

    This REPLACES the earlier 64000, which was described here as needing a decision because no
    published maximum had been found to cite. One probe found it, and 64000 turned out to be
    exactly half of it -- so the old text was wrong twice over: the number was a choice, and a
    citable ceiling did exist. The provider marks this attribute optional, so the cap could
    have been dropped instead; it is pinned rather than removed because the service default
    when the field is absent was not measured, and an unmeasured default is not a ceiling.

    Still a variable, for the original reason: so it stays visible and movable rather than
    becoming a constant nobody re-reads. The deploy root does not override it, so this default
    is what reaches the deployed Harness.
  EOT
  type        = number
  default     = 128000

  # The ceiling is ENFORCED here, not merely described. Without this, a `.tfvars` file in this
  # directory could set 200000 and the refusal would arrive from Bedrock at the far end of a
  # research run rather than at plan time. The Python side of this same cap is clamped with
  # `min(CEILING_OUTPUT_TOKENS, ...)` for exactly this reason; leaving the Terraform side
  # unbounded would document a ceiling on one path and not hold it on the other.
  validation {
    condition     = var.research_max_tokens >= 1024 && var.research_max_tokens <= 128000
    error_message = "research_max_tokens must be between 1024 and 128000: 128000 is the model limit converse_stream reports, and a higher value is refused by Bedrock at request time rather than here."
  }
}

variable "research_max_iterations" {
  description = <<-EOT
    How many model turns one component gets. High enough not to shape the answer: a component
    may publish its matrix across several documents in several repositories, and a loop that
    stops early turns a findable fact into a gap.

    A LIVENESS GUARD, NOT A RESEARCH BUDGET -- it exists so a model that is not converging
    fails with an artifact instead of at the wall clock.
  EOT
  type        = number
  default     = 100
}

variable "research_harness_timeout_seconds" {
  description = <<-EOT
    Wall-clock ceiling for one component's research INSIDE THE HARNESS.

    The Harness is not bound by Lambda's 900s limit, so it gets the longer
    window; whatever invokes it must be able to outlast this or the ceiling is the
    invoker's, not this one.
  EOT
  type        = number
  default     = 3600
}

variable "research_truncation_messages_count" {
  description = <<-EOT
    Sliding-window size for the Harness's conversation. Large, because the window holds
    fetched page bodies and dropping one mid-run loses the bytes a quote was going to cite.
  EOT
  type        = number
  default     = 1000
}

variable "upgrade_decision_watcher_arn" {
  description = <<-EOT
    ARN of the upgrade click watcher: `module.platform.upgrade_decision_watcher_arn`, wired in
    the deploy root. Supplying it adds an `upgrade-clicks/` filter to the bucket
    notification this module owns.

    WHY THIS MODULE OWNS A NOTIFICATION FOR ANOTHER MODULE'S FUNCTION.
    `aws_s3_bucket_notification` manages a bucket's ENTIRE notification configuration, so a
    second resource for the same bucket does not add a filter -- it REPLACES the existing one,
    silently, taking the fact approval's auto-resume with it. The two prefixes therefore have
    to be declared in one resource whether or not their functions live together, and this
    module already holds that resource.

    DEFAULTS TO NULL, AND THE DEFAULT IS LOAD-BEARING HERE in a way it is not elsewhere: with
    it null this module plans NO change to the live notification, so a branch that does not
    know about the upgrade stage cannot un-wire the fact approval by applying. Supplying it is
    the one edit that touches a resource the fact path depends on.

    APPLY infra/terraform/modules/platform FIRST. S3 checks that it may invoke each named function when the
    configuration is written, and the permission for this one is granted beside the function.
  EOT
  type        = string
  default     = null

  validation {
    condition = var.upgrade_decision_watcher_arn == null || can(
      regex("^arn:aws[a-z-]*:lambda:", var.upgrade_decision_watcher_arn)
    )
    error_message = "upgrade_decision_watcher_arn must be a Lambda function ARN, or null to leave the notification unchanged."
  }
}

variable "approval_email_from" {
  description = <<-EOT
    The From address the approval request is mailed as. Must be a verified SES identity;
    `approval_stage.tf` declares that verification.

    Empty DISABLES the mail, and that is a working configuration rather than a broken one:
    the approval stage still renders its page and still pauses the run, and the run can be
    resumed from the console or with the signed curl command on the page. Raising an error
    here would make the absence of an email address fail a pipeline that is otherwise fine.
  EOT
  type        = string
  default     = ""
}

variable "approval_email_to" {
  description = <<-EOT
    Who receives the approval request, and therefore who can approve.

    THIS LIST IS AN AUTHORISATION BOUNDARY, not a notification preference, and it is worth
    being blunt about that. The links in the mail are single-use capabilities; whoever reads
    the mailbox can approve a fact. The recorded approver is the FIRST address here, because
    a one-time link attests that somebody opened that mailbox and not which human it was.

    Keep it short, and prefer a mailbox one person reads over a shared or forwarded one.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for address in var.approval_email_to : can(regex("^[^@[:space:]]+@[^@[:space:]]+$", address))
    ])
    error_message = "every approval_email_to entry must be an email address."
  }
}

variable "approve_link_role_arns" {
  description = <<-EOT
    Extra IAM role ARNs permitted to invoke the approval link endpoint, on top of the
    approval stage's own role.

    WHY THIS IS A VARIABLE AND NOT A HARDCODED ROLE. The endpoint is `AWS_IAM`, so a request
    to it must be signed by a principal the resource policy names. The approval stage is
    named unconditionally because it mints and signs the links; anything else is
    deployment-specific -- a human's own role, a break-glass role, an operator group -- and
    a published copy of this project must not carry somebody else's role ARN.

    NAMING A ROLE HERE DOES NOT GRANT AN APPROVAL. The endpoint still refuses any request
    without a valid single-use grant, and the grants are minted by the stage and mailed away.
    So this list decides who may REACH the endpoint, not who may approve -- possession of a
    token is still required, and that is the control that scopes the action to one candidate.

    Role ARNs, not assumed-role session ARNs: `arn:aws:iam::<acct>:role/Admin`, never
    `arn:aws:sts::<acct>:assumed-role/Admin/<session>`. A resource policy names the role;
    the session is what the caller presents at request time.
  EOT
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for arn in var.approve_link_role_arns : can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:role/", arn))
    ])
    error_message = "every approve_link_role_arns entry must be an IAM role ARN (arn:aws:iam::<account>:role/<name>), not an assumed-role session ARN."
  }
}
