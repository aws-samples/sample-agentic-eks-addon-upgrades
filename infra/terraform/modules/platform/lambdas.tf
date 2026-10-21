# The five stage Lambdas, their execution roles, and their log groups.
#
# ONE ROLE PER STAGE, NOT ONE ROLE FOR ALL FIVE. Each stage gets exactly the
# permissions its own job needs, which makes the pipeline's shape visible in IAM:
#
#   scan       assume a per-cluster read role; call EKS MCP read-only; write snapshots/
#   inventory  read snapshots/; write inventories/
#   gaps       read snapshots/ + inventories/; read the fact store + marks; write gaps/
#   decide     read gaps/; read the fact store + marks; write decisions/
#   plan       read decisions/; read the fact store + marks; write plans/
#
# No stage can write the fact store. No stage can write a MARK either -- a stage may see
# that a fact was withdrawn and may not withdraw one. No stage but scan can reach a
# cluster. Stage 4 is the only place a verdict is formed, and it cannot read a cluster at
# all -- so a verdict provably comes from the stored ledger rather than from a fresh peek
# at the world.
#
# THE MARKS GRANT IS NEW, AND IT IS A FIX. Marks were written by the sweep and read by
# nothing in the pipeline, because `FACT_MARK_TABLE` reached only infra/terraform/modules/research's
# functions -- so `build_mark_store()` returned `EmptyMarkStore` in the three stages that
# read facts and a withdrawn fact kept answering. See marks.tf for the whole account.

locals {
  stages = {
    scan = {
      handler     = "entrypoints.stages.stage_scan"
      timeout     = 900
      memory      = 1024
      description = "Stage 1 - read the cluster, write a sanitized snapshot"
    }
    inventory = {
      handler     = "entrypoints.stages.stage_inventory"
      timeout     = 120
      memory      = 512
      description = "Stage 2 - identify components and resolve true versions"
    }
    gaps = {
      handler     = "entrypoints.stages.stage_gaps"
      timeout     = 120
      memory      = 512
      description = "Stage 3 - ledger known vs missing, every unknown typed"
    }
    decide = {
      handler     = "entrypoints.stages.stage_decide"
      timeout     = 120
      memory      = 512
      description = "Stage 4 - a verdict per component per version hop"
    }
    plan = {
      handler     = "entrypoints.stages.stage_plan"
      timeout     = 120
      memory      = 512
      description = "Stage 5 - the ordered interleaved upgrade sequence"
    }
  }

  # Which S3 prefixes each stage may read and write. This map IS the data-flow
  # contract; the IAM policies below are generated from it, so a stage cannot
  # quietly gain access to a document it has no business reading.
  stage_prefixes = {
    scan      = { read = [], write = ["snapshots"] }
    inventory = { read = ["snapshots"], write = ["inventories"] }
    gaps      = { read = ["snapshots", "inventories"], write = ["gaps"] }
    decide    = { read = ["gaps"], write = ["decisions"] }
    plan      = { read = ["decisions"], write = ["plans"] }
  }

  # Stages that read the fact store. Note that none WRITES it.
  #
  # This list also decides who is handed the MARKS table, and that coupling is deliberate:
  # a mark is a statement about a fact, so a stage that reads facts must see the marks
  # against them and a stage that reads no facts has no use for either. Two lists could
  # drift apart; one cannot.
  fact_store_readers = ["gaps", "decide", "plan"]
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

resource "aws_iam_role" "stage" {
  for_each = local.stages

  name               = "${local.name_prefix}-${each.key}"
  description        = each.value.description
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "stage" {
  for_each = local.stages

  name              = "/aws/lambda/${local.name_prefix}-${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.log_kms_key_arn
}

data "aws_iam_policy_document" "stage" {
  for_each = local.stages

  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.stage[each.key].arn}:*"]
  }

  # Read only the prefixes this stage consumes.
  dynamic "statement" {
    for_each = length(local.stage_prefixes[each.key].read) > 0 ? [1] : []
    content {
      sid       = "ReadUpstreamDocuments"
      effect    = "Allow"
      actions   = ["s3:GetObject"]
      resources = [for p in local.stage_prefixes[each.key].read : "${aws_s3_bucket.documents.arn}/${p}/*"]
    }
  }

  # Write only the prefix this stage produces.
  statement {
    sid       = "WriteOwnDocument"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = [for p in local.stage_prefixes[each.key].write : "${aws_s3_bucket.documents.arn}/${p}/*"]
  }

  # Read the fact store. Note the absence of PutItem/UpdateItem/DeleteItem: no
  # stage may write a fact. Promotion is a separate, human-gated act and is not
  # part of stages 1-5.
  dynamic "statement" {
    for_each = contains(local.fact_store_readers, each.key) ? [1] : []
    content {
      sid    = "ReadFactStore"
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
      ]
      resources = [aws_dynamodb_table.facts.arn]
    }
  }

  # Read the marks against those facts. Same three stages, same three read actions, and
  # deliberately no PutItem: a stage may see that a fact was withdrawn and may not
  # withdraw one. Writing marks belongs to the sweep and to a human revoking a fact.
  #
  # Without this the decoration in `storage/marks.py` is inert -- `build_mark_store()`
  # falls back to `EmptyMarkStore`, every fact answers, and the store digest does not move
  # when one is withdrawn. The table is created in marks.tf, and only the sweep writes it.
  dynamic "statement" {
    for_each = contains(local.fact_store_readers, each.key) ? [1] : []
    content {
      sid    = "ReadMarks"
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
      ]
      resources = [aws_dynamodb_table.fact_marks.arn]
    }
  }

  # Only the scan stage may assume a cluster read role, and only a role in the
  # namespace this deployment's own cluster-access module creates.
  #
  # A PREFIX PATTERN, NOT A LIST OF ARNS. The list version made this module depend
  # on cluster-access having already run, while cluster-access depends on this
  # module for the scan role ARN it trusts -- a cycle that forced a from-scratch
  # deploy through three applies (platform -> cluster-access -> platform) and, far
  # worse, meant a cluster onboarded without someone remembering to re-apply
  # platform SILENTLY DID NOT WORK. The scanner looked broken rather than
  # unauthorised, which is the failure mode this project exists to avoid.
  #
  # The prefix breaks the cycle: cluster-access names its role deterministically as
  # `${name_prefix}-read-${cluster_name}`, so this module needs to know nothing
  # about which clusters exist. Onboarding is now one apply of cluster-access and
  # nothing here changes.
  #
  # WHAT THE WIDENING COSTS, stated rather than glossed. The grant moves from
  # "these named roles" to "any role whose name starts with this prefix". That
  # namespace is created only by this deployment's own cluster-access module, and
  # every role in it carries AmazonEKSMCPReadOnlyAccess without
  # eks-mcp:CallPrivilegedTool -- read-only by construction.
  dynamic "statement" {
    for_each = each.key == "scan" ? [1] : []
    content {
      sid       = "AssumeAnyOnboardedClusterReadRole"
      effect    = "Allow"
      actions   = ["sts:AssumeRole"]
      resources = ["arn:aws:iam::${local.account_id}:role/${local.name_prefix}-read-*"]
    }
  }
}

resource "aws_iam_role_policy" "stage" {
  for_each = local.stages

  name   = "stage-permissions"
  role   = aws_iam_role.stage[each.key].id
  policy = data.aws_iam_policy_document.stage[each.key].json
}

# TWO artifacts, and the split is a guarantee rather than an optimisation.
#
#   stage1.zip   Stage 1 only. Carries the EKS MCP transport.
#   stages.zip   Stages 2-5. pydantic and nothing else - no transport at all.
#
# So "Stage 4 cannot read a cluster" is true of the deployed artifact, not just of
# the code: there is no MCP client in its package to call. Stages 2-5 also share one
# zip because their handlers are functions in one entrypoints/stages.py, which keeps them from
# drifting apart.
locals {
  stage_artifact = {
    for k, _ in local.stages : k => k == "scan" ? "stage1" : "stages"
  }
}

resource "aws_lambda_function" "stage" {
  for_each = local.stages

  function_name = "${local.name_prefix}-${each.key}"
  description   = each.value.description
  role          = aws_iam_role.stage[each.key].arn
  handler       = each.value.handler
  runtime       = "python3.12"
  architectures = [var.lambda_architecture]
  timeout       = each.value.timeout
  memory_size   = each.value.memory

  # ONE SCAN AT A TIME, ENFORCED BY LAMBDA. Measured, not guessed: the EKS MCP
  # Server returns HTTP 429 for concurrent sessions from this identity.
  #
  #   1 concurrent scan   SUCCEEDED, repeatedly, including straight after N=2 failed
  #   2 concurrent scans  BOTH FAILED
  #   4 concurrent scans  all FAILED
  #
  # The 429 surfaces two different ways, which is why the Step Functions cause field
  # was misleading. A container that is refused at handshake reports
  # "client failed to initialize"; one refused mid-session reports
  # "RuntimeError: Connection to the MCP server was closed" from
  # strands/tools/mcp/mcp_client.py stop(). Both are
  # `httpx.HTTPStatusError: Client error '429 ' for url https://eks-mcp.<region>.api.aws/mcp`
  # underneath, at mcp/client/streamable_http.py:381.
  #
  # A reserved concurrency of 1 makes Lambda refuse the second invocation with
  # `Lambda.TooManyRequestsException`, which the state machine already retries -- so
  # the queueing is handled by machinery that exists rather than by swallowing the
  # 429. See the scan-specific retry budget in workflow.tf, which had to be widened:
  # a scan runs ~31s, and the default 2/4/8s retry ladder exhausted in 14s, i.e.
  # before the run ahead of it could possibly finish.
  #
  # THIS IS A REAL CONSTRAINT, NOT A WORKAROUND. Clusters are scanned one at a time.
  # The fleet story still holds -- it is sequential rather than parallel, and the
  # per-cluster economics argument is about model calls, not scan parallelism -- but
  # anyone planning to scan a large fleet on a deadline needs to know the wall-clock
  # cost is N x ~35s. Raising it means asking AWS about the endpoint's limit, not
  # editing this line.
  reserved_concurrent_executions = each.key == "scan" ? 1 : -1

  filename         = "${path.module}/../../../../build/${local.stage_artifact[each.key]}.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/${local.stage_artifact[each.key]}.zip")

  # Per-stage where it matters, shared where it does not. The marks table goes only to the
  # stages that read facts -- handing it to `scan` or `inventory` would grant an address
  # neither has any use for, and the environment is where a reader checks what a function
  # can reach.
  environment {
    variables = merge(
      {
        DOCUMENT_BUCKET           = aws_s3_bucket.documents.id
        FACT_STORE_TABLE          = aws_dynamodb_table.facts.name
        TARGET_KUBERNETES_VERSION = var.target_kubernetes_version
      },
      contains(local.fact_store_readers, each.key) ? {
        FACT_MARK_TABLE = local.fact_mark_table
      } : {},
    )
  }

  depends_on = [
    aws_iam_role_policy.stage,
    aws_cloudwatch_log_group.stage,
  ]
}
