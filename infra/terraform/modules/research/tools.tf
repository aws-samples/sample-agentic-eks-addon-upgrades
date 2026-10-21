# The source-fetch tool the research Harness calls through its Gateway.
#
# WHY THIS LAMBDA EXISTS AT ALL, when the point of moving to Harness was to stop hand-rolling
# the agent host: because fetching is the one thing that must NOT be delegated to the
# platform. The defect this stage was rebuilt around is a quote spliced from two commits of
# one README passing a check that could not see the page. It is closed only because every
# fetched body is recorded and every quote is checked against that recording.
#
# If fetching were a managed capability the platform performed on the agent's behalf, the
# bodies would never reach the judging layer and that hole would reopen. So the Harness
# manages the model loop, the tool auth, the retries, the truncation and the tracing -- and
# the fetch itself stays a tool I own, whose first act is to write the bytes down.
#
# Everything else about it is deliberately small: one action, one prefix, no fact access, no
# Bedrock, no DynamoDB. It reads public HTTPS and writes evidence.

resource "aws_cloudwatch_log_group" "tool_fetch_source" {
  name              = "/aws/lambda/${local.name_prefix}-tool-fetch-source"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_iam_role" "tool_fetch_source" {
  name               = "${local.name_prefix}-tool-fetch-source"
  description        = "Stage 6 source fetch tool - reads public HTTPS, records what it read"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "tool_fetch_source" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.tool_fetch_source.arn}:*"]
  }

  # Write recorded bodies, and read them back so the judging layer can rebuild the evidence
  # set for a session. Scoped to the evidence prefix alone: this tool cannot read a snapshot,
  # a ledger, an assessment, a plan or a candidate.
  statement {
    sid    = "RecordAndReadFetchedBodies"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions = [
      "s3:PutObject",
      "s3:GetObject",
    ]
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

  # Deliberately absent: bedrock, dynamodb, and every other document prefix. A fetch tool
  # that could write a fact would make the approval boundary decorative.
}

resource "aws_iam_role_policy" "tool_fetch_source" {
  name   = "tool-permissions"
  role   = aws_iam_role.tool_fetch_source.id
  policy = data.aws_iam_policy_document.tool_fetch_source.json
}

resource "aws_lambda_function" "tool_fetch_source" {
  function_name = "${local.name_prefix}-tool-fetch-source"
  description   = "Fetch one pinned source page and record its bytes before the model sees them"
  role          = aws_iam_role.tool_fetch_source.arn
  handler       = "entrypoints.stages.tool_fetch_source"
  runtime       = "python3.12"
  architectures = ["arm64"]
  # Generous enough for a slow documentation host, bounded well inside the harness's own
  # per-tool patience.
  timeout     = 60
  memory_size = 512

  filename         = "${path.module}/../../../../build/research.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/research.zip")

  environment {
    variables = {
      DOCUMENT_BUCKET = local.document_bucket
    }
  }

  depends_on = [aws_cloudwatch_log_group.tool_fetch_source]
}

# The Gateway invokes this tool. Granted to the AgentCore service principal rather than to a
# role, because the Gateway is what calls it.
#
# SCOPED TO THIS GATEWAY'S ARN, not merely to this account. With SourceAccount alone, ANY
# AgentCore Gateway in the account could invoke this tool, which would hand another agent a
# source-fetching capability nobody granted it. SourceAccount is kept alongside SourceArn: the ARN pins which gateway, the
# account defends against a confused-deputy ARN from another account.
resource "aws_lambda_permission" "gateway_invoke_fetch_source" {
  statement_id   = "AllowResearchGatewayInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.tool_fetch_source.function_name
  principal      = "bedrock-agentcore.amazonaws.com"
  source_account = local.account_id

  # The Gateway this module now manages -- see agentcore.tf. Was `var.research_gateway_arn`
  # with a hardcoded default, when the Gateway belonged to a CloudFormation stack.
  source_arn = local.effective_gateway_arn
}

output "tool_fetch_source_name" {
  description = "Name of the source-fetch tool Lambda. infra/terraform/modules/brief reuses it unchanged."
  value       = aws_lambda_function.tool_fetch_source.function_name
}

output "tool_fetch_source_arn" {
  description = <<-EOT
    The source-fetch tool the research Gateway routes to. Referenced by
    agentcore.json's gateway target, and the reason hole 4 stays closed under a managed
    host: this tool records every body it serves before the model reads it.
  EOT
  value       = aws_lambda_function.tool_fetch_source.arn
}


# ---------------------------------------------------------------------------
# The judging Lambda. The deterministic half of Stage 6, deployed apart from the
# Harness that produced the extraction it judges.
#
# THE SPLIT IS THE THESIS AS TWO ARTIFACTS. The Harness reads sources and transcribes; it
# holds no judging code. This function judges; its package contains no model client and its
# role has no Bedrock permission. So "the model never issues a verdict" is a claim about what
# is installed where, not about discipline.
#
# It also cannot write a fact. Only the approval endpoint can, and only for a person. This
# role has read on the fact table so the change direction can be classified against what is
# currently held, and nothing more.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "judge" {
  name              = "/aws/lambda/${local.name_prefix}-judge"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_iam_role" "judge" {
  name               = "${local.name_prefix}-judge"
  description        = "Stage 6 judging - deterministic, no model, cannot write a fact"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "judge" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.judge.arn}:*"]
  }

  # Read the bodies the fetch tool recorded, which is what every quote is checked against.
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

  # Write the candidate, its evidence copy and its approval report.
  statement {
    sid       = "WriteCandidates"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${local.bucket_arn}/facts-candidates/*"]
  }

  # READ the fact store, to classify the change direction against what is held. NO PutItem:
  # this handler proposes and a person approves.
  statement {
    sid    = "ReadFactStore"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]
    resources = [local.facts_arn, local.fact_marks_arn]
  }

  # Deliberately absent: bedrock. There is no model in this package and no permission to
  # reach one if there were.
}

resource "aws_iam_role_policy" "judge" {
  name   = "judge-permissions"
  role   = aws_iam_role.judge.id
  policy = data.aws_iam_policy_document.judge.json
}

resource "aws_lambda_function" "judge" {
  function_name = "${local.name_prefix}-judge"
  description   = "Stage 6 judging - verify quotes against recorded bytes, propose for approval"
  role          = aws_iam_role.judge.arn
  handler       = "entrypoints.stages.stage_judge"
  runtime       = "python3.12"
  architectures = ["arm64"]
  timeout       = 120
  memory_size   = 1024

  filename         = "${path.module}/../../../../build/research.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/research.zip")

  environment {
    variables = {
      DOCUMENT_BUCKET  = local.document_bucket
      FACT_STORE_TABLE = local.fact_store_table
      FACT_MARK_TABLE  = local.fact_mark_table
      APPROVE_ENDPOINT = aws_lambda_function_url.approve.function_url
    }
  }

  depends_on = [aws_cloudwatch_log_group.judge]
}

output "judge_function_name" {
  description = <<-EOT
    The deterministic half of Stage 6. Invoke with {extraction, session_id, ...} after the
    Harness returns. Its package carries no model client and its role grants no Bedrock and
    no fact write.
  EOT
  value       = aws_lambda_function.judge.function_name
}
