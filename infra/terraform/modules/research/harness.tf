# The Stage 6 research Harness: the model loop, and the one tool it may reach.
#
# THE ONLY PLACE A MODEL RUNS IN THIS COMPONENT. It reads sources and transcribes them; it
# holds no judging code and no fact access. The deterministic half is a separate deployable
# -- the `judge` Lambda in tools.tf -- whose package contains no model client and whose role
# has no Bedrock. So "the model never issues a verdict" is a claim about what is installed
# where rather than about discipline.
#
# Its Gateway and that Gateway's single target are in gateway.tf, and the header there
# explains why these resources moved out of the CloudFormation stack that created them.
# ---------------------------------------------------------------------------
# The Harness's role: reach Bedrock, reach THIS Gateway, write its own telemetry.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "research_harness_assume" {
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

resource "aws_iam_role" "research_harness" {
  count = var.manage_agentcore ? 1 : 0

  name               = "${local.name_prefix}-research-harness"
  description        = "The Stage 6 research Harness. Reads sources through its Gateway; holds no fact write and no judging code."
  assume_role_policy = data.aws_iam_policy_document.research_harness_assume.json
}

data "aws_iam_policy_document" "research_harness" {
  # TWO resources, and the region wildcard on the first is REQUIRED rather than lazy: the
  # model is reached through a cross-region inference profile, so the call is authorized
  # against the foundation-model ARN in whichever region the profile routes to.
  statement {
    sid    = "InvokeResearchModel"
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

  # SCOPED TO THIS GATEWAY. The CDK role granted `gateway/*`, which let the research agent
  # invoke any Gateway in the account -- including the brief agent's.
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
      "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/harness_${var.research_harness_name}-*",
    ]
  }

  statement {
    sid       = "CreateOwnLogGroup"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }

  statement {
    sid       = "DescribeLogGroups"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:*"]
  }

  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
  }

  statement {
    sid       = "PublishOwnMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }

  statement {
    sid    = "Tracing"
    effect = "Allow"
    actions = [
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
      "xray:PutTelemetryRecords",
      "xray:PutTraceSegments",
    ]
    resources = ["*"]
  }

  # Deliberately absent, and each absence is a property rather than an oversight:
  #   dynamodb  -- the Harness cannot read or write a fact. The judge reads; only the
  #                approval endpoint writes, and only for a signed human.
  #   s3        -- the Harness never touches recorded evidence. The fetch tool writes it and
  #                the judge reads it; the model only ever sees a body in a tool response.
  #   ecr-public / sts:GetServiceBearerToken -- the CDK role carried both for pulling a
  #                container image. This Harness runs no image of ours.
}

resource "aws_iam_role_policy" "research_harness" {
  count = var.manage_agentcore ? 1 : 0

  name   = "harness-permissions"
  role   = aws_iam_role.research_harness[0].id
  policy = data.aws_iam_policy_document.research_harness.json
}

# ---------------------------------------------------------------------------
# The Harness itself: the model loop, and the one tool it may reach.
# ---------------------------------------------------------------------------

resource "aws_bedrockagentcore_harness" "research" {
  count = var.manage_agentcore ? 1 : 0

  harness_name       = var.research_harness_name
  execution_role_arn = aws_iam_role.research_harness[0].arn

  model {
    bedrock_model_config {
      model_id   = var.research_model_id
      api_format = "converse_stream"

      # THE PLATFORM CEILING, AND NO LONGER AN OPEN QUESTION. 128000 is the maximum output
      # tokens this model accepts, named in Bedrock's own rejection text and bisected
      # (128000 accepted, 128001 refused). The variable's description carries the quoted 400
      # from a live probe; an earlier version recorded 64000 as a choice
      # awaiting a decision because no citable maximum had been found. It had one, and 64000
      # was half of it.
      max_tokens = var.research_max_tokens

      # No `temperature` and no `top_p`: this model removed temperature and setting it
      # returns 400.
    }
  }

  # Read from the harness manifest, so the deployed prompt and the file the repository
  # reviews are the same bytes.
  system_prompt {
    text = local.research_harness_manifest.systemPrompt
  }

  # THE AGENT'S ENTIRE TOOL SURFACE: one Gateway with one target. `aws_iam` outbound auth
  # means the Gateway invokes the fetch tool as its OWN role, scoped to that one Lambda,
  # rather than as whoever called the agent.
  tool {
    type = "agentcore_gateway"
    name = "sources"

    config {
      agentcore_gateway {
        gateway_arn = local.effective_gateway_arn

        outbound_auth {
          aws_iam = true
        }
      }
    }
  }

  # `["*"]` IS NOT LAZINESS AND IT IS NOT A WIDENING -- the Gateway is the allowlist, and
  # scoping this field was tried and does not work. Three forms were tried --
  # `["fetch_source"]`, `["fetch-source___fetch_source"]` (the name the trace itself prints)
  # and both together -- each of which removed the gateway tool ENTIRELY: the model then
  # emitted `fetch_source(url=...)` as prose, made zero tool calls, and the run recorded no
  # bodies. A filter that silently deletes the one tool the agent needs is worse than no
  # filter. The managed runtime's injected `shell` and `file_operations` are kept out by not
  # registering them, proved structurally by an AST walk, not by this field.
  allowed_tools = ["*"]

  # Disabled: each invocation researches ONE component from a clean slate. Carrying
  # conversation across components is how one component's sources end up quoted for another.
  memory {
    disabled {}
  }

  # Written with `=` rather than as a block: the provider models `truncation` as a
  # list-of-object ATTRIBUTE, and both strategy keys must be present in the object even
  # though only one is in use -- hence the explicit `summarization = null`.
  truncation = [{
    strategy = "sliding_window"
    config = [{
      sliding_window = [{
        messages_count = var.research_truncation_messages_count
      }]
      summarization = null
    }]
  }]

  max_iterations  = var.research_max_iterations
  timeout_seconds = var.research_harness_timeout_seconds

  # No `authorizer_configuration`: its absence selects AWS_IAM, so reaching this agent needs
  # signed AWS credentials.

  lifecycle {
    # FAIL CLOSED. A Harness created against an empty or wrong gateway ARN reaches READY,
    # reports healthy and has no tools -- which is exactly the state this component spent a
    # day in. Refusing the apply beats deploying it again.
    precondition {
      condition     = can(regex("^arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:gateway/", local.effective_gateway_arn))
      error_message = "The research Harness needs a real Gateway ARN. Set manage_agentcore = true, or pass research_gateway_arn."
    }

    # `environment_variables` IS AWS-POPULATED AND UNMANAGED HERE. No harness block in this
    # repository sets it, so there is no configured value to compare against and the service
    # returns an empty map. aws v6.64.0 reads the attribute as Optional+Computed, plans
    # `{} -> null`, sends the update, gets `{}` back, and rejects its OWN result:
    #
    #   Error: Provider produced inconsistent result after apply
    #   .environment_variables: inconsistent values for sensitive attribute
    #
    # It repeats on every apply, and because the error aborts the run it strands everything
    # planned behind it. That matters most HERE: this component's plan carries 15 changes,
    # including six `aws_iam_role_policy` resources, so an abort part-way leaves IAM half
    # applied. Measured on the brief Harness, where it stopped an apply after one of four
    # Lambdas had updated.
    #
    # Ignoring beats writing `environment_variables = {}`: both converge, but the explicit empty
    # map asserts an empty environment as this agent's intended configuration, which is a claim
    # nobody made. Ignoring says what is true -- AWS owns this field.
    ignore_changes = [environment_variables]
  }
}

# ---------------------------------------------------------------------------
# Outputs: what the pipeline needs in order to route Stage 6 through the Harness.
# ---------------------------------------------------------------------------

output "research_harness_arn" {
  description = <<-EOT
    The research Harness. Invoke it with the `InvokeHarness` API -- NOT
    `InvokeAgentRuntime`, which refuses a harness-managed runtime with "is managed by a
    harness and cannot be invoked directly". Session ids must be at least 33 characters.

    Step Functions has no `invokeHarness` SDK integration, so the pipeline cannot call this
    directly; it reaches it through a Lambda that holds `bedrock-agentcore:InvokeHarness`.
  EOT
  value       = one(aws_bedrockagentcore_harness.research[*].arn)
}

output "research_harness_execution_role_arn" {
  description = "The Harness's role. Holds Bedrock and this one Gateway, and no fact access of any kind."
  value       = one(aws_iam_role.research_harness[*].arn)
}


# ---------------------------------------------------------------------------
# The Harness proxy: what the pipeline's `Research` Map actually invokes.
#
# WHY A LAMBDA SITS IN FRONT OF THE HARNESS AT ALL. Step Functions cannot call a Harness:
#
#   * a harness-managed runtime REFUSES `InvokeAgentRuntime` -- "is managed by a harness and
#     cannot be invoked directly. Use the InvokeHarness API";
#   * Step Functions has no `invokeHarness` SDK integration, so
#     `arn:aws:states:::aws-sdk:bedrockagentcore:invokeHarness` is not a resource ARN it
#     recognises.
#
# So the pipeline keeps invoking a Lambda -- which is also why platform's
# `research_function_arn` validation requiring a Lambda ARN is correct rather than an
# obstacle. This function is the ARN it should hold.
#
# It carries NO model client and NO judging code. It mints the session, calls the Harness,
# pulls the extraction out of the response stream, and invokes the judge.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "research_harness_proxy" {
  count = var.manage_agentcore ? 1 : 0

  name              = "/aws/lambda/${local.name_prefix}-research-harness"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_iam_role" "research_harness_proxy" {
  count = var.manage_agentcore ? 1 : 0

  name               = "${local.name_prefix}-research-harness-proxy"
  description        = "Invokes the research Harness and hands its extraction to the judge. No model, no fact access."
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "research_harness_proxy" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${one(aws_cloudwatch_log_group.research_harness_proxy[*].arn)}:*"]
  }

  # ONE `InvokeHarness` API CALL AUTHORIZES AGAINST TWO ACTIONS, and both are required. Found
  # by two consecutive live 403s against the HARNESS ARN, each naming the action the previous
  # grant did not cover:
  #
  #   1. with neither  -> not authorized to perform: bedrock-agentcore:InvokeAgentRuntime
  #   2. with only #1  -> not authorized to perform: bedrock-agentcore:InvokeHarness
  #
  # So granting the obvious one is not enough, and neither is granting only the one the error
  # first named. Note also that the resource is the HARNESS ARN for both, even though the
  # runtime a Harness manages refuses the `InvokeAgentRuntime` *API* outright -- the action
  # name and the callable API do not line up here, and the runtime ARN is the wrong resource.
  #
  # THE ONE HARNESS, not `harness/*`. A wildcard would let this proxy drive every other
  # Harness in the account as well.
  statement {
    sid    = "InvokeResearchHarness"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:InvokeHarness",
      "bedrock-agentcore:InvokeAgentRuntime",
    ]
    resources = [one(aws_bedrockagentcore_harness.research[*].arn)]
  }

  # THE ONE JUDGE. Synchronous, because the proxy returns the judge's verdict to the Map.
  statement {
    sid       = "InvokeJudge"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.judge.arn]
  }

  # THE INVENTORY, READ-ONLY, FOR THE TASK'S CHART LEADS. The Helm chart name and installed
  # chart version (from the release's `helm.sh/chart` label) go on the research task so the
  # agent looks for THAT chart. One prefix, `GetObject` only: the inventory carries what the
  # scan observed and no secret, and nothing here can read evidence, candidates or facts.
  statement {
    sid    = "ReadInventoryForChartLeads"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket_arn}/inventories/*"]
  }

  # Deliberately absent: bedrock, dynamodb, and any S3 write or any other prefix. This
  # function writes no evidence -- the fetch tool records bodies, the judge reads them back,
  # and only the approval endpoint writes a fact. A proxy that could reach those would make
  # every one of those separations a claim about code rather than about permissions.
}

resource "aws_iam_role_policy" "research_harness_proxy" {
  count = var.manage_agentcore ? 1 : 0

  name   = "proxy-permissions"
  role   = aws_iam_role.research_harness_proxy[0].id
  policy = data.aws_iam_policy_document.research_harness_proxy.json
}

resource "aws_lambda_function" "research_harness_proxy" {
  count = var.manage_agentcore ? 1 : 0

  function_name = "${local.name_prefix}-research-harness"
  description   = "Stage 6 via the Harness - InvokeHarness, then judge. Carries no model client."
  role          = aws_iam_role.research_harness_proxy[0].arn
  handler       = "entrypoints.research_harness.stage_research_harness"
  runtime       = "python3.12"
  architectures = ["arm64"]

  # Lambda's own maximum. One invocation is one component's whole research inside the
  # Harness, and the Harness's own ceiling is longer than this -- so 900s is the platform
  # limit, not a budget chosen to shape the answer.
  timeout     = 900
  memory_size = 1024

  # ITS OWN ZIP, the only one that bundles boto3. `InvokeHarness` is newer than the Lambda
  # runtime's bundled boto3, and a proxy relying on the runtime copy fails at call time with
  # "object has no attribute 'invoke_harness'" -- a failure no local test sees.
  filename         = "${path.module}/../../../../build/research_harness.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/research_harness.zip")

  environment {
    variables = {
      RESEARCH_HARNESS_ARN = one(aws_bedrockagentcore_harness.research[*].arn)
      JUDGE_FUNCTION_NAME  = aws_lambda_function.judge.function_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.research_harness_proxy]
}

output "research_harness_proxy_arn" {
  description = <<-EOT
    WHAT TO SET `research_function_arn` TO IN infra/terraform/modules/platform, to route the pipeline's
    Stage 6 through the Harness instead of the in-process research Lambda.

    Both paths run the same three deterministic modules -- verify, bounds, acceptance. The
    difference is where they run: this path records every fetched body out-of-band BEFORE the
    model reads it, and judges in a separate deployable that holds no model client.
  EOT
  value       = one(aws_lambda_function.research_harness_proxy[*].arn)
}
