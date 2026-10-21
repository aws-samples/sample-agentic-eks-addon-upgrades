# THE SWEEP STAGE: its Lambda, its role, its policy, its log group, and its wiring.
#
# WHY IT LIVES HERE, decided by the user: the sweep is a STAGE now -- a state in this
# module's state machine, between `Inventory` and `Gaps` -- so it belongs with the other
# stages rather than in `infra/terraform/modules/research`, which owns Stage 6, the judge and the approval
# surface. Its five resources were moved into this module's state with `terraform state mv`,
# which changes no infrastructure.
#
# WHAT MOVING IT HERE REMOVED, and this is the point rather than a side effect:
#
#   * NO CROSS-COMPONENT ARN. The state machine references `aws_lambda_function.sweep.arn`
#     directly, so there is no `sweep_function_arn` variable, no value to copy out of another
#     component's output, and no ordering between two applies. The three remaining gates
#     (resolve, research, present) still take ARNs because their functions genuinely live
#     elsewhere; this one does not any more.
#   * NO APPLY-ORDER TRAP. The two S3 grants the sweep needs -- GetObject on `inventories/*`
#     to read the run's components, PutObject on `sweeps/*` to publish its document -- are in
#     this file now, so they land in the same apply that inserts the state. It is no longer
#     possible to wire the state in before the grants exist.
#
# WHERE THE STATE GOES, AND WHY EXACTLY THERE:
#
#   Inventory  ->  Sweep  ->  Gaps
#
#   MARKS MUST LAND BEFORE THE FACT STORE IS READ. `Gaps` is the first state that opens
#   the store. A sweep after it would withdraw a fact the run had already decided against.
#
#   THE RESEARCH LOOP RE-ENTERS AT `Gaps`. Ahead of that re-entry the network read happens
#   once per RUN rather than once per research round. Nothing here depends on that loop
#   actually firing, and today it cannot: nothing sets `store_digest_after_research`.
#
# WHY IT IS A STATE AND NOT A STAGE IN THE CHAIN. It reads the NETWORK, which no stage may
# do: a stage document must be a pure function of already-fetched evidence, so a GitHub read
# inside `Gaps` or `Resolve` would make that document depend on the instant it ran and
# two runs would cite identical digests while disagreeing. The sweep publishes its own
# document instead, under `sweeps/`, whose content is honestly time-dependent because it is a
# report ON the network read. No stage reads it; the stages see marks, through the store
# adapter they already use.
#
# NO SCHEDULE. The 6-hourly EventBridge rule that used to invoke this function was torn down
# and stays down, by the user's decision: the sweep is invoked by the state machine now, once
# per run, scoped to that run's components. Nothing here declares a rule, a target or an
# invoke permission for one.

# ---------------------------------------------------------------------------
# The function. Its artifact is research.zip, shared with Stage 6, because both
# handlers live in one entrypoints/stages.py and handlers in one file cannot drift apart.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "sweep" {
  name              = "/aws/lambda/${local.name_prefix}-freshness"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.log_kms_key_arn
}

resource "aws_iam_role" "sweep" {
  name               = "${local.name_prefix}-freshness"
  description        = "Freshness sweep - mark facts whose upstream source moved. No model."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# THE IAM SHAPE IS THE SECURITY ARGUMENT, so read it as one:
#
#   read facts          to compare the revision each one pins against upstream
#   read+write marks    the sweep's own output, and the only thing it may write to DynamoDB
#   read inventories/   to scope itself to the components in THIS run
#   write sweeps/       to publish its document
#   NO PutItem on facts, NO Bedrock, NO cluster access
#
# So "the sweep cannot invent a fact" and "the sweep cannot call a model" are properties of a
# role rather than claims about code.
data "aws_iam_policy_document" "sweep" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.sweep.arn}:*"]
  }

  # Read facts in order to compare the revision each one pins against upstream. NO write
  # action: the sweep cannot alter a fact, only mark it, so a bug here cannot corrupt
  # knowledge.
  statement {
    sid    = "ReadFactStore"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]
    resources = [aws_dynamodb_table.facts.arn]
  }

  statement {
    sid    = "ReadAndWriteMarks"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:PutItem",
    ]
    resources = [aws_dynamodb_table.fact_marks.arn]
  }

  # Read the Stage 2 inventory, which is how the sweep learns which components the run it is
  # flying ahead of is about. One prefix and no other: it cannot read a snapshot, a gap
  # ledger, an assessment or a plan.
  statement {
    sid    = "ReadInventoryToScopeTheSweep"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/inventories/*"]
  }

  # Write its own document: which fact was withheld, the revision it pinned, the revision
  # upstream reports now, and when the comparison was made. Its own prefix, so the sweep
  # cannot write over any stage's output and no stage's grant reaches its output.
  statement {
    sid       = "WriteSweepDocument"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/sweeps/*"]
  }

  # The GitHub token, when one is configured. Same shape as `infra/terraform/modules/brief`'s grant to its
  # search tool, deliberately: one secret, one action, and this module holds no
  # `aws_secretsmanager_secret` resource, so Terraform never owns the credential's contents.
  dynamic "statement" {
    for_each = var.github_token_secret_arn == null ? [] : [var.github_token_secret_arn]

    content {
      sid       = "ReadGitHubToken"
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [statement.value]
    }
  }

  # Deliberately absent: any bedrock action. Resolving an upstream revision is one cheap read
  # per distinct source; the sweep has no reason to call a model and no ability to.
}

resource "aws_iam_role_policy" "sweep" {
  name   = "sweep-permissions"
  role   = aws_iam_role.sweep.id
  policy = data.aws_iam_policy_document.sweep.json
}

resource "aws_lambda_function" "sweep" {
  function_name = "${local.name_prefix}-freshness"
  description   = "Freshness sweep - mark expired facts; revoke a named fact. No model."
  role          = aws_iam_role.sweep.arn
  handler       = "entrypoints.stages.stage_sweep"
  runtime       = "python3.12"
  architectures = [var.lambda_architecture]
  timeout       = 120
  memory_size   = 512

  # research.zip, NOT stages.zip. The sweep needs `urllib` against GitHub and the research
  # package's own modules; the stage artifact deliberately carries pydantic and nothing else.
  filename         = "${path.module}/../../../../build/research.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/research.zip")

  # MERGE, NOT A LITERAL MAP, AND THE DIFFERENCE HAS BITTEN ONCE. Terraform replaces this map
  # wholesale on every apply, so anything set on the live function out of band is dropped
  # silently, with nothing reporting the loss. A GITHUB_TOKEN put here by hand vanished on the
  # next apply exactly that way. Declaring the secret ARN below is the fix: the token's
  # address is now config, so an apply restores it rather than erasing it.
  #
  # THE ARN AND NOT THE VALUE. A token in an environment variable is readable by anyone holding
  # `lambda:GetFunctionConfiguration`, a far wider set than `secretsmanager:GetSecretValue` on
  # one secret. `stages/freshness.py` resolves it at invocation through the same resolver the
  # brief's search tool uses, so rotating the secret needs no redeploy.
  environment {
    variables = merge(
      {
        DOCUMENT_BUCKET  = aws_s3_bucket.documents.id
        FACT_STORE_TABLE = aws_dynamodb_table.facts.name
        FACT_MARK_TABLE  = aws_dynamodb_table.fact_marks.name
      },
      var.github_token_secret_arn == null ? {} : {
        GITHUB_TOKEN_SECRET_ARN = var.github_token_secret_arn
      },
    )
  }

  depends_on = [
    aws_iam_role_policy.sweep,
    aws_cloudwatch_log_group.sweep,
  ]
}

# ---------------------------------------------------------------------------
# The wiring. Kept as one delimited block so a reorder of the later states has
# one expression to fold in rather than a diff spread through workflow.tf.
# ---------------------------------------------------------------------------
locals {
  # The successor of `Inventory`, in ONE place. workflow.tf reads this rather than naming
  # a state inline, so the order is decided here and nowhere else.
  after_inventory = "Sweep"

  # The state itself. UNCONDITIONAL, unlike the three ARN gates: the function is created in
  # this module now, so there is no null-ARN case to guard and no way to deploy the machine
  # without the stage it needs.
  sweep_state = {
    "Sweep" = {
      arn     = aws_lambda_function.sweep.arn
      next    = "Gaps"
      comment = "Mark facts whose upstream source moved, before the store is read. Reads the network; no model."
    }
  }
}
