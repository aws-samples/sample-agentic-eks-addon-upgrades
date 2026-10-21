# `freshness_function_name`, `freshness_function_arn` and `fact_mark_table_name` were removed
# with the resources they described. The sweep and the marks table are infra/terraform/modules/platform's
# now: the sweep is a stage of that module's state machine, so it references its own function
# directly and needs no ARN from here.

output "referenced_not_managed" {
  description = <<-EOT
    Resources this module reads and writes but does NOT manage. A plan that proposes
    to change or destroy any of these is a bug in this module.
  EOT
  value = {
    document_bucket  = local.document_bucket
    fact_store_table = local.fact_store_table
  }
}

output "research_model" {
  description = <<-EOT
    The model Stage 6 is configured to use, and its reasoning effort. Both are
    parameters; the code hardcodes neither.
  EOT
  value = {
    model_id = var.research_model_id
    effort   = var.research_effort
  }
}


output "approve_function_url" {
  description = <<-EOT
    The IAM-authenticated approval endpoint. There is no anonymous path: a caller must
    sign the request, and the resource policy grants invoke to the approver role alone.

    GET  lists what is pending approval (read-only, safe to surface anywhere).
    POST approves one candidate and writes the fact.
  EOT
  value       = aws_lambda_function_url.approve.function_url
}

output "approver_role_arn" {
  description = <<-EOT
    The role a person assumes in order to approve. When `denied_approver_role_names` is
    set, its trust policy carries an EXPLICIT DENY on those roles, so none of them can
    assume it.

    "A reader can show a pending approval and cannot grant one" rests on the approve
    function's resource policy allowing only this role, plus the handler's own approver
    check -- not on that Deny alone.
  EOT
  value       = aws_iam_role.approver.arn
}

output "roles_denied_from_approving" {
  description = "The principals explicitly denied from assuming the approver role."
  value       = local.denied_role_arns
}

output "only_writer_of_facts" {
  description = <<-EOT
    The only role permitted to write a fact. Stages 1-5 are read-only by design, and
    research PROPOSES while a person approves -- enforced by IAM as well as by code.
  EOT
  value       = aws_iam_role.approve.name
}


output "approval_function_arn" {
  description = <<-EOT
    ARN of the approval stage's Lambda. Feed it to infra/terraform/modules/platform as
    `approval_function_arn` to insert the `Approval` wait between `Research` and
    `Knowledge changed?`.

    Until platform is applied WITH this value, the state machine is unchanged and this
    function is deployed but unreachable -- which is a deliberate two-step: the stage and
    its table exist, and the pipeline only stops on it once somebody says so.
  EOT
  value       = aws_lambda_function.approval.arn
}

output "approval_link_endpoint" {
  description = <<-EOT
    The UNAUTHENTICATED function URL a one-time approval link points at.

    AWS performs no authorisation on requests to it; the single-use expiring token in the
    query string is the whole control. See the header of approval_stage.tf. Do not publish
    this URL anywhere an agent can read -- it is safe only because the tokens
    are not.
  EOT
  value       = aws_lambda_function_url.approve_link.function_url
}
