# The Step Functions state machine: the stages in order.
#
# WHAT IS ACTUALLY DECLARED. Twenty-seven states, and they are not all Tasks -- this header said
# ELEVEN and listed the original set, which stopped being true when the brief's five states landed,
# again when the two approvals did, and again when the approval pre-check did:
#
#   Scan                 Task     Anything to upgrade?  Choice   Anything approved?  Choice
#   Inventory            Task     Brief plan            Task     Release gate        Task
#   Sweep                Task     Any to research?      Choice   Release waiting     Wait
#   Gaps                 Task     Brief research        Map      Release polled      Task
#   Resolve              Task     Brief assemble        Task     Release decided?    Choice
#   Decide               Task     Approval survey       Task     Release approved    Pass
#   Plan                 Task     Anything to approve?  Choice   Release rejected    Fail
#   Research             Map      Approval              Task     Done                Pass
#   Research needed?     Choice   Upgrade approval      Task
#                                 Knowledge changed?    Choice
#
# `Approval survey` AND `Approval` ARE THE SAME LAMBDA, listed separately because they are
# separate states with different shapes: the survey is a plain `lambda:invoke` that counts, the
# wait is `.waitForTaskToken` and stops. That is the only difference between them and it is the
# reason the pause is no longer unconditional.
#
# `entrypoints/stages.py`'s transition table is the authority on the EDGES and
# `tests/test_the_pipeline_table_matches_the_deployed_machine.py` holds it to this file's state
# names. This list is the shapes, which is the thing that decides whether a state can be built by
# the `state_body` comprehension below or has to be written literally.
#
# THE NAMES CARRY NO ORDINALS, AND THAT IS THE NEWEST THING ABOUT THIS FILE. Every state
# used to be `1 Scan`, `2 Inventory`, ... `7 Resolve`. The numbers were dropped because
# they had stopped meaning anything readable: `7 Resolve` ran third, `Sweep` and the report
# carried no number at all while their immediate neighbours did, and a reader seeing
# `3 Gaps -> 7 Resolve -> 4 Decide` had to be told that the digits are identity rather than
# sequence before the graph made sense. A number that must be explained away is worse than
# no number. The order is the graph below and the edges in this file; nothing else claims it.
#
# THE HANDLER FUNCTIONS STILL CARRY THE ORDINALS -- `entrypoints.stages.stage_scan` through
# `entrypoints.stages.stage_resolve` in lambdas.tf, infra/terraform/modules/research and this module's `resolve.tf`. That axis
# was deliberately NOT renamed here: those strings are Lambda configuration pointing at
# Python functions in entrypoints/stages.py, so moving them is a change to code and to three components'
# configuration rather than to this one definition. So "stage 7" remains this project's
# vocabulary for the dispatch stage; `Resolve` is just what the STATE is called.
#
# The five document stages are unconditional. Everything after them is gated, and by TWO
# DIFFERENT gates, which is the whole point of the note below:
#
#   `Research` + BOTH Choice states on `research_function_arn` AND `resolve_function_arn`
#                                   together -- the three locals guarded by the expression
#                                   quoted further down
#   `Resolve`                      on `resolve_function_arn` ALONE -- `local.resolve_state`
#
# (Named by local rather than by line: see the note below on why this header does not cite
# its own line numbers.)
#
# SUPPLYING ONE ALONE IS NOT ONE CASE. IT IS TWO, AND THEY ARE NOT SYMMETRICAL. This comment
# used to describe both as "computes and publishes the dispatch queue but never opens the
# loop". That is true of exactly one of them:
#
#   resolve only  -> `Resolve` exists, so the dispatch queue IS computed and published, and
#                    the loop never opens. Scan, Inventory, Sweep, Gaps, Resolve, Decide,
#                    Plan, then the report entry. This is the useful degraded mode: what WOULD have been
#                    researched is inspectable before any model exists to research it.
#
#   research only -> NEITHER `Resolve` NOR `Research` is emitted. `local.resolve_state` is
#                    empty, so `Gaps` takes its `== null` branch to `Decide` and `Plan` takes
#                    its own to the report entry, and `Research`, `Research needed?` and `Knowledge
#                    changed?` are all gated on BOTH ARNs -- so the research ARN is accepted,
#                    validated, and then reaches nothing at all. Scan, Inventory, Sweep,
#                    Gaps, Decide, Plan, then the report entry. No dispatch queue is published and no
#                    model is ever invoked.
#
# THE SECOND CASE IS THE DANGEROUS ONE, AND IT IS SILENT. The definition is well-formed, the
# apply succeeds, and every run SUCCEEDS with two stages missing. Nothing fails, so nothing
# tells the operator that the research ARN they supplied is doing nothing whatsoever. If you
# want research, supply both; the deploy root (`infra/terraform/deploy/main.tf`) wires both for exactly this reason.
#
# THE ORDER, which is now the only place order is recorded:
#
#   Scan -> Inventory -> Sweep -> Gaps -> Resolve -> Decide -> Plan
#        -> Research needed? -> Research -> Approval survey -> Approval -> Knowledge changed?
#        -> the report entry (`local.report_entry`: the brief when it is wired, else straight
#           to `Upgrade approval` or `Readiness report`)
#
# `Resolve` runs THIRD of the Tasks, not last. It moved out from behind `Plan` to directly
# behind `Gaps` because the gap ledger is its entire input -- see the note on
# `stage_states."Gaps"` below -- so running it after Decide and Plan was arbitrary.
#
# `Sweep` sits BETWEEN `Inventory` and `Gaps`. It is NOT a gate -- its Lambda, role and
# policy are created in this module (sweep.tf), so there is no ARN to supply and nothing to
# leave null. All of it, including the successor of `Inventory`, lives in sweep.tf; this
# file holds three references and no logic: the successor local in `stage_states`, the merge
# into `task_states`, and the invoke grant above.
#
# The three places that enforce the pairing are the locals guarded by
#
#     var.research_function_arn == null || local.resolve_function_arn == null ? {} : {
#
# so `grep -n` finds them. Cited as an expression rather than by line number on purpose:
# an earlier version of this header named lines 221, 297 and 329, and rewriting the header
# itself shifted them by nineteen lines and made all three wrong immediately. A citation
# that its own edit invalidates is worse than no citation.
#
# ALL THREE GATE VARIABLES DEFAULT TO NULL, WHICH IS A TRAP, AND THE DEPLOY ROOT IS WHAT
# CLOSES IT. On defaults alone this module deploys stages 1-5 and would REMOVE every later
# state already on the live machine -- not a destroy or a replace that a guard would catch,
# but a plan that quietly un-wires a working stage. `infra/terraform/deploy/main.tf` wires
# every gate from the module that owns the function, so an apply always produces the
# complete pipeline. Do not read the null defaults as "safe".
#
# Each state passes the whole payload to the next, and each stage adds its own
# document URI and digest to it, so the execution's final output carries the URI and
# digest of every document the run produced -- which is what lets a caller report a
# finished run without opening a single object.
#
# THE CHAIN IS NOT A PURE CHAIN. Every edge exists because the state on its far side needs
# what the state on its near side produced, and a stage that fails stops the run rather than
# letting a later stage cite an upstream digest that does not exist. What it is not is a
# straight line: `Resolve` needs only the ledger, so it sits between `Gaps` and `Decide`,
# and Decide and Plan run after it having never been its inputs. Dropping the ordinals is
# what stopped the names from claiming otherwise.
#
# The research loop breaks the straight line further: `Research` fans out over the
# dispatch queue as a Map, and the two Choice states send the run back to `Gaps` when
# research changed the knowledge it was decided against. There are still no retries on
# validation errors, for the reason above.
#
# WITHOUT `Approval` THAT LOOP-BACK CANNOT FIRE, which is worth knowing before reading the Choice
# states as though they were live logic. `Knowledge changed?` compares
# `$.store_digest_after_research` against `$.store_digest_before_research`, and NOTHING
# sets the former: no handler returns it, and `Research` is a Map whose
# `ResultPath` is `$.research_results`, so a per-item field could not reach the payload
# root where the Choice looks for it. The `IsPresent = false` branch therefore wins on
# every execution and routes to the report entry, and `Default -> "Gaps"` is unreachable.
# Observed on a test run. This reorder neither
# causes nor fixes that; it is recorded here because the comments on those branches read
# as though the comparison were in force.
#
# `Approval` IS GATED ON `approval_function_arn`, which the deploy root wires from the research
# module. It is what makes the loop-back above
# REACHABLE, and the mechanism is worth stating because the paragraph above explains only
# why it is dead. Approval is the one thing that can write a fact inside an execution, so
# pausing between the Map and the Choice is what lets the store digest move before the
# comparison runs -- and its `ResultPath` nests the resumed output under `$.approval`, which
# is the path the Choice's new leading branch reads. Without that ARN, `Research` goes
# straight to `Knowledge changed?`.

resource "aws_cloudwatch_log_group" "workflow" {
  name              = "/aws/vendedlogs/states/${local.name_prefix}"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.log_kms_key_arn
}

data "aws_iam_policy_document" "states_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }

    # Confused-deputy guard: this role is assumable only on behalf of a state
    # machine in this account.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "workflow" {
  name               = "${local.name_prefix}-workflow"
  description        = "Sequences the five readiness stages. Can invoke those five functions and nothing else."
  assume_role_policy = data.aws_iam_policy_document.states_assume.json
}

data "aws_iam_policy_document" "workflow" {
  #checkov:skip=CKV_AWS_356:Log-delivery actions do not support resource-level permissions, and the release-gate execution id does not exist at plan time.
  # Exactly the functions this state machine invokes, and nothing else. The
  # presentation function joins the list only when the report is wired in, so the
  # role never carries a permission for a state that does not exist.
  #
  # The sid still reads "FiveStages" deliberately. A sid is part of the policy
  # document, so renaming it would make `terraform apply` with default variables an
  # in-place UPDATE of a deployed role policy instead of the zero-change no-op it
  # must be while the pipeline is being tested against. It is renamed in the same
  # change that turns the report on.
  statement {
    sid     = "InvokeTheFiveStages"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = concat(
      [for k, _ in local.stages : aws_lambda_function.stage[k].arn],
      local.resolve_function_arn == null ? [] : [local.resolve_function_arn],
      var.research_function_arn == null ? [] : [var.research_function_arn],
      # The upgrade approval. Created in this module, so its ARN is a reference rather than a
      # variable and there is no null case to guard -- see upgrade_approval.tf on why this
      # stage is not gated.
      [aws_lambda_function.upgrade_approval.arn],
      # The approval stage. Invoke only -- this role is NOT granted SendTaskSuccess, and
      # must not be: the state machine handing itself the power to resume its own wait
      # would make the human boundary decorative.
      var.approval_function_arn == null ? [] : [var.approval_function_arn],
      # The freshness sweep. Unconditional, unlike the three above: its function is
      # created in this module (sweep.tf), so there is no null-ARN case to guard.
      [aws_lambda_function.sweep.arn],
      # The upgrade brief's three ends. All-or-nothing with `local.brief_enabled`, so a partially
      # configured brief grants nothing rather than granting one third of a path.
      !local.brief_enabled ? [] : [
        var.brief_plan_function_arn,
        var.brief_component_function_arn,
        var.brief_assemble_function_arn,
      ],
    )
  }

  statement {
    sid    = "DeliverExecutionLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    # These log-delivery actions do not support resource-level permissions.
    resources = ["*"]
  }

  # Start the release gate's runbook, and no other runbook in this account.
  #
  # THE RESOURCE TYPE IS `automation-definition`, NOT `document`, and this is the sort of detail
  # that produces a gate denied on every run. `aws_ssm_document.release_gate.arn` returns
  # `...:document/<name>`, which `ssm:StartAutomationExecution` does not authorise on -- so the
  # ARN is composed here rather than referenced. `:*` covers the version, which moves whenever
  # the document's content changes.
  #
  # AN UNSCOPED GRANT WOULD BE THE REAL DEFECT, not a tidiness one. `ssm:StartAutomationExecution`
  # on `*` lets this state machine run ANY runbook in the account, including the AWS-owned ones
  # that terminate instances and delete volumes -- a far larger capability than anything else this
  # role holds.
  statement {
    sid       = "StartTheReleaseGate"
    effect    = "Allow"
    actions   = ["ssm:StartAutomationExecution"]
    resources = ["arn:${data.aws_partition.current.partition}:ssm:${var.region}:${local.account_id}:automation-definition/${aws_ssm_document.release_gate.name}:*"]
  }

  # Watch it while a person reads it. `*` for the approver policy's reason: the execution id does
  # not exist at plan time, so there is nothing narrower to name. This is a read of this account's
  # own automations and grants no ability to start, signal or stop one.
  statement {
    sid       = "WatchTheReleaseGate"
    effect    = "Allow"
    actions   = ["ssm:GetAutomationExecution"]
    resources = ["*"]
  }

  # HAND SSM THE AUTOMATION'S ROLE. `StartAutomationExecution` with an `AutomationAssumeRole` is a
  # PassRole, and the start is DENIED without this -- which would surface as `Release gate`
  # catching to `Release rejected` on every run rather than as anything naming IAM.
  #
  # Scoped to the one role AND conditioned on the one service that may receive it, so this cannot
  # be turned into a way to pass some other role to somewhere else.
  statement {
    sid       = "PassTheAutomationRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.release_gate_automation.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ssm.amazonaws.com"]
    }
  }

  # WRITE THE RELEASE, AND ONLY THE RELEASE. `Release recorded` puts one object per authorised run
  # under `releases/`, with no Lambda in the path -- so the ONLY principal in this deployment that
  # can write an authorisation is this role, driven by a state that runs after SSM reported
  # `Success`.
  #
  # THAT EXCLUSION IS THE POINT, not the grant. The approval stage renders the page a person
  # clicks and the watcher records their clicks; neither can reach this prefix, because a stage
  # able to write a release could manufacture an authorisation nobody gave. `upgrade_approval.tf`
  # makes the same argument about `upgrade-decisions/` in the other direction.
  #
  # PutObject ONLY, and on one prefix. No `s3:GetObject` anywhere in this role: the state machine
  # reads no document -- every stage reads its own -- and a release is written once and never
  # re-read by the thing that wrote it.
  statement {
    sid       = "RecordTheRelease"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.documents.arn}/releases/*"]
  }

  # STILL DELIBERATELY ABSENT: `states:SendTaskSuccess`. The note at `InvokeTheFiveStages` says a
  # state machine able to resume its own wait makes the human boundary decorative, and that is
  # now true of three waits rather than two. The release gate does not even need the permission
  # withheld here -- it has no task token to resume with, by construction.
  #
  # DELIBERATELY ABSENT: every `eks:` action. The approver list is resolved by a Lambda whose own
  # role holds the three read-only calls; this role invokes that function and reads its answer. A
  # state machine that could read access entries itself would be a second path to the same data
  # with no test around it.
}

resource "aws_iam_role_policy" "workflow" {
  name   = "invoke-stages"
  role   = aws_iam_role.workflow.id
  policy = data.aws_iam_policy_document.workflow.json
}

locals {
  # Retry only on genuinely transient faults. A ValidationError or a
  # RulesetUnavailable is a defect, and retrying it three times just delays the
  # report of a real problem.
  transient_retry = [{
    ErrorEquals = [
      "Lambda.ServiceException",
      "Lambda.AWSLambdaException",
      "Lambda.SdkClientException",
      "Lambda.TooManyRequestsException",
    ]
    IntervalSeconds = 2
    MaxAttempts     = 3
    BackoffRate     = 2.0
  }]

  # The scan stage QUEUES, so it needs a far longer budget than the others.
  #
  # WHY IT DIFFERS. `Scan` has a reserved concurrency of 1 (see lambdas.tf), because
  # the EKS MCP Server returns 429 for concurrent sessions. A second run therefore gets
  # `Lambda.TooManyRequestsException` and must WAIT for the run ahead of it, not fail.
  #
  # WHY THE DEFAULT LADDER WAS NOT ENOUGH, and this arithmetic is the point: a scan
  # takes ~31s of sequential MCP calls, while 2/4/8s over three attempts exhausts in
  # 14s -- 17 seconds before the run ahead could possibly finish. Bounding concurrency
  # without widening this would have swapped a 429 for a throttle error and still
  # failed, while looking like a fix.
  #
  # 30s over 5 attempts at 1.5x is ~30+45+67+101 = ~243s of waiting, so roughly six
  # queued scans clear. Past that the run fails and says so, which is the honest
  # outcome: silently queueing a hundred scans for an hour would be worse than
  # refusing one.
  scan_retry = [{
    ErrorEquals = [
      "Lambda.TooManyRequestsException",
      "Lambda.ServiceException",
      "Lambda.AWSLambdaException",
      "Lambda.SdkClientException",
    ]
    IntervalSeconds = 30
    MaxAttempts     = 5
    BackoffRate     = 1.5
  }]

  stage_states = {
    "Scan" = {
      arn     = aws_lambda_function.stage["scan"].arn
      next    = "Inventory"
      comment = "Read the cluster under its own read-only role. Writes a sanitized snapshot."
    }
    "Inventory" = {
      arn = aws_lambda_function.stage["inventory"].arn
      # `Gaps`, or the freshness sweep when one is wired in. Expressed as a local in
      # sweep.tf rather than a conditional here, so the successor is decided in one place.
      next    = local.after_inventory
      comment = "One record per component: identity, true version, confidence, classification."
    }
    "Gaps" = {
      arn = aws_lambda_function.stage["gaps"].arn
      # `Resolve` when the dispatch stage is wired in, `Decide` otherwise.
      #
      # RESOLVE SITS HERE, DIRECTLY AFTER GAP DETECTION, and the reason is that its whole
      # input is the ledger this stage just wrote. `stages/resolve.py` reads
      # `event["ledger_uri"]` (resolve.py:81) and `event["snapshot_captured_at"]`
      # (resolve.py:103) off the payload and nothing else -- no assessment, no plan, no
      # digest of either. So its inputs are complete the moment `Gaps` returns. It used
      # to run after `Plan`, which was arbitrary rather than wrong: nothing Decide or
      # Plan produces is an input to it. Putting "what should we research" next to "what
      # do we not know" is the whole point of the move.
      next    = local.resolve_function_arn == null ? "Decide" : "Resolve"
      comment = "What is known and what is missing, every unknown typed."
    }
    "Decide" = {
      arn     = aws_lambda_function.stage["decide"].arn
      next    = "Plan"
      comment = "A verdict per component per hop. The only stage that issues verdicts."
    }
    "Plan" = {
      arn = aws_lambda_function.stage["plan"].arn
      # The research loop's entry, or the report, or the end of the run.
      #
      # BOTH ARNs, NOT RESEARCH'S ALONE, and that is the one trap in this expression.
      # `Research needed?` exists only when research AND resolve are supplied together --
      # the guard on `choice_state` below. Naming the Choice on `research != null` alone
      # would name a state absent from `States` whenever resolve is null, and Step
      # Functions rejects the entire definition rather than that one edge.
      #
      # This state could afford a SINGLE condition while it led to `Resolve`, because
      # Resolve then chose its own successor and applied the pairing itself. Pointing
      # straight at the Choice moves that obligation here, which is the only place this
      # reorder adds a condition rather than removing one.
      # WITHOUT THE RESEARCH PAIR THIS STATE IS TERMINAL. The second branch used to read
      # `present_function_arn == null ? null : report_entry`; with the report state gone there
      # is no alternative left for it to name.
      #
      # THIS EDGE IS NOT THE ONE THE DEPLOYED MACHINE TAKES, so removing the condition changes
      # nothing live: the deploy root supplies both research and resolve, so every
      # deployed run leaves `Plan` for `Research needed?` and never evaluates the second branch.
      #
      # NOTE A GAP THIS INHERITS RATHER THAN INTRODUCES. A deployment with the brief enabled but
      # no research pair ends here instead of reaching the brief. That was already true -- the
      # old second branch reached `report_entry` only when the report was configured -- and
      # fixing it is a separate decision about whether the brief should run on a plan nobody
      # researched.
      next = (
        var.research_function_arn != null && local.resolve_function_arn != null
        ? "Research needed?"
        : null
      )
      comment = "The ordered interleaved sequence, each step reversible or one-way."
    }
  }

  # The dispatch stage's decision point, appended only when its function ARN is supplied.
  # It publishes the dispatch queue as a document and sets `research_required`; the
  # `Research needed?` Choice, after `Plan`, is what acts on it.
  #
  # ITS SUCCESSOR IS UNCONDITIONAL, WHICH IT DID NOT USED TO BE. This state ran last of
  # the Tasks and had to decide for itself whether a research stage existed to hand to.
  # Now it sits between `Gaps` and `Decide`, and Decide is one of the five
  # unconditional stages -- it is in `stage_states` and cannot be absent -- so there is
  # nothing to be conditional about. The pairing this expression used to carry moved to
  # `Plan`, which is the state that now names the Choice.
  #
  # WHAT IT STILL DOES WHEN NO RESEARCH STAGE IS WIRED IN, unchanged by the move: the
  # queue is computed and published anyway, and `Plan` ends the run or goes to the
  # report. That is deliberately useful on its own, because *what would have been
  # researched* is inspectable before any model exists to research it.
  #
  # THIS STATE USED TO BE CALLED `7 Resolve`, AND THE 7 IS WHY THE ORDINALS ARE GONE. It
  # runs third of the Tasks, so the number contradicted the graph and every comment near it
  # had to say so. Its Lambda handler is still `entrypoints.stages.stage_resolve` and its documents,
  # role and function still say resolve: "stage 7" survives as this project's vocabulary for
  # the dispatch stage. What is gone is the claim, made by a state name in a live
  # definition, that it is the seventh thing to happen.
  resolve_state = local.resolve_function_arn == null ? {} : {
    "Resolve" = {
      arn     = local.resolve_function_arn
      next    = "Decide"
      comment = "Which knowledge gaps are worth researching. Deterministic; no model."
    }
  }

  # `Research`, reached only through the Choice state, and only for gaps the dispatch stage decided
  # are legitimate to ask about. This edge is the ONLY place a model enters the
  # pipeline, and it is entered from a deterministic decision rather than by a model
  # deciding to enter it -- which is the property that keeps a model out of the verdict
  # path, the reason the agent-driven alternative was rejected.
  # Stage 6 is NOT a plain Task, and that is the integration defect this wiring fixes.
  #
  # Stage 7 emits `research_requests`, an ARRAY of {component_id, version_series, ...}.
  # Stage 6's handler takes ONE component per invocation -- `research.py:64` reads
  # `event["component_id"]` -- because it is invoked once per unresolved component rather
  # than once per run, so a slow or unreadable source delays nothing else.
  #
  # Wiring them with a direct Task edge passed the whole run payload to a handler
  # expecting one request, and the execution failed with
  #
  #   KeyError: 'component_id'   at eks_addon_readiness/stages/research.py:64
  #
  # Neither branch was wrong alone: stage 7 produces a queue, stage 6 consumes an item,
  # and nothing mapped between them. A Map state is that missing piece.
  #
  # MaxConcurrency 1 on purpose, for two independent reasons: research is the only stage
  # where a model runs, so it is the expensive one and there is no value in parallelism
  # here; and serialising it keeps two research passes from racing on the fact store.
  #
  # ResultPath writes the per-item results ALONGSIDE the payload rather than replacing
  # it, so `store_digest_before_research` survives for the `Knowledge changed?` Choice
  # that reads it next.
  research_state = (
    var.research_function_arn == null || local.resolve_function_arn == null ? {} : {
      "Research" = {
        Type           = "Map"
        Comment        = "Research each queued gap, one component per invocation. The only stage a model participates in."
        ItemsPath      = "$.research_requests"
        MaxConcurrency = 1
        ResultPath     = "$.research_results"
        # The approval PRE-CHECK when one is wired in, otherwise straight to the Choice. Gated
        # so a deployment without the approval stage keeps its existing behaviour exactly:
        # research proposes, the report shows the proposals, and the loop closes on the
        # next run.
        #
        # IT NAMES THE SURVEY AND NOT THE WAIT, which is half of the fix. Entering `Approval`
        # from here is what paused a run on an empty page; the survey counts first and the Choice
        # after it decides. BOTH edges into the wait had to move -- this one and the
        # `approval_pending` branch of `Research needed?` -- because either one left alone still
        # reaches the unconditional pause on its own path.
        Next = var.approval_function_arn == null ? "Knowledge changed?" : "Approval survey"
        Parameters = {
          # The item under research, from the Map's own context object.
          "component_id.$"   = "$$.Map.Item.Value.component_id"
          "version_series.$" = "$$.Map.Item.Value.version_series"
          # V3: the exact installed release rides with the request (null when the ledger
          # knew only the series), so the assessment can answer "does THIS work on the
          # target" rather than only "does this series".
          "installed_version.$" = "$$.Map.Item.Value.installed_version"
          # The Helm release name (null for a managed add-on or a manifest). It switches the
          # research prompt's chart section on, so the approved fact carries where the chart
          # is published and which chart version to install -- the values the upgrade lane
          # used to read from a committed catalogue.
          "helm_release.$" = "$$.Map.Item.Value.helm_release"
          # The run's Stage 2 inventory, so the research proxy can put the Helm chart's name and
          # installed chart version on the task (`research_harness._chart_leads`). Optional:
          # an unreadable inventory leaves those two lines out and research runs as before.
          "inventory_uri.$" = "$.inventory_uri"
          # The run context the handler needs for the verdict delta in its report.
          "cluster_name.$"               = "$.cluster_name"
          "bucket.$"                     = "$.bucket"
          "target_kubernetes_version.$"  = "$.target_kubernetes_version"
          "current_kubernetes_version.$" = "$.kubernetes_version"
        }
        Iterator = {
          StartAt = "Research one component"
          States = {
            "Research one component" = {
              Type       = "Task"
              Resource   = "arn:aws:states:::lambda:invoke"
              Parameters = { FunctionName = var.research_function_arn, "Payload.$" = "$" }
              OutputPath = "$.Payload"
              Retry      = local.transient_retry
              End        = true
            }
          }
        }
      }
    }
  )

  # `Approval`: THE ONLY STATE THAT STOPS, and the one that finally closes the loop below it.
  #
  #   Research -> APPROVAL (waits for a person) -> Knowledge changed? -> Gaps -> ... -> Present
  #
  # WHY IT EXISTS. `Knowledge changed?` has been on the deployed definition for a long time
  # and has never once taken its `Gaps` branch. The reason was structural, not a missing
  # field: research writes a CANDIDATE and `approve.py` is the only writer of facts, so
  # within one execution the store could not change and re-entering gap detection could not
  # change any verdict. The loop only ever closed ACROSS executions -- a person approved, and
  # the NEXT run picked the fact up. Putting the wait HERE, between the Map and the Choice,
  # is what lets an approval move the store digest inside the same execution.
  #
  # WHY `.waitForTaskToken` AND NOT A POLL LOOP. This costs nothing while it waits and
  # resumes in one call. A Wait/Choice poll pays for a transition per tick and puts an
  # arbitrary bound on how long a person may take; an Activity would need a worker held open.
  #
  # THE PAYLOAD IS SPELLED OUT FIELD BY FIELD rather than passed as `"Payload.$" = "$"`,
  # because the task token has to go IN it and ASL cannot spread a path and add a key in the
  # same object. Naming the fields is also the honest documentation of what this stage reads.
  #
  # `ResultPath = "$.approval"` IS LOAD-BEARING. The eventual `SendTaskSuccess` output lands
  # NESTED, so the whole run payload survives the wait -- including the
  # `store_digest_before_research` that `Resolve` put there, which the Choice below compares
  # against. Replacing the payload (`ResultPath = "$"`) would mean the approval endpoint had
  # to reconstruct every field of it, and it has none of them.
  #
  # TIMEOUT FAILS THE RUN, DELIBERATELY. There is no `Catch`, so `States.Timeout` fails the
  # execution. The alternative -- carry on and report anyway -- produces a report
  # indistinguishable from one nobody was ever asked about. A failed execution says plainly
  # that a decision was needed and never came, and the approved facts (if any) are already
  # durable, so the next run picks them up.
  approval_state = var.approval_function_arn == null ? {} : {
    "Approval" = {
      Type     = "Task"
      Comment  = "Pause on a page of proposed facts. Only a person's signature resumes it."
      Resource = "arn:aws:states:::lambda:invoke.waitForTaskToken"
      Parameters = {
        FunctionName = var.approval_function_arn
        Payload = {
          # The capability to resume this execution. It goes to the stage, which writes it
          # to the token store; it is never rendered onto the page.
          "task_token.$" = "$$.Task.Token"
          # The execution name is the run id: what the console shows, what the page's URL
          # carries, and what the resume request names. One value, three places.
          "execution_name.$"               = "$$.Execution.Name"
          "bucket.$"                       = "$.bucket"
          "cluster_name.$"                 = "$.cluster_name"
          "target_kubernetes_version.$"    = "$.target_kubernetes_version"
          "research_results.$"             = "$.research_results"
          "store_digest_before_research.$" = "$.store_digest_before_research"
        }
      }
      ResultPath     = "$.approval"
      TimeoutSeconds = var.approval_timeout_seconds
      Next           = "Knowledge changed?"
      # Only transport faults. A States.Timeout must NOT be retried -- re-entering the state
      # would mint a second token and re-render the page, so a person who was merely slow
      # would find their decisions attached to a token nobody is waiting on.
      Retry = local.transient_retry
    }
  }

  # `Approval survey`: THE STATE THAT STOPS THE PAUSE FROM BEING UNCONDITIONAL.
  #
  #   Research           -> Approval survey -> Anything to approve? -> Approval (waits)
  #   Research needed?  --^                                        -> Knowledge changed?
  #
  # WHY IT EXISTS, MEASURED RATHER THAN SUPPOSED. Both edges above used to enter `Approval`
  # directly, and `Approval` is `.waitForTaskToken`: the execution is paused the moment the state
  # is ENTERED, so the handler discovering an empty offered set could do nothing about it. On
  # one test run the stage returned `awaiting_approval` 0,
  # `offered_from_this_run` 0 and `offered_from_earlier_runs` 0, mailed somebody anyway, and held
  # the run on a token until a person pressed Done on a page with no rows. All three pending
  # candidates in the bucket belonged to components that already have a stored fact, so the
  # stage's own `_fact_is_already_stored` filter withheld every one -- correctly.
  #
  # THE DECISION CANNOT BE TAKEN INSIDE THE WAIT, AND MUST NOT BECOME POSSIBLE THERE. The
  # approval stage deliberately holds no `states:SendTaskSuccess` (see the closing statement of
  # `infra/terraform/modules/research/approval_stage.tf`'s policy document) because a stage able to resume its
  # own pause is not a pause. So the count has to be taken BEFORE the token is minted, which is
  # what this state is.
  #
  # NO NEW LAMBDA, NO NEW ROLE, NO NEW VARIABLE. It invokes `var.approval_function_arn` -- the
  # same function the wait invokes -- with `survey_only` set, which puts the handler in a mode
  # that computes the offered set through the same expression and returns only the counts:
  # `stages/approval_gate.py::survey`. Every permission it uses (list and read
  # `facts-candidates/decision=PENDING_APPROVAL/`, scan the fact table) is already on that role,
  # and the three it must not use -- write the page, put a token row, send mail -- are not
  # withheld by IAM but by the code path, which is why `survey`'s docstring enumerates them.
  #
  # A PLAIN `lambda:invoke`, NOT `.waitForTaskToken`. That is the entire difference between this
  # state and `Approval`, and it is the difference between counting and stopping.
  #
  # APPLY ORDER MATTERS FOR THIS STATE, AND IT IS THE ONE OPERATIONAL HAZARD THIS CHANGE ADDS.
  # `infra/terraform/modules/research` owns the Lambda's CODE and this module owns the STATE that invokes it, so
  # `build/research.zip` must carry a handler that knows `survey_only` BEFORE this state is live:
  #
  #   research then platform   correct. The function understands the flag by the time a state
  #                            exists to send it.
  #   platform then research   the survey invocation reaches a handler with no survey mode, the
  #                            flag is ignored, and the PAUSE runs instead: it writes the page,
  #                            mints the grants, stores a token row whose task_token is EMPTY and
  #                            mails somebody -- then `Approval` does all of it again. Nothing
  #                            FAILS, because the pause's own result also carries
  #                            `awaiting_approval` and the Choice reads a usable count. It just
  #                            quietly pages a person twice and leaves an unusable grant behind.
  #
  # The same shape as the `$.approval_pending` incident recorded at `Research needed?`: a
  # definition reaching a stage older than itself. Stated rather than enforced because a
  # `depends_on` cannot cross two Terraform states -- the same reason
  # `infra/terraform/modules/research/approval_stage.tf` states its own S3-notification ordering in a sentence.
  # So: build the packages, apply `research`, then apply `platform`.
  #
  # `research_results` IS NAMED AND IS ALWAYS PRESENT, which is not luck. `Resolve` returns
  # `"research_results": []` for exactly this reason -- see its own note: Step Functions FAILS a
  # state whose Parameters name an absent path, and the `approval_pending` edge reaches here
  # without `Research` having run, so the Map never wrote the field. The wait's Payload has named
  # it on that path since it was wired, and this one names it for the same reason.
  #
  # ITS OWN `ResultPath`, SO IT CANNOT CLOBBER THE RUN PAYLOAD. `$.approval_survey` nests the
  # whole result beside the payload, exactly as `$.approval` does for the wait, so
  # `store_digest_before_research`, `bucket` and `cluster_name` all survive for the states after
  # it -- `Approval` itself names those fields one by one and would fail on a payload this state
  # had replaced.
  #
  # GATED ON THE APPROVAL STAGE EXISTING, like every other approval-shaped thing in this file: a
  # Choice naming a state that was never emitted is refused whole by Step Functions, so with no
  # `approval_function_arn` neither this state nor the Choice after it is emitted and
  # `Research.Next` goes straight to `Knowledge changed?` as it always did.
  approval_survey_state = var.approval_function_arn == null ? {} : {
    "Approval survey" = {
      Type     = "Task"
      Comment  = "Count what a page would offer. Mints nothing, writes nothing, mails nothing."
      Resource = "arn:aws:states:::lambda:invoke"
      Parameters = {
        FunctionName = var.approval_function_arn
        Payload = {
          # THE FLAG, AND IT IS A REAL JSON BOOLEAN. The handler tests `is True` rather than
          # truthiness, so a string here would fall through to the PAUSE -- which is the safe
          # direction but not the intended one, and it would fail silently as a run that still
          # pages somebody about nothing.
          survey_only = true
          # The same fields the wait reads, minus the task token -- there is none to give, which
          # is the point. Named one by one for the wait's reason: ASL cannot spread a path and
          # add a key in one object, and naming them is the honest documentation of what this
          # invocation reads.
          "execution_name.$"               = "$$.Execution.Name"
          "bucket.$"                       = "$.bucket"
          "cluster_name.$"                 = "$.cluster_name"
          "target_kubernetes_version.$"    = "$.target_kubernetes_version"
          "research_results.$"             = "$.research_results"
          "store_digest_before_research.$" = "$.store_digest_before_research"
        }
      }
      # Unwrap the Lambda envelope, then nest. Without the selector the Choice after this would
      # have to read `$.approval_survey.Payload.awaiting_approval`, and a Choice whose Variable
      # resolves to nothing does not fail -- it takes its Default, which here means pausing every
      # run. That is the same silent-drift shape
      # `test_the_release_gate_never_pages_for_an_empty_list.py` exists for, so the path is
      # asserted in a test rather than left to a reader to get right.
      #
      # ALL FOUR COUNTS ARE SELECTED, NOT JUST THE ONE THE CHOICE BRANCHES ON, and that is not
      # padding. `ResultSelector` REPLACES the result with what it names, so selecting only
      # `awaiting_approval` would drop the other three from this state's output -- and
      # `pending_candidates_skipped` is the one that has to survive. It says a pending candidate
      # is still unreachable, which is the condition the approval stage exists to end, and a run
      # that SKIPS the wait is exactly the run where nobody will see a page reporting it. Losing
      # it here would leave it inferable only from a Lambda log line, which is how the original
      # stranding went unreported on metrics-server for every run it blocked.
      #
      # The two provenance counts come along for the reason they were added to the wait's result:
      # "did this run find something, or is it showing me an answer somebody left waiting?" is
      # the first question asked about the approval leg, and it must be answerable on the skip
      # path too.
      ResultSelector = {
        "awaiting_approval.$"          = "$.Payload.awaiting_approval"
        "offered_from_this_run.$"      = "$.Payload.offered_from_this_run"
        "offered_from_earlier_runs.$"  = "$.Payload.offered_from_earlier_runs"
        "pending_candidates_skipped.$" = "$.Payload.pending_candidates_skipped"
      }
      ResultPath = "$.approval_survey"
      Next       = "Anything to approve?"
      Retry      = local.transient_retry
      # THE PRE-CHECK MUST NOT BE ABLE TO FAIL A RUN, NOR TO SKIP A PAUSE IT COULD NOT COUNT.
      # It is an optimisation over behaviour that already worked, so every RUNTIME failure of it --
      # a throttle, a cold-start timeout, a missing permission, a bug in `survey`, a
      # `ResultSelector` naming a field the handler stopped returning -- lands on the wait, which
      # is what the run would have done without this state at all.
      #
      # "EVERY WAY IT CAN FAIL" WOULD BE AN OVERSTATEMENT, so it is not claimed. `States.Runtime`
      # is not catchable by any `Catch`, here or anywhere: a malformed path in this state's own
      # `Parameters` fails the execution outright. That is a DEFINITION defect rather than a
      # runtime one -- it fires on every execution, on the first one after an apply, so it cannot
      # reach production unnoticed the way an intermittent throttle could. The paths named above
      # are the ones that could, and those are what this Catch covers.
      #
      # Catching to `Anything to approve?` would be the wrong target even though its Default is
      # `Approval`: the error result would land at the same ResultPath and the Choice would read
      # a count that is not there. Naming the wait directly says what happens, once.
      #
      # `ResultPath` KEEPS THE PAYLOAD. `"$"` would replace the run with the error object and
      # `Approval` would then fail naming `$.bucket`, turning a survey hiccup into a failed
      # execution -- the exact outcome this Catch exists to prevent.
      Catch = [{
        ErrorEquals = ["States.ALL"]
        ResultPath  = "$.approval_survey_error"
        Next        = "Approval"
      }]
    }
  }

  # `Anything to approve?`: the Choice that spends the count.
  #
  # A POSITIVE TEST FOR ZERO, AND THE DEFAULT IS THE PAUSE. `upgrade_stage.tf` makes the same
  # argument about a list, and the asymmetry here is deliberate and the opposite of `Anything
  # approved?`'s. That Choice defaults to `Done` because `Done` grants no authority, so an
  # unreadable payload must land there. This one defaults to `Approval`, because the costs are
  # not symmetrical either:
  #
  #   pause when nothing was pending   a person opens a page with no rows and presses Done.
  #                                    Wasteful, visible, and self-correcting.
  #   skip when something WAS pending  the candidate stays PENDING_APPROVAL, `Resolve` keeps
  #                                    withholding its component, and no surface offers it. That
  #                                    is the stranding `Approval` was extended to end, and it
  #                                    reports as nothing at all.
  #
  # So only a COUNTED ZERO skips the wait. An absent count, a non-numeric one, or a survey that
  # errored (which the Catch above routes past this state entirely) all pause.
  #
  # GUARDED WITH `IsPresent`, for the reason stated at `Research needed?`: a bare comparison on
  # an absent path does not evaluate false, it FAILS THE EXECUTION with `States.Runtime: Invalid
  # path`. This file has been bitten by that once already, when a definition reached a payload
  # older than itself.
  #
  # WHERE THE ZERO BRANCH GOES, AND WHY THAT IS SAFE RATHER THAN MERELY CONVENIENT.
  # `Knowledge changed?` reads `$.approval.store_digest_after_research`, which does not exist on
  # a run that skipped the wait. Its first branch is `And [IsPresent true, Not StringEqualsPath]`
  # and the `And` SHORT-CIRCUITS, so the absent path evaluates false rather than faulting on the
  # `StringEqualsPath` beside it -- verified against the LIVE definition with
  # `stepfunctions test-state` on the deployed `Knowledge changed?` and an input carrying only
  # `store_digest_before_research`: nextState `Anything to upgrade?`, status SUCCEEDED, no
  # `States.Runtime`. Which is also the right answer: nothing was approved, so the store cannot
  # have moved, so there is nothing to re-run and the report is where the run belongs.
  approval_needed_state = var.approval_function_arn == null ? {} : {
    "Anything to approve?" = {
      Type    = "Choice"
      Comment = "Skip the wait only when the survey counted nothing to ask about."
      Choices = [{
        And = [
          {
            Variable  = "$.approval_survey.awaiting_approval"
            IsPresent = true
          },
          {
            Variable      = "$.approval_survey.awaiting_approval"
            NumericEquals = 0
          },
        ]
        Next = "Knowledge changed?"
      }]
      Default = "Approval"
    }
  }

  # The stage that asks whether we may change THIS cluster. Unconditional, like `Sweep` and
  # unlike the four gated states: its function is created in this module (upgrade_approval.tf),
  # so there is no ARN to withhold and no way to leave it out. For the one state standing
  # between a plan and somebody changing a production cluster, that is the property worth
  # having -- a null-defaulted gate silently un-wires a live stage and no destroy/replace
  # guard catches it, which is why the deploy root wires every one.
  # THE FLAG-OFF TERMINUS. A plain invoke of the approval function with the run under a
  # `readiness_report` key; the handler renders the approval page with no controls, writes it
  # beside where the approval page would go, mails it, and returns. `Done` follows. Emitted ONLY
  # when the upgrade stage is off, because a state nothing names is refused by Step Functions --
  # and when it is on, nothing names this one.
  readiness_report_state = local.upgrade_enabled ? {} : {
    "Readiness report" = {
      Type     = "Task"
      Comment  = "Execution is disabled: render the plan and its brief read-only, mail it, finish."
      Resource = "arn:aws:states:::lambda:invoke"
      Parameters = {
        FunctionName = aws_lambda_function.upgrade_approval.arn
        Payload = {
          "readiness_report.$" = "$"
        }
      }
      ResultSelector = { "result.$" = "$.Payload" }
      ResultPath     = "$.readiness_report"
      Next           = "Done"
      Retry          = local.transient_retry
    }
  }

  upgrade_approval_state = {
    "Upgrade approval" = {
      Type     = "Task"
      Comment  = "Pause on the report with a control per step. Per cluster, not per fact."
      Resource = "arn:aws:states:::lambda:invoke.waitForTaskToken"
      Parameters = {
        FunctionName = aws_lambda_function.upgrade_approval.arn
        Payload = {
          # The capability to resume this execution. It goes to the stage, which writes it to
          # the upgrade token store; it is never rendered onto the page.
          "task_token.$" = "$$.Task.Token"
          # The execution name is the run id: what the console shows, what the page's URL
          # carries, and what the resume names. One value, three places.
          "execution_name.$" = "$$.Execution.Name"
          # THE WHOLE PAYLOAD, NESTED, rather than a field list -- and this is the one place
          # this state differs from `Approval` on purpose. Naming fields here means naming
          # `brief_uri`, and `"brief_uri.$" = "$.brief_uri"` FAILS THE STATE when the field is
          # absent. It is absent on exactly one path: `Brief assemble`'s catch, which sets
          # `$.brief_error` and carries the payload on without a brief. That is the degraded
          # path the entire brief stage was built to survive, and naming the field would turn
          # it into the one thing a brief must never be -- a reason the run stops.
          "run.$" = "$"
        }
      }
      ResultPath     = "$.upgrade_approval"
      TimeoutSeconds = var.upgrade_approval_timeout_seconds
      # THE RELEASE GATE FOLLOWS, AND IT USED TO BE `Done`. This edge is the only reason the gate
      # is reachable: a definition that declares seven new states and forgets this one line is
      # accepted by Step Functions without complaint -- unreachable states are legal -- and every
      # run behaves exactly as it did before the gate existed.
      Next = "Anything approved?"
      # Only transport faults. A States.Timeout must NOT be retried: re-entering would mint a
      # second set of capabilities and re-render the page, so a person who was merely slow
      # would find their decisions attached to a token nobody is waiting on. Every decision
      # they did make is already durable in S3, which is what makes failing the safe outcome.
      Retry = local.transient_retry
    }
  }

  # ---------------------------------------------------------------------------
  # The release gate. THE THIRD STATE THAT STOPS, and the FIRST one where AWS has authenticated
  # whoever answered.
  #
  # `Approval` asks whether a claim about a component is true. `Upgrade approval` asks whether we
  # may change THIS cluster. Both are answered by a click on a presigned form, which proves
  # somebody opened a mailbox and NOT which human it was -- hence
  # `StepDecision.approver_authority` recording `EMAIL_LINK_TOKEN` and never claiming a principal.
  # This gate closes exactly that gap: `aws:approve` refuses a signal from a principal outside its
  # `Approvers` list, so reaching `Done` means a named IAM principal stood behind the anonymous
  # page clicks. INTENT, THEN AUTHORISATION. Neither is an execution grant.
  #
  # See `release_gate.tf` for the runbook, the two roles, and at length for why the task token is
  # never handed to SSM.
  # ---------------------------------------------------------------------------

  # WHERE THE APPROVED STEPS ACTUALLY ARE, AND GETTING THIS WRONG IS A GATE THAT NEVER FIRES.
  # `upgrade_decision_watcher._resume` calls SendTaskSuccess with an output of
  # `{"upgrade_approval": {...}}`, and `Upgrade approval` above sets
  # `ResultPath = "$.upgrade_approval"`. `ResultPath` places the WHOLE task result at that path,
  # so the object nests under its own name and the live path is DOUBLED. Verified on SUCCEEDED
  # execution `toast-191932`, whose `Upgrade approval` output carries
  # `upgrade_approval.upgrade_approval.approved_steps = [1, 2]`.
  #
  # `$.upgrade_approval.approved_steps` DOES NOT EXIST, and reading it is a SILENT failure rather
  # than an error: a Choice whose Variable resolves to nothing takes its Default, the Default here
  # is `Done`, and the whole gate would be skipped on every execution with the run going green.
  # One local rather than four spellings, because three of the four states agreeing is a gate that
  # half works.
  upgrade_approval_result = "$.upgrade_approval.upgrade_approval"

  anything_approved_state = {
    "Anything approved?" = {
      Type    = "Choice"
      Comment = "Nothing approved means nothing to authorise. Never page a person for an empty list."
      Choices = [{
        # THE FIRST ELEMENT, NOT THE LIST. JSONPath Choice rules have no array-length comparator,
        # so `IsPresent` on index 0 is how "this list is non-empty" is written. `IsPresent` on the
        # list itself would be TRUE for an empty list, and the gate would wake somebody up to
        # authorise nothing.
        Variable  = "${local.upgrade_approval_result}.approved_steps[0]"
        IsPresent = true
        Next      = "Release approvers"
      }]
      # DEFAULT IS `Done`, AND IT IS THE FAIL-CLOSED DIRECTION HERE. This is the opposite choice
      # from `Anything to upgrade?` further up, whose Default proceeds -- and the difference is
      # what the two states do rather than a disagreement about defaults. That one invokes a cheap
      # advisory stage, so proceeding on an unreadable count costs one Lambda call. This one wakes
      # a person to authorise a cluster change, and reaching `Done` authorises NOTHING. So both an
      # empty list and a payload this state cannot read route to the outcome that grants no
      # authority.
      Default = "Done"
    }
  }

  # WHO MAY AUTHORISE THIS RELEASE, ASKED OF THE CLUSTER RATHER THAN OF A TFVARS FILE.
  #
  # `Approvers` used to be baked into the SSM document from `var.release_approver_arns`. This
  # state replaces that: it resolves the principals the cluster's own access entries make cluster
  # administrators, and builds one line per approved step -- `coredns 1.11.1 -> 1.12.0` -- for the
  # mail to name. See `stages/release_prep.py`, which is emphatic about the allowlist and about
  # failing closed.
  #
  # IT IS THE UPGRADE-APPROVAL FUNCTION, INVOKED WITH A DIFFERENT PAYLOAD. No Lambda is added for
  # this: `entrypoints.stages.stage_upgrade_approval` dispatches on the `release_prep` key. That function is the
  # only one whose role may already read `plans/`, and it gains exactly three read-only `eks:`
  # actions -- see `upgrade_approval.tf`.
  #
  # `ResultSelector` RATHER THAN `OutputPath = "$.Payload"`, WHICH EVERY STAGE STATE USES. A stage
  # returns `{**event, ...}` and legitimately replaces the payload; this returns two lists and
  # nothing else, so unwrapping the envelope onto `$` would DESTROY the payload -- including the
  # `upgrade_approval` result the next four states read. The selector unwraps and the ResultPath
  # nests, so the payload survives and gains one key.
  release_prep_state = {
    "Release approvers" = {
      Type     = "Task"
      Comment  = "Resolve the cluster's admins and name the changes. Fails closed on an empty list."
      Resource = "arn:aws:states:::lambda:invoke"
      Parameters = {
        FunctionName = aws_lambda_function.upgrade_approval.arn
        Payload = {
          # NESTED UNDER ITS OWN KEY, which is the discriminator the handler dispatches on. The
          # whole payload is deliberately NOT passed: this invocation needs three values, and
          # handing it the run would mean a second path by which the paused-approval branch
          # could be reached with a token-shaped payload.
          "release_prep" = {
            "cluster.$"        = "${local.upgrade_approval_result}.cluster"
            "run_id.$"         = "${local.upgrade_approval_result}.run_id"
            "approved_steps.$" = "${local.upgrade_approval_result}.approved_steps"
            # THE PLAN SURVIVES THE STAGE RESULTPATHS, which is what makes this possible without
            # a second S3 read anywhere else. Stage 5 returns `{**event, "plan_uri": ...}` and
            # `Upgrade approval` nests its own result under `$.upgrade_approval`, so the URI Stage
            # 5 wrote is still on the payload here.
            "plan_uri.$" = "$.plan_uri"
            # THE INVENTORY, for installed versions: an approved unsupported-at-target row is a
            # move from what is installed to the newest recorded release, and only the inventory
            # says what is installed. This role already reads `inventories/*` (Release assembled).
            "inventory_uri.$" = "$.inventory_uri"
          }
        }
      }
      ResultSelector = {
        "approvers.$" = "$.Payload.approvers"
        "changes.$"   = "$.Payload.changes"
      }
      ResultPath = "$.release_prep"
      Next       = "Release gate"
      Retry      = local.transient_retry
      # EVERY FAILURE HERE IS A REFUSAL. An empty resolved list, a denied `eks:` call, a plan the
      # approved orders do not match -- all of them raise, and all of them land on the Fail state.
      # There is deliberately no path to `Done` and no path onward to `Release gate`: a gate that
      # could not work out who may authorise must not ask nobody, and must not ask everybody.
      Catch = [{
        ErrorEquals = ["States.ALL"]
        ResultPath  = "$.release_error"
        Next        = "Release rejected"
      }]
    }
  }

  release_gate_state = {
    "Release gate" = {
      Type     = "Task"
      Comment  = "Start the approval automation. The task token never leaves Step Functions."
      Resource = "arn:aws:states:::aws-sdk:ssm:startAutomationExecution"
      Parameters = {
        DocumentName = aws_ssm_document.release_gate.name
        Parameters = {
          # EVERY VALUE IS A LIST OF ONE STRING, because that is the shape of the SSM API's
          # `Parameters` map (`AutomationParameterMap`: key -> list of strings). A bare string
          # here is refused by the SDK integration.
          #
          # AND EVERY VALUE GOES THROUGH `States.Array`, INCLUDING THE STATIC ROLE ARN, so every
          # element of this object is a STRING before Terraform sees it. Writing the role as a
          # literal `[arn]` would put a `list(string)` beside four `string`s in one object and
          # invite the type unification documented at `States` below -- the bug that turned
          # `End = true` into the string `"true"` and made Step Functions refuse the definition
          # whole.
          "RunId.$"   = "States.Array(${local.upgrade_approval_result}.run_id)"
          "Cluster.$" = "States.Array(${local.upgrade_approval_result}.cluster)"
          "Summary.$" = "States.Array(${local.upgrade_approval_result}.summary)"
          # STRINGIFIED, because `approved_steps` is a list of INTEGERS and an SSM parameter value
          # is a string. `States.JsonToString` renders `[1,2]`, which is what the mail shows and
          # what a reader of the automation's parameters sees.
          "ApprovedSteps.$"        = "States.Array(States.JsonToString(${local.upgrade_approval_result}.approved_steps))"
          "AutomationAssumeRole.$" = "States.Array('${aws_iam_role.release_gate_automation.arn}')"
          # THE TWO PARAMETERS THE PREVIOUS STATE RESOLVED, AND THEY ARE PASSED DIFFERENTLY
          # BECAUSE THEIR DECLARED TYPES DIFFER. The SSM `Parameters` map is always name -> LIST
          # of strings, so what varies is whether the value is already one.
          #
          # `Approvers` IS THE AUTHORISATION BOUNDARY AND IT ARRIVES HERE FROM THE CLUSTER. It is
          # a `StringList` in the document, consumed as a list by `aws:approve`, and the resolver
          # returns it as a list of strings -- which is already the shape this map wants, so it
          # goes through NO `States.Array`. Wrapping it would produce a list containing a list and
          # SSM refuses that. The document holds no default for it, so a start call that omitted
          # it would be refused rather than defaulting to somebody -- and `Release approvers`
          # raises rather than returning an empty list, so this path cannot be reached with
          # nobody on it.
          "Approvers.$" = "$.release_prep.approvers"
          # `Changes` IS WRAPPED, AND THE DIFFERENCE IS THE FIX FOR A DEFECT AWS CAUGHT AT APPLY
          # TIME. It was a `StringList` too, and document creation failed: `Parameter "Changes"
          # is "STRING_LIST" type and can't be used as a substring parameter`, because the mail
          # bodies read it inside a string. It is now a `String` holding the lines joined by the
          # resolver -- so it is a single string here, and a single string must become a
          # one-element list exactly like the four above it.
          "Changes.$" = "States.Array($.release_prep.changes)"
        }
      }
      ResultPath = "$.release_started"
      Next       = "Release waiting"
      Retry      = local.transient_retry
      # A START THAT CANNOT HAPPEN IS A REFUSAL, NOT A SKIP. Every way this state can fail -- a
      # denied PassRole, a document that is not there, a parameter SSM will not take -- means
      # nobody was asked, so every one of them lands on the Fail state. There is deliberately no
      # path from here to `Done`: three of the brief's edges catch ONWARD because a brief is
      # advisory and must not fail a run, and copying that idiom here would be following this
      # file's most-repeated pattern into exactly the wrong place.
      Catch = [{
        ErrorEquals = ["States.ALL"]
        ResultPath  = "$.release_error"
        Next        = "Release rejected"
      }]
    }
  }

  release_waiting_state = {
    "Release waiting" = {
      Type    = "Wait"
      Comment = "Poll interval while a person reads the console. Bounded by aws:approve's timeout."
      # SIXTY SECONDS, HARDCODED, and deliberately not a fourth variable. The approval WINDOW is
      # the knob that matters and it has one; this only sets how soon the machine notices an
      # answer already given. At the 2h ceiling it costs at most ~120 extra transitions per gated
      # run, which is not a number worth making configurable.
      Seconds = 60
      Next    = "Release polled"
    }
  }

  release_poll_state = {
    "Release polled" = {
      Type     = "Task"
      Comment  = "Read the automation's status. No outputs are selected here -- see the comment."
      Resource = "arn:aws:states:::aws-sdk:ssm:getAutomationExecution"
      Parameters = {
        "AutomationExecutionId.$" = "$.release_started.AutomationExecutionId"
      }
      # THE WHOLE RESPONSE, WITH NO ResultSelector, AND THAT IS THE LOAD-BEARING PART.
      # `AutomationExecution.Outputs` is ABSENT while the execution is still `Waiting`, and a
      # `"....$"` selector naming an absent path FAILS the state -- the same trap
      # `Upgrade approval` documents for `brief_uri`. Selecting `approver_decisions` here would
      # therefore fail the FIRST poll, every time, before anybody had a chance to approve. So this
      # state selects nothing and `Release approved` does the reshaping, once the status has
      # proved the outputs exist.
      ResultPath = "$.release_poll"
      Next       = "Release decided?"
      Retry      = local.transient_retry
      Catch = [{
        ErrorEquals = ["States.ALL"]
        ResultPath  = "$.release_error"
        Next        = "Release rejected"
      }]
    }
  }

  release_decided_state = {
    "Release decided?" = {
      Type    = "Choice"
      Comment = "Approved, refused, or still waiting."
      Choices = [
        {
          Variable     = "$.release_poll.AutomationExecution.AutomationExecutionStatus"
          StringEquals = "Success"
          Next         = "Release approved"
        },
        # EVERY OTHER TERMINAL STATUS IS A REFUSAL, NAMED ONE BY ONE rather than swept up by the
        # Default -- because the Default here has to mean "keep waiting", and a status this list
        # forgot would then loop until the automation was long gone. A rejection arrives as
        # `Failed` (the `approve` step carries `onFailure: Abort`), an expiry as `TimedOut`, and a
        # cancelled or revoked automation as its own status. All of them mean nobody authorised
        # this release, and all of them must FAIL the run.
        {
          Or = [
            { Variable = "$.release_poll.AutomationExecution.AutomationExecutionStatus", StringEquals = "Failed" },
            { Variable = "$.release_poll.AutomationExecution.AutomationExecutionStatus", StringEquals = "TimedOut" },
            { Variable = "$.release_poll.AutomationExecution.AutomationExecutionStatus", StringEquals = "Cancelled" },
            { Variable = "$.release_poll.AutomationExecution.AutomationExecutionStatus", StringEquals = "Cancelling" },
            { Variable = "$.release_poll.AutomationExecution.AutomationExecutionStatus", StringEquals = "Rejected" },
            { Variable = "$.release_poll.AutomationExecution.AutomationExecutionStatus", StringEquals = "CompletedWithFailure" },
            { Variable = "$.release_poll.AutomationExecution.AutomationExecutionStatus", StringEquals = "Exited" },
          ]
          Next = "Release rejected"
        },
      ]
      # DEFAULT IS "KEEP WAITING", which is the only non-terminal reading available: Pending,
      # InProgress, Waiting, PendingApproval, Approved. It cannot spin forever -- the `approve`
      # step carries `release_approval_timeout_seconds` and SSM drives every execution to one of
      # the statuses above -- so nothing counts the iterations, and that is a property of SSM
      # rather than something this definition enforces.
      Default = "Release waiting"
    }
  }

  release_approved_state = {
    "Release approved" = {
      Type    = "Pass"
      Comment = "Reshape the automation's outputs into $.release. The first record of a principal."
      Parameters = {
        "run_id.$"                  = "${local.upgrade_approval_result}.run_id"
        "cluster.$"                 = "${local.upgrade_approval_result}.cluster"
        "approved_steps.$"          = "${local.upgrade_approval_result}.approved_steps"
        "automation_execution_id.$" = "$.release_started.AutomationExecutionId"
        # THE APPROVER'S ARN LIVES HERE, and it is the only thing this whole gate produces that
        # nothing before it could. `aws:approve` emits `ApproverDecisions` -- "a JSON map that
        # includes the approval decision of each approver" -- carrying the ARN SSM resolved.
        #
        # READ OUT OF `StepExecutions[]`, NOT OUT OF THE EXECUTION'S `Outputs` MAP, because the
        # obvious version of this shipped and was WRONG. `release_gate.tf` used to promote these
        # two to document-level outputs; with the `approve` step in status `Success` and its own
        # `Outputs` holding the real ARN, the promoted value came back as the literal string
        # "No output available yet because the step is not successfully executed", and a run
        # SUCCEEDED carrying that sentence where the principal should be. A gate that records a
        # complaint about missing output authorised nobody, so the promotion is gone and this
        # reads where the value actually is.
        #
        # SELECTED BY `StepName`, NOT BY POSITION. `StepExecutions[1]` resolves the same value
        # today and breaks the day a step is inserted before `approve` -- silently, into whatever
        # that step's outputs happen to hold. The filter names what it wants.
        #
        # NO `approve.` PREFIX AND NO BRACKETS-FOR-A-DOT HERE: a step's own `Outputs` keys are
        # bare (`ApproverDecisions`), unlike the dotted document-level names.
        #
        # `States.ArrayGetItem(..., 0)` BECAUSE A FILTER RETURNS ONE ENTRY PER MATCH and each
        # entry is itself the array SSM puts in an output value -- so the bare filter yields
        # `[["{...}", "{...}"]]`, and the run of 2026-09-20 recorded exactly that. A reader
        # reaching for the first ARN would have to know to index twice, and `Release.approver_arns`
        # is a flat tuple, so the nesting would land on whoever validates this payload next. There
        # is exactly one `approve` step -- the step names are literals in `release_gate.tf` -- so
        # taking match 0 discards nothing. What remains is one string per listed approver, each a
        # JSON object naming the principal and its decision.
        "approver_decisions.$" = "States.ArrayGetItem($.release_poll.AutomationExecution.StepExecutions[?(@.StepName=='approve')].Outputs['ApproverDecisions'], 0)"
        "approval_status.$"    = "States.ArrayGetItem($.release_poll.AutomationExecution.StepExecutions[?(@.StepName=='approve')].Outputs['ApprovalStatus'], 0)"
      }
      ResultPath = "$.release"
      Next       = "Release assembled"
    }
  }

  # THE RECORD, BUILT IN CODE BECAUSE ASL CANNOT BUILD IT. `Release approved` above produces what
  # the automation said, in the shape SSM said it: `approved_steps` is a list of ORDERS, each
  # `ApproverDecisions` entry is JSON TEXT, and `approval_status` is a one-element array because
  # every value in a step's `Outputs` map is array-valued. The first real record stored exactly
  # that, and reading the principal who authorised a cluster change out of it took three parses.
  #
  # THREE THINGS THIS STATE DOES THAT NO `Pass` STATE COULD:
  #
  #   1. DECODE THE DECISIONS. A parse per element, and ASL has no map-over-an-array intrinsic --
  #      `States.StringToJson` takes one string, and a positional version would hardcode how many
  #      approvers there are, which the resolver decides per cluster. A three-admin cluster would
  #      silently lose the third.
  #   2. SAY WHAT WAS APPROVED. `[1, 2, 4, 7]` names nothing without the Stage 5 plan beside it, and
  #      reading a document from S3 is not something a `Pass` state can do. The moves come from the
  #      same derivation that built the mail's `Changes` lines, so what the approver read and what
  #      is recorded cannot disagree. TWO documents are read, because the plan does not say whether
  #      a component is a Helm release or an EKS managed add-on -- the Stage 2 inventory does, and
  #      that is the field the executor branches on.
  #   3. REFUSE. An approved order the plan does not contain, a decision naming no principal, orders
  #      that disagree with the moves described -- each raises rather than storing a gap.
  #
  # NO NEW LAMBDA FUNCTION. It is the upgrade-approval function again, dispatched on a
  # `release_record` payload key exactly as `Release approvers` is on `release_prep` -- the one
  # function whose role may already read `plans/`. It still cannot write a release: `releases/` is
  # granted to this state machine's role and to nothing else.
  #
  # DELIBERATELY NO `Catch`, FOR `Release recorded`'S REASON. By this state somebody HAS approved,
  # so routing a failure to `Release rejected` would state something false. The execution fails
  # with the error, which names the real problem -- and nothing reaches `Done` without a record.
  release_assembled_state = {
    "Release assembled" = {
      Type     = "Task"
      Comment  = "Build the release record: the moves, the principal, the instant. Refuses gaps."
      Resource = "arn:aws:states:::lambda:invoke"
      Parameters = {
        FunctionName = aws_lambda_function.upgrade_approval.arn
        Payload = {
          "release_record" = {
            # THE AUTOMATION'S OWN ACCOUNT, PASSED WHOLE. The handler validates it key by key and
            # refuses a shape it does not know, so narrowing it here would move that check into
            # Terraform where nothing tests it.
            "release.$" = "$.release"
            # The plan is what turns an order into a move. Live on the payload since `Plan`; the
            # same URI `Release approvers` read.
            "plan_uri.$" = "$.plan_uri"
            # THE INVENTORY IS WHAT SAYS HELM OR EKS MANAGED ADD-ON, and the executor calls a
            # different API for each -- `helm upgrade` against a release, `UpdateAddon` against an
            # add-on. The plan does not carry that; `ComponentReport.install_method` does. Live on
            # the payload since `Inventory`, and this role already reads `inventories/*`.
            "inventory_uri.$" = "$.inventory_uri"
          }
        }
      }
      # THE PAYLOAD IS THE DOCUMENT, unwrapped from the Lambda envelope. `OutputPath = "$.Payload"`
      # would work and would also discard everything else on the payload -- which `Done` reports --
      # so the envelope is unwrapped by selector and nested by path instead.
      ResultSelector = { "document.$" = "$.Payload" }
      ResultPath     = "$.release_record"
      Next           = "Release recorded"
      Retry          = local.transient_retry
    }
  }

  # THE RELEASE, MADE DURABLE. Until this state, the one thing this whole gate produces that
  # nothing upstream could -- the ARN of a principal AWS authenticated -- existed only in the
  # execution's payload. Step Functions history is retained for 90 days and is not a record an
  # executor can read by cluster; an object under `releases/cluster=.../run=.../release.json` is.
  #
  # NO LAMBDA, AND NO PREFIX HANDED TO A STAGE. `aws-sdk:s3:putObject` writes it directly, so the
  # only principal that can write a release is the state machine role -- not the approval stage,
  # which renders the page a person clicks, and not the watcher, which records their clicks. A
  # stage able to write a release could manufacture an authorisation.
  #
  # THE DOCUMENT IS BUILT BY `Release assembled` AND WRITTEN HERE UNCHANGED, which is a split the
  # first real record forced. Everything this state stores is decided in Python -- see
  # `stages/release_record.py` -- and this state's only job is the write.
  #
  # NO TOKEN AND NO NONCE IN IT, by construction rather than by filtering. The document is built
  # from the automation's own outputs and the Stage 5 plan, and neither the task token nor a click
  # nonce is on either path. The key carries neither either; see `storage/keys.py:release_key`,
  # which this `States.Format` must agree with.
  release_recorded_state = {
    "Release recorded" = {
      Type     = "Task"
      Comment  = "Write the authorisation to S3. The first durable record of a named principal."
      Resource = "arn:aws:states:::aws-sdk:s3:putObject"
      Parameters = {
        Bucket = aws_s3_bucket.documents.id
        # THE CLUSTER IS A PATH SEGMENT, for `step_decision_key`'s reason: everything ever
        # authorised for one cluster is one prefix listing, and a release for one cluster can
        # never land on another's key.
        # `cluster.name` AND NOT `cluster`, because the assembled document holds a
        # `ClusterReference` -- a name and a region -- where the raw gate payload held a bare
        # string. `storage/keys.py:release_key` takes the name, so the key is unchanged.
        "Key.$" = "States.Format('releases/cluster={}/run={}/release.json', $.release_record.document.cluster.name, $.release_record.document.run_id)"
        # THE DOCUMENT ITSELF, WITH NO INTRINSIC AROUND IT, AND THE FIRST REAL RUN IS WHY. This was
        # `States.JsonToString($.release)`, and the first test run wrote 549
        # bytes whose first byte is a double quote: the intrinsic serialised the object to a
        # string, and the `s3:putObject` integration then serialised THAT -- so the record of who
        # authorised a cluster change was a JSON string containing JSON, and `json.load` over it
        # returned a `str`. `ContentType` below made it worse than cosmetic by announcing a type
        # the body did not have.
        #
        # THE INTEGRATION SERIALISES THE BODY VALUE. That is the fact the old version was missing:
        # pass an object and one JSON object is stored, pass a string and a JSON string is. The
        # same intrinsic is CORRECT on `ApprovedSteps` in the start call above, where the value
        # really is embedded in a mail -- which is why the distinction is asserted in
        # `tests/test_the_release_gate_records_a_real_principal.py` rather than left as prose.
        "Body.$"    = "$.release_record.document"
        ContentType = "application/json"
      }
      # THE RESPONSE IS DISCARDED except for its path, which keeps the payload intact for `Done`.
      ResultPath = "$.release_recorded"
      # THE UPGRADE STAGE HANGS HERE, and this local is the whole of its footprint in this file.
      # `local.after_release_recorded` is `"Done"` unless `var.upgrade_stage_enabled` is true, in
      # which case it is `"Upgrade lanes"`. Declared in `upgrade_stage.tf` for `sweep.tf`'s reason:
      # the component that adds states owns the local that names its own entry point.
      #
      # IT HANGS OFF THIS STATE RATHER THAN `Release approved`, WHICH IS WHERE IT USED TO, and the
      # move is an improvement rather than an accommodation to this merge. `Release approved` is a
      # Pass that reshapes the automation's outputs; THIS state is the one that makes the release
      # durable. So a lane now opens only after the authorisation it acts on exists as an object,
      # and a run that could not record its authorisation never reaches a lane at all.
      Next = local.after_release_recorded
      # NO `Retry`, AND NOT BY OVERSIGHT. `local.transient_retry` names four `Lambda.*` errors
      # only, so attaching it to an S3 SDK integration would be decoration -- it can never match.
      # The SDK's own throttling retries are what cover a transient S3 refusal here, and a real
      # failure should fail the run rather than be retried three times into the same wall.
      #
      # DELIBERATELY NO `Catch`, AND IT IS THE ONE STATE IN THE GATE WITHOUT ONE. Routing a failed
      # write to `Release rejected` would state something false -- that nobody authorised the
      # release -- when in fact somebody did and the record could not be written. So the execution
      # FAILS with the S3 error itself, which names the real problem. It still fails closed: no
      # run reaches `Done` without a durable record of who authorised it.
    }
  }

  release_rejected_state = {
    "Release rejected" = {
      Type    = "Fail"
      Comment = "Nobody with AWS-authenticated authority approved this release."
      Error   = "ReleaseRejected"
      # FAILING IS THE POINT, NOT A FALLBACK, on `upgrade_approval_timeout_seconds`'s reasoning.
      # A run that quietly reached `Done` without an authorisation would be indistinguishable
      # from one that was authorised -- same terminal state, same green execution, same payload
      # shape -- and that is the worst outcome available here. Every per-step decision the person
      # made is already durable in S3, so failing loses the RELEASE and never the ANSWERS, and the
      # undecided steps are offered again on the next run.
      Cause = "The release gate was rejected, timed out, or could not be started. No release is authorised for this run. The upgrade decisions themselves are already recorded under upgrade-decisions/, and the steps are offered again on the next run."
    }
  }

  task_states = merge(
    local.stage_states,
    # Between `Inventory` and `Gaps`. Defined in sweep.tf together with the successor
    # local above; unconditional, because its function is created in this module.
    local.sweep_state,
    local.resolve_state,
  )

  # The Choice state, which is the only branch in the whole pipeline.
  #
  # It is a separate local because a Choice is not a Task: it has no Resource, no
  # Retry and no Parameters, so it cannot be built from `state_body` and must not be
  # swept into the Task comprehensions below.
  #
  # It branches on `research_required`, which Stage 7 puts on the payload from its own
  # document -- a boolean rather than a count, so the branch cannot drift from the
  # document's answer. The default path is onward, so a workflow that cannot evaluate
  # the condition proceeds rather than stalling.
  choice_state = (
    var.research_function_arn == null || local.resolve_function_arn == null ? {} : {
      # TWO RULES, AND THE SECOND ONE CLOSES A TRAP THAT LEFT A COMPONENT STUCK FOREVER.
      #
      # `Resolve` withholds any component whose answer is ALREADY awaiting a decision, so it is
      # not researched twice -- correct, and the reason is in its own docstring. But with only
      # the `research_required` rule, a run whose every gap was withheld took the Default and
      # routed straight PAST `Approval`. So the pending answer suppressed research AND was never
      # offered to anybody: not merely unresearched, unreachable. Observed on metrics-server,
      # whose candidate was written by an execution somebody stopped and then blocked every
      # later run.
      #
      # ORDER MATTERS AND RESEARCH COMES FIRST. Both flags can be true in one round -- some
      # gaps dispatched, others withheld -- and `Research.Next` already leads into the approval
      # leg when the approval stage is configured. So researching first lets ONE page carry this
      # round's new answers and the ones already waiting; branching into the approval leg first
      # would strand the dispatch queue.
      #
      # GATED ON THE APPROVAL STAGE EXISTING. A Choice naming a state that was never emitted is
      # refused whole by Step Functions, so with no `approval_function_arn` there is nothing to
      # send a withheld gap to and the rule is simply absent -- the same shape as every other
      # gate in this file.
      "Research needed?" = {
        Type    = "Choice"
        Comment = "Dispatch what Stage 7 queued; if it queued nothing because answers are already waiting, go and ask."
        Choices = concat(
          [{
            Variable      = "$.research_required"
            BooleanEquals = true
            Next          = "Research"
          }],
          # GUARDED WITH `IsPresent`, AND THE GUARD IS NOT DEFENSIVE PADDING -- a bare
          # `BooleanEquals` on an absent path does not evaluate false, it FAILS THE EXECUTION
          # with `States.Runtime: Invalid path`. The `Resolve` Lambda
          # that emits this field is defined in this module's `resolve.tf`, so applying
          # platform and research alone left the machine testing a field the deployed stage did
          # not yet produce. Every run then died at this Choice, one state after `Plan`, with
          # nothing to do with the thing being decided.
          #
          # So the pairing is stated here rather than assumed: a definition may reach a payload
          # older than itself -- a stage rolled back, a module applied out of order, an
          # execution in flight across a deploy -- and the honest behaviour for all three is to
          # take the default rather than to fail.
          #
          # IT NAMES THE SURVEY AND NOT THE WAIT. This is the edge that produced the defect: a
          # run whose every gap was withheld arrived here, branched straight into
          # `.waitForTaskToken`, and paused on a page whose every candidate the stage then
          # filtered out as already answered. Counting first is the whole change, and BOTH edges
          # into the wait route through the Choice -- this one and `Research.Next` -- because
          # either one left pointing at `Approval` still pauses unconditionally on its own path.
          var.approval_function_arn == null ? [] : [{
            And = [
              {
                Variable  = "$.approval_pending"
                IsPresent = true
              },
              {
                Variable      = "$.approval_pending"
                BooleanEquals = true
              },
            ]
            Next = "Approval survey"
          }],
        )
        Default = local.report_entry
      }
    }
  )

  # THE STORE-DIGEST BRANCH. DEPLOYED AND DORMANT.
  #
  # "Prepared, gated, and NOT APPLIED" is what this said, and it is no longer true. This
  # local is merged into `States` below and `describe-state-machine` shows `Knowledge
  # changed?` on the live definition. It IS applied. What it is not is REACHABLE: see the
  # note on the first Choice below. Applied-and-dormant and not-applied look identical from
  # a run's behaviour and are completely different when someone comes to finish the wiring,
  # which is the reason to be exact about it.
  #
  # `resume_decision` in loop/resolve_loop.py computes whether the enlarged store warrants
  # re-running decide and plan, from the store digest changing rather than from what
  # research reported. Nothing calls it: this is that comparison, expressed in the one
  # place that can act on it.
  #
  # `StringEqualsPath` compares two fields of the payload, which is exactly the shape
  # needed: Stage 7 puts `store_digest_before_research` on the payload, and a research
  # stage that honours the contract in loop/dispatch.py returns
  # `store_digest_after_research`. THE SECOND HALF IS THE MISSING ONE -- no handler returns
  # that field, and `Research` is a Map whose `ResultPath` is `$.research_results`, so a
  # per-item value cannot reach the payload root where this Choice looks for it.
  #
  # THE FIRST CHOICE IS THE COMPATIBILITY ONE, AND IT IS WHY THE SECOND NEVER RUNS. If a
  # research stage does not report the digest, the path does not exist and a bare
  # `StringEqualsPath` would fail the execution with `States.Runtime`. `IsPresent = false`
  # catches that -- and today it catches EVERY execution, because nothing sets the field at
  # all. Choices evaluate in order, so the comparison below it is never reached.
  #
  # WHERE THAT BRANCH GOES, STATED PLAINLY, BECAUSE THIS COMMENT USED TO GET IT BACKWARDS.
  # It said `IsPresent = false` "routes that case to today's unconditional re-run". It does
  # not: its `Next` is `Present`. There is no unconditional re-run and there never was one
  # on this definition -- the unconditional route is to the REPORT, and `Default -> Gaps` is
  # the unreachable branch. Observed on a test
  # run.
  #
  # So the compatibility argument still holds -- a Stage 6 built before this contract keeps
  # working -- but "keeps working" means "reaches the report", not "re-runs".
  knowledge_changed_state = (
    var.research_function_arn == null || local.resolve_function_arn == null ? {} : {
      "Knowledge changed?" = {
        Type    = "Choice"
        Comment = "Re-run only if research actually wrote a fact."
        Choices = concat(
          # THE BRANCH THAT ACTUALLY FIRES, and it is first because it has to be. The two
          # conditions below it were written for a definition with no approval stage: the
          # first matches whenever the digest is absent, which was EVERY execution, so
          # anything after it was unreachable. This one is evaluated before either.
          #
          # It reads the NESTED path because `Approval`'s ResultPath nests the resume
          # output under `$.approval`, and it compares against the root
          # `store_digest_before_research` that `Resolve` wrote before research ran.
          #
          # `And` + `Not` rather than two conditions: "the approval stage reported a digest
          # AND it differs from the one before research" is one question, and splitting it
          # would need a third branch for the absent case that the conditions below already
          # answer correctly.
          var.approval_function_arn == null ? [] : [{
            And = [
              {
                Variable  = "$.approval.store_digest_after_research"
                IsPresent = true
              },
              {
                Not = {
                  Variable         = "$.approval.store_digest_after_research"
                  StringEqualsPath = "$.store_digest_before_research"
                }
              },
            ]
            Next = "Gaps"
          }],
          [
            {
              # NO DIGEST REPORTED -> NOTHING WAS WRITTEN -> GO TO THE REPORT.
              #
              # This branch used to route to `Gaps`, as a compatibility guard for a Stage 6
              # that predates the digest contract. Observed live, that made the loop run its
              # maximum rounds every single time: four rounds, nine model calls where three
              # were needed, each round re-deriving byte-identical documents at the same keys.
              #
              # The reason is structural, not a missing field. Stage 6 writes a CANDIDATE, never
              # a fact -- `approve.py` is the only writer and it is behind an IAM-signed function
              # URL a human calls. So within one execution the store CANNOT change as a result of
              # research, and re-entering gap detection cannot change any verdict. Confirmed by
              # that run: the fact store still held only the seeded entry afterwards, with keda
              # REJECTED and cert-manager PENDING_APPROVAL.
              #
              # The loop closes ACROSS executions -- a human approves, the next run picks the
              # fact up -- not within one. So an absent digest means "nothing to re-run".
              Variable  = "$.store_digest_after_research"
              IsPresent = false
              Next      = local.report_entry
            },
            {
              # Reported and unchanged: nothing was written, so a re-run would reproduce
              # byte-identical documents. Stop instead of paying for two more rounds --
              # research is the only stage where a model runs, so it is the expensive one.
              Variable         = "$.store_digest_after_research"
              StringEqualsPath = "$.store_digest_before_research"
              Next             = local.report_entry
            },
          ],
        )
        # Changed: the store grew, so rebuild the ledger, the assessment and the plan.
        Default = "Gaps"
      }
    }
  )

  # A terminal Pass, needed only when there is a Choice and no presentation stage to
  # fall through to: a Choice's Default must name a state that exists.
  # ---------------------------------------------------------------------------
  # THE UPGRADE BRIEF: four states between `Knowledge changed?` and `Present`.
  #
  #   Anything to upgrade?  Choice  reads $.plan_counts -- costs no invocation
  #   Brief plan            Task    splits the moves into reuse and research
  #   Any to research?      Choice  skips the Map entirely when everything was cached
  #   Brief research        Map     one invocation per component that needs the agent
  #   Brief assemble        Task    writes the one advisory document
  #
  # WHY IT SITS AFTER `Knowledge changed?` AND NOT BEFORE IT. That Choice's changed branch loops
  # back to `Gaps` and REBUILDS the plan. A brief written before it would describe a plan that is
  # about to be replaced, and the brief is stored under the version hop it describes, so it would
  # be paid for and then superseded. Placing it on the terminal edges means the brief always
  # describes the FINAL plan. The changed branch needs no wiring at all: it comes round again and
  # eventually leaves by one of these edges.
  #
  # EVERY STATE CATCHES TO THE REPORT. This is the load-bearing property, in the user's own words:
  # if there is no brief we say so and they decide. The handlers turn every expected failure into
  # a recorded absence and return normally -- an unreachable project, a rate limit, a model that
  # would not answer. What they CANNOT report on is a failure to invoke them at all: a throttle, a
  # missing permission, a cold-start timeout. Without these catches those would fail the whole
  # execution and an ADVISORY stage would be able to deny a person their upgrade approval.
  #
  # The Map catches to `Brief assemble` rather than to `Present`, so the components that succeeded
  # are still assembled and the ones that died are named in `unbriefed` with their reason.
  # ONE LOCAL PER STATE, for the reason this file already gives for the Choices and the Map: a
  # Choice carries `Choices`/`Default`, a Task carries `Resource`/`Parameters`/`Retry`/`Catch`, and
  # a Map carries `Iterator`/`ItemsPath`. HCL cannot unify those object types across the values of
  # one map -- "Inconsistent conditional result types", which is exactly what a single
  # `brief_states` map earned. Splitting them keeps each one's attribute set uniform.
  brief_gate_state = (
    !local.brief_enabled ? {} : {
      "Anything to upgrade?" = {
        Type    = "Choice"
        Comment = "Brief only when the plan actually moves a component. Reads the payload; invokes nothing."
        Choices = [
          {
            Variable           = "$.plan_counts.component_moves"
            NumericGreaterThan = 0
            Next               = "Brief plan"
          },
          {
            Variable           = "$.plan_counts.recommended_upgrades"
            NumericGreaterThan = 0
            Next               = "Brief plan"
          },
        ]
        # DEFAULT IS `Brief plan`, NOT the report, and the asymmetry is deliberate: the two Choices
        # above fire on a KNOWN count, so the Default is reached when `plan_counts` is absent or
        # unreadable -- an older payload shape, say. The count is then UNKNOWN rather than zero, and
        # `Brief plan` reads the plan document itself and gets the truth for one cheap invocation.
        # Routing an unknown to the report would silently skip briefing a plan that did move
        # something, which is the failure this whole stage exists to prevent.
        Default = "Brief plan"
      }
    }
  )

  brief_plan_state = (
    !local.brief_enabled ? {} : {
      "Brief plan" = {
        Type       = "Task"
        Comment    = "Split the plan's component moves into briefs to reuse and briefs to research."
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = { FunctionName = var.brief_plan_function_arn, "Payload.$" = "$" }
        OutputPath = "$.Payload"
        Retry      = local.transient_retry
        Next       = "Any to research?"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.brief_error"
          Next        = local.brief_exit
        }]
      }
    }
  )

  brief_research_gate_state = (
    !local.brief_enabled ? {} : {
      "Any to research?" = {
        Type    = "Choice"
        Comment = "Skip the Map when every brief was served from storage."
        Choices = [{
          Variable           = "$.brief_research_count"
          NumericGreaterThan = 0
          Next               = "Brief research"
        }]
        # Nothing to research means every move already had a brief inside the reuse window, so go
        # straight to assembling them.
        Default = "Brief assemble"
      }
    }
  )

  brief_research_state = (
    !local.brief_enabled ? {} : {
      "Brief research" = {
        Type    = "Map"
        Comment = "One brief per component move, through the brief agent's own Harness."
        # The whole work item is the iteration's input -- no Parameters block. `brief-plan` built
        # each item to be exactly what the handler reads, so restating the fields here would be a
        # second place for the two to disagree.
        ItemsPath  = "$.brief_to_research"
        ResultPath = "$.brief_results"
        # ONE AT A TIME. Each iteration runs a multi-minute model loop and issues GitHub search
        # calls, and that endpoint allows 10 per minute per IP unauthenticated. Concurrency here
        # would turn a rate limit into several components reporting their issues as UNKNOWN.
        MaxConcurrency = 1
        Next           = "Brief assemble"
        Iterator = {
          StartAt = "Brief one component"
          States = {
            "Brief one component" = {
              Type       = "Task"
              Resource   = "arn:aws:states:::lambda:invoke"
              Parameters = { FunctionName = var.brief_component_function_arn, "Payload.$" = "$" }
              OutputPath = "$.Payload"
              Retry      = local.transient_retry
              End        = true
              # ONE BRIEF FAILING MUST NOT DROP THE OTHERS. Without this catch a single crashed
              # iteration fails the whole Map, and the Map-level catch below then hands the
              # assembler NO results at all -- every move loses its brief over one bad one
              # (observed live: one unknown quote verdict blanked six briefs). Caught here, the
              # failure becomes an ordinary result with no `brief_uri`, which the assembler
              # already records as an `unbriefed` step carrying this reason.
              Catch = [{
                ErrorEquals = ["States.ALL"]
                ResultPath  = "$.brief_failure"
                Next        = "Brief failed"
              }]
            }
            "Brief failed" = {
              Type = "Pass"
              Parameters = {
                stage           = "brief-component"
                "step_order.$"  = "$.step.order"
                "component.$"   = "$.step.component"
                briefed         = false
                blocks_pipeline = false
                "reason.$"      = "States.Format('the brief for this move failed ({}), so its release notes, known issues and new features are UNKNOWN, not empty. The other moves were briefed normally.', $.brief_failure.Error)"
              }
              End = true
            }
          }
        }
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.brief_error"
          # Reached only if the Map itself fails (a single iteration's failure is caught inside
          # the iterator above and becomes an `unbriefed` result). The assembler still runs, so
          # the report says the brief is missing rather than silently omitting it.
          Next = "Brief assemble"
        }]
      }
    }
  )

  brief_assemble_state = (
    !local.brief_enabled ? {} : {
      "Brief assemble" = {
        Type       = "Task"
        Comment    = "Write the run's one advisory brief document. Nothing downstream reads it."
        Resource   = "arn:aws:states:::lambda:invoke"
        Parameters = { FunctionName = var.brief_assemble_function_arn, "Payload.$" = "$" }
        OutputPath = "$.Payload"
        Retry      = local.transient_retry
        Next       = local.brief_exit
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.brief_error"
          Next        = local.brief_exit
        }]
      }
    }
  )

  done_state = (
    (length(local.choice_state) > 0 || length(local.knowledge_changed_state) > 0) ? {
      "Done" = {
        Type    = "Pass"
        Comment = "Nothing left to research."
        End     = true
      }
    } : {}
  )

  # Everything a state carries except its successor. Extracted so the terminal and
  # non-terminal states can be built by two separate comprehensions -- see the note
  # at `States` below, which explains why that split is load-bearing.
  # Task-shaped states only. `Research` is a Map and arrives already complete, so it
  # joins the literal merge below alongside the Choices rather than being wrapped.
  # ALL THREE ARNs OR NONE. A definition naming a state whose Resource is null is refused whole
  # by Step Functions, which is the trap `Plan.next` documents for the research pair -- so the
  # brief is enabled only when every end of it exists.
  brief_enabled = (
    var.brief_plan_function_arn != null
    && var.brief_component_function_arn != null
    && var.brief_assemble_function_arn != null
  )

  # Where the brief hands back to. Its own local because four brief edges name it and getting one
  # of them wrong would strand an execution on a state that does not exist.
  # WHERE THE BRIEF HANDS BACK TO. Its own local because four brief edges name it and getting
  # one of them wrong would strand an execution on a state that does not exist.
  #
  # IT WAS `Present`, THEN BRIEFLY `Done`, AND IS NOW THE APPROVAL. The report state was removed
  # because this stage renders the same report WITH controls; sending the brief to `Done` was
  # the intermediate step in that removal, and it is not a state anyone should reach directly
  # -- a run that produced a brief and then finished without asking anybody is the outcome this
  # stage exists to prevent.
  #
  # ALL FOUR EDGES INCLUDING THE CATCHES, which is the load-bearing part. `Brief plan`'s catch
  # and `Brief assemble`'s catch both name this, so a brief that could not be produced still
  # reaches the approval -- with the absence recorded and the page saying so per step. A brief
  # cannot gate an approval, and that is enforced here rather than trusted.
  # WHERE THE BRIEF HANDS OFF DEPENDS ON WHETHER THIS DEPLOYMENT EXECUTES. With the upgrade stage
  # enabled, to the approval gate that pauses the run and asks. Without it, to `Readiness report`:
  # the same page rendered without a control, mailed, and straight on to `Done`. Asking a person
  # to approve changes a deployment will never apply -- and then telling them nothing -- was what
  # the flag-off path did before this local learned about the flag.
  brief_exit = local.upgrade_enabled ? "Upgrade approval" : "Readiness report"

  # WHAT EVERY EDGE THAT USED TO NAME THE REPORT NOW NAMES. One local, used in all four places, so
  # the brief cannot be reachable from three of them and skipped by the fourth -- which is exactly
  # the shape of bug that would leave one degraded path silently unbriefed.
  report_entry = local.brief_enabled ? "Anything to upgrade?" : local.brief_exit

  all_states = local.task_states

  state_body = {
    for name, state in local.task_states : name => {
      Type       = "Task"
      Comment    = state.comment
      Resource   = "arn:aws:states:::lambda:invoke"
      Parameters = { FunctionName = state.arn, "Payload.$" = "$" }
      # Unwrap the Lambda envelope so the next state receives the stage's own
      # return value as its input, which is what makes the chain read as
      # document-in / document-out.
      OutputPath = "$.Payload"
      # COMPARED AGAINST THE STATE NAME, so it moved when the ordinals were dropped. If
      # this still read `"1 Scan"` it would match nothing, no error would be raised, and
      # Scan would silently inherit the 2/4/8s transient ladder -- 14s of waiting for a
      # stage that needs ~243s, which is the failure the scan ladder exists to prevent.
      # Checked against the deployed definition: Scan is the one state whose Retry is
      # IntervalSeconds 30 / MaxAttempts 5.
      Retry = name == "Scan" ? local.scan_retry : local.transient_retry
    }
  }
}

resource "aws_sfn_state_machine" "readiness" {
  name     = local.name_prefix
  role_arn = aws_iam_role.workflow.arn

  definition = jsonencode({
    # Left verbatim for the same reason as the sid above: this string is
    # part of the state machine definition, so editing it would turn a
    # default-variable apply into an in-place update.
    Comment = "EKS add-on upgrade readiness: five stages, one document each."
    StartAt = "Scan"
    # TWO comprehensions rather than one with a conditional, and the split is
    # load-bearing rather than stylistic.
    #
    # A single `for` yielding both `{ Next = <string> }` and `{ End = true }` makes
    # Terraform unify those two shapes into one map element type, and the only type
    # that holds a string and a bool is string -- so `End` encodes as the STRING
    # "true" and Step Functions refuses the definition with
    # `SCHEMA_VALIDATION_FAILED: Expected value of type Boolean`. That is exactly
    # what happened when `"Plan".next` stopped being a literal null and became a
    # conditional, which is what made the element types uniform in the first place.
    #
    # Giving the terminal state its own comprehension keeps each one's values
    # uniformly typed, so the boolean stays a boolean. Checked against the deployed
    # definition, which carries `End: true`.
    States = merge(
      {
        for name, state in local.all_states : name => merge(
          local.state_body[name], { Next = state.next }
        ) if state.next != null
      },
      {
        for name, state in local.all_states : name => merge(
          local.state_body[name], { End = true }
        ) if state.next == null
      },
      # Not Tasks, and written literally rather than derived, so their booleans stay
      # booleans for the reason given above.
      local.choice_state,
      local.knowledge_changed_state,
      local.done_state,
      # `Research`, a Map state, already carries its own Type, Next, Iterator and
      # Parameters. Written literally for the same reason the Choices are: deriving it
      # would unify its element types with the Task states and turn its booleans into
      # strings.
      local.research_state,
      # `Approval`, a `.waitForTaskToken` Task. Literal for the same reason: it carries its
      # own Resource, Parameters, ResultPath and TimeoutSeconds, and `state_body` would
      # overwrite the first two with a plain `lambda:invoke`.
      #
      # OMITTING THIS LINE IS A LIVE DEFECT, NOT A COSMETIC ONE, and it was omitted for
      # several passes. `Research.Next` becomes `"Approval"` as soon as
      # `approval_function_arn` is supplied, so without this merge the definition names a
      # successor that is not in `States` and Step Functions refuses the whole thing --
      # nothing subtle, but nothing catches it until an apply is attempted, because every
      # `terraform validate` passes and the null-ARN default hides it entirely.
      local.approval_state,
      # The approval PRE-CHECK and the Choice that spends its count. Literal for the reason every
      # line here is: the Task carries its own Resource, ResultSelector, ResultPath and Catch, and
      # the Choice is not a Task at all.
      #
      # OMITTING EITHER IS THE SAME LIVE DEFECT `local.approval_state` DOCUMENTS ABOVE, and now
      # doubly so: `Research.Next` becomes `"Approval survey"` as soon as
      # `approval_function_arn` is supplied and that state's `Next` names `Anything to approve?`,
      # so a missing merge leaves the definition naming a successor that is not in `States` and
      # Step Functions refuses the whole thing. `terraform validate` passes either way.
      local.approval_survey_state,
      local.approval_needed_state,
      # The brief's five states. Literal for the reason the Choices and the Map above are: they
      # carry their own Type, Choices, Iterator and Catch, and `state_body` would flatten them.
      local.brief_gate_state,
      local.brief_plan_state,
      local.brief_research_gate_state,
      local.brief_research_state,
      local.brief_assemble_state,
      # The upgrade approval. Literal for `approval_state`'s reason: it carries its own Type,
      # Resource, Parameters and TimeoutSeconds, and `state_body` would flatten a
      # `.waitForTaskToken` invocation into a plain `lambda:invoke` -- which would run the
      # handler and carry straight on, never pausing, with nothing reporting that a person was
      # supposed to be asked.
      #
      # EMITTED EXACTLY WHEN THE UPGRADE STAGE IS ON, and the release gate below with it. It used
      # to be unconditional so that omitting it could not be a silent defect; now `brief_exit`
      # names `Readiness report` when the stage is off, nothing names this state, and Step
      # Functions refuses a state nothing reaches. The reachability check still holds the halves
      # together: a definition that named the approval and dropped the gate -- or the reverse --
      # is refused whole.
      local.upgrade_enabled ? local.upgrade_approval_state : {},
      # The release gate's TEN states. Literal for `upgrade_approval_state`'s reason and more
      # emphatically: a Choice, a Wait, four Tasks, a Pass and a Fail all carry their own Type, and
      # `state_body` would flatten every one of them into a plain `lambda:invoke` -- turning the
      # gate into eight invocations of Lambda functions that do not exist.
      #
      # `Release approvers` IS A `lambda:invoke` AND STILL CANNOT COME FROM `state_body`: that
      # comprehension passes the whole payload and unwraps the envelope onto `$`, which would
      # destroy the `upgrade_approval` result the four states after it read.
      #
      # GATED WITH THE APPROVAL, for its reason: `Upgrade approval.Next` names `Anything
      # approved?`, so with the approval present a definition missing these is refused whole, and
      # with it absent these would be unreachable and refused for that.
      local.upgrade_enabled ? local.anything_approved_state : {},
      local.upgrade_enabled ? local.release_prep_state : {},
      local.upgrade_enabled ? local.release_gate_state : {},
      local.upgrade_enabled ? local.release_waiting_state : {},
      local.upgrade_enabled ? local.release_poll_state : {},
      local.upgrade_enabled ? local.release_decided_state : {},
      local.upgrade_enabled ? local.release_approved_state : {},
      local.upgrade_enabled ? local.release_assembled_state : {},
      local.upgrade_enabled ? local.release_recorded_state : {},
      local.upgrade_enabled ? local.release_rejected_state : {},
      # The flag-off terminus, present exactly when the ten above are not.
      local.readiness_report_state,
      # The upgrade stage's four top-level states, or NOTHING when
      # `var.upgrade_stage_enabled` is false -- which is its default. Literal for the reason every
      # line above is: a Map, a Choice and a Fail carry their own Type, and `state_body` would
      # flatten all three into a plain `lambda:invoke`.
      #
      # GATED, UNLIKE THE APPROVAL AND THE RELEASE GATE, and the asymmetry is the point. Those two
      # are unconditional so that omitting them cannot be a silent defect. This one must be
      # omittable on purpose: a stage that CHANGES a cluster should be absent from a machine
      # somebody is watching until they have said so, and with the flag false `Release approved`
      # still names `Done` and not one of these states is emitted. See `upgrade_stage.tf`.
      local.upgrade_stage_states,
    )
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.workflow.arn}:*"
    include_execution_data = true
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  depends_on = [aws_iam_role_policy.workflow]
}
