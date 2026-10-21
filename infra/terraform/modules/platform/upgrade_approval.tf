# The upgrade approval: the stage that asks whether we may change THIS cluster, and the
# watcher that records what a person answered.
#
# CREATED IN THIS MODULE RATHER THAN BEHIND AN ARN VARIABLE, which is `sweep.tf`'s
# arrangement and not the one `research`, `resolve` and `approval` use. Those three are gated
# on a variable that defaults to null, and that default is a trap the deploy root closes by
# wiring them: a defaults-only apply silently un-wires a live stage, and no
# destroy/replace guard catches it. A stage created here has no ARN to withhold and no way to
# leave out, so the trap does not exist for it. For the one stage that stands between a plan
# and somebody changing a production cluster, that is the property worth having.
#
# TWO FUNCTIONS, TWO ROLES, AND THE SPLIT IS THE SECURITY ARGUMENT.
#
#   upgrade_approval          renders, mints capabilities, stores the token, mails a link.
#                             NO `states:SendTaskSuccess`. A state machine able to resume its
#                             own wait makes the human boundary decorative.
#   upgrade_decision_watcher  redeems one capability, writes the decision, and IS the thing
#                             that resumes. It is reached only by an object arriving in S3,
#                             which is to say only by a person acting.
#
# Neither holds any Bedrock action. Neither reads the fact store. An upgrade decision is about
# one cluster and writes nothing the rest of the fleet can see.

# ---------------------------------------------------------------------------
# The token store
# ---------------------------------------------------------------------------

# A SECOND TABLE, NOT A SECOND KEY SPACE IN THE FIRST, and this is the one piece of
# infrastructure that exists purely because one execution now pauses TWICE. Both waits are
# handed a task token for the SAME execution, and `TokenStore` keys its rows on `run_id`
# alone -- so one table would have the upgrade pause overwrite the token the fact pause was
# waiting on, and the visible symptom would be a fact approval that can never be resumed.
#
# Same schema as `-approval-tokens` deliberately: one hash key, one TTL attribute. Two shapes
# would be two things to keep in step for no gain.
resource "aws_dynamodb_table" "upgrade_tokens" {
  #checkov:skip=CKV_AWS_28:Single-use approval grants with a TTL. Restoring a backup would resurrect spent capabilities.
  name = "${local.name_prefix}-upgrade-tokens"

  # PAY_PER_REQUEST for the fact table's reason: a handful of items per run and long idle
  # gaps between runs. Provisioned capacity would bill continuously for a table written once
  # and read a few times per execution.
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"

  attribute {
    name = "run_id"
    type = "S"
  }

  # HOUSEKEEPING, NOT THE APPROVAL DEADLINE. The execution fails on
  # `upgrade_approval_timeout_seconds` (2h by default) long before this fires. This exists so
  # an abandoned run's row does not sit forever holding a token nobody can use.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  # Encrypt at rest with the AWS-owned DynamoDB key, matching the facts table in storage.tf.
  # A short-lived single-use token store does not warrant a per-organisation CMK, so this
  # mirrors `aws_dynamodb_table.facts` rather than taking the logs CMK: default SSE still
  # satisfies "DynamoDB Table Not Encrypted". A CMK here is left to the deployer (CKV_AWS_119).
  server_side_encryption {
    enabled = true
  }
}

# ---------------------------------------------------------------------------
# The stage that asks
# ---------------------------------------------------------------------------

resource "aws_iam_role" "upgrade_approval" {
  name               = "${local.name_prefix}-upgrade-approval"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "upgrade_approval" {
  name              = "/aws/lambda/${local.name_prefix}-upgrade-approval"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.log_kms_key_arn
}

data "aws_iam_policy_document" "upgrade_approval" {
  statement {
    sid       = "Logs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.upgrade_approval.arn}:*"]
  }

  # Read the four documents the report is built from, the brief, and the snapshot. Six prefixes
  # and no others: this stage renders what earlier stages decided and derives nothing of its own.
  #
  # `snapshots/*` IS FOR THE ADD-ON CATALOGUE, not for re-reading the cluster. Stage 1 captured
  # `describe-addon-versions` into the snapshot, and this stage reads it to WITHHOLD the control
  # on a managed-add-on step whose build AWS does not publish for the minor the cluster runs --
  # e.g. kube-proxy `v1.36.0-eksbuild.25` offered on a 1.35 cluster, which EKS refuses.
  # The lane planner reads the same catalogue from the same object for the same decision.
  statement {
    sid    = "ReadTheDocumentsTheReportIsBuiltFrom"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.documents.arn}/inventories/*",
      "${aws_s3_bucket.documents.arn}/gaps/*",
      "${aws_s3_bucket.documents.arn}/decisions/*",
      "${aws_s3_bucket.documents.arn}/plans/*",
      "${aws_s3_bucket.documents.arn}/briefs/*",
      "${aws_s3_bucket.documents.arn}/snapshots/*",
    ]
  }

  # Write the page, and nowhere else. Its own prefix rather than `approvals/`, because an S3
  # notification on that one drives the FACT watcher -- a second kind of page arriving there
  # would be handed to code that knows nothing about steps.
  statement {
    sid       = "WriteTheApprovalPage"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/upgrade-approvals/*"]
  }

  # READ ITS OWN PAGE BACK, so the email can carry a presigned link to it. A SigV4 presigned URL
  # is only honoured if the SIGNING principal holds the action being signed for -- AWS evaluates
  # that identity's policy at request time -- so without this the stage signs a link it may not
  # use and the recipient gets a 403 naming this role. Which is precisely what the first mailed
  # link did.
  statement {
    sid    = "PresignTheApprovalPage"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/upgrade-approvals/*"]
  }

  # WHAT MAKES THE BUTTONS WORK, and its absence is the quietest failure on this page. Every
  # Approve, Reject and Done control is a presigned S3 POST to `upgrade-clicks/` signed by this
  # role, so the rule above applies again: with no PutObject here all sixteen controls render
  # perfectly and every one fails on click, with the error going to the browser rather than to
  # any log this project reads.
  #
  # PutObject ONLY, and only on that prefix. This role must not read a click back, and must not
  # reach `upgrade-decisions/` -- that belongs to the watcher, for the reason stated at the foot
  # of this policy: a stage able to write a decision could approve on a person's behalf.
  statement {
    sid       = "SignTheDecisionButtons"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/upgrade-clicks/*"]
  }

  # Read the fact store, to resolve what each component's recorded knowledge says. Read only:
  # this stage writes no fact and must not be able to.
  statement {
    sid       = "ReadTheFactStore"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [aws_dynamodb_table.facts.arn]
  }
  # And the marks over it, so `build_fact_store()` here answers exactly as it does in `Plan`:
  # a fact the sweep withheld is invisible to both. `release_record` reads the approved fact's
  # chart onto the release; a withheld fact must not hand the lane a chart the plan did not use.
  statement {
    sid       = "ReadMarks"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
    resources = [aws_dynamodb_table.fact_marks.arn]
  }

  # Hold the paused run: the task token, the offer, and one grant per control.
  statement {
    sid       = "HoldThePausedUpgrade"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.upgrade_tokens.arn]
  }

  # Mail the link. Scoped to this deployment's SES sender identity ARN, with the
  # `ses:FromAddress` condition kept as defence in depth.
  dynamic "statement" {
    for_each = var.approval_email_from == "" ? [] : [var.approval_email_from]

    content {
      sid       = "MailTheApprovalLink"
      effect    = "Allow"
      actions   = ["ses:SendEmail", "ses:SendRawEmail"]
      resources = [local.ses_sender_identity_arn]

      condition {
        test     = "StringEquals"
        variable = "ses:FromAddress"
        values   = [statement.value]
      }
    }
  }

  # READ WHO MAY AUTHORISE A RELEASE, WHICH IS THE CLUSTER'S ANSWER AND NOT THIS REPOSITORY'S.
  #
  # THREE ACTIONS, ALL READS, AND NO FOURTH. The release gate's `Approvers` list used to be a
  # hand-kept `var.release_approver_arns`; it is now resolved per run from the cluster's access
  # entries by `stages/release_prep.py`, invoked in THIS function with a `release_prep` payload --
  # see `entrypoints.stages.stage_upgrade_approval`. That is why the grant lands on this role: no Lambda is added
  # for the resolver, and this is the only existing function whose role may already read `plans/`,
  # which is where the change lines the mail names come from.
  #
  # TWO RESOURCE SHAPES, BECAUSE THE THREE CALLS DO NOT AUTHORISE ON THE SAME THING.
  # `ListAccessEntries` authorises on the CLUSTER arn; `DescribeAccessEntry` and
  # `ListAssociatedAccessPolicies` authorise on the ACCESS-ENTRY arn, which embeds a per-entry
  # UUID generated by EKS -- `...:access-entry/<cluster>/role/<uuid>`. That id is unknowable at
  # plan time and changes when an entry is recreated, so a wildcard beneath `access-entry/` is the
  # narrowest grant that can work. Naming a literal entry would be a resolver that breaks the day
  # somebody re-adds an admin.
  #
  # SCOPED TO NAMED CLUSTERS, NOT TO `*`, and the names come from a variable because THIS MODULE
  # DOES NOT OTHERWISE KNOW A CLUSTER'S NAME. The cluster a run is about arrives in the execution
  # input, and per-cluster IAM lives in `infra/terraform/cluster-access` -- see `ReadThisClusterOnly`
  # there, and the note beside this module's read-role reference in `lambdas.tf`: "so this module
  # needs to know nothing". `cluster/*` would be the no-variable alternative and it is worse: a
  # resolver able to read ANY cluster's access entries can be pointed at another cluster's admins
  # by a payload, and the gate would then ask people who hold no authority over the cluster being
  # changed.
  #
  # WHY A LIST HERE IS NOT THE DRIFT `release_approver_arns` WAS DELETED FOR, which is the obvious
  # objection and worth answering in place. That variable named WHO MAY APPROVE, so a stale entry
  # meant a real authorisation the cluster no longer agreed with, and nothing reported it. This one
  # names WHICH CLUSTERS MAY BE READ, and a stale entry is an `AccessDeniedException` on
  # `ListAccessEntries` -- which the resolver does not swallow, so the run fails at
  # `Release rejected` naming the call. Drift here fails closed and loudly; drift there granted
  # silently.
  #
  # NO WRITE, EVER. `CreateAccessEntry`, `AssociateAccessPolicy` and their kin are absent and must
  # stay absent: a principal that could grant itself cluster admin could then authorise its own
  # release, which is the entire boundary this gate exists to draw.
  statement {
    sid    = "ReadWhoMayAuthoriseARelease"
    effect = "Allow"
    actions = [
      "eks:ListAccessEntries",
      "eks:DescribeAccessEntry",
      "eks:ListAssociatedAccessPolicies",
    ]
    resources = flatten([
      for name in var.release_gate_cluster_names : [
        "arn:${data.aws_partition.current.partition}:eks:${var.region}:${local.account_id}:cluster/${name}",
        "arn:${data.aws_partition.current.partition}:eks:${var.region}:${local.account_id}:access-entry/${name}/*",
      ]
    ])
  }

  # DELIBERATELY ABSENT: `states:SendTaskSuccess`. This stage cannot resume its own run. The
  # permission is on the watcher, which is reached only by a person acting.
  #
  # DELIBERATELY ABSENT: any `bedrock:` action. Nothing here calls a model.
  #
  # DELIBERATELY ABSENT: `s3:PutObject` on `upgrade-decisions/`. The stage asks; the watcher
  # records. A stage able to write a decision could approve on a person's behalf.
  #
  # DELIBERATELY ABSENT: `s3:PutObject` on `releases/`. Resolving who may authorise and recording
  # what they authorised are different jobs. The release object is written by the state machine
  # itself, after SSM reported success -- see `RecordTheRelease` in workflow.tf.
  #
  # DELIBERATELY ABSENT: `eks:DescribeCluster` and every other cluster read. Only `scan` reaches a
  # cluster for its contents; the three actions above read an IAM-shaped access list, not the
  # Kubernetes API, and nothing here holds a kubeconfig or a cluster token.
}

resource "aws_iam_role_policy" "upgrade_approval" {
  name   = "upgrade-approval-permissions"
  role   = aws_iam_role.upgrade_approval.id
  policy = data.aws_iam_policy_document.upgrade_approval.json
}

resource "aws_lambda_function" "upgrade_approval" {
  function_name = "${local.name_prefix}-upgrade-approval"
  description   = "Upgrade approval - render the plan and its brief, ask per cluster, wait."
  role          = aws_iam_role.upgrade_approval.arn
  handler       = "entrypoints.stages.stage_upgrade_approval"
  runtime       = "python3.12"
  architectures = [var.lambda_architecture]

  # Longer than the five document stages. It reads five documents, renders a page that runs to
  # ~180KB against a real seven-step plan, presigns two forms per step and sends a mail -- all
  # sequential, and the presigning is a round trip each.
  timeout     = 120
  memory_size = 512

  # stages.zip: entrypoints/stages.py plus pydantic, which is everything this handler needs. It calls no
  # model and reaches no GitHub, so it does not want research.zip's extra weight.
  filename         = "${path.module}/../../../../build/stages.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/stages.zip")

  # MERGE, NOT A LITERAL MAP, for `sweep.tf`'s reason: Terraform replaces this map wholesale
  # on every apply, so anything set out of band is dropped silently. A literal map is how a
  # hand-set variable vanishes with nothing reporting the loss.
  environment {
    variables = merge(
      {
        DOCUMENT_BUCKET     = aws_s3_bucket.documents.id
        READINESS_BUCKET    = aws_s3_bucket.documents.id
        FACT_STORE_TABLE    = aws_dynamodb_table.facts.name
        FACT_MARK_TABLE     = local.fact_mark_table
        UPGRADE_TOKEN_TABLE = aws_dynamodb_table.upgrade_tokens.name
        APPROVAL_EMAIL_FROM = var.approval_email_from
        APPROVAL_EMAIL_TO   = join(",", var.approval_email_to)
      },
    )
  }

  depends_on = [
    aws_iam_role_policy.upgrade_approval,
    aws_cloudwatch_log_group.upgrade_approval,
  ]
}

# ---------------------------------------------------------------------------
# The watcher that records the answer
# ---------------------------------------------------------------------------

resource "aws_iam_role" "upgrade_decision_watcher" {
  name               = "${local.name_prefix}-upgrade-decision"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "upgrade_decision_watcher" {
  name              = "/aws/lambda/${local.name_prefix}-upgrade-decision"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.log_kms_key_arn
}

data "aws_iam_policy_document" "upgrade_decision_watcher" {
  statement {
    sid       = "Logs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.upgrade_decision_watcher.arn}:*"]
  }

  # Redeem one capability and note the decision against the paused run. `UpdateItem` is what
  # makes redemption single-use: the condition and the removal are one call, so two clicks on
  # one link cannot both succeed.
  statement {
    sid       = "RedeemOneCapability"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
    resources = [aws_dynamodb_table.upgrade_tokens.arn]
  }

  # Write the durable decision, and read it back to assemble the run summary. Its own prefix,
  # so no stage's grant reaches a recorded decision and this handler cannot write anywhere a
  # stage's output lives.
  statement {
    sid    = "RecordTheDecision"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/upgrade-decisions/*"]
  }

  # RESUME THE EXECUTION. This is the only role in this module that holds it, and holding it
  # here rather than on the stage is the whole boundary: a person's decision is what continues
  # the pipeline, so the permission sits on the path that decision travels down.
  #
  # No `SendTaskFailure` and no `SendTaskHeartbeat`. A rejection is a decision the run carries
  # forward, not a failure of the run -- the state's own timeout is what fails an unattended
  # one, and that needs no permission at all.
  statement {
    sid       = "ResumeTheRunOnce"
    effect    = "Allow"
    actions   = ["states:SendTaskSuccess"]
    resources = ["*"]
  }

  # DELIBERATELY ABSENT: `s3:GetObject` on the click prefix. The object's body is never read.
  # Everything that decides what happens comes from the grant named in the key, which is why a
  # form submitted for a key nobody was given achieves nothing.
  #
  # DELIBERATELY ABSENT: any access to the fact store. An upgrade decision is about one
  # cluster; a handler that could write a fact from one would make it fleet-wide.
}

resource "aws_iam_role_policy" "upgrade_decision_watcher" {
  name   = "upgrade-decision-permissions"
  role   = aws_iam_role.upgrade_decision_watcher.id
  policy = data.aws_iam_policy_document.upgrade_decision_watcher.json
}

resource "aws_lambda_function" "upgrade_decision_watcher" {
  function_name = "${local.name_prefix}-upgrade-decision"
  description   = "Record one upgrade decision from an S3 click; on Done, resume the run."
  role          = aws_iam_role.upgrade_decision_watcher.arn
  handler       = "entrypoints.stages.upgrade_decision"
  runtime       = "python3.12"
  architectures = [var.lambda_architecture]

  # Short. It redeems one grant, writes one small object, and on Done reads back at most a
  # handful more before one `SendTaskSuccess`.
  timeout     = 60
  memory_size = 256

  filename         = "${path.module}/../../../../build/stages.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/stages.zip")

  environment {
    variables = {
      READINESS_BUCKET    = aws_s3_bucket.documents.id
      DOCUMENT_BUCKET     = aws_s3_bucket.documents.id
      UPGRADE_TOKEN_TABLE = aws_dynamodb_table.upgrade_tokens.name
    }
  }

  depends_on = [
    aws_iam_role_policy.upgrade_decision_watcher,
    aws_cloudwatch_log_group.upgrade_decision_watcher,
  ]
}

# S3's permission to invoke the watcher. Scoped to this bucket and this account, so no other
# bucket -- and no other account's bucket of the same name -- can reach it.
#
# GRANTED HERE, WHERE THE FUNCTION IS, while the notification itself lives in
# infra/terraform/modules/research beside the configuration it has to share. See the note on that resource:
# `aws_s3_bucket_notification` manages a bucket's ENTIRE configuration, so the two prefixes
# have to be declared together in one resource. This module must therefore be applied before
# that notification is, or S3 rejects the configuration for a function it may not invoke.
resource "aws_lambda_permission" "upgrade_decision_watcher_s3" {
  statement_id   = "AllowS3ToInvokeOnAnUpgradeClick"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.upgrade_decision_watcher.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.documents.arn
  source_account = data.aws_caller_identity.current.account_id
}

output "upgrade_decision_watcher_arn" {
  description = <<-EOT
    ARN of the upgrade click watcher, for infra/terraform/modules/research's bucket notification.

    That module owns `aws_s3_bucket_notification.decisions`, which manages the bucket's whole
    notification configuration -- so the `upgrade-clicks/` filter has to be declared there
    alongside the `approval-decisions/` one rather than in a second resource here, which would
    silently replace it.
  EOT
  value       = aws_lambda_function.upgrade_decision_watcher.arn
}
