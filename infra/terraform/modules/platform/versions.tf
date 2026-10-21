terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # Every name derives from one prefix plus the account and region, so two
  # deployments in the same account never collide and nothing is hardcoded.
  name_prefix = var.name_prefix
  bucket_name = coalesce(var.document_bucket_name, "${var.name_prefix}-${local.account_id}-${var.region}-an")
  table_name  = "${var.name_prefix}-facts"

  # The server-access-log bucket's name. Keeps the account-regional suffix the namespace
  # requires (`-<account>-<region>-an`) with `-logs` before it, so it is collision-proof and
  # never clashes with the document bucket's own name.
  access_log_bucket_name = "${var.name_prefix}-logs-${local.account_id}-${var.region}-an"

  # The SES identity this deployment sends as. Every mail grant in this module targets this ARN,
  # never "*", and keeps the `ses:FromAddress` condition as defence in depth.
  ses_sender_identity_arn = "arn:${data.aws_partition.current.partition}:ses:${var.region}:${local.account_id}:identity/${var.approval_email_from}"
}
