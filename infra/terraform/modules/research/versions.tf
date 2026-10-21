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

data "aws_partition" "current" {}

locals {
  account_id  = data.aws_caller_identity.current.account_id
  name_prefix = var.name_prefix

  # The SES identity this deployment sends as. The mail grant targets this ARN, never "*", and
  # keeps the `ses:FromAddress` condition as defence in depth.
  ses_sender_identity_arn = "arn:${data.aws_partition.current.partition}:ses:${var.region}:${local.account_id}:identity/${var.approval_email_from}"

  # The document bucket, resolved the same way the platform module derives it, so
  # the two agree without this module holding the account id in source.
  document_bucket = coalesce(
    var.document_bucket_name,
    "${var.name_prefix}-${local.account_id}-${var.region}-an"
  )

  fact_store_table = coalesce(var.fact_store_table_name, "${var.name_prefix}-facts")

  # DERIVED, NOT OWNED: infra/terraform/modules/platform creates the marks table now, along with the sweep
  # that writes it. The three functions here still READ it -- so a fact a human revoked does
  # not answer them either -- and they address it by the same derivation from the same prefix
  # platform uses, so the two agree by construction rather than by anyone remembering to
  # re-apply. A required cross-component variable would instead make this module unappliable
  # whenever platform had not run, which is the trade already made for the cluster read roles.
  fact_mark_table = "${var.name_prefix}-fact-marks"

  bucket_arn     = "arn:aws:s3:::${local.document_bucket}"
  facts_arn      = "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.fact_store_table}"
  fact_marks_arn = "arn:aws:dynamodb:${var.region}:${local.account_id}:table/${local.fact_mark_table}"
}
