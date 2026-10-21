# Stage 7, the resolve loop's decision point: one Lambda, one role, one log group.
#
# It runs from the same `stages.zip` as stages 1-5, and like them it holds no model client and no
# cluster access. It used to be its own module, a separate Terraform root added after this one held
# live state; with a single deploy root that separation bought nothing, so it lives here beside the
# stages it sits between.
#
# WHAT THIS STAGE MAY NOT DO, AND WHY EACH ABSENCE MATTERS
#
#   no dynamodb at all       it cannot write a fact -- promotion stays a separate,
#                            human-gated act -- and as of the freshness-gate removal it
#                            cannot READ one either. The dispatch queue is a function of
#                            the gap ledger alone, and the role now says so.
#   no lambda:InvokeFunction it cannot perform the resume itself. It returns
#                            `research_required` and the workflow branches, so the
#                            re-run is the REAL gaps/decide/plan functions over the
#                            enlarged store and the digest chain stays honest.
#   no cluster permission    the dispatch decision is a function of documents.
#   no Bedrock, no network   its package is stages.zip: pydantic and nothing else.
#                            "No model decided what to research" is a property of the
#                            deployed artifact, not a claim about the code.

locals {
  resolve_function_name = "${local.name_prefix}-resolve"
  # The state machine's `Resolve` state and its invoke grant read this. It is always set now that
  # the function lives in this module; workflow.tf keeps its null guards from when it was an input.
  resolve_function_arn = aws_lambda_function.resolve.arn
}

resource "aws_iam_role" "resolve" {
  name               = local.resolve_function_name
  description        = "Stage 7 - decide which knowledge gaps are worth researching"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "resolve" {
  name              = "/aws/lambda/${local.resolve_function_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.log_kms_key_arn
}

data "aws_iam_policy_document" "resolve" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.resolve.arn}:*"]
  }

  # Reads the gap ledger and nothing else. Not the snapshot, not the assessment, not
  # the plan: the dispatch decision is a function of what is KNOWN, and the ledger is
  # the document that records exactly that.
  statement {
    sid    = "ReadTheGapLedger"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/gaps/*"]
  }

  statement {
    sid       = "WriteOwnDispatchDocument"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/dispatch/*"]
  }

  # LIST THE CANDIDATES AWAITING APPROVAL, and deliberately not read them.
  #
  # A component whose answer is already sitting in `facts-candidates/decision=PENDING_APPROVAL/`
  # is no longer re-dispatched. Research writes a CANDIDATE and Stage 3 looks for a FACT, so
  # without this the gap never closed and the same question was asked again every run: measured on
  # this bucket at 100 pending candidates answering three questions, one component holding 40
  # DISTINCT answers.
  #
  # LIST ONLY, NO GetObject. The component is a path segment of `candidate_key`, so counting per
  # component needs no object opened -- which makes it structurally impossible for the CONTENT of
  # a pending candidate to influence whether a gap is researched again. Only how many are waiting.
  statement {
    sid       = "ListCandidatesAwaitingApproval"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["facts-candidates/decision=PENDING_APPROVAL/*"]
    }
  }

  # THE FACT-STORE GRANT WAS REMOVED WITH THE FRESHNESS GATE, which was its only reason to
  # exist: it read the store to ask how old the researched fact answering a component was.
  # Staleness is now upstream drift, decided by a sweep outside the pipeline, and a
  # withheld fact is already absent from the ledger by the time this stage sees it -- so
  # Stage 7 opens no store at all. `stages/resolve.py` no longer calls
  # `build_fact_store()`, so this grant permitted a read nothing performs.
}

resource "aws_iam_role_policy" "resolve" {
  name   = "stage-permissions"
  role   = aws_iam_role.resolve.id
  policy = data.aws_iam_policy_document.resolve.json
}

resource "aws_lambda_function" "resolve" {
  function_name = local.resolve_function_name
  description   = "Stage 7 - the deterministic dispatch queue: which gaps to research, and why"
  role          = aws_iam_role.resolve.arn
  handler       = "entrypoints.stages.stage_resolve"
  runtime       = "python3.12"
  architectures = [var.lambda_architecture]
  timeout       = 120
  memory_size   = 512

  filename         = "${path.module}/../../../../build/stages.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/stages.zip")

  environment {
    variables = {
      DOCUMENT_BUCKET = aws_s3_bucket.documents.id
      # Bound on loop rounds. The queue shrinks only when facts are written, so a
      # component whose research is repeatedly held would otherwise re-queue forever.
      READINESS_MAX_RESOLVE_ITERATIONS = var.max_resolve_iterations
    }
  }

  depends_on = [
    aws_iam_role_policy.resolve,
    aws_cloudwatch_log_group.resolve,
  ]
}

output "resolve_function_name" {
  description = "Stage 7, the resolve function."
  value       = aws_lambda_function.resolve.function_name
}

output "dispatch_prefix" {
  description = "Where each round's dispatch document lands."
  value       = "s3://${aws_s3_bucket.documents.id}/dispatch/"
}
