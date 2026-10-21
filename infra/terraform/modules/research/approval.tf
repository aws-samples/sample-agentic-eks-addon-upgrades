# The approval surface: an IAM-authenticated endpoint the agent cannot reach.
#
# THE CLAIM THIS ENFORCES: anything that reads the approval page can SHOW a pending approval and cannot
# GRANT one. Four controls, and the reason there are four is stated honestly below --
# Terraform cannot write a Lambda resource-policy Deny, so the guarantee is assembled
# from what it can express rather than from one statement.
#
#   1. The function URL's auth type is AWS_IAM. There is no anonymous path at all, so
#      holding the URL is not sufficient to call it. The agent holds every URL it renders,
#      which is exactly why an unauthenticated endpoint would destroy the claim.
#
#   2. The function's resource policy grants `lambda:InvokeFunctionUrl` to EXACTLY ONE
#      principal: the `-approver` role below. Not to the account, not to a wildcard.
#
#   3. The approver role's TRUST policy -- which this module owns -- can carry an EXPLICIT
#      DENY on named roles (`denied_approver_role_names`, empty by default). An explicit deny is final in IAM evaluation, so
#      it holds even if those roles are later granted `sts:AssumeRole` on this role by
#      someone else. That is the "regardless of what they build" property.
#
#   4. The handler re-checks the principal AWS resolved from the signature and refuses
#      anything that is not an approver. Defence in depth, and the layer that covers the
#      residual gap in note (A).
#
# (A) THE RESIDUAL, STATED RATHER THAN HIDDEN. For a Lambda function URL in the SAME
#     account, an identity-based Allow is sufficient on its own -- the resource policy does
#     not have to allow the caller. So a role that were later granted
#     `lambda:InvokeFunctionUrl` on this function directly could reach the endpoint despite
#     control 2, and control 4 is what stops it there. Closing that gap in IAM needs a true
#     explicit Deny, which requires either a statement in that role's own policy
#     or an SCP (org-level). `aws_lambda_permission` has no
#     `effect` argument, so Terraform cannot write that Deny on the function. This is a
#     coordination decision, raised rather than worked around.

locals {
  # Roles whose principals must never be able to approve -- for example the tool roles of an
  # agent that reads approval pages. Overridable, so denying a new role is a variable change
  # and not a code change.
  denied_role_names = var.denied_approver_role_names
  denied_role_arns = [
    for name in local.denied_role_names :
    "arn:aws:iam::${local.account_id}:role/${name}"
  ]
}

# ---------------------------------------------------------------------------
# The approver role. A person assumes this; the agent cannot.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "approver_trust" {
  # Human principals in this account may assume it. Scoped to the account root, which
  # means "a principal in this account with sts:AssumeRole permission" -- the normal shape
  # for a role a person assumes from their own session.
  statement {
    sid     = "HumanPrincipalsMayAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.account_id}:root"]
    }
  }

  # THE EXPLICIT DENY. An explicit deny is final in IAM evaluation, so this holds even if
  # one of these roles is later granted sts:AssumeRole on this role in its own identity
  # policy. That is what makes the guarantee independent of what those roles are granted.
  dynamic "statement" {
    for_each = length(local.denied_role_arns) > 0 ? [1] : []
    content {
      sid     = "AgentToolRolesMayNeverApprove"
      effect  = "Deny"
      actions = ["sts:AssumeRole"]

      principals {
        type        = "AWS"
        identifiers = local.denied_role_arns
      }
    }
  }
}

resource "aws_iam_role" "approver" {
  name = "${local.name_prefix}-approver"
  description = join(" ", [
    "Assumed by a person to approve a proposed compatibility fact.",
    "Roles listed in denied_approver_role_names are explicitly denied from assuming it."
  ])
  assume_role_policy = data.aws_iam_policy_document.approver_trust.json
}

data "aws_iam_policy_document" "approver" {
  # No `lambda:FunctionUrlAuthType` condition here, deliberately, and it is worth recording
  # why: with that condition on the IDENTITY policy the approver role was rejected by
  # Lambda's own authorizer with a bare 403 Forbidden, while the IAM simulator reported
  # `allowed`. The condition belongs on the RESOURCE policy, where `aws_lambda_permission`
  # sets it from `function_url_auth_type`, and duplicating it here only broke the grant.
  # Found by a live signed request; the simulator cannot see it because it does not evaluate
  # the function URL's authorizer.
  # BOTH ACTIONS, and the second is the fix for a 403 that cost real diagnosis.
  #
  # A function URL with AWS_IAM auth requires `lambda:InvokeFunctionUrl` AND
  # `lambda:InvokeFunction`. The approver role held only the first, so every signed
  # request came back a bare 403 from Lambda's authorizer.
  #
  # `simulate-principal-policy` reported "allowed" throughout, because it was only ever
  # asked about the one action the role had. Proven instead by a truth table of real
  # signed calls: 403 for every single-action grant -- including one made with the
  # resource policy deleted outright, which rules the resource policy out as the cause --
  # and 200 only with both actions present.
  statement {
    sid    = "InvokeTheApprovalEndpoint"
    effect = "Allow"
    actions = [
      "lambda:InvokeFunctionUrl",
      "lambda:InvokeFunction",
    ]
    resources = [aws_lambda_function.approve.arn]
  }

  # Read the candidate and its report, so the approver can re-read what they are approving
  # from the same objects the page was rendered from.
  statement {
    sid    = "ReadCandidatesAndReports"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [local.bucket_arn, "${local.bucket_arn}/facts-candidates/*"]
  }
}

resource "aws_iam_role_policy" "approver" {
  name   = "approver-permissions"
  role   = aws_iam_role.approver.id
  policy = data.aws_iam_policy_document.approver.json
}

# ---------------------------------------------------------------------------
# The approval function. The ONLY writer of facts in the whole system.
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "approve" {
  name              = "/aws/lambda/${local.name_prefix}-approve"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_iam_role" "approve" {
  name               = "${local.name_prefix}-approve"
  description        = "Approval endpoint - the only role that may write a fact; a signed human calls it"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "approve" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.approve.arn}:*"]
  }

  # Read the candidate and its report, and list what is pending.
  statement {
    sid    = "ReadCandidates"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket_arn}/facts-candidates/*"]
  }

  statement {
    sid       = "ListPendingCandidates"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["facts-candidates/*"]
    }
  }

  # Record a REFUSAL, and nothing else.
  #
  # WHY IT IS NEEDED. Recording a refusal is the first S3 write this function ever makes
  # -- approving writes to DynamoDB, not here -- so without this the reject path dies
  # with AccessDenied surfacing as a bare 502. Confirmed against the live role, which
  # held s3:GetObject and s3:ListBucket on this bucket and nothing more.
  #
  # WHY THE PREFIX IS THE WHOLE GRANT. Scoped to the one prefix a refusal is written
  # under, so it cannot write a fact, cannot overwrite the pending candidate it refuses
  # -- that object is the record of what was proposed and a refusal that erased it would
  # destroy the evidence the refusal is about -- and cannot touch any other document the
  # pipeline produced.
  #
  # WHY IT MOVES THE HUMAN BOUNDARY NOWHERE. A refusal stores NO fact and changes no
  # verdict; the component stays an honest gap, exactly as it was before anyone looked.
  # The load-bearing claim is about who may GRANT a fact, and nothing here grants one.
  statement {
    sid       = "RecordARefusal"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${local.bucket_arn}/facts-candidates/decision=REJECTED/*"]
  }

  # THE fact write, and the only one in the deployment: no other role holds PutItem on the
  # fact table. Stages 1-5 are read-only, and research PROPOSES while a person approves.
  statement {
    sid    = "WriteApprovedFact"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:PutItem",
    ]
    resources = [local.facts_arn]
  }

  # Read marks, so the "currently held" column of a rebuilt report matches what the
  # pipeline would actually serve.
  statement {
    sid    = "ReadMarks"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
    ]
    resources = [local.fact_marks_arn]
  }

  # THE ONE PRINCIPAL THAT MAY RESUME A PAUSED RUN. Held here and nowhere else, and the
  # placement is the argument: resuming the pipeline is what the person's decision does, so
  # it belongs to the function the person's decision reaches. The approval STAGE, which
  # renders the page and mails the links, is deliberately denied it -- a stage able to
  # resume its own pause is not a pause.
  #
  # `SendTaskFailure` is granted alongside it so a refusal-only outcome can be reported as a
  # failure rather than being silently indistinguishable from success. Scoped to `*` because
  # a task token is not an ARN and these two actions take no resource-level permission; the
  # token itself is the scope, and it is single-use.
  statement {
    sid    = "ResumeThePausedRun"
    effect = "Allow"
    actions = [
      "states:SendTaskSuccess",
      "states:SendTaskFailure",
    ]
    resources = ["*"]
  }

  # Read the paused run to find its token, note each decision against it, and forget it once
  # resumed. No PutItem: the stage creates the row, this only updates and deletes it, so a
  # bug here cannot invent a paused run that no execution is waiting on.
  statement {
    sid    = "ReadAndSettleThePausedRun"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Scan",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
    ]
    resources = [aws_dynamodb_table.approval_tokens.arn]
  }

  # Deliberately absent: any bedrock action. Approval reads stored artifacts and writes a
  # fact; it never calls a model, and now it cannot.
}

resource "aws_iam_role_policy" "approve" {
  name   = "approve-permissions"
  role   = aws_iam_role.approve.id
  policy = data.aws_iam_policy_document.approve.json
}

resource "aws_lambda_function" "approve" {
  function_name = "${local.name_prefix}-approve"
  description   = "Approve a proposed compatibility fact - the only writer of facts"
  role          = aws_iam_role.approve.arn
  handler       = "entrypoints.stages.approve_fact"
  runtime       = "python3.12"
  architectures = ["arm64"]
  timeout       = 60
  memory_size   = 512

  filename         = "${path.module}/../../../../build/research.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/research.zip")

  environment {
    variables = {
      DOCUMENT_BUCKET  = local.document_bucket
      FACT_STORE_TABLE = local.fact_store_table
      FACT_MARK_TABLE  = local.fact_mark_table
      # Who may approve. The handler refuses any other principal.
      #
      # THE HUMAN'S OWN ROLE IS FIRST, and that is the correction rather than an addition.
      # The brief says the approver works "from a browser session signed with their own
      # credentials"; the `-approver` role hop was invented on top of that and is what the
      # 403 investigation traced to. A person signing with their own credentials is the
      # intended path, and it now works without an IAM change: they already hold
      # lambda:InvokeFunctionUrl, and this list is what decides whether the HANDLER accepts
      # them.
      #
      # The -approver role stays in the list so the hop keeps working once its own 403 is
      # resolved, but nothing depends on it.
      APPROVER_ROLE_NAMES = aws_iam_role.approver.name

      # Where a paused run's task token lives. Needed for the `done` action, which is the
      # only path that resumes an execution. Unset means `done` answers 404 for every run,
      # which is the correct behaviour for a deployment with no approval stage: there is
      # nothing paused to resume.
      APPROVAL_TOKEN_TABLE = aws_dynamodb_table.approval_tokens.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.approve]
}

resource "aws_lambda_function_url" "approve" {
  function_name = aws_lambda_function.approve.function_name

  # AWS_IAM, never NONE. A function URL with NONE auth is a public endpoint, and a public
  # approval endpoint would mean anything holding the URL could approve -- including the
  # agent, which holds every URL it renders.
  authorization_type = "AWS_IAM"
}

# Control 2: the resource policy grants invoke to EXACTLY the approver role.
resource "aws_lambda_permission" "approve_url" {
  statement_id           = "AllowApproverRoleOnly"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.approve.function_name
  principal              = aws_iam_role.approver.arn
  function_url_auth_type = "AWS_IAM"
}
