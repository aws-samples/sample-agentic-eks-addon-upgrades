variable "region" {
  description = "AWS region for every resource in this module."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = <<-EOT
    Prefix for every resource name. Must be distinct from any existing
    deployment's prefix, because that is what keeps a new deployment from
    colliding with an old one.
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
    Explicit name for the stage-document bucket. Leave null to derive
    "<name_prefix>-<account>-<region>-an", which is unique without embedding an account id
    in source. The bucket is created in the account-regional namespace, so an explicit name
    must end in that same `-<account>-<region>-an` suffix.
  EOT
  type        = string
  default     = null
}

variable "target_kubernetes_version" {
  description = <<-EOT
    Default target Kubernetes version for a readiness run. Overridable per
    execution on the Step Functions payload, so this is a default and not a
    constraint. 1.36 is the newest version the EKS add-on catalogue offers in
    us-east-1 as of 2026-09-12.
  EOT
  type        = string
  default     = "1.36"

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+$", var.target_kubernetes_version))
    error_message = "target_kubernetes_version must be major.minor, for example 1.36."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention for every log group in this module."
  type        = number
  default     = 30
}

variable "lambda_architecture" {
  description = "Lambda instruction set. arm64 is cheaper and is what the build script targets."
  type        = string
  default     = "arm64"
}

variable "max_resolve_iterations" {
  description = <<-EOT
    How many rounds the resolve loop may run in one execution. The queue shrinks only when a
    fact is written, so a component whose research is repeatedly held would re-queue every
    round; this is the bound that makes that terminate.
  EOT
  type        = number
  default     = 3

  validation {
    condition     = var.max_resolve_iterations >= 1
    error_message = "max_resolve_iterations must be at least 1."
  }
}

variable "research_function_arn" {
  description = <<-EOT
    ARN of Stage 6's research Lambda. Supplying it adds the research
    states: a Choice state on Stage 7's
    `research_required` routes queued gaps to research.

    IT DOES NOT CLOSE THE LOOP, AND THIS DESCRIPTION USED TO SAY IT DID. The return edge
    to `Gaps` exists in the definition and is UNREACHABLE: `Knowledge changed?` branches
    on `$.store_digest_after_research`, nothing sets that field, so its `IsPresent = false`
    branch matches on every execution and routes to `Present`. `Default -> Gaps` never
    fires. The loop closes ACROSS executions instead -- a person approves a candidate, the
    next run picks the fact up.

    SUPPLY BOTH ARNS OR NEITHER. This one alone emits neither `Resolve` nor `Research`,
    publishes no dispatch queue, and every run still SUCCEEDS -- see the note in
    workflow.tf's header.

    The loop re-enters at gap detection rather than patching a document, which is what
    keeps the digest chain intact across it: nothing rewrites a stage document, the
    stages are re-run and their digests move to reflect the new knowledge.

    THE ONE MODEL EDGE IN THE PIPELINE, and it is entered from a deterministic
    decision. Gap detection, dispatch and resume are all code; the research behind
    this ARN is fully agentic. Making the pipeline an agent that decides
    to research was considered and rejected, because a model would re-enter the verdict path.

    DEFAULTS TO NULL.
  EOT
  type        = string
  default     = null

  validation {
    condition = var.research_function_arn == null || can(
      regex("^arn:aws[a-z-]*:lambda:", var.research_function_arn)
    )
    error_message = "research_function_arn must be a Lambda function ARN, or null."
  }
}

variable "approval_function_arn" {
  description = <<-EOT
    ARN of the approval stage's Lambda: `module.research.approval_function_arn`, wired in the
    deploy root. Supplying it inserts an `Approval` state between `Research` and
    `Knowledge changed?`, and points `Research.Next` at it.

    IT IS THE ONLY STATE THAT STOPS. Its resource is
    `arn:aws:states:::lambda:invoke.waitForTaskToken`, so the execution pauses until the
    approval endpoint calls `SendTaskSuccess`, or until `approval_timeout_seconds` expires
    and FAILS the run.

    WHAT IT MAKES REACHABLE, which is the reason it exists. `Knowledge changed?` has been on
    the deployed definition for a long time and has never once taken its `Gaps` branch:
    research writes a CANDIDATE, the approval endpoint is the only writer of facts, so
    within one execution the store could not change and the return edge was dead. See the
    note on `research_function_arn` above, which describes that dead edge. Pausing between the Map and the Choice is what lets an approval move the store
    digest inside one execution, so the run that proposed a fact is the run that reports on
    it.

    DEFAULTS TO NULL, AND THE DEFAULT IS LOAD-BEARING. With it null, `Research` goes
    straight to `Knowledge changed?` exactly as it does today, the Choice keeps only its two
    pre-existing conditions, and an apply produces zero change to the deployed state
    machine. A deployment without it keeps the current behaviour: research proposes, the
    report shows the proposals, and the loop closes on the next run.
  EOT
  type        = string
  default     = null

  validation {
    condition = var.approval_function_arn == null || can(
      regex("^arn:aws[a-z-]*:lambda:", var.approval_function_arn)
    )
    error_message = "approval_function_arn must be a Lambda function ARN, or null to leave the state machine unchanged."
  }
}

variable "approval_timeout_seconds" {
  description = <<-EOT
    How long the `Approval` state waits for a person before the execution FAILS.

    FAILING IS THE POINT, not a fallback. The alternative -- carry on and report anyway --
    produces a report indistinguishable from one nobody was ever asked about, which is the
    worst outcome this pipeline can produce. A failed execution says plainly that a decision
    was needed and never came, and any fact already approved is durable, so the next run
    picks it up rather than re-researching it.

    TWO HOURS by default. It was 24h first, on the reasoning that a request arriving
    outside working hours should survive the night -- but this pipeline is operated
    interactively, and a wait that long mostly means a forgotten run sits pending until
    somebody notices. Two hours is comfortably longer than the minutes an approver actually
    takes, and short enough that a run nobody attended fails while the context is still
    fresh.

    Failing is cheap to recover from, which is what makes the shorter window safe: any fact
    already approved is durable, so the next run picks it up rather than re-researching it.
    Only the undecided proposals come round again.

    The token store's own TTL is 7 days and is NOT the approval deadline -- it exists only
    so abandoned rows do not accumulate. It is deliberately much longer than this, so the
    row outlives the execution that owns it.

    Unused when `approval_function_arn` is null, so changing it alone cannot alter a
    deployed definition.
  EOT
  type        = number
  default     = 7200

  validation {
    # Step Functions caps a task at one year. The lower bound guards against a value that
    # would fail every run before a person could plausibly read the email.
    condition     = var.approval_timeout_seconds >= 300 && var.approval_timeout_seconds <= 31536000
    error_message = "approval_timeout_seconds must be between 300 (5 minutes) and 31536000 (one year)."
  }
}

variable "upgrade_approval_timeout_seconds" {
  description = <<-EOT
    How long the `Upgrade approval` state waits for a person before the execution FAILS.

    ITS OWN VARIABLE, AND THAT IS THE POINT. It duplicates `approval_timeout_seconds`'s
    default and its band, and it must stay a separate value: the two waits ask different
    questions of different people. The fact wait asks whether a claim about a component is
    true; this one asks whether we may change one cluster. Reusing the variable would tie
    them together, so tuning one for a slow approver would silently retune the other, and
    the first sign of that would be a run failing at the wait nobody adjusted.

    TWO HOURS, ON THE SAME REASONING AS THE FACT WAIT and with the same evidence behind it.
    That variable records why it was cut from 24h: this pipeline is operated interactively,
    and a wait that long mostly means a forgotten run sits pending until somebody notices.
    Two hours is comfortably longer than the minutes an approver takes and short enough that
    an unattended run fails while the context is fresh.

    THE RECOVERY PROPERTY THAT REASONING RESTS ON HOLDS HERE TOO, which is what makes the
    shorter window safe rather than merely shorter. A `StepDecision` is written to S3 as each
    click lands, so every decision made before the timeout is already durable -- failing
    loses the resume, never the answers. And an undecided step is offered again on the next
    run rather than being taken as a refusal, exactly as an undecided fact is. So a longer
    window buys nothing this stage does not already have.

    FAILING IS THE POINT, not a fallback. A run that quietly finished without asking would
    be indistinguishable from one nobody was asked about, which is the worst outcome
    available here -- the page would exist, unread, while the run reported success.

    The upgrade token store's own TTL is 7 days and is NOT this deadline. It exists only so
    abandoned rows do not accumulate, and is deliberately much longer than this so the row
    outlives the execution that owns it.

    Unused when `upgrade_approval_function_arn` is null, so changing it alone cannot alter a
    deployed definition.
  EOT
  type        = number
  default     = 7200

  validation {
    # The same band as the fact wait. Step Functions caps a task at one year; the lower
    # bound guards a value that would fail every run before a person could read the email.
    condition = (
      var.upgrade_approval_timeout_seconds >= 300
      && var.upgrade_approval_timeout_seconds <= 31536000
    )
    error_message = "upgrade_approval_timeout_seconds must be between 300 (5 minutes) and 31536000 (one year)."
  }
}

variable "approval_email_from" {
  description = <<-EOT
    The From address the upgrade approval request is mailed as. Must be a verified SES
    identity; infra/terraform/modules/research declares that verification for the fact approval, and this
    module mails as the same address rather than adding a second one.

    DECLARED HERE AS WELL AS IN infra/terraform/modules/research. The deploy root passes the one
    `approval_email_from` value to both modules, so they cannot drift.

    EMPTY DISABLES THE MAIL, and that is a working configuration. The stage still renders its
    page and still pauses the run; the link can be reached from the console. Raising here
    would make a missing address fail a pipeline that is otherwise fine, which is exactly
    what the fact stage learned by failing a run holding a good page and five valid grants.
  EOT
  type        = string
  default     = ""
}

variable "approval_email_to" {
  description = <<-EOT
    Who receives the upgrade approval request, and therefore who can approve a cluster change.

    THIS LIST IS AN AUTHORISATION BOUNDARY, not a notification preference, and it is worth
    being blunt about it here for a reason the fact stage does not have: these capabilities
    change a running cluster. The forms on the page are single-use, but whoever reads this
    mailbox can approve an add-on move on the cluster the run is about. The recorded approver
    is the FIRST address, because a one-time capability attests that somebody opened that
    mailbox and not which human it was -- which is why `StepDecision.approver_authority`
    records `EMAIL_LINK_TOKEN` and never claims an AWS principal.

    Empty disables the mail on the same terms as the From address above.
  EOT
  type        = list(string)
  default     = []
}

variable "brief_plan_function_arn" {
  description = <<-EOT
    The upgrade brief's PLAN end, from infra/terraform/modules/brief's `brief_plan_function_arn` output.

    THE BRIEF IS ADVISORY AND ITS THREE ARNs ARE ALL-OR-NOTHING. `local.brief_enabled` requires
    all three, because a definition naming a state whose ARN is null is refused whole by Step
    Functions -- the trap `Plan.next` already documents for the research pair.

    Supplying none is a working configuration: the run goes straight from `Knowledge changed?` to
    `Present`, exactly as it does today.
  EOT
  type        = string
  default     = null
}

variable "brief_component_function_arn" {
  description = <<-EOT
    The brief's per-component end, from infra/terraform/modules/brief's `brief_component_function_arn` output.
    Invoked once per component move by the `Brief research` Map.
  EOT
  type        = string
  default     = null
}

variable "brief_assemble_function_arn" {
  description = <<-EOT
    The brief's ASSEMBLE end, from infra/terraform/modules/brief's `brief_assemble_function_arn` output. Reads
    every brief the run resolved and writes the one advisory document under `briefs/`.
  EOT
  type        = string
  default     = null
}

variable "github_token_secret_arn" {
  description = <<-EOT
    A Secrets Manager secret holding a GitHub token, granted to the freshness sweep.

    WHY THE SWEEP NEEDS ONE. The sweep asks GitHub "what is the newest release in this
    repository?" once per distinct source. Unauthenticated that allowance is 60 requests
    per hour per IP, and a Lambda shares its egress address across invocations, so a sweep
    over a fleet can exhaust it and then report an unresolved source as "not moved" --
    indistinguishable from a source that genuinely has not moved. A token raises the
    allowance to 5000 per hour.

    THE ARN, NOT THE TOKEN. A value here would be readable by anyone holding
    `lambda:GetFunctionConfiguration`, a far wider set than `secretsmanager:GetSecretValue`
    on one secret, which is all the sweep role is granted. `stages/freshness.py` resolves it
    at invocation through the same resolver the brief's search tool uses, so rotating the
    secret needs no redeploy. The secret itself is created by hand and is deliberately not
    managed here: Terraform owning a credential's contents would put it in the state file.

    Leave null and the sweep runs unauthenticated, exactly as it did before this existed.
    The same ARN is supplied to `infra/terraform/modules/brief`, which grants it to its search tool.
  EOT
  type        = string
  default     = null

  validation {
    condition = var.github_token_secret_arn == null || can(
      regex("^arn:aws[a-z-]*:secretsmanager:", var.github_token_secret_arn)
    )
    error_message = "github_token_secret_arn must be a Secrets Manager secret ARN, or null."
  }
}

# ---------------------------------------------------------------------------
# The release gate
# ---------------------------------------------------------------------------

# DELIBERATELY ABSENT: `release_approver_arns`. There is no variable naming who may authorise a
# release, and there must not be one again.
#
# IT USED TO LIVE HERE, holding `role/Admin` and the approver role, concatenated into the
# `aws:approve` step's `Approvers` list at plan time. It is now RESOLVED PER RUN from the target
# cluster's own access entries -- see `stages/release_prep.py` and the `Release approvers` state --
# and passed to the document as a StringList parameter at start time.
#
# WHY THE VARIABLE WAS THE BUG RATHER THAN A MERELY DULL ALTERNATIVE. A hand-kept list drifts in
# both directions and neither direction announces itself. Somebody who loses cluster admin keeps
# authorising releases until a human remembers this file; somebody who gains cluster admin --
# who AWS already considers able to change the cluster by hand -- is refused by the gate. Both
# readings of the authorisation boundary were then true at once, and the tfvars copy was the one
# the release object recorded.
#
# WHAT REPLACED IT IS NOT A DIFFERENT DEFAULT, IT IS A REFUSAL. If the cluster's access entries
# name no cluster administrator, the resolver raises and the run fails at `Release rejected`.
# There is no fallback list here or anywhere, which is why deleting this variable was safe to do
# in one change: nothing reads it, and nothing may be added that does.
#
# THE ONE FACT WORTH KEEPING FROM ITS OLD DOCUMENTATION, because the gate still rests on it: a
# role ARN in `Approvers` covers the sessions assumed from it. A caller of `SendAutomationSignal`
# is never literally a role ARN -- an AWS request identity is a user ARN or an
# `arn:aws:sts::<account>:assumed-role/<Role>/<Session>` session ARN -- so
# `arn:aws:iam::123456789012:role/Admin` coming back from an access entry is what lets
# `arn:aws:sts::123456789012:assumed-role/Admin/approver-session` approve. SSM does that matching
# inside `aws:approve`; nothing in this repository compares a principal to a list, which is why
# `stages/approve.py`'s `endswith("/" + name)` defect has nowhere to live in this gate.

variable "release_gate_cluster_names" {
  description = <<-EOT
    WHICH CLUSTERS' ACCESS ENTRIES THE APPROVER RESOLVER MAY READ. An IAM blast radius, and
    deliberately NOT an approver list.

    THE DISTINCTION IS THE WHOLE JUSTIFICATION FOR THIS VARIABLE EXISTING at all, so soon after
    `release_approver_arns` was deleted for being a hand-kept list. That one named WHO MAY
    APPROVE: a stale entry was a real authorisation the cluster no longer agreed with, granted
    silently. This one names WHICH CLUSTERS MAY BE READ, and a missing entry is an
    `AccessDeniedException` on `eks:ListAccessEntries` that the resolver propagates -- the run
    fails at `Release rejected` naming the call. Both kinds of drift are possible; only one of
    them grants anything.

    WHY IT IS NEEDED AT ALL: this module is fleet-wide and holds no cluster name. The cluster a
    run is about comes from the execution input, and per-cluster IAM otherwise lives in
    `infra/terraform/cluster-access`. Without these names the `ReadWhoMayAuthoriseARelease` statement
    would have to say `cluster/*`, which is a resolver that can be pointed at any cluster in the
    account.

    NO DEFAULT. `["*"]` and `[]` are both traps -- the first restores the wildcard this exists to
    avoid, the second makes every release fail at the resolve with an IAM error rather than a
    legible one. Naming the clusters this deployment actually runs against is the only correct
    value, and a validation below refuses `*` outright.

    The deploy root wires this from the single required `target_cluster`, so there is one name
    per deployment. Onboarding a second cluster adds a line here, and the failure mode for
    forgetting is a failed release, not an unauthorised one.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.release_gate_cluster_names) >= 1
    error_message = "release_gate_cluster_names must name at least one cluster, or the release gate can never resolve an approver."
  }

  validation {
    # `*` here would be `cluster/*` in IAM by the back door, which is the one shape this variable
    # exists to make impossible.
    condition     = length([for n in var.release_gate_cluster_names : n if can(regex("[*?]", n))]) == 0
    error_message = "release_gate_cluster_names must name clusters literally; a wildcard would let the resolver read any cluster's access entries."
  }
}

variable "release_approval_timeout_seconds" {
  description = <<-EOT
    How long the release gate waits for a named AWS principal before the run FAILS.

    ITS OWN VARIABLE, AND NOT A THIRD COPY OF THE SAME NUMBER. Read
    `upgrade_approval_timeout_seconds`'s docstring above: it is emphatic that the two waits ask
    different questions of different people, so tuning one for a slow approver must not
    silently retune the other. This is a THIRD question again. `Approval` asks whether a claim
    about a component is true. `Upgrade approval` asks whether we may change this cluster. This
    asks whether a principal AWS has AUTHENTICATED will stand behind that answer -- a different
    person may hold that authority, and a different amount of patience is reasonable for it.

    TWO HOURS, ON THE SAME EVIDENCE AS THE OTHER TWO: this pipeline is operated interactively,
    and a wait much longer than that mostly means a forgotten run sits pending until somebody
    notices.

    THE RECOVERY PROPERTY IS DIFFERENT HERE, AND WEAKER, AND SAYING SO IS THE POINT. The
    upgrade wait can afford to fail because every decision made before the timeout is already
    durable in S3. This wait records NOTHING before it completes -- half an authorisation is not
    a thing. What survives a timeout is the earlier stage's `StepDecision` objects, already
    written, so the loss is the release and never the answers, and the next run offers the same
    steps again.

    A NARROWER BAND THAN ITS TWO SIBLINGS, WHICH IS NOT AN INCONSISTENCY. Those two bound a
    Step Functions task, which AWS caps at one year. This bounds an `aws:approve` step, and the
    action reference says the maximum is 30 days (2592000 seconds). Accepting 31536000 here
    would move a legible plan-time failure to an apply-time one: SSM refuses the value when the
    document is created.

    CHANGING THIS UPDATES THE SSM DOCUMENT, NOT THE STATE MACHINE DEFINITION. The deadline is
    enforced by SSM inside the runbook rather than by a Step Functions `TimeoutSeconds`, which
    is also why there is exactly one of it: the state machine polls the automation's status and
    has no deadline of its own to keep in step.
  EOT
  type        = number
  default     = 7200

  validation {
    condition = (
      var.release_approval_timeout_seconds >= 300
      && var.release_approval_timeout_seconds <= 2592000
    )
    error_message = "release_approval_timeout_seconds must be between 300 (5 minutes) and 2592000 (30 days, the aws:approve maximum)."
  }
}
