output "cluster_read_role_arn" {
  description = <<-EOT
    This cluster's read-only role. Pass it as `reader_role_arn` when starting a
    run for this cluster.

    NOTHING NEEDS TO CHANGE IN THE PLATFORM MODULE. Its scan role already grants
    sts:AssumeRole on `<name_prefix>-read-*`, which this role's name matches by
    construction. Onboarding is this one apply.
  EOT
  value       = aws_iam_role.cluster_read.arn
}

output "start_a_run" {
  description = "Copy-paste command to start a readiness run against this cluster."
  value = join(" ", [
    "aws stepfunctions start-execution --region ${var.region}",
    "--state-machine-arn ${local.state_machine_arn}",
    "--input '{\"cluster_name\":\"${var.cluster_name}\",\"bucket\":\"${local.document_bucket}\",",
    "\"reader_role_arn\":\"${aws_iam_role.cluster_read.arn}\"}'",
  ])
}
