# The upgrade brief: an agent, its two tools, and the three functions that drive it.
#
# WHY THIS IS ITS OWN MODULE AND NOT A BLOCK IN platform/
#
# This module creates the brief's functions and outputs their ARNs; nothing here reaches the
# pipeline. The Map state that calls them lives in infra/terraform/modules/platform, which receives
# the ARNs from the deploy root (`infra/terraform/deploy/main.tf`). The brief is advisory: with its ARNs unset the
# run goes straight on, so nothing here can gate a run.
#
# WHAT THIS MODULE OWNS
#
#   the brief Harness       its own, with its own prompt. NOT the research Harness -- see
#                           `brief_harness_name` for why sharing one would erode the discipline
#                           every stored fact depends on.
#   its Gateway             three targets: fetch_source (reused from infra/terraform/modules/research),
#                           search_issues (created here), register_repository (reused).
#   search_issues           the tool that finds issues with a long trail, and records both the
#                           issues and a receipt naming its own outcome.
#   three functions         brief-plan, brief-component, brief-assemble.
#
# WHAT IT DELIBERATELY DOES NOT OWN
#
#   fetch_source            infra/terraform/modules/research's, reused unchanged. Its first act is to write
#                           the fetched bytes down, and that recording is the only reason a
#                           quote in either agent's output can be checked. A second copy would
#                           be a second implementation of the property both agents rest on.
#   any fact write          this module reads the fact store and never writes it. No role here
#                           carries PutItem on it, so "the brief cannot change what the
#                           pipeline believes" is a property of the deployed grants.
#
# THE ONE PROPERTY THE WIRING MUST PRESERVE, stated here because this is where the functions are
# declared: the brief is ADVISORY and cannot fail a run. Every handler returns a recorded
# absence rather than raising -- an unreachable project, a rate limit, a model that would not
# answer. The Map that calls them must carry a catch to the report anyway, because a Lambda that
# cannot be invoked at all is not something the handler can report on. See infra/terraform/modules/platform's
# change.

locals {
  account_id  = data.aws_caller_identity.current.account_id
  name_prefix = var.name_prefix

  # Derived the same way the platform module derives them, so no account id appears in source
  # and no cross-component variable is required for a module that has not run yet.
  bucket_name = coalesce(
    var.document_bucket_name,
    "${var.name_prefix}-${local.account_id}-${var.region}-an",
  )
  bucket_arn = "arn:aws:s3:::${local.bucket_name}"

  fact_table = coalesce(var.fact_store_table_name, "${var.name_prefix}-facts")
  mark_table = coalesce(var.fact_mark_table_name, "${var.name_prefix}-fact-marks")

  fact_table_arn = "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.fact_table}"
  mark_table_arn = "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.mark_table}"

  # infra/terraform/modules/research's fetch tool, addressed by derived name. See the variable for the trade.
  fetch_source_function = coalesce(
    var.fetch_source_function_name,
    "${var.name_prefix}-tool-fetch-source",
  )
  fetch_source_arn = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.fetch_source_function}"

  # The deployment package. One zip for all four brief handlers -- see entrypoints.brief_harness.py for
  # why they share a package and not a role.
  package = "${path.module}/../../../../build/brief_harness.zip"

  # The agent's instruction, read from the file rather than restated here, so the deployed prompt
  # and the reviewed one are the same bytes. It is GENERATED from `brief/prompt.py`, which is in
  # the brief's ruleset digest, and `scripts/build-packages.sh` fails the build when it is stale
  # -- so a prompt cannot be deployed that nobody reviewed.
  #
  # A `.md`, NOT a harness manifest, and that is not a style choice: `agents/*/harness.json` is
  # gitignored (`.gitignore:64`), so a manifest read with `file()` would be missing from a fresh
  # clone and the plan would fail for a reason invisible in the repository. Every other field such
  # a manifest would carry is a variable in this module already.
  system_prompt = file("${path.module}/../../../../agents/readiness_brief/system-prompt.md")

  tool_schemas = {
    for name in ["fetch_source", "search_issues", "register_repository"] :
    name => jsondecode(file("${path.module}/../../../../tools/brief/schemas/${name}.json"))[0]
  }

  # The three orchestration functions. `search_issues` is not here: it is a TOOL, invoked by the
  # Gateway rather than by the pipeline, and giving it a pipeline-shaped role would grant it the
  # bucket reads it must not have.
  functions = {
    "brief-plan" = {
      handler     = "entrypoints.brief_harness.stage_brief_plan"
      timeout     = 120
      memory      = 512
      description = "Reads the plan, emits one brief work item per component move. Advisory; cannot fail a run."
    }
    "brief-component" = {
      handler = "entrypoints.brief_harness.stage_brief_component"
      # Lambda's own maximum. One invocation is one component's whole brief inside the Harness:
      # several release-note fetches, an issue search, and the reasoning over all of it. 900s is
      # the platform ceiling, not a budget chosen to shape the answer.
      timeout     = 900
      memory      = 1024
      description = "One component's brief through the brief Harness, verified against recorded bytes."
    }
    "brief-assemble" = {
      handler     = "entrypoints.brief_harness.stage_brief_assemble"
      timeout     = 300
      memory      = 1024
      description = "Assembles the run's one advisory brief document from the per-component partials."
    }
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# The three orchestration functions
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "function" {
  for_each = local.functions

  name              = "/aws/lambda/${local.name_prefix}-${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_iam_role" "function" {
  for_each = local.functions

  name               = "${local.name_prefix}-${each.key}"
  description        = each.value.description
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# `brief-plan` reads the plan and the fact store, and writes nothing.
data "aws_iam_policy_document" "brief_plan" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.function["brief-plan"].arn}:*"]
  }

  # The plan document and the Stage 2 inventory, and nothing else in the bucket. Note the narrow
  # prefixes: this function cannot read a snapshot, a ledger, an assessment or a candidate.
  #
  # THE INVENTORY IS READ FOR ONE FIELD: how each component was installed. An EKS managed add-on
  # is not briefed at all, because AWS publishes and qualifies those versions itself, and the
  # install method is the only durable place that distinction is recorded -- a `PlanStep` carries
  # versions and ordering, not provenance.
  statement {
    sid    = "ReadThePlanAndTheInventory"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket_arn}/plans/*", "${local.bucket_arn}/inventories/*"]
  }

  # READ ONLY on both tables. The brief resolves a repository and a chart name from a fact; it
  # stores nothing. No writer action appears here or in any role in this module.
  statement {
    sid       = "ReadFactsAndMarks"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [local.fact_table_arn, local.mark_table_arn]
  }

  # LIST, AND DELIBERATELY NOT GET. This function decides whether a brief already exists for an
  # upgrade, and the KEY carries everything that decision needs -- the component, the version pair,
  # the ruleset digest and the date. So it never opens one. `brief-assemble` holds the GetObject; the
  # split means a reuse decision cannot be influenced by the CONTENT of a brief, only by its
  # existence and its age.
  statement {
    sid       = "ListStoredBriefs"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["briefs/components/*"]
    }
  }
}

# `brief-component` invokes the Harness, reads back the evidence the tools recorded, and writes
# one partial. It does NOT read the plan: the step reaches it on the payload from `brief-plan`.
data "aws_iam_policy_document" "brief_component" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.function["brief-component"].arn}:*"]
  }

  # THE BRIEF HARNESS AND ONLY THIS ONE. Scoped to the ARN rather than to `harness/*`, so this
  # function cannot invoke the research agent -- which would be a way to run the transcription
  # prompt with the brief's inputs and get an answer nobody designed.
  # ONE `InvokeHarness` API CALL AUTHORIZES AGAINST TWO ACTIONS, and both are required.
  # infra/terraform/modules/research learned this from two consecutive live 403s and wrote it down; this module
  # granted only the obvious one anyway and earned the first of those 403s verbatim:
  #
  #   not authorized to perform: bedrock-agentcore:InvokeAgentRuntime on resource:
  #   arn:aws:bedrock-agentcore:...:harness/readiness_brief-EsHW7rgf1X
  #
  # Note that the RESOURCE is the harness ARN for both, even though a harness-managed runtime
  # refuses the `InvokeAgentRuntime` API outright -- the action name and the callable API do not
  # line up here, and the runtime ARN is the wrong resource.
  #
  # THE ONE HARNESS, not `harness/*`. A wildcard would let this function drive the research agent
  # too, which is the separation this whole component exists to make.
  statement {
    sid    = "InvokeBriefHarness"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:InvokeHarness",
      "bedrock-agentcore:InvokeAgentRuntime",
    ]
    resources = [local.effective_harness_arn]
  }

  # Read the recorded bodies back, so the quote check runs against bytes that were persisted
  # before the agent saw them. Read and LIST only -- this function records nothing itself; the
  # tools do.
  statement {
    sid    = "ReadRecordedEvidence"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket_arn}/research-evidence/*"]
  }

  statement {
    sid       = "ListRecordedEvidence"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["research-evidence/*"]
    }
  }

  # Write the one brief this invocation produced, under the upgrade it describes so a later run can
  # serve it. Scoped to `briefs/components/*`: this function cannot write the assembled run document
  # and cannot overwrite another component's brief prefix by accident.
  statement {
    sid       = "StoreOneComponentBrief"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${local.bucket_arn}/briefs/components/*"]
  }
}

# `brief-assemble` reads the partials and writes the one document.
data "aws_iam_policy_document" "brief_assemble" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.function["brief-assemble"].arn}:*"]
  }

  statement {
    sid    = "ReadThePlan"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket_arn}/plans/*"]
  }

  # Read every brief the run resolved -- reused and freshly stored alike -- and write the assembled
  # document. NO ListBucket: the uris arrive on the payload from `brief-plan` and from the Map, so
  # this function follows what actually happened rather than searching for what ought to be there.
  statement {
    sid    = "ReadComponentBriefsAndWriteTheRunDocument"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${local.bucket_arn}/briefs/*"]
  }

  statement {
    sid       = "ReadFactsAndMarks"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [local.fact_table_arn, local.mark_table_arn]
  }
}

resource "aws_iam_role_policy" "function" {
  for_each = local.functions

  name = "${local.name_prefix}-${each.key}"
  role = aws_iam_role.function[each.key].id
  policy = {
    "brief-plan"      = data.aws_iam_policy_document.brief_plan.json
    "brief-component" = data.aws_iam_policy_document.brief_component.json
    "brief-assemble"  = data.aws_iam_policy_document.brief_assemble.json
  }[each.key]
}

resource "aws_lambda_function" "function" {
  for_each = local.functions

  function_name = "${local.name_prefix}-${each.key}"
  description   = each.value.description
  role          = aws_iam_role.function[each.key].arn
  handler       = each.value.handler
  runtime       = "python3.12"
  architectures = ["arm64"]
  timeout       = each.value.timeout
  memory_size   = each.value.memory

  # ONE ZIP FOR ALL FOUR HANDLERS, and it is the one that vendors boto3: `InvokeHarness` is
  # newer than the Lambda runtime's bundled copy, and a proxy relying on the runtime version
  # fails at call time with "object has no attribute 'invoke_harness'" -- a failure no local
  # test sees.
  filename         = local.package
  source_code_hash = filebase64sha256(local.package)

  environment {
    variables = merge(
      {
        DOCUMENT_BUCKET = local.bucket_name
        # THE NAMES THE CODE ACTUALLY READS. `storage/facts.py` reads FACT_STORE_TABLE and
        # `storage/marks.py` reads FACT_MARK_TABLE -- the same names platform/sweep.tf sets. An
        # earlier revision of this file invented READINESS_FACT_TABLE and
        # READINESS_FACT_MARKS_TABLE, which deployed cleanly and left both stores unconfigured:
        # `build_fact_store` would have found an empty name and fallen back, so every component
        # would have resolved no repository and every brief would have been five NOT_LOOKED
        # sections with a plausible-sounding reason. Caught by reading the deployed function's
        # environment back and comparing it against the reader, not by any plan.
        FACT_STORE_TABLE = local.fact_table
        FACT_MARK_TABLE  = local.mark_table
        # How long a stored brief may be served before it is researched again. On every function
        # because `brief-plan` applies it and the others report it, and a window that differed
        # between them would produce a document whose reuse note disagreed with the decision.
        READINESS_BRIEF_REUSE_MAX_AGE_DAYS = tostring(var.brief_reuse_max_age_days)
      },
      each.key == "brief-component" ? {
        BRIEF_HARNESS_ARN = local.effective_harness_arn
      } : {},
    )
  }

  depends_on = [aws_cloudwatch_log_group.function]
}
