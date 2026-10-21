variable "region" {
  description = "Region for the Harness, the proxy and the bucket read. Passed explicitly everywhere."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for this component's resources. Match the platform module's."
  type        = string
  default     = "eks-addon-upgrades"
}

variable "document_bucket_name" {
  description = <<-EOT
    The pipeline's document bucket. Null derives `<name_prefix>-<account>-<region>-an`, which is what
    platform creates.

    The proxy reads ONE prefix of it -- `upgrade-diagnosis/` -- and holds no other action on it.
  EOT
  type        = string
  default     = null
}

variable "manage_agentcore" {
  description = <<-EOT
    Whether this module creates the Harness and its role.

    False is for a checkout running against a Harness owned elsewhere; pass `upgrade_harness_arn` then.
    With neither, `coalesce` errors at PLAN time rather than producing a null that would reach an IAM
    policy as `"Resource": null` and an environment variable as an empty string -- a proxy that starts,
    calls nothing, and reports "UPGRADE_HARNESS_ARN is unset" for every component in the run.
  EOT
  type        = bool
  default     = true
}

variable "upgrade_harness_arn" {
  description = "An externally-managed Harness to use instead of creating one. Null when this module owns it."
  type        = string
  default     = null
}

variable "upgrade_harness_name" {
  description = "Name of the Harness. Distinct from the research and brief agents': three agents, three instructions, three names."
  type        = string
  default     = "readiness_upgrade"
}

variable "upgrade_model_id" {
  description = <<-EOT
    Inference profile or model id the upgrade agent reasons with.

    A CROSS-REGION PROFILE by default, which is why the Harness policy's first resource carries a
    region wildcard: the call is authorized against the foundation-model ARN in whichever region the
    profile routes to.

    IT MUST BE A MODEL THAT ACCEPTS `output_config.effort`, AND THE FIRST VALUE HERE WAS NOT. This
    defaulted to `us.anthropic.claude-sonnet-4-5-20250929-v1:0` while `harness.tf` passes
    `additionalModelRequestFields.output_config.effort` -- a field Sonnet 5 accepts and 4.5 rejects:

        ValidationException: The model returned the following errors:
        output_config.effort: Extra inputs are not permitted

    The passthrough was copied from `infra/terraform/modules/brief/harness.tf`, which pairs it with
    `us.anthropic.claude-sonnet-5`; only the model id was left behind. `harness.tf` had already
    written down that a passthrough is unverified until an invocation proves it, and the invocation
    disproved this one -- but not until IAM stopped failing first and let the call reach the model.

    SO THE MODEL MOVED RATHER THAN THE PASSTHROUGH BEING DROPPED. `upgrade_effort` defaults to `high`
    deliberately: this agent decides whether returning a component to its previous version would hide
    something a person must see, and the cost of getting that wrong is a cluster nobody looked at.
    Keeping the effort control and matching brief's verified model preserves that; dropping
    `additional_params` would have silently lowered the reasoning on the more consequential of the two
    agents.
  EOT
  type        = string
  default     = "us.anthropic.claude-sonnet-5"
}

variable "upgrade_max_tokens" {
  description = <<-EOT
    The model's per-turn output ceiling.

    A PLATFORM CEILING WITH ITS SOURCE, not a budget chosen here. 128000 is the maximum number of
    output tokens `anthropic.claude-sonnet-5` accepts -- the same model this component's
    `upgrade_model_id` names, so the same ceiling as `research_max_tokens` and `brief_max_tokens`
    applies for the same reason and with the same citation:

      ValidationException: The maximum tokens you requested exceeds the model limit of 128000.
      Try again with a maximum tokens value that is lower than 128000.

    probed read-only on `converse_stream` -- the `api_format` this variable actually feeds -- and
    bisected rather than assumed: 128000 accepted, 128001 refused. See `research/extract.py`'s
    header for the Messages-API wording of the same limit.

    IT WAS 4096, AND THAT NUMBER TRUNCATED A WORKING READING. The old description read "a verdict
    plus its observations, not an essay", which is the right size for a verdict and the wrong thing
    to size. Once the harness runs its own tool loop, a turn is reasoning at `high` effort plus the
    arguments for every tool call the model decides to make. Measured on this live harness against
    this cluster:

      runtimeClientError: Model stopped generating due to maximum token limit. The partial message
      has been added to the conversation history.

    The reading itself was fine -- twenty-two MCP calls, all of them useful -- and it was cut off
    mid-turn, which the lane recorded as `unavailable`: the same word an IAM denial produces, and
    the fourth distinct fault to hide behind it.

    SO THE CAP IS LIFTED TO THE PLATFORM'S, NOT TO A NUMBER THAT LOOKED BIG ENOUGH. The first fix
    written here was 32768, which is exactly the mistake the 4096 was: a figure with no source that
    happens to clear the one case measured. An unhealthy component reads more than a healthy one,
    and there is no reading of this system in which "32768 is where the model should be interrupted"
    is a true sentence. The provider marks the attribute optional, so the cap could be dropped
    instead; it is pinned rather than removed because the service default when the field is absent
    was never measured here, and an unmeasured default is not a ceiling.

    IT DOES NOT MAKE THE ANSWER LONGER. What bounds the verdict is `UpgradeTriage`, which refuses
    anything that is not the object it describes, and a prompt that asks for that object and nothing
    else. What bounds the LOOP is `upgrade_max_iterations` and `upgrade_harness_timeout_seconds`.
    This number only stops the model being interrupted while it works.
  EOT
  type        = number
  default     = 128000

  # ENFORCED HERE, NOT MERELY DESCRIBED, matching research and brief. Without this an
  # `.tfvars` file could set 200000 and the refusal would arrive from Bedrock at a
  # checkpoint -- inside the lane's health window, with the write grant open -- rather than at plan
  # time.
  validation {
    condition     = var.upgrade_max_tokens >= 1024 && var.upgrade_max_tokens <= 128000
    error_message = "upgrade_max_tokens must be between 1024 and 128000: 128000 is the model limit converse_stream reports, and a higher value is refused by Bedrock at request time rather than here."
  }
}

variable "upgrade_effort" {
  description = <<-EOT
    Reasoning effort, via the `additionalModelRequestFields` passthrough.

    HIGHER THAN THE BRIEF AGENT'S on purpose: the brief is read as advice, and this decides whether
    returning a component to its previous version would hide something a person must see. Treat the
    passthrough as unverified until an invocation proves it -- one the service silently ignores looks
    identical to one it honours.
  EOT
  type        = string
  default     = "high"
}

variable "upgrade_max_iterations" {
  description = <<-EOT
    Iteration budget for one triage.

    RAISED FROM 4 WHEN THE AGENT GOT TOOLS, AND FROM 12 WHEN IT GOT THE ONES THAT WORK. The first
    value came with the sentence "with no tools there is no fetch loop", which stopped being true.
    The second, 12, was set while the read path was ten inline-function tools that never actually
    ran -- so it was never tested against a reading that happened.

    12 WAS NOT ENOUGH, MEASURED. The first successful reading through the Gateway made TWENTY-TWO MCP
    calls on a single-pod HEALTHY component and ended:

        messageStop: {"stopReason": "max_iterations_exceeded"}

    It had read everything it needed and was cut off before it could answer, so the proxy got prose
    and no verdict -- indistinguishable, in the lane's report, from a model that read nothing. An
    UNHEALTHY component reads more than a healthy one, and a cluster with twenty pods per workload
    reads more than this one, so 40 is the ceiling for the case this agent exists for rather than the
    easy case.

    STILL BOUNDED, because the lane's write grant is open the whole time. `upgrade_harness_timeout_seconds`
    is the limit that should actually bind -- iterations bound a runaway, seconds bound the lane.
    There is no longer a proxy-side twin: `MAX_TOOL_ROUNDS` is deleted along with the loop it counted.
  EOT
  type        = number
  default     = 40
}

variable "upgrade_harness_timeout_seconds" {
  description = <<-EOT
    Ceiling on one Harness invocation, and the limit that should bind BEFORE any client timeout.

    RAISED FROM 300 WHEN THE AGENT GOT THE MCP READ PATH. A HEALTHY verdict on a single-pod component
    measured 158 seconds and twenty tool calls against the live cluster; an unhealthy component reads
    more and a large cluster reads slower.

    IT MUST STAY BELOW THE PROXY'S READ TIMEOUT (870) so the SERVICE stops the loop and says why. If
    the client gives up first, the proxy has nothing to report but "the call did not come back", which
    the lane records as `unavailable` -- the same word that IAM failures, a model field mismatch and a
    parsing bug all produced during this stage's history, and the reason each took a run to tell apart.
  EOT
  type        = number
  default     = 840
}

variable "upgrade_truncation_messages_count" {
  description = "Sliding-window size. A triage is one prompt and at most one re-ask, so four messages covers it with room."
  type        = number
  default     = 8
}

variable "upgrade_target_cluster" {
  description = <<-EOT
    The ONE cluster the agent may read through MCP. Same value as platform's variable of this name.

    NO DEFAULT AND NO WILDCARD. `list_k8s_resources` takes `cluster_name` as an argument, so without an
    IAM resource scope the agent could read any cluster MCP can reach -- a wider blast radius than the
    the prompt alone could bound. This is what puts the cluster in the authorizer instead.
  EOT
  type        = string
  default     = ""
}

variable "upgrade_build_role_arn" {
  description = <<-EOT
    ARN of the upgrade lane's CodeBuild service role, from platform's `upgrade_build_role_arn` output.
    It is the ONLY principal allowed to invoke the proxy, by resource policy.

    Null omits the resource policy statement entirely, which leaves the proxy unreachable -- correct
    for a plan before platform exists, and a working deployment must set it.
  EOT
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "Retention for the proxy's log group."
  type        = number
  default     = 30
}

variable "log_kms_key_arn" {
  description = <<-EOT
    ARN of the customer-managed KMS key that encrypts this module's CloudWatch log group:
    `module.platform.log_kms_key_arn`, wired in the deploy root. One key encrypts every log
    group across all four modules; the Logs service principal is the only principal in its
    key policy, because CloudWatch Logs is what encrypts the data.

    Null leaves the log group on the default (AWS-owned) CloudWatch Logs encryption.
  EOT
  type        = string
  default     = null
}
