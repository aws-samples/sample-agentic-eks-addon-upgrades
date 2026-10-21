output "state_machine_arn" {
  description = "The readiness pipeline. Start one execution per cluster you want assessed."
  value       = module.platform.state_machine_arn
}

output "document_bucket" {
  description = "Where every stage document, report and approval page is written."
  value       = module.platform.document_bucket
}

output "scan_role_arn" {
  description = "The read-only role Stage 1 scans clusters with. Give it to ../cluster-access for each cluster you onboard."
  value       = module.platform.scan_role_arn
}
