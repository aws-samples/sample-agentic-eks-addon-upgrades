# The Harness. Reaches Bedrock, writes its own telemetry, and can reach nothing else.

data "aws_iam_policy_document" "harness_assume" {
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
      values   = ["arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "harness" {
  count = var.manage_agentcore ? 1 : 0

  name               = "${local.name_prefix}-upgrade-harness"
  description        = "The upgrade agent. Triages a sealed diagnosis bundle; reaches no cluster and no store."
  assume_role_policy = data.aws_iam_policy_document.harness_assume.json
}

data "aws_iam_policy_document" "harness" {
  # The region wildcard on the first resource is REQUIRED rather than lazy: the model is reached
  # through a cross-region inference profile, so the call is authorized against the foundation-model
  # ARN in whichever region the profile routes to.
  statement {
    sid    = "InvokeTriageModel"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = [
      "arn:${local.partition}:bedrock:*::foundation-model/*",
      "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:*",
    ]
  }

  statement {
    sid    = "WorkloadIdentity"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    ]
    resources = [
      "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
      "arn:${local.partition}:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/*",
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
    resources = ["arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/*"]
  }

  # GRANTED WHEN THERE IS A GATEWAY TO INVOKE, and this comment used to forbid it outright -- "there is
  # no Gateway to invoke. A grant here would be the first half of giving this agent a way to reach
  # something, which is the thing its design turns on not having." That was true of the design as
  # built: the agent was handed a sealed bundle in its prompt and called nothing.
  #
  # IT IS NOT TRUE OF THE DESIGN AS ASKED FOR. The agent reads the cluster itself now, and the read
  # path is four EKS MCP tools behind a Gateway. Without this grant the harness cannot call that
  # Gateway, so the tool block above would be present, advertised to the model, and fail on use -- the
  # exact class of half-wired grant that has cost this stage several runs.
  #
  # ONE GATEWAY ARN, never `gateway/*`. The research and brief agents have their own Gateways in this
  # account, and the whole reason three separate agents exist is that their instructions and their
  # reach must not be interchangeable.
  dynamic "statement" {
    for_each = var.manage_mcp_gateway ? [1] : []
    content {
      sid       = "InvokeTheClusterReadGateway"
      effect    = "Allow"
      actions   = ["bedrock-agentcore:InvokeGateway"]
      resources = [aws_bedrockagentcore_gateway.mcp[0].gateway_arn]
    }
  }

  # DELIBERATELY ABSENT: every `s3:` action. The bundle reaches the model in the PROMPT, read by the
  #   proxy under the proxy's own scoped grant. So "the agent cannot see anything the capture did not
  #   record" is true by construction rather than by this omission -- but the omission is what keeps it
  #   true if somebody later adds a tool.
  # DELIBERATELY ABSENT: every `eks:` action and every cluster ARN. This agent triages a cluster
  #   change and cannot make one, cannot read a cluster, and cannot open a lane.
  # DELIBERATELY ABSENT: every `dynamodb:` action. No fact store and no token store: a triage is about
  #   one cluster's one component and must not be able to write anything fleet-wide.
  # DELIBERATELY ABSENT: every `lambda:` action. It cannot invoke the proxy that invoked it, which is
  #   the loop that would let a model re-ask itself with a prompt it chose.
}

resource "aws_iam_role_policy" "harness" {
  count = var.manage_agentcore ? 1 : 0

  name   = "harness-permissions"
  role   = aws_iam_role.harness[0].id
  policy = data.aws_iam_policy_document.harness.json
}

resource "aws_bedrockagentcore_harness" "upgrade" {
  count = var.manage_agentcore ? 1 : 0

  harness_name       = var.upgrade_harness_name
  execution_role_arn = aws_iam_role.harness[0].arn

  model {
    bedrock_model_config {
      model_id = var.upgrade_model_id

      # `converse_stream` MUST match what the proxy parses. `upgrade_triage.py::_extract_text` walks a
      # converse-style event stream for `contentBlockDelta` text; changing this without changing that
      # produces a Harness that answers correctly into a shape nothing reads, and every component then
      # reports "the Harness returned no text".
      api_format = "converse_stream"

      max_tokens = var.upgrade_max_tokens

      # REASONING EFFORT, through the only door the provider offers -- there is no `effort` attribute
      # on this block. `additionalModelRequestFields` is the key the service named itself; the
      # anthropic-native `output_config` object goes inside it. See `infra/terraform/modules/brief/harness.tf` for
      # the 400 that established this, and treat a passthrough as unverified until an invocation
      # proves it: one the service silently ignores looks identical to one it honours.
      #
      # HIGHER EFFORT THAN THE BRIEF AGENT'S, deliberately. The brief judges what an operator would
      # care about in a CHANGELOG and is read as advice. This decides whether returning a component to
      # its previous version would hide something a person must see, from logs and events, and the
      # cost of getting it wrong is a cluster left in a state nobody looked at.
      additional_params = jsonencode({
        additionalModelRequestFields = {
          output_config = {
            effort = var.upgrade_effort
          }
        }
      })

      # No `temperature` and no `top_p`: this model removed temperature and setting it returns 400.
    }
  }

  system_prompt {
    text = local.system_prompt
  }

  # `allowed_tools` IS ABSENT ON PURPOSE AND MUST STAY ABSENT. Measured against this live harness:
  # passing it alongside a tool SUPPRESSED that tool -- the model returned `end_turn` with no tool call
  # at all -- while omitting it returned a real tool call. It is not the allowlist its name suggests: it
  # FILTERS tools configured on this resource, so naming anything would exclude the Gateway, which is
  # the agent's only read path. Which tools the agent sees is decided by the Gateway target's
  # `mcp_tool_schema`, where four of the server's twenty are declared.
  #
  # AND THE DEPLOYED RESOURCE REPORTS `allowed_tools = ["*"]` WHATEVER IS WRITTEN HERE. That is what
  # admits the harness's own built-in `shell` tool, which the model reached for when it had no other
  # tools and ran `aws eks list-clusters` under this execution role. It failed only because this role
  # holds `bedrock:InvokeModel`, workload identity and logs, and nothing else -- so the minimal policy
  # above is load-bearing in a way it does not look.

  # Disabled: each invocation triages ONE component from a clean slate. Carrying conversation across
  # components is how one add-on's logs end up cited for another, and the proxy already mints a
  # session id per component per run so that cannot happen silently.
  memory {
    disabled {}
  }

  # SLIDING WINDOW RATHER THAN SUMMARISATION, and it is a correctness choice. The pod logs and events
  # ARE the evidence here. A summarised window would leave the agent citing a paraphrase of a log line
  # instead of the line, and the system prompt requires it to quote the bundle when the exact text
  # matters -- which it could then no longer do.
  #
  # Written with `=` rather than as a block: the provider models `truncation` as a list-of-object
  # ATTRIBUTE, and both strategy keys must be present in the object even though only one is in use.
  truncation = [{
    strategy = "sliding_window"
    config = [{
      sliding_window = [{
        messages_count = var.upgrade_truncation_messages_count
      }]
      summarization = null
    }]
  }]

  # RAISED WHEN THE AGENT GOT TOOLS. The old sentence here was "with no tools there is no fetch loop to
  # run: the model reads one prompt and answers once", which stopped being true -- each cluster read is
  # an iteration the harness counts. An iteration budget
  # larger than that would only buy a model room to talk to itself while the lane's access entry
  # stays open.
  max_iterations  = var.upgrade_max_iterations
  timeout_seconds = var.upgrade_harness_timeout_seconds

  # THE READ PATH, WHEN THE GATEWAY EXISTS. Four EKS MCP tools -- list_k8s_resources,
  # read_k8s_resource, get_pod_logs, get_k8s_events -- reached through an AgentCore Gateway that signs
  # SigV4 as its own role and holds AmazonEKSAdminViewPolicy on one cluster.
  #
  # THE HARNESS CALLS THESE ITSELF, which is what makes the whole read path one API call. The proxy
  # sends no `tools` parameter and runs no loop: it asks once per checkpoint and the reading happens
  # inside that call.
  #
  # CONDITIONAL ON THE GATEWAY EXISTING, WHICH IS NOT THE SAME AS OPTIONAL. `manage_mcp_gateway` is
  # set true in the deploy root; with it false this block vanishes and the harness has NO
  # tools at all -- there is no other read path to fall back to, so the agent would be asked to judge a
  # component's health with no way to look. It answered that question once in that state, by reaching
  # for its built-in `shell` tool.
  dynamic "tool" {
    for_each = var.manage_mcp_gateway ? [1] : []
    content {
      name = "cluster_reads"
      type = "agentcore_gateway"
      config {
        agentcore_gateway {
          gateway_arn = aws_bedrockagentcore_gateway.mcp[0].gateway_arn
          outbound_auth {
            aws_iam = true
          }
        }
      }
    }
  }

  # No `authorizer_configuration`: its absence selects AWS_IAM, so reaching this agent needs signed
  # AWS credentials -- and the only principal granted `InvokeHarness` on it is the proxy below.

  # A PROVIDER BUG, WORKED AROUND RATHER THAN FOUGHT, AND IT COSTS NOTHING HERE BECAUSE THIS FILE
  # NEVER SETS THE ATTRIBUTE. `environment_variables` is computed and marked sensitive by the
  # provider. On create it returns a value; the next plan then proposes setting it to `null`, and on
  # apply the provider's own consistency check refuses its own response:
  #
  #     Error: Provider produced inconsistent result after apply
  #     .environment_variables: inconsistent values for sensitive attribute
  #     This is a bug in the provider, which should be reported [...]
  #
  # Measured here: the harness WAS created -- it came back with an ARN -- but the apply aborted on
  # that check, which tainted the resource and skipped every resource after it in the graph. Without
  # this block the following plan proposes a delete-and-recreate that fails the same way, so each
  # attempt abandons a harness in the account and makes no progress.
  #
  # Ignoring it is safe because nothing in this configuration writes it: the entire diff it produces
  # is `(sensitive value) -> null` on an attribute we do not manage. If this harness ever DOES need
  # environment variables, this line has to go and the bug has to be dealt with properly -- which is
  # why it names the one attribute instead of being written as `all`.
  lifecycle {
    ignore_changes = [environment_variables]
  }
}
