variable "region" {
  description = "AWS region for every resource in this module. Must match the platform stack's."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = <<-EOT
    Prefix for every resource name. Must match the platform module's prefix, since this
    module refers to that module's bucket by derived name, and to infra/terraform/modules/research's
    fetch-source tool the same way.
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
    The stage-document bucket, which this module READS AND WRITES but does not manage. Leave
    null to derive "<name_prefix>-<account>-<region>-an", matching how the platform module
    derives it. No account id appears in source either way.
  EOT
  type        = string
  default     = null
}

variable "fact_store_table_name" {
  description = <<-EOT
    The fact table, which this module READS and never writes. Leave null to derive
    "<name_prefix>-facts".

    READ ONLY, and that is the whole of this module's relationship with the knowledge base.
    The brief resolves a component's repository, documentation hosts, chart name and recorded
    releases from its fact; it stores no fact and proposes no candidate. The role below grants
    GetItem, Query and Scan and no writer action at all, so "the brief cannot change what the
    pipeline believes" is a property of the deployed grant.
  EOT
  type        = string
  default     = null
}

variable "fact_mark_table_name" {
  description = <<-EOT
    The marks table, read so a withheld fact does not answer the brief either. Leave null to
    derive "<name_prefix>-fact-marks".

    Read for the same reason infra/terraform/modules/research reads it: the brief uses the MARKED store, so
    a fact a human revoked or the sweep withdrew resolves to nothing here as well. Reading
    around a mark for the brief's convenience would make this the one place a revoked fact
    still speaks.
  EOT
  type        = string
  default     = null
}

variable "fetch_source_function_name" {
  description = <<-EOT
    The source-fetch tool, OWNED BY infra/terraform/modules/research and reused here unchanged. Leave null
    to derive "<name_prefix>-tool-fetch-source".

    REUSED RATHER THAN DUPLICATED, deliberately. That Lambda's first act is to write the bytes
    it fetched to `research-evidence/`, and that recording is the only reason any quote in
    either agent's output can be checked. A second copy here would be a second implementation
    of the one property both agents depend on, and the two would drift.

    The cost is a cross-component reference by DERIVED NAME rather than by resource reference,
    which is the trade this project has already made for the marks table and the cluster read
    roles: a name computed in two places can drift if the convention changes, while a required
    cross-component variable would make this module unappliable whenever the other had not run.
  EOT
  type        = string
  default     = null
}

variable "brief_harness_name" {
  description = <<-EOT
    The Harness's name. ITS OWN, not the research Harness's, and that is the load-bearing
    decision this component exists to deploy.

    The research agent is instructed to transcribe and not to reason past what a page says,
    because the defect it was rebuilt around was a model turning a tested window into a
    supported ceiling. The brief agent is instructed to read a CHANGELOG and judge what an
    operator would care about. One prompt cannot say both, and the discipline that would erode
    is the one every stored fact rests on -- so: separate Harness, separate ARN, separate
    prompt, separate ruleset digest, so tuning this cannot move that.
  EOT
  type        = string
  default     = "readiness_brief"

  validation {
    # UNDERSCORES ONLY, NO HYPHENS, and the bound is 40 rather than 48. Both come from the
    # service's own rejection, which a plan surfaced: "Valid characters are a-z, A-Z, 0-9, _
    # (underscore). The name must begin with a letter and can have up to 40 characters." This
    # regex first allowed hyphens and the default was `readiness-brief`, so the validation passed
    # and the API refused it -- a validation looser than the service it guards is worse than none,
    # because it moves the failure from plan to apply. Mirrors `readiness_research`.
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,39}$", var.brief_harness_name))
    error_message = "brief_harness_name must start with a letter and contain only letters, digits and underscores, up to 40 characters."
  }
}

variable "brief_model_id" {
  description = <<-EOT
    The Bedrock model the brief agent reads with. A PARAMETER, deliberately: which model reads a
    CHANGELOG is a deployment decision.

    The default is the cross-region INFERENCE PROFILE for Claude Sonnet 5, not the bare model id.
    The `us.` prefix is what routes the call across regions; the bare id is rejected for this
    model on this endpoint, which is a thing only a live invocation reveals.
  EOT
  type        = string
  default     = "us.anthropic.claude-sonnet-5"
}

variable "brief_harness_arn" {
  description = <<-EOT
    An existing brief Harness to invoke, for a checkout running with `manage_agentcore = false`.
    Leave null and this module creates its own.

    With `manage_agentcore = false` AND this null, the plan FAILS rather than producing a null
    that would reach an IAM policy and an environment variable -- see `local.effective_harness_arn`
    in harness.tf for why a plan-time error beats that runtime symptom.
  EOT
  type        = string
  default     = null

  validation {
    condition = var.brief_harness_arn == null || can(
      regex("^arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:harness/", var.brief_harness_arn)
    )
    error_message = "brief_harness_arn must be a bedrock-agentcore harness ARN."
  }
}

variable "brief_gateway_arn" {
  description = <<-EOT
    An existing Gateway to attach the brief Harness to, for a checkout running with
    `manage_agentcore = false`. Leave null and this module creates its own.
  EOT
  type        = string
  default     = null

  validation {
    condition = var.brief_gateway_arn == null || can(
      regex("^arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:gateway/", var.brief_gateway_arn)
    )
    error_message = "brief_gateway_arn must be a bedrock-agentcore gateway ARN."
  }
}

variable "manage_agentcore" {
  description = <<-EOT
    Whether this module owns the AgentCore resources -- the brief Gateway, its three targets,
    the Harness, and the two execution roles.

    TRUE IS THE INTENDED STATE, and this variable exists to make the other state expressible
    rather than to invite it. Setting it false does NOT delete anything on a fresh state; on a
    state that already holds them it is a DESTROY, the same warning infra/terraform/modules/research carries.
  EOT
  type        = bool
  default     = true
}

variable "github_token_secret_arn" {
  description = <<-EOT
    A Secrets Manager secret holding a GitHub token, granted to the search_issues tool.

    NOT AN OPTIMISATION, and this is the one variable whose absence changes what the brief can
    say. GitHub's SEARCH endpoints allow 10 requests per minute per IP unauthenticated and 30
    with a token -- a much tighter window than the 60-per-hour core limit, and an agent that
    issues several searches for one component can exhaust it inside a minute. Every exhausted
    search becomes a NOT_LOOKED known-issues section, which is honest and useless.

    Leave null and the tool runs unauthenticated. It still works; it just reports "could not
    look" more often, and the document records `authenticated: false` on every lookup so the
    reason is visible rather than deduced.
  EOT
  type        = string
  default     = null
}

variable "brief_max_tokens" {
  description = <<-EOT
    Output-token ceiling for one brief.

    THE PLATFORM MAXIMUM IS 128000 for this model on Bedrock, named in the service's own
    rejection text and bisected (128000 accepted, 128001 refused). The
    default here is well under it because a brief is five sections of bullets rather than a
    long transcription, so this is headroom rather than a target.
  EOT
  type        = number
  default     = 32000

  validation {
    condition     = var.brief_max_tokens >= 1024 && var.brief_max_tokens <= 128000
    error_message = "brief_max_tokens must be between 1024 and 128000, the ceiling this model accepts."
  }
}

variable "brief_effort" {
  description = <<-EOT
    Reasoning effort for the brief agent: "low", "medium" or "high".

    MEDIUM BY DEFAULT, AND THAT IS THE OPPOSITE OF THE RESEARCH STAGE ON PURPOSE. Research runs
    low because its task is faithful transcription -- copy the numbers a table states -- and
    reasoning past what the page says is the exact defect it was rebuilt to fix. This agent's task
    is the other thing: read a CHANGELOG and judge which entries an operator would care about,
    which IS reasoning past the page. Low would be actively wrong here.

    Not raised to high by default because the loop is already several minutes over a dozen release
    pages, and the judgement this needs is filtering rather than deep derivation. Raise it if the
    relevance filter proves weak.

    HOW IT REACHES THE MODEL, AND WHAT IS NOT PROVEN. The provider models no effort field on
    `bedrock_model_config`, so it goes through `additional_params`, which is a JSON passthrough.
    The key is the one this project probed live against this model and recorded as ACCEPTED in
    `research/schema.py`: `output_config={"effort": ...}`. What is NOT established is that a
    Harness on `converse_stream` forwards that passthrough the same way the messages API accepts
    it. A wrong key here fails in one of two ways -- a loud 400 at invoke time, or silent
    ignoring. The second is the dangerous one and it is the same shape as the environment-variable
    defect this component already shipped once, so treat effort as UNVERIFIED until an invocation
    either rejects it or measurably changes behaviour.
  EOT
  type        = string
  default     = "medium"

  validation {
    condition     = contains(["low", "medium", "high"], var.brief_effort)
    error_message = "brief_effort must be low, medium or high."
  }
}

variable "brief_max_iterations" {
  description = <<-EOT
    How many tool-calling turns one brief gets.

    Higher than the research agent needs for a single matrix, because finding release notes is
    genuinely a search: a project may take several fetches to locate its CHANGELOG, then one
    per release, then an issue search. A LIVENESS GUARD, not a research budget -- it exists so
    a non-converging loop fails with an artifact rather than at the wall clock.
  EOT
  type        = number
  default     = 60
}

variable "brief_harness_timeout_seconds" {
  description = <<-EOT
    Wall clock for one component's brief inside the Harness.

    MUST STAY UNDER THE PROXY'S READ TIMEOUT, which is 890s, itself under Lambda's 900s
    ceiling. Ordered that way so the Harness gives up before the proxy does and the proxy
    reports a real answer instead of a timeout -- the failure the research proxy hit live when
    botocore's 60s default aborted calls that were working.
  EOT
  type        = number
  default     = 840

  validation {
    condition     = var.brief_harness_timeout_seconds > 0 && var.brief_harness_timeout_seconds <= 880
    error_message = "brief_harness_timeout_seconds must be 1-880, leaving headroom under the proxy's 890s read timeout."
  }
}

variable "brief_truncation_messages_count" {
  description = <<-EOT
    Sliding-window size for the Harness conversation.

    Sliding window rather than summarisation, and that is a correctness choice: release-note
    bodies ARE the evidence, and a summarised window would leave the agent quoting a paraphrase
    of a page instead of the page -- which the byte-for-byte quote check would then reject,
    correctly and unhelpfully.
  EOT
  type        = number
  default     = 40
}

variable "brief_reuse_max_age_days" {
  description = <<-EOT
    How many days a stored brief may be served again before it is researched from scratch.

    A brief is stored under the UPGRADE it describes -- component, from-version, to-version -- so
    any later run making the same move serves it instead of paying for the agent. This is how long
    that stays acceptable.

    SEVEN DAYS by default, the user's window. What goes stale is exactly one section: new issues get
    filed and old ones get closed. Breaking changes in an already-published release are history and
    do not move.

    THIS IS A CLOCK EXPIRY, WHICH THE FACT STORE DELIBERATELY REFUSES -- and the difference is not
    an inconsistency. `research/freshness.py` rejects a TTL for facts because every fact is
    human-approved, so a TTL would mean re-approving every fact forever, and because it would make
    the same snapshot produce different verdicts on different days. Neither applies here: nobody
    approves a brief, and a brief is already declared `reproducible: false` because its
    known-issues section reads a tracker that changes daily. A window does not introduce a property
    it was promising not to have.

    ZERO DISABLES REUSE, which is the honest way to say "always research fresh" and needs no second
    flag.
  EOT
  type        = number
  default     = 7

  validation {
    condition     = var.brief_reuse_max_age_days >= 0 && var.brief_reuse_max_age_days <= 365
    error_message = "brief_reuse_max_age_days must be between 0 (never reuse) and 365."
  }
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

    Null leaves the log groups on the default (AWS-owned) CloudWatch Logs encryption.
  EOT
  type        = string
  default     = null
}
