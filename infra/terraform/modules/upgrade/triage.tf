# The proxy: the one Lambda in this component, and the only caller of the Harness.
#
# WHY A PROXY EXISTS AT ALL, as in infra/terraform/modules/research and infra/terraform/modules/brief: a harness-managed runtime
# refuses the `InvokeAgentRuntime` API -- "is managed by a harness and cannot be invoked directly. Use
# the InvokeHarness API" -- and there is no `invokeHarness` SDK integration for Step Functions or for a
# buildspec. So something invokes a Lambda, and this is that Lambda.
#
# READ THAT SENTENCE AS BEING ABOUT THE API, NOT ABOUT IAM, because the two use the same words for
# different things and the confusion cost this stage two runs. The API to call is `InvokeHarness`; the
# API that a harness-managed runtime REFUSES is `InvokeAgentRuntime`; and IAM authorises the
# `InvokeHarness` call by requiring BOTH `bedrock-agentcore:InvokeHarness` AND
# `bedrock-agentcore:InvokeAgentRuntime` on the harness ARN. The policy statement below carries the
# measured proof for both, including the intermediate wrong answer.
#
# ITS CALLER IS THE LANE'S CODEBUILD BUILD, NOT THE STATE MACHINE, and that is the one unusual thing
# about this component. The triage has to happen at a precise moment inside the lane -- after
# validation refuses the component and after the `after` bundle is SEALED, and before the rollback --
# and only the build knows when that is. Putting it in the state machine would mean a second build to
# act on the answer, with the access entry open across both.
#
# IT WRITES NOTHING. Its grant is `s3:GetObject` under the diagnosis prefix, the two invoke actions on
# one Harness ARN, and `lambda:InvokeFunction` on one function -- the read tools, which it calls on the
# The verdict travels back as this function's RETURN VALUE and the build is what records
# it, so a model's output cannot reach an object a reader trusts without passing through code that
# validated it against `UpgradeTriage` first.

resource "aws_cloudwatch_log_group" "triage" {
  name              = "/aws/lambda/${local.name_prefix}-upgrade-triage"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

data "aws_iam_policy_document" "triage_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "triage" {
  name               = "${local.name_prefix}-upgrade-triage"
  description        = "Reads one sealed diagnosis bundle and asks the upgrade agent. Writes nothing."
  assume_role_policy = data.aws_iam_policy_document.triage_assume.json
}

data "aws_iam_policy_document" "triage" {
  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.triage.arn}:*"]
  }

  # READ THE DIAGNOSIS BUNDLES AND NOTHING ELSE IN THE BUCKET. One prefix, named. Every stage
  # document, every approval decision and every release lives in this same bucket, and a proxy that
  # could read them could put a plan or an authorisation into a model's prompt -- which is the one
  # thing this whole system's read/write split exists to prevent.
  statement {
    sid    = "ReadTheDiagnosisBundles"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket_arn}/upgrade-diagnosis/*"]
  }

  # SCOPED TO THIS HARNESS, not `harness/*`. A wildcard would let this proxy invoke the research
  # agent's Harness and the brief agent's -- and the whole reason three separate Harnesses exist is
  # that their instructions must not be interchangeable.
  #
  # ONE API CALL, TWO IAM ACTIONS, AND IT TOOK TWO FAILED RUNS TO ESTABLISH THAT. `InvokeHarness` is
  # authorised by BOTH `bedrock-agentcore:InvokeAgentRuntime` AND `bedrock-agentcore:InvokeHarness` on
  # the harness ARN. Granting either one alone is denied on the other. Measured, in order:
  #
  #   granted InvokeHarness only ->
  #     not authorized to perform: bedrock-agentcore:InvokeAgentRuntime on resource:
  #     .../harness/readiness_upgrade-...
  #
  #   granted InvokeAgentRuntime only ->
  #     not authorized to perform: bedrock-agentcore:InvokeHarness on resource:
  #     .../harness/readiness_upgrade-...  because no identity-based policy allows the
  #     bedrock-agentcore:InvokeHarness action
  #
  # THE PREVIOUS VERSION OF THIS COMMENT ASSERTED THE OPPOSITE and was wrong: it said `InvokeHarness`
  # "is not the action the authorizer checks", so granting it alongside would be a dead grant. The
  # second error above is that claim being falsified. The reasoning that produced it was sound and the
  # premise was not -- there was one observation, and one observation could not distinguish "the
  # authorizer wants A instead of B" from "the authorizer wants A as well as B".
  #
  # BOTH COSTS NOTHING EXTRA IN REACH. Two action names, one resource, one API. Neither name grants
  # anything the other does not already imply for this call, so the pair is not wider than whichever
  # one turns out to be load-bearing -- it is just the pair the service actually requires.
  #
  # Each failure cost a full run: every checkpoint returned `available: false`, the health deadline
  # expired with no verdict, and an upgrade that had landed cleanly was rolled back.
  statement {
    sid    = "InvokeOwnHarness"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:InvokeHarness",
      "bedrock-agentcore:InvokeAgentRuntime",
    ]
    resources = [local.effective_harness_arn]
  }

  # DELIBERATELY ABSENT: every `s3:PutObject`. The verdict is a return value. A proxy that could write
  #   would be a path from a model's output to an object a later reader trusts, with nothing in between
  #   that validated it.
  # DELIBERATELY ABSENT: every `eks:` action and every cluster ARN. It triages a cluster change and
  #   cannot make one or read one.
  # DELIBERATELY ABSENT: every `dynamodb:` action. No fact store, no token store.
  # DELIBERATELY ABSENT: `bedrock:InvokeModel`. The proxy reaches the model only THROUGH the Harness,
  #   which is what applies the reviewed system prompt. A direct grant would be a way to ask the same
  #   model a different question with no prompt anybody reviewed.
  # DELIBERATELY ABSENT: every `states:` action. It cannot resume, fail or signal any execution.
}

resource "aws_iam_role_policy" "triage" {
  name   = "upgrade-triage-permissions"
  role   = aws_iam_role.triage.id
  policy = data.aws_iam_policy_document.triage.json
}

resource "aws_lambda_function" "triage" {
  function_name = "${local.name_prefix}-upgrade-triage"
  description   = "Triage one upgraded component from its sealed diagnosis bundle. Never raises."
  role          = aws_iam_role.triage.arn
  handler       = "entrypoints.upgrade_harness.stage_upgrade_triage"
  runtime       = "python3.12"
  architectures = ["arm64"]

  # LONGER THAN A STAGE AND SHORTER THAN THE BUILD. The proxy allows one re-ask, each bounded by a
  # 180s read timeout, so the ceiling is two model loops plus the bundle read. The build is waiting on
  # this with the lane's access entry open, which is why the number is a ceiling and not a generous
  # default.
  # 900 IS AWS'S HARD MAXIMUM for a Lambda, and this needs it: one checkpoint is a whole model loop
  # with twenty tool calls against a cluster, measured at 158s on a healthy single-pod component and
  # slower on anything bigger. The proxy's own read timeout is 870, so this outlives it by 30 seconds
  # -- enough to return `available: false` with a reason rather than being killed mid-answer.
  timeout     = 900
  memory_size = 1024

  # Its own zip, carrying a boto3 new enough for `InvokeHarness` -- the Lambda runtime's bundled copy
  # is older than the API, and `entrypoints/stages.py`'s package deliberately ships no boto3 at all.
  filename         = local.package
  source_code_hash = filebase64sha256(local.package)

  environment {
    variables = {
      UPGRADE_HARNESS_ARN = local.effective_harness_arn
      DOCUMENT_BUCKET     = local.bucket
    }
  }

  depends_on = [aws_cloudwatch_log_group.triage]

  lifecycle {
    # FAIL CLOSED AT PLAN TIME. A proxy deployed against an empty or wrong Harness ARN starts, calls
    # nothing, and reports "UPGRADE_HARNESS_ARN is unset" for every component in the run -- which the
    # lane then treats as "triage unavailable" and rolls back, silently, forever. That is the state
    # the research component spent a day in. A plan-time error names the missing input; that runtime
    # symptom does not.
    precondition {
      condition     = can(regex("^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:", local.effective_harness_arn))
      error_message = "The upgrade triage proxy needs a real Harness ARN. Set manage_agentcore = true, or pass upgrade_harness_arn."
    }
  }
}

# THE ONE PRINCIPAL THAT MAY INVOKE THIS PROXY, named by ARN. Without this the build's
# `lambda:InvokeFunction` grant is half a permission: a resource policy is what lets a principal in
# another component's IAM reach this function, and the alternative is a role-side grant with no
# corresponding statement here, which reads as "anyone in the account with lambda:InvokeFunction".
#
# DECLARED HERE RATHER THAN IN `platform`, matching `infra/terraform/modules/brief/gateway.tf`'s reasoning: the
# statement that grants access to THIS function belongs to the component that owns THIS function, so
# removing this component removes its own reachability and nothing else's. The hazard worth naming is
# the same one: neither module's plan shows the other's half, so an auditor checking who may invoke
# this function has to look in both.
resource "aws_lambda_permission" "build_invokes_triage" {
  # Gated on the same input that decides whether platform creates the build role
  # (`upgrade_target_cluster != ""`), so the count is known at plan time even when the role
  # ARN itself is not.
  count = var.upgrade_target_cluster == "" ? 0 : 1

  statement_id  = "AllowUpgradeLaneBuildInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.triage.function_name
  principal     = var.upgrade_build_role_arn
}
