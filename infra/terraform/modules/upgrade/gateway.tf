# THE AGENT'S READ PATH: an AgentCore Gateway in front of AWS's own EKS MCP server, and the only way
# this agent sees a cluster.
#
# FOUR TOOLS OUT OF THE SERVER'S TWENTY, declared on the target in `mcp_tools.json`:
#
#     list_k8s_resources    pods, deployments, replicasets, endpoints -- by kind, in one namespace
#     read_k8s_resource     one named object in full, including its status and conditions
#     get_pod_logs          a pod's log, with `previous` for the container that already died
#     get_k8s_events        pull, probe, scheduling and OOM failures, before they surface elsewhere
#
# The server's own declared schema was read from `tools/list` rather than assumed.
#
# WHY FOUR AND NOT MORE. `get_eks_insights` is the tempting one -- AWS's own UPGRADE_READINESS
# assessment -- and it answers a different question. Insights assesses whether a cluster is READY TO
# UPGRADE; this agent runs AFTER one component has been upgraded and says whether that component came
# up healthy. Handing it a cluster-wide pre-upgrade report invites it to cite a finding about something
# it never touched as a reason to roll back. The job, in the user's words: "to tell whether the
# componnt upgraded is healthy or not by looking at basic checks a platform engineer would do for the
# component and then the logs and stuff". Pods, rollout, logs, events.
#
# READ-ONLY IS ENFORCED BY DISTINCT IAM ACTIONS, not by anything in this repository. The server is not
# read-only: `apply_yaml`, `manage_k8s_resource`, `manage_eks_stacks` and `add_inline_policy` all
# mutate, and every one of them requires `eks-mcp:CallPrivilegedTool`. `AmazonEKSMCPReadOnlyAccess`
# grants `CallReadOnlyTool` and never the privileged one -- the same guarantee Stage 1 has relied on
# since 2026-09-12. The access entry below carries `AmazonEKSAdminViewPolicy`, which has no write verb
# and DOES read secrets -- see that resource for the trade.
#
# THE MODEL CAN NAME A CLUSTER AND A NAMESPACE, and three things narrow that. `list_k8s_resources`
# takes both as arguments, so the fence cannot be "there is nothing to name":
#   * the Gateway's role is scoped to ONE cluster ARN, so no other cluster is reachable
#   * `mcp_tool_schema` on the target decides which tools are advertised at all
#   * the prompt names one component and the verdict must echo that name back
# The first is the only one the authorizer enforces, and it is the one that bounds the blast radius.
#
# `manage_mcp_gateway = true` IS SET IN THE DEPLOY ROOT rather than left to an operator. The variable
# defaults false, so with the value unset a plain `terraform plan` proposed
# destroying all seven resources here while `harness.tf` kept its tool block -- an agent invoked, asked
# to judge health, and pointed at a Gateway ARN that no longer exists.

variable "manage_mcp_gateway" {
  description = <<-EOT
    Whether to create the MCP Gateway and its target.

    TRUE IN THE DEPLOY ROOT, WHICH IS THE VALUE THAT MATTERS. The default is false and FALSE IS
    NOT A STATE THIS DEPLOYMENT CAN RETURN TO: `harness.tf`'s `tool` block points at this Gateway, so
    false does not mean "no Gateway yet", it means "delete the agent's only read path from under a
    wired harness".

    READ THE DEFAULT AS UNSAFE, exactly as `workflow.tf` says of the upgrade stage's flag. It is kept
    as a variable rather than hard-coded so a checkout running against a Gateway owned elsewhere can
    still plan -- the same shape `manage_agentcore` has.
  EOT
  type        = bool
  default     = false
}

variable "mcp_endpoint" {
  description = <<-EOT
    The EKS MCP server's streamable-HTTP endpoint.

    Same template `scan/adapters/eks_mcp.py` uses, and it is a variable rather than a literal so a
    probe can point at a different region without editing this file.
  EOT
  type        = string
  default     = null
}

locals {
  effective_mcp_endpoint = coalesce(var.mcp_endpoint, "https://eks-mcp.${var.region}.api.aws/mcp")
}

# THE GATEWAY'S OWN IDENTITY, which is what signs the MCP calls. `credential_provider_configuration`
# below selects `gateway_iam_role`, so the Gateway signs SigV4 as THIS role -- which is why the read
# path needs no hand-built STS token, no presigned URL and no HTTP client of ours anywhere.
data "aws_iam_policy_document" "mcp_gateway_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    # CONFUSED-DEPUTY CONDITIONS, copied from the trust policy on this deployment's two READY
    # gateways rather than invented -- `<prefix>-brief-gateway` and
    # `<prefix>-research-gateway` both carry exactly this pair. Without them the service principal could be
    # induced to assume this role on behalf of some other account's gateway.
    #
    # THEY ARE NOT WHAT FIXED THE FIRST FAILURE, and saying so matters because it would be easy to
    # read this block as the fix. The first apply created the role and the target in one pass, and
    # the target came back FAILED with "Gateway service is not authorized to assume the execution
    # role" -- while the service principal was ALREADY correct and identical to the working
    # gateways'. Conditions only ever narrow, so their absence cannot deny. The likely cause is IAM
    # eventual consistency: the role was seconds old when the gateway tried to assume it, the same
    # race that made `aws_sfn_state_machine` fail once on a policy created in its own apply.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:bedrock-agentcore:${var.region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

resource "aws_iam_role" "mcp_gateway" {
  count = var.manage_mcp_gateway ? 1 : 0

  name               = "${local.name_prefix}-upgrade-mcp-gateway"
  description        = "Signs the upgrade agent's EKS MCP reads. Read-only tools only."
  assume_role_policy = data.aws_iam_policy_document.mcp_gateway_assume.json
}

# READ-ONLY BY IAM ACTION, NOT BY CHOICE OF TOOL. This managed policy is the same one every onboarded
# cluster-read role carries, and its whole point is that it grants `eks-mcp:CallReadOnlyTool` and never
# `eks-mcp:CallPrivilegedTool`. A future change that wanted the agent to mutate would have to attach a
# different policy, which is a visible edit here rather than a tool name nobody noticed.
resource "aws_iam_role_policy_attachment" "mcp_gateway_readonly" {
  count = var.manage_mcp_gateway ? 1 : 0

  role       = aws_iam_role.mcp_gateway[0].name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEKSMCPReadOnlyAccess"
}

# ONE CLUSTER, BY ARN. The managed policy above says what KIND of tool may be called; this says which
# cluster it may be called against. Without it the agent could read any cluster in the account that MCP
# can reach -- `list_k8s_resources` takes `cluster_name` as an argument, so this is what puts the
# cluster in the authorizer rather than in the prompt.
data "aws_iam_policy_document" "mcp_gateway" {
  count = var.manage_mcp_gateway ? 1 : 0

  statement {
    sid    = "ReachOneClusterThroughMcp"
    effect = "Allow"
    actions = [
      "eks:DescribeCluster",
      "eks:AccessKubernetesApi",
      "eks:ListInsights",
      "eks:DescribeInsight",
      "eks:ListAddons",
      "eks:DescribeAddon",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/${var.upgrade_target_cluster}",
      "arn:${data.aws_partition.current.partition}:eks:${var.region}:${data.aws_caller_identity.current.account_id}:insight/${var.upgrade_target_cluster}/*",
      "arn:${data.aws_partition.current.partition}:eks:${var.region}:${data.aws_caller_identity.current.account_id}:addon/${var.upgrade_target_cluster}/*/*",
    ]
  }

  # DELIBERATELY ABSENT: `eks-mcp:CallPrivilegedTool`. Not merely unattached -- never written here, so
  #   granting it is an addition somebody has to make and justify.
  # DELIBERATELY ABSENT: every `eks:Update*` and `eks:Create*`. The agent judges a cluster change and
  #   must not be able to make one, whatever tool it is handed.
  # DELIBERATELY ABSENT: `logs:*` beyond what the managed policy carries for `get_cloudwatch_logs`.
}

resource "aws_iam_role_policy" "mcp_gateway" {
  count = var.manage_mcp_gateway ? 1 : 0

  name   = "upgrade-mcp-gateway-permissions"
  role   = aws_iam_role.mcp_gateway[0].id
  policy = data.aws_iam_policy_document.mcp_gateway[0].json
}

resource "aws_bedrockagentcore_gateway" "mcp" {
  count = var.manage_mcp_gateway ? 1 : 0

  # HYPHENS. The attribute validates against `^([0-9a-zA-Z][-]?){1,100}$`, so underscores are
  # rejected -- which is the OPPOSITE of `aws_bedrockagentcore_harness.harness_name`, where
  # `readiness_upgrade` is the working value and a hyphen is refused. Two adjacent resources in the
  # same service with inverted naming rules; an earlier version of this file asserted the inversion
  # the wrong way round and the plan caught it.
  name          = "${local.name_prefix}-upgrade-mcp"
  description   = "The upgrade agent's read path: AWS EKS MCP, read-only tools, one cluster."
  role_arn      = aws_iam_role.mcp_gateway[0].arn
  protocol_type = "MCP"

  # AWS_IAM, so reaching this Gateway needs signed credentials -- the same reason the Harness itself
  # carries no `authorizer_configuration`. The only principal that will be allowed to call it is the
  # harness's execution role.
  authorizer_type = "AWS_IAM"
}

resource "aws_bedrockagentcore_gateway_target" "eks_mcp" {
  count = var.manage_mcp_gateway ? 1 : 0

  gateway_identifier = aws_bedrockagentcore_gateway.mcp[0].gateway_id
  # HYPHENS, NOT UNDERSCORES -- same rule as the gateway above, which is not what I first wrote here.
  # `eks_mcp` is rejected at plan time.
  name        = "eks-mcp"
  description = "AWS's EKS MCP server. Read-only tools; the privileged ones are withheld by IAM."

  target_configuration {
    mcp {
      mcp_server {
        endpoint = local.effective_mcp_endpoint

        # FOUR TOOLS OUT OF TWENTY, AND THE SIXTEEN WITHHELD ARE NOT ALL DANGEROUS. The four mutators
        # -- apply_yaml, manage_k8s_resource, manage_eks_stacks, add_inline_policy -- are withheld for
        # the obvious reason, though IAM already refuses them: this role holds no
        # `eks-mcp:CallPrivilegedTool` and AdminView is a read policy carrying no write verb.
        #
        # THE OTHER TWELVE ARE WITHHELD FOR A DIFFERENT AND MORE IMPORTANT REASON: they answer
        # questions this agent was not asked. Its whole job is to look at ONE component that was just
        # upgraded and say whether it came up healthy, the way a platform engineer would -- pods,
        # readiness, restarts, logs, events, rollout. That is it.
        #
        # `get_eks_insights` IS THE CLEAREST CASE AND I ARGUED FOR IT BEFORE THE USER CORRECTED ME. It
        # returns AWS's UPGRADE_READINESS assessment for the CLUSTER -- "kube-proxy version skew" and
        # the like. That is a pre-upgrade, cluster-wide question, already answered by Stages 1 and 3
        # before anyone approved anything, and it says nothing about whether metrics-server 0.9.0 came
        # up thirty seconds ago. Worse, it invites a false verdict: an insight about a DIFFERENT
        # component would read as evidence against this one. `search_eks_documentation`,
        # `get_eks_vpc_config`, `get_cloudwatch_metrics` and the rest are the same mistake in smaller
        # print.
        #
        # AND A NARROW SURFACE IS PROBABLY PART OF WHY THE PREVIOUS ATTEMPT RETURNED NOTHING. Handed
        # twenty tools and a health question, the model spent its iteration budget exploring and never
        # produced a verdict. Four tools with one obvious order of use is a better prompt than any
        # amount of instruction.
        #
        # THE SCHEMAS ARE CAPTURED, NOT WRITTEN. `mcp_tools.json` was produced by listing this very
        # Gateway and keeping four entries verbatim, so the declared parameters cannot drift from what
        # the server accepts. Transcribing them by hand is how a declared argument ends up spelled
        # `pod` when the server wants `pod_name`, and the model then calls it wrongly forever.
        #
        # THE PAYLOAD IS AN OBJECT WITH A `tools` ARRAY, not a bare array. A bare array is accepted by
        # the plan and refused by the service on apply, leaving the target UPDATE_UNSUCCESSFUL:
        #
        #     Invalid MCP ToolSchema. mcpToolSchema must be an object with 'tools' array
        mcp_tool_schema {
          inline_payload {
            payload = file("${path.module}/mcp_tools.json")
          }
        }
      }
    }
  }

  # SIGV4, SIGNED AS THE GATEWAY'S ROLE. This is the attribute that made the whole approach viable: the
  # harness's `remote_mcp` tool type takes only a URL and static headers, which cannot sign an AWS
  # request, so a direct `remote_mcp` to EKS MCP could never have authenticated. A Gateway target can.
  credential_provider_configuration {
    gateway_iam_role {
      service = "eks-mcp"
      region  = var.region
    }
  }
}

output "mcp_gateway_arn" {
  description = <<-EOT
    The Gateway to put in the harness's `tool { config { agentcore_gateway { gateway_arn = ... } } }`.

    Null while `manage_mcp_gateway` is false, which leaves the harness with no tools at all -- see
    that variable for why false is not a state this deployment can return to.
  EOT
  value       = one(aws_bedrockagentcore_gateway.mcp[*].gateway_arn)
}

output "mcp_gateway_url" {
  description = "The Gateway's MCP URL. What a probe points a client at to list the tools it exposes."
  value       = one(aws_bedrockagentcore_gateway.mcp[*].gateway_url)
}

# THE ACCESS ENTRY THAT MAKES THE KUBERNETES TOOLS WORK AT ALL.
#
# WITHOUT THIS, EVERY KUBERNETES READ IS 401 -- AND THAT IS NOT A PRIVILEGE PROBLEM. Measured against
# the live Gateway before this existed:
#
#     list_k8s_resources -> Failed to list Pod resources: Kubernetes API request failed:
#                           Unauthorized (reason: Unauthorized, code: 401)
#     get_eks_insights   -> Successfully retrieved 5 insights ... UPGRADE_READINESS
#
# The split is the explanation. `get_eks_insights` is an EKS CONTROL-PLANE call, so IAM alone
# authorises it. `list_k8s_resources` and `get_pod_logs` go to the cluster's own API server, which
# needs TWO things: `eks:AccessKubernetesApi` in IAM, which the role already had, and an ACCESS ENTRY
# telling the cluster this principal exists at all. With no entry the API server has never heard of it,
# so it refuses everything -- reads included. Nothing about the 401 implied the agent needed write
# access, and an earlier version of this plan muddled the two by talking about privileged tools.
#
# `AmazonEKSAdminViewPolicy`, AND IT WAS `AmazonEKSViewPolicy` UNTIL A LIVE RUN SHOWED THE GAP. View
# binds the built-in `view` ClusterRole, whose apiGroups -- read off this cluster rather than assumed --
# are core, apps, autoscaling, batch, discovery, events, extensions, metrics, networking, policy and
# resource. There is NO `apiregistration.k8s.io`, and the agent's own verdict carried the consequence:
#
#     read_k8s_resource APIService v1beta1.metrics.k8s.io: forbidden (403, cluster-scoped read
#     denied to this role)
#
# For metrics-server the APIService IS the object that says the aggregated API registered, so the agent
# reached HEALTHY on corroborating evidence and said outright it could not confirm the one thing that
# mattered most. `view` also omits `apiextensions` (CRDs), `admissionregistration` (webhooks),
# `storage.k8s.io` and nodes -- so a webhook add-on, a CSI driver and a node-pressure fault would each
# hit the same wall.
#
# THE TRADE, STATED PLAINLY: ADMINVIEW READS SECRETS. `view` excluded them, and "the agent cannot read
# secrets" was a claim this system made and had PROVED with a live probe. It is no longer true of this
# principal. Helm keeps release state in Secrets in `kube-system`, so this agent can now read the
# contents of every release on the cluster, and anything else any namespace keeps in a Secret. The user
# chose this over a hand-written supplementary ClusterRole, explicitly and with the trade named.
#
# WHAT SURVIVES IT. Still no write verb -- AdminView is a read policy -- and the Gateway's IAM holds
# `eks-mcp:CallReadOnlyTool` and never `CallPrivilegedTool`, so a mutating MCP tool is refused by the
# authorizer whatever Kubernetes would permit. The narrowing that actually bounds this grant is the ONE
# CLUSTER ARN on the Gateway's IAM policy, not the Kubernetes verb list.
#
# CLUSTER SCOPE RATHER THAN NAMESPACE, deliberately. The agent must tell "this component is broken"
# from "this cluster is broken", and a node under memory pressure or an unavailable APIService is not
# in the component's namespace. Namespace scope would make a cluster fault look like a component fault
# and roll back a good release.
#
# STANDING RATHER THAN PER-RUN, which is a departure from the write lane and matches scan. The write
# grant is created and destroyed per lane because it can change the cluster; this one cannot change
# anything, so the JIT machinery would buy nothing and would add a failure mode -- an agent that cannot
# read because its entry was not ready yet.
resource "aws_eks_access_entry" "mcp_gateway" {
  count = var.manage_mcp_gateway ? 1 : 0

  cluster_name  = var.upgrade_target_cluster
  principal_arn = aws_iam_role.mcp_gateway[0].arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "mcp_gateway_view" {
  count = var.manage_mcp_gateway ? 1 : 0

  cluster_name  = var.upgrade_target_cluster
  principal_arn = aws_iam_role.mcp_gateway[0].arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSAdminViewPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_access_entry.mcp_gateway]
}
