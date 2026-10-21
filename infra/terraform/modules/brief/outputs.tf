# What infra/terraform/modules/platform needs in order to wire the brief into the state machine.
#
# THE WIRING IS A SEPARATE CHANGE WITH ITS OWN PLAN AND ITS OWN AUTHORISATION. This component
# creates the functions and publishes their ARNs; nothing here reaches the pipeline. Applying this
# module changes no run's behaviour -- the brief is invoked only once platform names these ARNs,
# which is the next change and not this one.

output "brief_plan_function_arn" {
  description = <<-EOT
    WHAT TO SET `brief_plan_function_arn` TO IN infra/terraform/modules/platform.

    The first of the three states: reads the Stage 5 plan and emits one work item per component
    move for the Map to fan out over.
  EOT
  value       = aws_lambda_function.function["brief-plan"].arn
}

output "brief_component_function_arn" {
  description = <<-EOT
    WHAT TO SET `brief_component_function_arn` TO IN infra/terraform/modules/platform.

    The Map's ITERATOR: one invocation per component move. Each one invokes the brief Harness,
    verifies every quote against the bytes the tools recorded, and writes one partial.

    ITS MAP MUST CARRY A CATCH, and this is the one thing the wiring cannot get wrong. The handler
    turns every expected failure into a recorded absence and returns normally -- an unreachable
    project, a rate limit, a model that would not answer. What it cannot report on is a failure to
    invoke it at all: a throttle, a missing permission, a cold-start timeout. Without a catch to
    the report those fail the whole execution, which would make an ADVISORY stage able to deny the
    user their upgrade approval. That inverts the design's own rule, in the user's words: if there
    is no brief, say so and let them decide.
  EOT
  value       = aws_lambda_function.function["brief-component"].arn
}

output "brief_assemble_function_arn" {
  description = <<-EOT
    WHAT TO SET `brief_assemble_function_arn` TO IN infra/terraform/modules/platform.

    The last of the three: reads the partials back and writes the one UpgradeBrief document under
    `briefs/`. A step whose partial is missing is named in `unbriefed` with the reason rather than
    omitted, so a Map iteration that died leaves a visible gap instead of a document that quietly
    covers four moves out of five.
  EOT
  value       = aws_lambda_function.function["brief-assemble"].arn
}

output "brief_harness_arn" {
  description = <<-EOT
    The brief agent. ITS OWN Harness, not the research one -- see harness.tf for why sharing would
    erode the discipline every stored fact depends on.

    Null when `manage_agentcore = false`, in which case it was supplied rather than created.
  EOT
  value       = one(aws_bedrockagentcore_harness.brief[*].arn)
}

output "brief_gateway_arn" {
  description = "The brief agent's tool surface. Three targets; see gateway.tf."
  value       = local.effective_gateway_arn
}

output "tool_search_issues_arn" {
  description = <<-EOT
    The issue-search tool, owned by this module and reachable only through the Gateway.

    Published for the same reason infra/terraform/modules/research publishes its fetch tool's ARN: an operator
    reading a Gateway target listing should be able to confirm the target points at the function
    this module created, rather than take the wiring on faith.
  EOT
  value       = aws_lambda_function.search_issues.arn
}

output "advisory" {
  description = <<-EOT
    A STATED PROPERTY OF THIS COMPONENT, published so the next change cannot forget it.

    Everything this module deploys is advisory. No stage reads the brief document, nothing derives
    from it, and it writes no fact -- so a brief that fails costs a reader an explanation and costs
    the run nothing. The wiring must preserve that: the Map catches to the report, and the
    execution continues with the absence recorded.
  EOT
  value       = true
}
