# The brief Harness: the model loop that reads release notes, and the tools it may reach.
#
# THE SECOND HARNESS IN THIS SYSTEM, AND THE SEPARATION IS THE POINT. The research Harness is
# instructed to transcribe and not to reason past what a page says, at low effort, because the
# defect Stage 6 was rebuilt around was a model turning a tested window into a supported ceiling
# with perfect arithmetic. This one is instructed to read a CHANGELOG and judge what an operator
# would care about -- which is reasoning past the page, deliberately. One agent serving both would
# need a prompt telling it to be strict and lenient in the same breath, and the discipline that
# would erode is the one every stored fact rests on.
#
# So: its own ARN, its own prompt, its own iteration budget, its own Gateway. Tuning this cannot
# move the research agent's behaviour, and no fact in the store depends on anything here.
#
# WHAT LICENSES THE LATITUDE. This agent decides nothing the pipeline relies on. The version to
# move to is already settled by an approved fact and the plan; this explains a move that has been
# chosen. It writes no fact and no candidate, and its role grants no DynamoDB at all.

locals {
  # The Harness this module's functions invoke. Prefer the one managed here; the variable exists
  # for a checkout running with `manage_agentcore = false` against a Harness owned elsewhere.
  #
  # FAILS CLOSED. With neither, `coalesce` errors at plan time rather than producing a null that
  # would reach an IAM policy as `"Resource": null` and an environment variable as an empty
  # string -- a proxy that starts, calls nothing and reports "BRIEF_HARNESS_ARN is unset" for
  # every component in the run. A plan-time error names the missing input; that runtime symptom
  # does not.
  effective_harness_arn = coalesce(
    var.brief_harness_arn,
    one(aws_bedrockagentcore_harness.brief[*].arn),
  )
}

# ---------------------------------------------------------------------------
# The Harness's role: reach Bedrock, reach THIS Gateway, write its own telemetry.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "brief_harness_assume" {
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

resource "aws_iam_role" "brief_harness" {
  count = var.manage_agentcore ? 1 : 0

  name               = "${local.name_prefix}-brief-harness"
  description        = "The brief agent. Reads release notes and issues through its Gateway; writes no fact."
  assume_role_policy = data.aws_iam_policy_document.brief_harness_assume.json
}

data "aws_iam_policy_document" "brief_harness" {
  # The region wildcard on the first resource is REQUIRED rather than lazy: the model is reached
  # through a cross-region inference profile, so the call is authorized against the
  # foundation-model ARN in whichever region the profile routes to.
  statement {
    sid    = "InvokeBriefModel"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:${var.region}:${local.account_id}:*",
    ]
  }

  # SCOPED TO THIS GATEWAY, not `gateway/*`. A wildcard would let the brief agent invoke any
  # Gateway in the account -- including the research agent's fetch surface. The whole reason this component exists is that the two agents are
  # separate; a wildcard here would quietly undo it at the tool layer.
  statement {
    sid       = "InvokeOwnGateway"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [local.effective_gateway_arn]
  }

  statement {
    sid    = "WorkloadIdentity"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/*",
    ]
  }

  statement {
    sid    = "WriteOwnTelemetry"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/*"]
  }

  # Deliberately absent: s3 and dynamodb of any kind. The agent reaches evidence only by calling
  # its tools, which record what they fetched; it cannot read a snapshot, a plan, a fact or the
  # bodies its own tools wrote. So "the agent cannot see anything it did not fetch through a
  # recording tool" is a property of this grant rather than a claim about the prompt.
}

resource "aws_iam_role_policy" "brief_harness" {
  count = var.manage_agentcore ? 1 : 0

  name   = "harness-permissions"
  role   = aws_iam_role.brief_harness[0].id
  policy = data.aws_iam_policy_document.brief_harness.json
}

# ---------------------------------------------------------------------------
# The Harness itself
# ---------------------------------------------------------------------------

resource "aws_bedrockagentcore_harness" "brief" {
  count = var.manage_agentcore ? 1 : 0

  harness_name       = var.brief_harness_name
  execution_role_arn = aws_iam_role.brief_harness[0].arn

  model {
    bedrock_model_config {
      model_id = var.brief_model_id

      # `converse_stream` MUST match what the proxy parses. `brief_harness.py::_extract_text`
      # walks a converse-style event stream for `contentBlockDelta` text; changing this without
      # changing that produces a Harness that answers correctly into a shape nothing reads, and
      # every component then reports "the Harness returned no text".
      api_format = "converse_stream"

      max_tokens = var.brief_max_tokens

      # REASONING EFFORT, through the only door the provider offers. There is no `effort`
      # attribute on this block, so it goes in the JSON passthrough. See `brief_effort` for the
      # key's provenance and for why it must be treated as unverified until an invocation proves
      # it -- a passthrough the service silently ignores looks identical to one it honours.
      # `additionalModelRequestFields`, WHICH THE SERVICE NAMED ITSELF. The first attempt put
      # `output_config` at the top level -- the key the messages API accepts -- and the invoke
      # failed at parameter validation, loudly, which is the good half of the two failure shapes:
      #
      #   Unknown parameter in input: "output_config", must be one of: modelId, messages, system,
      #   inferenceConfig, toolConfig, guardrailConfig, additionalModelRequestFields, ...
      #
      # `additional_params` is spliced into the CONVERSE call, not into a messages call, and
      # converse carries model-native fields in `additionalModelRequestFields`. The anthropic-native
      # `output_config` object goes inside that.
      additional_params = jsonencode({
        additionalModelRequestFields = {
          output_config = {
            effort = var.brief_effort
          }
        }
      })

      # No `temperature` and no `top_p`: this model removed temperature and setting it returns
      # 400.
    }
  }

  # Read from the generated prompt file, so the deployed instruction and the reviewed one are the
  # same bytes. Generated from `brief/prompt.py`, which is in the brief's ruleset digest, and
  # `scripts/build-packages.sh` fails the build when the file is stale -- so a prompt nobody
  # reviewed cannot reach this argument.
  system_prompt {
    text = local.system_prompt
  }

  # THE AGENT'S ENTIRE TOOL SURFACE: one Gateway, three targets. `aws_iam` outbound auth means
  # the Gateway invokes each tool as its OWN role, scoped to those functions, rather than as
  # whoever called the agent.
  tool {
    type = "agentcore_gateway"
    name = "release_notes_and_issues"

    config {
      agentcore_gateway {
        gateway_arn = local.effective_gateway_arn

        outbound_auth {
          aws_iam = true
        }
      }
    }
  }

  # `["*"]` IS NOT A WIDENING -- the Gateway is the allowlist, and scoping this field was tried on
  # the research Harness and does not work: three forms were tried, each of which
  # removed the gateway tool ENTIRELY, after which the model emitted tool calls as prose and the
  # run recorded nothing. A filter that silently deletes the tools the agent needs is worse than
  # no filter. Which tools exist is decided by which targets are registered in gateway.tf, and
  # that is checkable in a target listing.
  allowed_tools = ["*"]

  # Disabled: each invocation briefs ONE component from a clean slate. Carrying conversation
  # across components is how one add-on's release notes end up quoted for another -- and the
  # per-component evidence session exists precisely so that cannot happen silently.
  memory {
    disabled {}
  }

  # Written with `=` rather than as a block: the provider models `truncation` as a
  # list-of-object ATTRIBUTE, and both strategy keys must be present in the object even though
  # only one is in use -- hence the explicit `summarization = null`.
  #
  # SLIDING WINDOW RATHER THAN SUMMARISATION, and that is a correctness choice rather than a cost
  # one. Release-note bodies ARE the evidence here. A summarised window would leave the agent
  # quoting a paraphrase of a page instead of the page, and the byte-for-byte quote check would
  # then mark those findings unverified -- correctly, and unhelpfully.
  truncation = [{
    strategy = "sliding_window"
    config = [{
      sliding_window = [{
        messages_count = var.brief_truncation_messages_count
      }]
      summarization = null
    }]
  }]

  max_iterations  = var.brief_max_iterations
  timeout_seconds = var.brief_harness_timeout_seconds

  # No `authorizer_configuration`: its absence selects AWS_IAM, so reaching this agent needs
  # signed AWS credentials.

  lifecycle {
    # FAIL CLOSED. A Harness created against an empty or wrong gateway ARN reaches READY, reports
    # healthy and has no tools -- the state the research component spent a day in. Refusing the
    # apply beats deploying that again.
    precondition {
      condition     = can(regex("^arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:gateway/", local.effective_gateway_arn))
      error_message = "The brief Harness needs a real Gateway ARN. Set manage_agentcore = true, or pass brief_gateway_arn."
    }

    # `environment_variables` IS AWS-POPULATED AND UNMANAGED HERE, and without this the component
    # cannot converge at all. No harness block in this repository sets the attribute, so there is
    # no configured value to compare against; the service returns an empty map. aws v6.64.0 reads
    # that as Optional+Computed, plans `{} -> null`, sends the update, gets `{}` back, and then
    # rejects its OWN result:
    #
    #   Error: Provider produced inconsistent result after apply
    #   .environment_variables: inconsistent values for sensitive attribute
    #
    # That is not transient -- it repeats on every apply, and because the error aborts the run it
    # also strands the Lambdas planned after it, so `brief` could never report "No changes".
    # Measured: it stopped an apply here after one of four functions had updated.
    #
    # Declining to manage the attribute is the honest fix. Writing `environment_variables = {}`
    # would also converge, but it would assert an empty map as this agent's intended environment,
    # which is a claim nobody made. Ignoring it says what is true: AWS owns this field.
    ignore_changes = [environment_variables]
  }
}
