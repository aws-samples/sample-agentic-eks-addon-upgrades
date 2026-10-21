output "upgrade_triage_function_arn" {
  description = <<-EOT
    ARN of the triage proxy, for platform's `upgrade_triage_function_arn`.

    PLATFORM DEFAULTS IT TO NULL, and the whole stage works with it unset: the lane never calls out and
    rolls an unhealthy component back deterministically. So wiring this is what turns a deterministic
    rollback into a triaged one, and leaving it unwired degrades rather than breaks -- the same property
    `infra/terraform/modules/brief` has, because nothing advisory may gate a run.
  EOT
  value       = aws_lambda_function.triage.arn
}

output "upgrade_harness_arn" {
  description = "ARN of the Harness, for a checkout that sets manage_agentcore = false."
  value       = local.effective_harness_arn
}

output "upgrade_agent_reachable_by" {
  description = <<-EOT
    The one principal permitted to invoke the proxy, or a note that nobody is.

    An output because the alternative to checking it is a deployed agent nothing can reach, whose
    symptom is every component reporting "triage unavailable" and rolling back -- which looks exactly
    like a working deterministic fallback.
  EOT
  value       = var.upgrade_build_role_arn == null ? "nobody: upgrade_build_role_arn is unset, so no resource policy was written" : var.upgrade_build_role_arn
}
