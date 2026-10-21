# ONE customer-managed KMS key, shared by every CloudWatch log group this sample creates.
#
# WHY ONE KEY AND WHY HERE. Log groups live in four modules (platform, research, brief,
# upgrade), but a single key encrypts them all: a log group is encrypted by the CloudWatch
# Logs SERVICE on the deployer's behalf, so the only principal that has to be in the key
# policy is `logs.<region>.amazonaws.com` -- not Lambda, not CodeBuild, not Step Functions,
# not AgentCore. Each of those produces log events, but every one of them is written through
# the Logs service, which is the identity that calls KMS. One key, one grant.
#
# It is created in `platform` because platform owns the other shared resources (the document
# bucket, the fact table) and the other modules already derive names from platform's prefix.
# The ARN is published as an output (outputs.tf) and the deploy root passes it to the three
# other modules as `log_kms_key_arn`, the same wiring the bucket and fact table already use.
#
# WHAT IT COSTS. A CMK is about $1/month plus $0.03 per 10,000 API calls. CloudWatch Logs
# caches the data key, so steady-state request cost is negligible for a sample.

resource "aws_kms_key" "logs" {
  description             = "${local.name_prefix} CloudWatch Logs encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 7

  # The key policy. Two statements and no more:
  #   1. Account root keeps full administrative control, so the key is manageable and never
  #      orphaned -- the AWS-recommended baseline for any CMK.
  #   2. The CloudWatch Logs service principal for THIS region may use the key, but only to
  #      encrypt/decrypt log data, and only for log groups in THIS account and region. The
  #      `kms:EncryptionContext:aws:logs:arn` condition is what scopes it: CloudWatch Logs
  #      passes the log-group ARN as encryption context on every call, so a `logs.*` principal
  #      in another account cannot borrow this key.
  policy = data.aws_iam_policy_document.logs_kms.json
}

resource "aws_kms_alias" "logs" {
  name          = "alias/${local.name_prefix}-logs"
  target_key_id = aws_kms_key.logs.key_id
}

data "aws_iam_policy_document" "logs_kms" {
  # A KMS KEY POLICY'S RESOURCE IS ALWAYS "*", MEANING "THIS KEY". A key policy is attached to
  # the key itself, so `Resource = ["*"]` is scoped to that one key by definition -- it cannot
  # name another. Checkov's generic IAM-policy checks do not model that, so they are skipped
  # here with the same inline convention the release-gate and workflow policy documents use.
  #checkov:skip=CKV_AWS_356:A KMS key policy's resource is the key it is attached to; "*" means this key only.
  #checkov:skip=CKV_AWS_111:Account-root admin on a KMS key policy is the AWS-recommended baseline so the key is never orphaned.
  #checkov:skip=CKV_AWS_109:The CloudWatch Logs grant is constrained by the aws:logs:arn encryption-context condition to this account and region.
  statement {
    # Account root administers the key, so it is never orphaned. The actions are the
    # key-administrator set from the AWS default key policy, listed instead of `kms:*`:
    # nobody but the CloudWatch Logs service below needs to encrypt or decrypt with this key
    # (reading a log group goes through Logs, which decrypts as itself), and a wildcard
    # Allow without a condition is what kics query 7ebc9038 ("KMS Key With Vulnerable Policy")
    # reports.
    sid    = "EnableAccountRootAdmin"
    effect = "Allow"
    actions = [
      "kms:Create*",
      "kms:Describe*",
      "kms:Enable*",
      "kms:List*",
      "kms:Put*",
      "kms:Update*",
      "kms:Revoke*",
      "kms:Disable*",
      "kms:Get*",
      "kms:Delete*",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:ScheduleKeyDeletion",
      "kms:CancelKeyDeletion",
    ]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid    = "AllowCloudWatchLogs"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }

    actions = [
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:ReEncrypt*",
      "kms:GenerateDataKey*",
      "kms:DescribeKey",
    ]
    resources = ["*"]

    # Scope the grant to log groups in THIS account and region. CloudWatch Logs sets
    # `aws:logs:arn` as the encryption context on every KMS call it makes for a log group, so
    # this is what stops the key being usable for any log group outside this deployment.
    condition {
      test     = "ArnEquals"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${data.aws_partition.current.partition}:logs:${var.region}:${local.account_id}:log-group:*"]
    }
  }
}

locals {
  # The one place every log group in this module reads its key from, and the value the deploy
  # root forwards to the other three modules.
  log_kms_key_arn = aws_kms_key.logs.arn
}
