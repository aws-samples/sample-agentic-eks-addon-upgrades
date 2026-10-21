# The research Gateway: the Harness's entire tool surface, and the one target on it.
#
# WHY THE AGENTCORE RESOURCES ARE IN TERRAFORM AT ALL. The Gateway, its target and the
# Harness were created by `agentcore deploy`, which is CDK, which means CloudFormation:
# stack `AgentCore-eksReadinessResearch-default`. So Stage 6 was described in three places
# at once -- this module's Terraform state for the Lambdas, IAM and log groups, that
# CloudFormation stack for the AgentCore resources, and the declarative `agentcore/`
# project that generates the stack. Nothing was unmanaged, but no single plan could tell you
# what Stage 6 was, and the copies could disagree without any of them reporting it.
#
# They did. Drift detection on the stack found TWO resources recorded as CREATE_COMPLETE and
# actually deleted: this Gateway's `fetch-source` target, and the researchAgent Runtime. The
# missing target is why the research Harness sat READY with no tools for a day -- a Gateway
# with no target exposes nothing and reports healthy, and neither the stack nor the CLI's
# `deployed-state.json` noticed.
#
# Terraform owns it now.
#
# WHAT IS DELIBERATELY DIFFERENT FROM WHAT THE CDK STACK CREATED:
#
#   * The Gateway role loses `s3:Get*`/`s3:List*` on `cdk-hnb659fds-assets-*`. That is CDK
#     bootstrap machinery -- the bucket a synth stages templates through. A Gateway whose
#     only job is invoking one Lambda has no business reading it, and once the stack is gone
#     there is nothing there to read.
#   * Names follow this module's `name_prefix` instead of CDK's generated ones, so the
#     resources sort with the pipeline they serve and no random suffix has to be pinned in
#     source to keep a plan quiet.
#
# The Harness itself is in harness.tf; the tool Lambdas it reaches are in tools.tf.
locals {
  # Prefer the Gateway this module now manages. The variable survives for a consumer that
  # sets `manage_agentcore = false` and points at a Gateway created elsewhere.
  effective_gateway_arn = coalesce(
    var.research_gateway_arn,
    one(aws_bedrockagentcore_gateway.research[*].gateway_arn),
  )

  # The single source of truth for the model's instructions and the tool contract. The prompt
  # is read out of the harness manifest rather than restated here, and the tool schema out of
  # the file the handler was written against.
  research_harness_manifest = jsondecode(
    file("${path.module}/../../../../agents/readiness_research/harness.json")
  )
  fetch_source_schema = jsondecode(
    file("${path.module}/../../../../tools/research/schemas/fetch_source.json")
  )[0]
}


# ---------------------------------------------------------------------------
# The Gateway's role. It invokes exactly one Lambda and does nothing else.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "research_gateway_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # The CDK role had a bare service principal with no conditions. Both guards are added
    # here: the account defends against a confused-deputy ARN from another account, and the
    # ARN pattern keeps the trust inside AgentCore.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "research_gateway" {
  count = var.manage_agentcore ? 1 : 0

  name               = "${local.name_prefix}-research-gateway"
  description        = "Invoked by the research Gateway to reach the source-fetch tool. One Lambda, nothing else."
  assume_role_policy = data.aws_iam_policy_document.research_gateway_assume.json
}

data "aws_iam_policy_document" "research_gateway" {
  # BOTH the unqualified ARN and its `:*` alias form, because the Gateway may address the
  # function by version or alias and a grant on only the bare ARN fails the qualified call.
  statement {
    sid     = "InvokeFetchSourceTool"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [
      "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.name_prefix}-tool-fetch-source",
      "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.name_prefix}-tool-fetch-source:*",
    ]
  }

  # Deliberately absent: the CDK asset-bucket read, s3 of any kind, bedrock, dynamodb. This
  # role's entire purpose is one lambda:InvokeFunction.
}

resource "aws_iam_role_policy" "research_gateway" {
  count = var.manage_agentcore ? 1 : 0

  name   = "gateway-permissions"
  role   = aws_iam_role.research_gateway[0].id
  policy = data.aws_iam_policy_document.research_gateway.json
}

# ---------------------------------------------------------------------------
# The Gateway, and the target that gives it a tool surface.
#
# WITHOUT THE TARGET THE GATEWAY EXPOSES NOTHING, and it reaches READY anyway. That is not
# hypothetical here: the target was CREATE_COMPLETE in the CDK stack and then deleted, and
# for a day the research Harness reported READY with zero tools. `terraform plan` naming a
# missing target is the check that state was not giving.
# ---------------------------------------------------------------------------

resource "aws_bedrockagentcore_gateway" "research" {
  count = var.manage_agentcore ? 1 : 0

  name        = "${local.name_prefix}-research-sources"
  description = "Stage 6 source reading. One read-only tool: fetch one pinned page from the component's own repository or docs. Every body is recorded before the model sees it."
  role_arn    = aws_iam_role.research_gateway[0].arn

  # NONE, because the only caller is the Harness reaching in over its own outbound IAM auth.
  # There is no human or JWT holder on this path.
  authorizer_type = "NONE"
  protocol_type   = "MCP"

  protocol_configuration {
    mcp {
      search_type = "SEMANTIC"
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "fetch_source" {
  count = var.manage_agentcore ? 1 : 0

  gateway_identifier = aws_bedrockagentcore_gateway.research[0].gateway_id
  name               = "fetch-source"

  # SHORT ON PURPOSE -- the service caps a target description at 200 characters, and the
  # tool schema's own description is 484. The long one is the text the MODEL reads, and it
  # is carried below on `inline_payload` where the length limit does not apply. This one is
  # only what an operator sees in a target listing, so restating the whole contract here
  # would truncate at apply and duplicate the thing it truncated.
  description = "Fetch one pinned page from the component's own repository or docs, recording its bytes before the model reads them."

  # REQUIRED -- the service refuses a target without it:
  #   ValidationException: Credential provider configurations is not defined
  #
  # `gateway_iam_role` invokes the tool as the GATEWAY's role, the one above, scoped to the
  # single fetch-source ARN. `caller_iam_credentials` would instead invoke as whoever called
  # the agent, so what the tool could reach would vary by caller and the grant above would
  # stop describing the tool's actual reach.
  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.tool_fetch_source.arn

        tool_schema {
          inline_payload {
            name        = local.fetch_source_schema.name
            description = local.fetch_source_schema.description

            input_schema {
              type        = local.fetch_source_schema.inputSchema.type
              description = try(local.fetch_source_schema.inputSchema.description, null)

              dynamic "property" {
                for_each = local.fetch_source_schema.inputSchema.properties
                content {
                  name        = property.key
                  type        = property.value.type
                  description = try(property.value.description, null)
                  required = contains(
                    try(local.fetch_source_schema.inputSchema.required, []),
                    property.key
                  )
                }
              }
            }
          }
        }
      }
    }
  }
}

output "research_gateway_arn" {
  description = "The research Gateway, whose single target is the source-fetch tool."
  value       = local.effective_gateway_arn
}
