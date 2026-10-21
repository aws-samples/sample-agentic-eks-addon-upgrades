# The `search_issues` tool the brief agent calls through its Gateway.
#
# WHY THIS IS A TOOL I OWN RATHER THAN A CAPABILITY THE PLATFORM PROVIDES. The same argument
# infra/terraform/modules/research makes for the fetch tool, applied to a different endpoint: the agent's quotes
# are only checkable because the bytes were written down before it saw them. If issue search were
# something the Harness did on the agent's behalf, an issue title in the brief would be an
# assertion nobody could re-check.
#
# WHY IT COULD NOT JUST BE THE FETCH TOOL. Release notes are a page; issues are a query.
# `GET /search/issues` has a different response shape, a different rate limit and a different
# failure vocabulary, and the fetch tool's whole contract is "one pinned URL, recorded, scoped to
# hosts the component declares". Bolting a query mode onto it would blur the one thing it does
# well.
#
# IT RECORDS TWO DIFFERENT THINGS, and both are load-bearing:
#
#   one object per issue   keyed by the issue's own html_url and `issue-<number>`. This is what a
#                          quote is verified against, so one search call fans out into N citable
#                          objects.
#   one lookup receipt     `*.lookup.json`, naming the outcome. With a managed Harness nothing
#                          downstream sees tool calls, so without receipts an agent that got three
#                          HTTP 403s and then wrote "no known issues" would be indistinguishable
#                          from a genuinely clean project. The receipts make that checkable on
#                          disk, and `brief/document.py` downgrades the section when every attempt
#                          failed.
#
# ITS GRANTS ARE THE NARROWEST IN THIS MODULE: one prefix, no Bedrock, no DynamoDB, no plan, no
# brief. It reads public HTTPS and writes evidence.

resource "aws_cloudwatch_log_group" "search_issues" {
  name              = "/aws/lambda/${local.name_prefix}-tool-search-issues"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_iam_role" "search_issues" {
  name               = "${local.name_prefix}-tool-search-issues"
  description        = "Brief issue-search tool - queries public GitHub, records what it read and whether it looked"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "search_issues" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.search_issues.arn}:*"]
  }

  # Write recorded issues and lookup receipts, and read them back. Scoped to the evidence prefix
  # alone: this tool cannot read a snapshot, a ledger, an assessment, a plan, a candidate or a
  # brief. It shares that prefix with infra/terraform/modules/research's fetch tool by design -- one session's
  # evidence is one prefix, whichever tool wrote each object, which is what lets the proxy rebuild
  # a single PageSet covering pages and issues alike.
  statement {
    sid    = "RecordAndReadIssueEvidence"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:PutObject", "s3:GetObject"]
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

  # The GitHub token, when one is configured. Read at invocation rather than baked into the
  # environment, so rotating the secret does not require redeploying the function.
  dynamic "statement" {
    for_each = var.github_token_secret_arn == null ? [] : [var.github_token_secret_arn]

    content {
      sid       = "ReadGitHubToken"
      effect    = "Allow"
      actions   = ["secretsmanager:GetSecretValue"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "search_issues" {
  name   = "${local.name_prefix}-tool-search-issues"
  role   = aws_iam_role.search_issues.id
  policy = data.aws_iam_policy_document.search_issues.json
}

resource "aws_lambda_function" "search_issues" {
  function_name = "${local.name_prefix}-tool-search-issues"
  description   = "Brief tool - search GitHub issues with a long trail, record them and whether the search ran"
  role          = aws_iam_role.search_issues.arn
  handler       = "entrypoints.brief_harness.tool_search_issues"
  runtime       = "python3.12"
  architectures = ["arm64"]

  # ONE HTTP CALL PLUS ITS RECORDING. Generous rather than tight because a slow GitHub response
  # that times out here becomes a NOT_LOOKED section -- honest, but a lost answer -- and the
  # module's own client timeout is 20s, so this bounds the recording rather than the query.
  timeout     = 60
  memory_size = 512

  # The shared brief zip. See entrypoints.brief_harness.py: four handlers, one package, four roles.
  filename         = local.package
  source_code_hash = filebase64sha256(local.package)

  environment {
    variables = merge(
      { DOCUMENT_BUCKET = local.bucket_name },
      # WHY THE TOKEN IS AN ARN AND NOT A VALUE. A token in an environment variable is readable by
      # anyone with `lambda:GetFunctionConfiguration`, which is a much wider set than
      # `secretsmanager:GetSecretValue` on one secret. The handler resolves it at invocation.
      var.github_token_secret_arn == null ? {} : {
        GITHUB_TOKEN_SECRET_ARN = var.github_token_secret_arn
      },
    )
  }

  depends_on = [aws_cloudwatch_log_group.search_issues]
}
