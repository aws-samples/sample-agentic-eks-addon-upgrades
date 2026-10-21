# The brief agent's Gateway, and the three targets that give it a tool surface.
#
# WITHOUT A TARGET A GATEWAY EXPOSES NOTHING, AND IT REACHES READY ANYWAY. That is not
# hypothetical in this project: the research Gateway's target was CREATE_COMPLETE in a
# CloudFormation stack and then deleted, and for a day that Harness reported READY with zero
# tools while nothing anywhere said so. `terraform plan` naming a missing target is the check
# state was not giving.
#
# TWO OF THE THREE TOOLS ARE NOT OWNED HERE. `fetch_source` and `register_repository` are one
# Lambda in infra/terraform/modules/research, reused unchanged: its first act is to write the fetched bytes to
# `research-evidence/`, and that recording is the only reason a quote in either agent's output can
# be checked at all. A second copy would be a second implementation of the property both agents
# rest on, and the two would drift.
#
# THE TOOL DESCRIPTIONS ARE THIS AGENT'S OWN, THOUGH, even for the shared Lambda. The description
# is the text the MODEL reads, and the brief agent needs to be told to use the fetch tool for
# CHANGELOGs, upgrade guides and release-listing payloads -- not for a compatibility matrix. Same
# function, different instruction, which is exactly the split this component exists to make.

locals {
  # Prefer the Gateway this module manages. The variable survives for a consumer that sets
  # `manage_agentcore = false` and points at a Gateway created elsewhere.
  effective_gateway_arn = coalesce(
    var.brief_gateway_arn,
    one(aws_bedrockagentcore_gateway.brief[*].gateway_arn),
  )

  # Which Lambda serves each target. `search_issues` is this module's; the other two are
  # infra/terraform/modules/research's one function, addressed by derived name -- see the variable for the trade
  # this project has already made for the marks table and the cluster read roles.
  target_functions = {
    fetch_source        = local.fetch_source_arn
    register_repository = local.fetch_source_arn
    search_issues       = aws_lambda_function.search_issues.arn
  }

  # Every function this Gateway may invoke, deduplicated. Two targets share one ARN, and granting
  # the same resource twice in one policy statement is noise a reader has to reconcile.
  invokable_functions = distinct(values(local.target_functions))
}

# ---------------------------------------------------------------------------
# The Gateway's role. It invokes two Lambdas and does nothing else.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "brief_gateway_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

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

resource "aws_iam_role" "brief_gateway" {
  count = var.manage_agentcore ? 1 : 0

  name               = "${local.name_prefix}-brief-gateway"
  description        = "The brief Gateway. Invokes the fetch and issue-search tools and nothing else."
  assume_role_policy = data.aws_iam_policy_document.brief_gateway_assume.json
}

data "aws_iam_policy_document" "brief_gateway" {
  # BOTH the unqualified ARN and its `:*` alias form, because the Gateway may address a function
  # by version or alias and a grant on only the bare ARN fails the qualified call.
  statement {
    sid       = "InvokeBriefTools"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = concat(local.invokable_functions, [for a in local.invokable_functions : "${a}:*"])
  }

  # Deliberately absent: s3, bedrock, dynamodb. This role's entire purpose is
  # lambda:InvokeFunction on two functions.
}

resource "aws_iam_role_policy" "brief_gateway" {
  count = var.manage_agentcore ? 1 : 0

  name   = "gateway-permissions"
  role   = aws_iam_role.brief_gateway[0].id
  policy = data.aws_iam_policy_document.brief_gateway.json
}

# THE GRANT ON ANOTHER MODULE'S FUNCTION, and it is declared here on purpose.
#
# infra/terraform/modules/research already has an `aws_lambda_permission` on `-tool-fetch-source`, scoped to
# RESEARCH'S Gateway ARN. That statement does not cover this Gateway, so without the one below the
# brief agent's very first fetch returns AccessDenied -- a failure that appears only in a
# deployed run, as a refused tool call the agent reports as "could not read the release notes".
#
# DECLARED HERE RATHER THAN ADDED TO infra/terraform/modules/research, for two reasons. The dependency runs the
# right way round: research would need this module's Gateway ARN, which would make research
# unappliable until brief had run. And ownership stays legible: the statement that grants THIS
# Gateway belongs to the component that owns THIS Gateway, so removing this component removes its
# own access and nothing else's.
#
# A Lambda resource policy holds many statements keyed by `statement_id`, so two modules writing
# to one function do not collide. The hazard worth naming: neither module's plan shows the other's
# statement, so a reader auditing who may invoke that function has to look in both.
resource "aws_lambda_permission" "gateway_invoke_fetch_source" {
  count = var.manage_agentcore ? 1 : 0

  statement_id   = "AllowBriefGatewayInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = local.fetch_source_function
  principal      = "bedrock-agentcore.amazonaws.com"
  source_account = local.account_id
  source_arn     = local.effective_gateway_arn
}

# ---------------------------------------------------------------------------
# The Gateway
# ---------------------------------------------------------------------------

resource "aws_bedrockagentcore_gateway" "brief" {
  count = var.manage_agentcore ? 1 : 0

  name        = "${local.name_prefix}-brief-sources"
  description = "Upgrade-brief reading: release notes, upgrade guides and open issues. Every body is recorded before the model sees it."
  role_arn    = aws_iam_role.brief_gateway[0].arn

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

# ---------------------------------------------------------------------------
# The three targets
#
# ONE RESOURCE WITH for_each RATHER THAN THREE BLOCKS. The schemas are generated from
# `brief/prompt.py::tools()` into one file each, so the three targets differ only in which schema
# they carry and which ARN they point at. Three near-identical blocks would be three places to
# forget to update when a fourth tool is added, and the generator already guarantees the shape.
# ---------------------------------------------------------------------------

resource "aws_bedrockagentcore_gateway_target" "tool" {
  for_each = var.manage_agentcore ? local.tool_schemas : {}

  gateway_identifier = aws_bedrockagentcore_gateway.brief[0].gateway_id
  # Hyphens rather than underscores: the target name is a service identifier, and the research
  # component's `fetch-source` target established the convention.
  name = replace(each.key, "_", "-")

  # SHORT ON PURPOSE -- the service caps a target description at 200 characters, and these tools'
  # own descriptions run past 400. The long one is the text the MODEL reads and it is carried
  # below on `inline_payload`, where the length limit does not apply. This one is only what an
  # operator sees in a target listing, so restating the contract here would truncate at apply and
  # duplicate the thing it truncated.
  description = substr(each.value.description, 0, 195)

  # REQUIRED -- the service refuses a target without it:
  #   ValidationException: Credential provider configurations is not defined
  #
  # `gateway_iam_role` invokes the tool as the GATEWAY's role, the one above, scoped to the two
  # tool ARNs. `caller_iam_credentials` would instead invoke as whoever called the agent, so what
  # the tool could reach would vary by caller and the grant above would stop describing the tool's
  # actual reach.
  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = local.target_functions[each.key]

        tool_schema {
          inline_payload {
            name        = each.value.name
            description = each.value.description

            input_schema {
              type        = each.value.inputSchema.type
              description = try(each.value.inputSchema.description, null)

              dynamic "property" {
                for_each = each.value.inputSchema.properties
                content {
                  name        = property.key
                  type        = property.value.type
                  description = try(property.value.description, null)
                  required = contains(
                    try(each.value.inputSchema.required, []),
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
