# Stage 6: research, the deterministic judge, and the fact approval surface.
#
# The pipeline reaches Stage 6 through the `Research` Map in infra/terraform/modules/platform's
# state machine, which invokes `<prefix>-research-harness` (harness.tf) once per component.
# The harness proxy calls the AgentCore research Harness, whose only tool is the source-fetch
# Lambda behind the research Gateway (gateway.tf, tools.tf). The judge (tools.tf) then rates the
# extraction against the bytes that were actually fetched, and a person approves the result
# (approval.tf, approval_stage.tf). No fact enters the store without that approval.
#
# WHAT THIS MODULE DOES NOT MANAGE: the document bucket, the fact table and the marks table
# belong to infra/terraform/modules/platform. They appear here only as names and ARNs derived
# from the shared `name_prefix` (versions.tf), in IAM policy documents. There is no
# `aws_s3_bucket` and no `aws_dynamodb_table` resource in this module.
#
# The functions here READ the marks table, so a fact a human revoked does not answer them
# either. The sweep that writes marks is a platform stage (platform/sweep.tf).

# ---------------------------------------------------------------------------
# Shared by every Lambda role in this module.
# ---------------------------------------------------------------------------
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
