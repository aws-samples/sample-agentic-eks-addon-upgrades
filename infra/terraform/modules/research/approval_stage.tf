# The approval stage: the pause, the page, the email, and the link that resumes the run.
#
# ---------------------------------------------------------------------------------------
# THE LINK ENDPOINT IS AUTHENTICATED. IT USED NOT TO BE, AND THAT WAS THE DEFECT.
# ---------------------------------------------------------------------------------------
#
# `aws_lambda_function_url.approve_link` is `AWS_IAM`, reached by a SigV4-PRESIGNED URL that
# the approval stage mints per link. Two independent gates govern a request:
#
#   1. THE SIGNATURE, verified by AWS before the function is invoked at all. It is bound to
#      one method, one path and one query string, so a link cannot be edited into a different
#      action or a different candidate, and it expires on `X-Amz-Expires` -- set to the
#      approval window, so a link cannot outlive the run it resumes.
#   2. THE SINGLE-USE GRANT in the query string: scoped to ONE action on ONE candidate, with
#      the URI and both digests baked into the STORED grant rather than taken from the
#      request; consumed by a conditional delete, so it works exactly once; and delivered
#      ONLY by email, so it is never written to S3, never returned in a stage result and
#      never logged.
#
# THE FIRST VERSION HAD ONLY THE SECOND GATE, on a `NONE` function URL. That is the shape AWS
# publishes as its own Step Functions "Human Approval" sample, and it is how most approval
# emails work -- but an endpoint open to the world is a real finding regardless of what guards
# it downstream. A security scanner raised it as world-accessible, correctly, and an org-level guardrail
# refused every request:
#
#   HTTP 403 {"Message":"Forbidden. For troubleshooting Function URL authorization ..."}
#
# returned while the function's own resource policy said `Allow` to `Principal: *` -- an
# `Allow` plus a 403 being the signature of a denial above the account. The fix is not a
# suppression or an exception: `Principal: "*"` is gone, and the only principal permitted to
# reach the URL is the approval stage's role.
#
# WHAT IS STILL GIVEN UP, stated plainly because presigning does not fix it: anyone who
# obtains a valid link before it is used can approve, and the recorded approver is the mailbox
# the link was mailed to rather than a verified human. A forwarded mail, a shared inbox, or a
# scanner that prefetches GET links are all real ways for that to happen. Slack is the shape
# that closes it -- its callback signs a payload naming the clicking user -- and it costs an
# app, a signing secret and an approved ingress, which is a trade this sample does not make.
#
# ---------------------------------------------------------------------------------------
# WHAT THIS FILE CREATES. All of it is new; nothing here modifies an existing resource
# except the approve role's policy, which gains two statements.
#
#   aws_dynamodb_table.approval_tokens      one item per paused run
#   aws_lambda_function.approval            the stage: renders, mints, stores, mails
#   aws_lambda_function.approve_link        the endpoint a link hits
#   aws_lambda_function_url.approve_link    AWS_IAM auth -- see above
#   aws_ses_email_identity.approval_*       sender and recipient verification
#
# WHY THE STAGE LIVES HERE AND NOT IN infra/terraform/modules/platform. It shares the token table with
# the approve function, which is this module's, and it needs the approve function URL to
# render the break-glass command. Platform owns only the WIRING -- the state that invokes
# it -- because the state machine is platform's resource.

# ---------------------------------------------------------------------------
# The token store
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "approval_tokens" {
  name = "${local.name_prefix}-approval-tokens"

  # PAY_PER_REQUEST because the access pattern is a handful of items per run and long idle
  # gaps between runs. Provisioned capacity would bill continuously for a table that is
  # written once and read a few times per pipeline execution.
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"

  attribute {
    name = "run_id"
    type = "S"
  }

  # The TTL is HOUSEKEEPING, NOT THE APPROVAL DEADLINE, and the difference matters: the
  # execution fails on its own `approval_timeout_seconds` (2h by default) long before this
  # fires. This exists so an abandoned run's row does not sit there forever holding a token
  # nobody can use.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Encrypt at rest with the AWS-owned DynamoDB key, matching the facts table in
  # modules/platform/storage.tf. A single-use token store does not warrant a per-organisation
  # CMK, so this mirrors `aws_dynamodb_table.facts`; default SSE still satisfies "DynamoDB
  # Table Not Encrypted". A CMK here is left to the deployer (CKV_AWS_119).
  server_side_encryption {
    enabled = true
  }

  # A paused run's token is recoverable state: losing it means an execution that cannot be
  # resumed and must time out and fail. Cheap insurance on a table this small.
  point_in_time_recovery {
    enabled = true
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# The approval stage Lambda
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "approval" {
  name              = "/aws/lambda/${local.name_prefix}-approval"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_iam_role" "approval" {
  name               = "${local.name_prefix}-approval"
  description        = "Renders the approval page and mails the links. Cannot resume its own run."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "approval" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.approval.arn}:*"]
  }

  # The page, and nothing else in the bucket. Scoped to the one prefix it writes so a bug
  # here cannot overwrite a snapshot, a ledger, a candidate or a report.
  statement {
    sid       = "WriteTheApprovalPage"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${local.bucket_arn}/approvals/*"]
  }

  statement {
    sid       = "RecordThePausedRun"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.approval_tokens.arn]
  }

  statement {
    sid       = "MailTheApprovalRequest"
    effect    = "Allow"
    actions   = ["ses:SendEmail", "ses:SendRawEmail"]
    resources = [local.ses_sender_identity_arn]

    # Scoped to this deployment's SES sender identity ARN; the `ses:FromAddress` condition is
    # kept as defence in depth.
    condition {
      test     = "StringEquals"
      variable = "ses:FromAddress"
      values   = [var.approval_email_from]
    }
  }

  # WHAT LETS IT SIGN THE PAGE'S BUTTONS. A presigned S3 POST is only honoured if the signing
  # principal actually holds the permission being signed for -- so this grant IS the button's
  # authority, and its narrowness is the button's narrowness. Scoped to the one prefix a
  # browser may write, which is a DIFFERENT prefix from the pages this role also writes:
  # `approvals/*` is written BY the pipeline and read by a person, `approval-decisions/*` is
  # written by a person and read by the pipeline. Opposite directions, so separate statements.
  #
  # It does NOT let this role approve anything. `decision_watcher` refuses a decision object
  # whose key names no unspent grant, and the grants are minted here and handed out -- so
  # holding this permission without holding a nonce achieves nothing.
  statement {
    sid       = "SignTheDecisionButtons"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${local.bucket_arn}/approval-decisions/*"]
  }

  # Read its own page back, so the email can carry a presigned link to it rather than a console
  # URL that only works for somebody already signed in to the console.
  statement {
    sid    = "PresignTheEvidencePage"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket_arn}/approvals/*"]
  }

  # FIND AND READ THE CANDIDATES EARLIER RUNS LEFT PENDING. Without these two the gate cannot
  # see them at all, and the way it fails is the reason this comment is long: `list_pending`
  # swallows a listing error and returns nothing -- deliberately, because the alternative
  # default silently stops researching real gaps. So an AccessDenied here does not raise, does
  # not log, and renders a page reading "Nothing is awaiting approval" for a run that stopped
  # precisely because something WAS. Observed exactly that way on the first deploy.
  #
  # `ListBucket` IS A BUCKET-LEVEL ACTION, so its resource is the bucket itself and the prefix
  # is expressed as a condition rather than in the ARN -- writing `bucket/facts-candidates/*`
  # grants nothing and denies nothing, which is the quiet way to think this is fixed.
  statement {
    sid       = "FindWhatEarlierRunsLeftPending"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["facts-candidates/decision=PENDING_APPROVAL/*"]
    }
  }

  # The candidate, its report sidecar and its extraction, which is what a reconstructed control
  # is built from. Read-only, and only under the pending prefix: this stage must not be able to
  # read a REJECTED candidate it would then offer.
  statement {
    sid    = "ReadAPendingCandidateToOfferIt"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket_arn}/facts-candidates/decision=PENDING_APPROVAL/*"]
  }

  # WHAT LETS IT SIGN A LINK. A SigV4 presigned URL is only honoured if the signing principal
  # actually holds the permission being signed for -- the signature carries the identity, and
  # AWS evaluates that identity's policy at request time. So this grant IS the link's
  # authority, and its narrowness is the link's narrowness: one action on one function.
  #
  # It does NOT let this role approve anything. `approve-link` refuses a request without a
  # valid single-use grant, and the grants are minted by this stage and mailed away -- so
  # holding this permission without holding a nonce achieves nothing.
  statement {
    sid       = "SignApprovalLinks"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunctionUrl"]
    resources = [aws_lambda_function.approve_link.arn]
  }

  # DELIBERATELY ABSENT: states:SendTaskSuccess. This role renders the page and mails the
  # links; it must not be able to decide that a human is finished. If it held that
  # permission the wait would be decorative -- a stage able to resume its own pause is not
  # a pause. That permission is the approve function's, because the person's decision is
  # what resumes the run.
  #
  # READ THE FACT STORE, AND ONLY READ IT. Added when the gate began offering candidates left
  # pending by EARLIER runs: it must not draw a control the approval endpoint would refuse, and
  # whether it would be refused depends on the store. `approve.py::_rebuild_report` re-derives
  # the report with `current=build_fact_store().find(component_id)` rather than hashing the
  # stored one, so once a fact exists the rebuild no longer matches the sidecar the offered
  # `report_digest` came from. Measured against the live bucket: two of three pending candidates
  # rendered controls the endpoint would have refused.
  #
  # WITHOUT THIS THE FILTER FAILS SILENTLY, WHICH IS THE WHOLE RISK. An absent
  # `FACT_STORE_TABLE` does not raise -- `build_fact_store()` returns a store that finds nothing
  # -- so every candidate looks unanswered, the filter never fires, and the dead controls come
  # back with no error in any log. A missing permission here is invisible rather than loud.
  statement {
    sid    = "ReadTheFactStoreToKnowWhatIsAlreadyAnswered"
    effect = "Allow"
    # `Scan` IS THE ONE `find` ACTUALLY USES, and leaving it out cost a whole run to find:
    # `DynamoDbFactStore.find` takes a component id while the table is keyed on component AND
    # series, so it scans -- and `storage/facts.py` catches the denial INSIDE the store and
    # returns None. So a missing Scan does not raise and does not log; every component simply
    # reads as having no fact, the filter never fires, and the page offers controls the endpoint
    # will refuse. `GetItem` and `Query` are kept for the store's other read paths.
    actions   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [local.facts_arn]
  }

  # DELIBERATELY ABSENT: any dynamodb WRITE on the fact table, and any bedrock action. This
  # stage forms no judgement and stores no fact. The read above decides what it may OFFER and
  # is never a step towards writing.
}

resource "aws_iam_role_policy" "approval" {
  name   = "approval-permissions"
  role   = aws_iam_role.approval.id
  policy = data.aws_iam_policy_document.approval.json
}

resource "aws_lambda_function" "approval" {
  function_name = "${local.name_prefix}-approval"
  description   = "Approval stage - pause the run on a page of proposed facts and mail the links"
  role          = aws_iam_role.approval.arn
  handler       = "entrypoints.stages.stage_approval"
  runtime       = "python3.12"
  architectures = ["arm64"]
  timeout       = 120
  memory_size   = 512

  filename         = "${path.module}/../../../../build/research.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/research.zip")

  environment {
    variables = {
      DOCUMENT_BUCKET      = local.document_bucket
      APPROVAL_TOKEN_TABLE = aws_dynamodb_table.approval_tokens.name
      # Which candidates are already answered, so none is offered with a control the endpoint
      # would refuse. Unset, this reads as "nothing is answered" WITHOUT failing -- see the
      # note on the role's fact-store statement.
      FACT_STORE_TABLE = local.fact_store_table
      # The IAM-signed endpoint, rendered into the PAGE as a curl command: the break-glass
      # path for an operator who would rather not use the email.
      APPROVE_ENDPOINT    = aws_lambda_function_url.approve.function_url
      APPROVAL_EMAIL_FROM = var.approval_email_from
      APPROVAL_EMAIL_TO   = join(",", var.approval_email_to)
    }
  }

  depends_on = [aws_cloudwatch_log_group.approval]
}

# ---------------------------------------------------------------------------
# The link endpoint
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "approve_link" {
  name              = "/aws/lambda/${local.name_prefix}-approve-link"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_iam_role" "approve_link" {
  name               = "${local.name_prefix}-approve-link"
  description        = "Redeems a one-time approval link. Writes no fact; invokes the one writer."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "approve_link" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.approve_link.arn}:*"]
  }

  # Redeeming a grant is an UpdateItem with a condition. GetItem is not granted: this role
  # has no reason to read a paused run it is not consuming a grant from, and `consume_grant`
  # returns the grant out of `ReturnValues` rather than reading it first.
  statement {
    sid       = "RedeemAGrant"
    effect    = "Allow"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.approval_tokens.arn]
  }

  # The whole of what it can do once a grant is redeemed. THIS IS WHY THIS FUNCTION IS NOT A
  # SECOND WRITER OF FACTS: it holds no PutItem on the fact table and no PutObject anywhere.
  # It can invoke exactly one function, and that function is the one that has always been
  # the only writer -- so the argument for that writer does not have to be made twice.
  statement {
    sid       = "InvokeTheOnlyWriterOfFacts"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.approve.arn]
  }

  # DELIBERATELY ABSENT: states:SendTaskSuccess. Resuming the run happens inside the approve
  # function on the `done` action, so this role does not need it and does not get it. One
  # principal holds that permission, not two.
}

resource "aws_iam_role_policy" "approve_link" {
  name   = "approve-link-permissions"
  role   = aws_iam_role.approve_link.id
  policy = data.aws_iam_policy_document.approve_link.json
}

resource "aws_lambda_function" "approve_link" {
  function_name = "${local.name_prefix}-approve-link"
  description   = "Redeem a one-time approval link from the email - authorises on the token, not on IAM"
  role          = aws_iam_role.approve_link.arn
  handler       = "entrypoints.stages.approve_from_link"
  runtime       = "python3.12"
  architectures = ["arm64"]

  # LONGER THAN THE FUNCTION IT WAITS ON, which the approve function's 60s is. This one
  # consumes the grant and then invokes `approve` synchronously, so approve's whole run
  # happens inside this budget. At 60s both raced: a slow approve made this one time out and
  # render "that did not go through" while approve carried on and WROTE THE FACT -- a false
  # negative on a real write, with the link already spent. See `_INVOKE_CONFIG` in
  # `stages/approve_link.py`, which pins the client's wait at 150s between the two.
  timeout     = 180
  memory_size = 512

  filename         = "${path.module}/../../../../build/research.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/research.zip")

  environment {
    variables = {
      APPROVAL_TOKEN_TABLE  = aws_dynamodb_table.approval_tokens.name
      APPROVE_FUNCTION_NAME = aws_lambda_function.approve.function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.approve_link]
}

# AWS_IAM, NOT NONE, AND THE EARLIER CLAIM ABOVE THIS RESOURCE WAS WRONG. It said a link in
# an email cannot carry a SigV4 signature, so an authenticated URL would be a control that
# silently fails. That is false: a SigV4 QUERY signature goes in the query string, which is
# exactly what a link is made of. Verified against this account -- a presigned GET to an
# AWS_IAM function URL returns 200.
#
# The `NONE` version was refused by an org-level guardrail, and correctly. It answered every
# request with
#
#   HTTP 403 {"Message":"Forbidden. For troubleshooting Function URL authorization ..."}
#
# while its own resource policy said `Allow` to `Principal: *` -- the signature of a denial
# above the account. A security scanner raised it as world-accessible, which it was.
#
# What replaces it is STRONGER than what it had, not a compromise: AWS verifies the signature
# before the function is invoked at all, and the signature is bound to one method, path and
# query string, so the link cannot be edited into a different action or a different candidate.
# The single-use nonce is KEPT on top. Two independent gates -- the signature decides whether
# the request arrives, the grant decides what it may do and that it may do it once.
resource "aws_lambda_function_url" "approve_link" {
  function_name      = aws_lambda_function.approve_link.function_name
  authorization_type = "AWS_IAM"
}

# WHO MAY REACH THIS URL. An allowlist of named roles, and `Principal: "*"` is GONE -- that
# statement is what made the function world-accessible, and removing it fixes the security-scanner
# finding at its root rather than suppressing the alarm.
#
# The approval stage is named unconditionally because it mints and signs the links.
# `approve_link_role_arns` adds deployment-specific principals: a human's own role, a
# break-glass role, an operator group. It is a variable rather than a hardcoded ARN because a
# published copy of this project must not carry somebody else's role.
#
# REACHING THE ENDPOINT IS NOT APPROVING. Every principal here still has to present a valid
# single-use grant, and the grants are minted by the stage and mailed away -- so this
# allowlist decides who may knock, and the token decides what may happen. That separation is
# why widening this list is not a widening of who can approve a fact.
locals {
  approve_link_principals = merge(
    { "TheApprovalStage" = aws_iam_role.approval.arn },
    {
      # Keyed by role name so a statement id is stable and legible. Two roles with the same
      # name in different accounts would collide, which is not a case this deployment has.
      for arn in var.approve_link_role_arns :
      replace(regex("role/(.+)$", arn)[0], "/[^0-9A-Za-z]/", "") => arn
    }
  )
}

resource "aws_lambda_permission" "approve_link_url" {
  for_each = local.approve_link_principals

  statement_id           = "AllowInvoke${each.key}"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.approve_link.function_name
  principal              = each.value
  function_url_auth_type = "AWS_IAM"
}

# ---------------------------------------------------------------------------
# SES identities
# ---------------------------------------------------------------------------
#
# ONE CLICK IS UNAVOIDABLE HERE, AND TERRAFORM CANNOT REMOVE IT. Creating an
# `aws_ses_email_identity` makes AWS mail a verification link to that address, and the
# identity stays `Pending` until a human opens it. There is no API to self-approve, no
# argument to skip it, and no amount of Terraform that changes it -- the click is what
# proves the mailbox is real, so an API that bypassed it would defeat the mechanism.
#
# THE ONE SHAPE THAT IS FULLY AUTOMATABLE is a DOMAIN identity, verified by DKIM CNAME
# records Terraform writes into the zone (`aws_sesv2_email_identity` +
# `aws_route53_record`). It needs control of the DNS zone, and a deployment that mails from a
# corporate domain usually does not control that zone. So the address route is the only
# one available and the click comes with it.
#
# WHAT IS AUTOMATED, so the manual part is exactly one action and not a checklist: the
# identity resources, the address, the IAM grant bounded to that From address, and the
# environment wiring are all declared. After apply, one link in one inbox.
#
# AND IT IS ONE EMAIL, NOT TWO. The sandbox requires both ends verified, and `_recipients`
# below drops the From address from the recipient set -- so a deployment where sender and
# recipient are the same person, as `terraform.tfvars.example` shows, creates a single
# identity. Without the `setsubtract` this planned two resources for one address: harmless
# to apply, but it mails the same person twice and reads like a bug.

locals {
  # Recipients that are not already covered by the sender identity above.
  approval_recipient_identities = setsubtract(
    toset(var.approval_email_to), toset([var.approval_email_from])
  )
}

resource "aws_ses_email_identity" "approval_from" {
  email = var.approval_email_from
}

resource "aws_ses_email_identity" "approval_to" {
  for_each = local.approval_recipient_identities
  email    = each.value
}

# ---------------------------------------------------------------------------
# The decision watcher: a click, arriving as an S3 object
# ---------------------------------------------------------------------------
#
# THIS IS WHAT MAKES THE APPROVAL PAGE'S BUTTONS REAL, after two designs that could not be.
# The page carries presigned S3 POST forms; a browser submits one on click and writes a few
# bytes under `approval-decisions/`; that arrival triggers this function, which redeems the
# one-time grant named in the key and invokes `approve`.
#
# WHY THIS SHAPE AND NOT AN ENDPOINT. Both endpoint designs died on constraints outside this
# project. A `NONE` function URL is world-accessible -- a security scanner raised it and an org
# guardrail refused every request. An `AWS_IAM` URL reached by a presigned link satisfies the
# guardrail and is unusable, because Lambda function URLs cap SigV4 freshness at five minutes
# and ignore `X-Amz-Expires`; measured at 200 on t+0s and 403 "Signature expired" at t+330s,
# with the clock starting when the mail was SENT.
#
# S3 has neither problem: the credential travels in the form BODY, S3 honours the policy's own
# expiry, and the thing being written to is a PRIVATE BUCKET. There is no internet-facing
# endpoint in this path at all, which is why it is also the most defensible of the three.
#
# WHAT THE BROWSER MAY DO, pinned by the presigned policy: write ONE object at ONE key, 1-256
# bytes. It cannot read, list, overwrite anything else, or point the form at another candidate
# -- the key carries the run and the nonce and the signature covers the key.

resource "aws_cloudwatch_log_group" "decision_watcher" {
  name              = "/aws/lambda/${local.name_prefix}-decision-watcher"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_iam_role" "decision_watcher" {
  name               = "${local.name_prefix}-decision-watcher"
  description        = "Turns a clicked decision object into an approval. Writes no fact."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "decision_watcher" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.decision_watcher.arn}:*"]
  }

  # Redeem the grant named in the key. UpdateItem only -- the conditional delete inside
  # `consume_grant` returns the grant out of ReturnValues, so there is no reason to grant a
  # read of a paused run this function is not consuming from.
  statement {
    sid       = "RedeemAGrant"
    effect    = "Allow"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.approval_tokens.arn]
  }

  # NOT A SECOND WRITER OF FACTS. No PutItem on the fact table, no PutObject anywhere. It can
  # invoke exactly one function, and that function has always been the only writer -- so the
  # security argument for that writer does not have to be made twice.
  statement {
    sid       = "InvokeTheOnlyWriterOfFacts"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.approve.arn]
  }

  # DELIBERATELY ABSENT: s3:GetObject on the decision object. The body is never read -- every
  # field that decides what happens comes from the stored grant, so a decision cannot name a
  # candidate it was not issued for. Reading the body would create exactly the trust this
  # design avoids.
  #
  # DELIBERATELY ABSENT: states:SendTaskSuccess. Resuming happens inside `approve` on the
  # `done` action, so one principal holds that permission rather than two.
}

resource "aws_iam_role_policy" "decision_watcher" {
  name   = "decision-watcher-permissions"
  role   = aws_iam_role.decision_watcher.id
  policy = data.aws_iam_policy_document.decision_watcher.json
}

resource "aws_lambda_function" "decision_watcher" {
  function_name = "${local.name_prefix}-decision-watcher"
  description   = "S3-triggered: redeem a clicked decision and invoke the approver"
  role          = aws_iam_role.decision_watcher.arn
  handler       = "entrypoints.stages.approve_from_page"
  runtime       = "python3.12"
  architectures = ["arm64"]

  # Longer than the 60s `approve` it waits on, for the reason `approve_link` carries the same
  # note: a caller that gives up first turns a completed fact write into a reported failure.
  timeout     = 180
  memory_size = 512

  filename         = "${path.module}/../../../../build/research.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/research.zip")

  environment {
    variables = {
      APPROVAL_TOKEN_TABLE  = aws_dynamodb_table.approval_tokens.name
      APPROVE_FUNCTION_NAME = aws_lambda_function.approve.function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.decision_watcher]
}

resource "aws_lambda_permission" "decision_watcher_s3" {
  statement_id   = "AllowTheDocumentBucketToNotify"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.decision_watcher.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = local.bucket_arn
  source_account = local.account_id
}

# SCOPED TO THE ONE PREFIX A BROWSER MAY WRITE. Deliberately narrow: a notification on the
# whole bucket would fire this function for every snapshot, ledger, candidate and report the
# pipeline writes -- thousands of invocations that all fall through `parse_decision_key` and
# do nothing, burying the handful that matter.
#
# NOTE FOR WHOEVER ADDS A SECOND NOTIFICATION: `aws_s3_bucket_notification` manages the
# bucket's ENTIRE notification configuration, so a second resource for the same bucket
# silently replaces this one. Add another filter block here instead.
resource "aws_s3_bucket_notification" "decisions" {
  bucket = local.document_bucket

  lambda_function {
    lambda_function_arn = aws_lambda_function.decision_watcher.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "approval-decisions/"
  }

  # THE SECOND FILTER, ADDED HERE FOR THE REASON THE NOTE ABOVE GIVES. A separate
  # `aws_s3_bucket_notification` for this prefix would not add to the configuration -- it would
  # REPLACE it, silently, taking the fact approval's auto-resume with it. So the two prefixes
  # are declared together whether or not their functions are.
  #
  # THE FUNCTION IS NOT THIS MODULE'S. `infra/terraform/modules/platform` creates the upgrade approval stage
  # and its watcher, and grants S3 permission to invoke the watcher there, beside the function.
  # Only the notification has to live here, so the ARN arrives as a variable.
  #
  # NULL LEAVES THE CONFIGURATION EXACTLY AS IT IS, which is what makes this additive: a branch
  # or a deployment that does not know about the upgrade stage plans no change to the live
  # notification at all.
  #
  # ORDER MATTERS ON FIRST APPLY. S3 validates that it may invoke each named function when the
  # configuration is written, so platform must be applied before this. Expressing that as a
  # `depends_on` is not possible across two states -- hence the sentence rather than the code.
  dynamic "lambda_function" {
    for_each = var.upgrade_decision_watcher_arn == null ? [] : [var.upgrade_decision_watcher_arn]

    content {
      lambda_function_arn = lambda_function.value
      events              = ["s3:ObjectCreated:*"]
      filter_prefix       = "upgrade-clicks/"
    }
  }

  depends_on = [aws_lambda_permission.decision_watcher_s3]
}
