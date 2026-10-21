# The whole sample, in one apply.
#
#   platform   the core: document bucket, fact store, stages 1-5 and 7, the freshness sweep, the
#              Step Functions state machine, the release gate and the upgrade stage
#   research   Stage 6: the research agent, the deterministic judge, fact approval
#   brief      the upgrade brief agent (advisory only)
#   upgrade    the upgrade triage agent and its read-only EKS MCP gateway
#
# The modules reference each other's outputs directly, so Terraform orders the creates and
# no ARN is copied by hand. Shared resources (the bucket and the fact table) are created by
# `platform` and passed to the other modules, which only ever hold names and ARNs for them.

module "platform" {
  source = "../modules/platform"

  region                    = var.region
  name_prefix               = var.name_prefix
  target_kubernetes_version = var.target_kubernetes_version

  # Stages that live in other modules.
  research_function_arn        = module.research.research_harness_proxy_arn
  approval_function_arn        = module.research.approval_function_arn
  brief_plan_function_arn      = module.brief.brief_plan_function_arn
  brief_component_function_arn = module.brief.brief_component_function_arn
  brief_assemble_function_arn  = module.brief.brief_assemble_function_arn
  upgrade_triage_function_arn  = module.upgrade.upgrade_triage_function_arn

  approval_email_from     = var.approval_email_from
  approval_email_to       = var.approval_email_to
  github_token_secret_arn = var.github_token_secret_arn

  release_gate_cluster_names       = [var.target_cluster]
  release_approval_timeout_seconds = var.release_approval_timeout_seconds

  # Every resource is created whatever `upgrade_stage_enabled` says. The flag only decides whether a
  # run routes past the readiness report into upgrade approval, the release gate and the lanes.
  upgrade_stage_enabled            = var.upgrade_stage_enabled
  upgrade_target_cluster           = var.target_cluster
  upgrade_lane_groups              = var.upgrade_lane_groups
  upgrade_health_deadline_seconds  = var.upgrade_health_deadline_seconds
  upgrade_health_checkpoints       = var.upgrade_health_checkpoints
  upgrade_revalidation_checkpoints = var.upgrade_revalidation_checkpoints
  upgrade_build_timeout_minutes    = var.upgrade_build_timeout_minutes
  upgrade_helm_version             = var.upgrade_helm_version
  upgrade_helm_sha256              = var.upgrade_helm_sha256
  upgrade_kubectl_version          = var.upgrade_kubectl_version
  upgrade_kubectl_sha256           = var.upgrade_kubectl_sha256
}

module "research" {
  source = "../modules/research"

  research_harness_name = "${local.agent_name_prefix}_research"

  region                = var.region
  name_prefix           = var.name_prefix
  tags                  = var.tags
  document_bucket_name  = module.platform.document_bucket
  fact_store_table_name = module.platform.fact_store_table
  log_kms_key_arn       = module.platform.log_kms_key_arn

  approval_email_from          = var.approval_email_from
  approval_email_to            = var.approval_email_to
  upgrade_decision_watcher_arn = module.platform.upgrade_decision_watcher_arn
}

module "brief" {
  source = "../modules/brief"

  brief_harness_name = "${local.agent_name_prefix}_brief"

  region                     = var.region
  name_prefix                = var.name_prefix
  document_bucket_name       = module.platform.document_bucket
  fact_store_table_name      = module.platform.fact_store_table
  fetch_source_function_name = module.research.tool_fetch_source_name
  github_token_secret_arn    = var.github_token_secret_arn
  log_kms_key_arn            = module.platform.log_kms_key_arn
}

module "upgrade" {
  source = "../modules/upgrade"

  upgrade_harness_name = "${local.agent_name_prefix}_upgrade"

  region                 = var.region
  name_prefix            = var.name_prefix
  document_bucket_name   = module.platform.document_bucket
  manage_mcp_gateway     = true
  upgrade_target_cluster = var.target_cluster
  upgrade_build_role_arn = module.platform.upgrade_build_role_arn
  log_kms_key_arn        = module.platform.log_kms_key_arn
}

locals {
  # AgentCore harness names allow letters, digits and underscores only, and are account-wide, so
  # they derive from `name_prefix` like every other resource name. The modules' own defaults
  # (`readiness_research`, ...) are fixed strings, which would collide with any other copy of this
  # pipeline in the account.
  agent_name_prefix = replace(var.name_prefix, "-", "_")
}
