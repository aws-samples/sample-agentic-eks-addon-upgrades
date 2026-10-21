output "document_bucket" {
  description = "Bucket holding every stage document."
  value       = aws_s3_bucket.documents.id
}

output "fact_store_table" {
  description = "DynamoDB table holding compatibility knowledge."
  value       = aws_dynamodb_table.facts.name
}

output "log_kms_key_arn" {
  description = <<-EOT
    The customer-managed KMS key that encrypts every CloudWatch log group. The deploy root
    passes it to the research, brief and upgrade modules as `log_kms_key_arn` so all four
    modules encrypt their log groups with one key.
  EOT
  value       = aws_kms_key.logs.arn
}

output "state_machine_arn" {
  description = "The readiness workflow. Start a run against this."
  value       = aws_sfn_state_machine.readiness.arn
}

output "stage_function_names" {
  description = "The five stage Lambdas, by stage."
  value       = { for k, _ in local.stages : k => aws_lambda_function.stage[k].function_name }
}

output "scan_role_arn" {
  description = <<-EOT
    The scan stage's execution role. Pass this to the cluster-access module as
    `platform_scan_role_arn` so the cluster's read-only role trusts it.
  EOT
  value       = aws_iam_role.stage["scan"].arn
}

output "start_a_run" {
  description = "Copy-paste command to start a readiness run."
  value = join(" ", [
    "aws stepfunctions start-execution",
    "--state-machine-arn ${aws_sfn_state_machine.readiness.arn}",
    "--input '{\"cluster_name\":\"<CLUSTER>\",\"bucket\":\"${aws_s3_bucket.documents.id}\",\"reader_role_arn\":\"<CLUSTER_READ_ROLE_ARN>\"}'",
  ])
}
