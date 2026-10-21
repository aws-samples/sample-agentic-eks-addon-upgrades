# The release gate: the state that asks a NAMED AWS PRINCIPAL to authorise what a person
# approved on the page.
#
# WHY IT EXISTS, AND WHAT IT IS NOT. `Upgrade approval` renders a report with a control per step
# and collects INTENT: somebody read the plan and said yes to steps 1 and 2. What it cannot
# collect is AUTHORISATION, and it says so honestly -- every control is a single-use presigned
# form, so what a click proves is that whoever held the capability opened the mailbox it was sent
# to. Not which human. Not which principal. That is why `StepDecision.approver_authority` records
# `EMAIL_LINK_TOKEN` and never claims AWS resolved anybody.
#
# This gate closes that gap and nothing else. `aws:approve` refuses a signal from a principal
# outside its `Approvers` list, so reaching `Done` means AWS authenticated somebody who was named
# in advance. It is still NOT an execution grant -- see `upgrade.py`'s module docstring, which is
# emphatic that these documents are an audit trail and a resume record. The executor, when it
# lands, reads them and applies its own gate.
#
# NO LAMBDA HOLDS A RESUME CAPABILITY, WHICH IS THE PART OF "NO LAMBDA" THAT WAS EVER LOAD-
# BEARING. Everything in this file is still a declaration: an SSM Automation runbook, two IAM
# roles, and states in `workflow.tf` that start the runbook and watch it. Nothing here can
# resume the execution or signal its own approval.
#
# ONE PIECE OF CODE DOES RUN BEFORE THE GATE, and it is honest to say so here rather than let a
# reader of this header believe otherwise. `stages/release_prep.py` resolves the approver list
# from the cluster's access entries and builds the change lines the mail names -- three read-only
# `eks:` calls and no write of any kind, hosted in the EXISTING upgrade-approval function, so the
# deployment gains no function and no role. It decides who is ON the list; it never decides
# whether a signal counts, which remains `aws:approve`'s job and SSM's alone.
#
# THE TASK TOKEN NEVER LEAVES STEP FUNCTIONS, which is the one design decision worth knowing
# before reading any of this. The obvious shape is
# `ssm:startAutomationExecution.waitForTaskToken` with the token passed as a document parameter,
# and it CANNOT WORK: a real task token from this state machine is 664 characters and an SSM
# document parameter value is capped at 512 (`AutomationParameterValue`, max 512 / min 1 -- so
# splitting it across padded parameters does not close it either, because an empty part is
# refused and SSM has no way to trim a sentinel back off). Every start would be refused with a
# ValidationException and the gate would fail closed on every single run -- a dead end, not a
# working gate.
#
# So the direction of the wait is INVERTED: the state machine starts the automation and polls
# `GetAutomationExecution` until SSM reports a terminal status. Two things follow, and both are
# improvements rather than consolations:
#
#   1. NOTHING SENSITIVE IS EXPOSED. A document parameter is returned verbatim by
#      `GetAutomationExecution` -- which every approver must be able to call -- and recorded in
#      CloudTrail's `requestParameters` for `StartAutomationExecution`, with no masking available
#      (schema 0.3 has no secure parameter type). A task token passed that way would be a
#      capability sitting in a log, which this project's convention forbids. Passing none is the
#      only version of that convention that needs no exception.
#   2. THE AUTOMATION ROLE NEEDS NO POWER OVER THE EXECUTION AT ALL -- no `states:SendTaskSuccess`,
#      no `states:SendTaskFailure`. It can send one email and nothing else. That is strictly less
#      than `upgrade_decision_watcher` holds, and it is the strongest form of the boundary
#      `upgrade_approval.tf` argues for: a thing that asks should not be able to answer.

data "aws_partition" "current" {}

# THERE IS NO `release_approvers` LOCAL ANY MORE, AND ITS ABSENCE IS THE CHANGE.
#
# It used to be `concat([aws_iam_role.release_approver.arn], var.release_approver_arns)`: a
# hand-kept list of ARNs in a tfvars file, baked into the document body at apply time.
# Both ways that drifts are silent. A principal who loses cluster admin keeps authorising
# releases until somebody remembers to edit the tfvars, and a new cluster admin is refused even
# though AWS already considers them able to change the cluster by hand -- with nothing anywhere
# reporting either.
#
# The list is now RESOLVED PER RUN from the access entries of the cluster the run is about, by
# `stages/release_prep.py`, and passed to `StartAutomationExecution` as the `Approvers` document
# parameter below. So the document carries no approver at all, this file names no principal, and
# the gate's authorisation boundary cannot disagree with the cluster's because there is only one
# boundary. `aws:approve` still does the matching; nothing in this repository compares a caller
# to a list.

# ---------------------------------------------------------------------------
# The runbook
# ---------------------------------------------------------------------------

resource "aws_ssm_document" "release_gate" {
  name            = "${local.name_prefix}-release-gate"
  document_type   = "Automation"
  document_format = "YAML"

  # `yamlencode` RATHER THAN A HEREDOC, so the document is a Terraform value that references
  # `var.release_approval_timeout_seconds` directly. A heredoc would interpolate it into a
  # string, and a typo in a `${...}` inside YAML is a document that creates successfully with
  # the literal text in it.
  content = yamlencode({
    description   = "Release gate: ask a named AWS principal to authorise the approved upgrade steps."
    schemaVersion = "0.3"

    # A RUNBOOK PARAMETER, NOT A CONSTANT ARN. Both are supported; a parameter is what lets the
    # start call name the role, so `terraform plan` shows the role moving rather than the
    # document body changing whenever the role is replaced.
    assumeRole = "{{ AutomationAssumeRole }}"

    parameters = {
      RunId = {
        type        = "String"
        description = "The execution name of the run being authorised."
      }
      Cluster = {
        type        = "String"
        description = "The cluster this release would change."
      }
      Summary = {
        type        = "String"
        description = "One line: how many steps were approved, rejected and left undecided."
      }
      ApprovedSteps = {
        type        = "String"
        description = "The step orders a person approved, as a JSON array."
      }
      Changes = {
        # ONE ENTRY PER APPROVED STEP -- `coredns 1.11.1 -> 1.12.0` -- built by
        # `stages/release_prep.py` from the Stage 5 plan and passed at start time.
        #
        # A `String` HOLDING A JOINED LIST, AND NOT A `StringList`, AND THAT IS NOT A STYLE
        # CHOICE. It WAS a StringList, and AWS refused the document at apply time:
        #
        #     Parameter "Changes" is "STRING_LIST" type and can't be used as a substring
        #     parameter
        #
        # SSM substitutes a StringList only where a list is CONSUMED as a list -- the entire
        # value of a step input, as `Approvers` is below -- and never inside a string. This
        # parameter exists to be read inside two mail bodies, which is a substring position by
        # construction, so the type had to be the one that can live there. The resolver joins
        # the lines with `, ` before they are passed; `tests/test_the_release_gate_document_
        # types_match_its_substitutions.py` is the guard that would have caught this locally,
        # and it checks every parameter against its declared type in both directions.
        #
        # WHY THE COUNTS WERE NOT ENOUGH. `Summary` says "2 approved, 1 rejected", and a count
        # is not checkable: an approver cannot verify a number against a cluster, so
        # authorising it is authorising a claim. A component with a from-version and a
        # to-version can be checked in one command. The counts stay, beside this -- `Summary`
        # is also the `aws:approve` message, and the two answer different questions. Fixing the
        # type error by deleting `{{ Changes }}` from the mail was the cheap option and was
        # refused: it would have traded the whole capability for a valid document.
        #
        # NO DEFAULT, deliberately, so a start call that forgets the changes is refused by SSM
        # rather than mailing an authorisation request that names nothing.
        type        = "String"
        description = "The approved steps as text: component, from version, to version, joined."
      }
      Approvers = {
        # THE AUTHORISATION BOUNDARY, AS A PARAMETER. Resolved per run from the cluster's own
        # access entries -- see the comment where the old `release_approvers` local used to be,
        # and `stages/release_prep.py` for the resolution itself.
        #
        # A StringList, because `aws:approve`'s `Approvers` input is a list and a String would
        # arrive as one comma-joined pseudo-ARN that matches nobody. A run with one approver
        # passes a one-element list.
        #
        # NO DEFAULT, AND THAT IS THE LOAD-BEARING PART OF THIS BLOCK. An SSM parameter with a
        # default is a value the document supplies when the caller supplies none -- which is
        # precisely how "who may authorise a cluster change" could come to have an answer
        # nobody chose. There is no default here, no default in the resolver, and no fallback
        # list anywhere on the path: an unresolvable list fails the run at `Release rejected`.
        type        = "StringList"
        description = "The principals the cluster's access entries make cluster administrators."
      }
      AutomationAssumeRole = {
        type        = "String"
        description = "The role this automation runs as."
      }
    }

    # NO DOCUMENT-LEVEL `outputs`, AND THE ABSENCE IS THE FIX FOR A DEFECT THAT SHIPPED.
    #
    # This block used to promote `approve.ApproverDecisions` and `approve.ApprovalStatus` so that
    # `Release approved` could read them from `GetAutomationExecution`. It does not work for
    # `aws:approve`. Deployed and run on 2026-09-19, with the `approve` step in status `Success`
    # and its OWN `Outputs` holding the real ARN, the execution's `Outputs` map returned the
    # literal string:
    #
    #     "No output available yet because the step is not successfully executed"
    #
    # So the state machine copied that placeholder into `$.release.approver_decisions` and the run
    # went green having recorded no principal at all -- the one failure this whole gate exists to
    # prevent, wearing the face of a success.
    #
    # `Release approved` now reads the `approve` step's own `Outputs` out of `StepExecutions[]`,
    # where the value was always present. Nothing needs promoting, so nothing is promoted: a
    # declaration that silently yields a placeholder is worse than no declaration, because the
    # reader of this file would believe it.

    mainSteps = [
      {
        name        = "notify"
        action      = "aws:executeAwsApi"
        description = "Mail the request. SES trouble must never fail the gate."

        # `Continue`, AND THAT ALSO MAKES THE STEP NON-CRITICAL -- per the shared-properties
        # reference, `isCritical` defaults to false when `onFailure` is `Continue`. So a bounced
        # or denied send leaves the automation Waiting for an approver who can still find it in
        # the console, exactly as an empty `approval_email_from` leaves the upgrade approval
        # usable. The alternative is a run that fails because a mail server was unhappy, holding
        # a perfectly good authorisation request nobody was allowed to answer.
        onFailure = "Continue"
        nextStep  = "approve"

        inputs = {
          Service     = "ses"
          Api         = "SendEmail"
          Source      = var.approval_email_from
          Destination = { ToAddresses = var.approval_email_to }

          # THE SAME TEMPLATE THE OTHER TWO APPROVAL MAILS USE, and matching it was the point.
          # This message was plain text with a key-value block -- `cluster`, `run`, `approved
          # steps`, `summary` -- which neither `notify/email.py` nor `notify/upgrade_email.py`
          # does, and which reads like a log line rather than a request. It also shouted "NAMED
          # AWS PRINCIPAL" in capitals at somebody who has not yet been told what is being asked.
          #
          # So it now carries the shared shape: the uppercase eyebrow, the cluster as the
          # heading, what is being asked in one sentence, the counts in the bordered callout the
          # upgrade mail reserves for the thing most likely to be misread, one accent button, and
          # the fine print in the footer with the run id. Nothing here is new design; it is the
          # HTML of `upgrade_email.py` with this gate's words in it.
          #
          # HTML AND TEXT BOTH, because the previous version sent only text and some clients get
          # only what you send. The text part carries the same sentences in the same order, so a
          # reader who sees the fallback is not reading a different message.
          #
          # THE CALLOUT NOW NAMES THE ACTUAL CHANGES, with the counts beside them rather than
          # instead of them. `{{ Changes }}` is one entry per approved step -- `coredns 1.11.1 ->
          # 1.12.0` -- and `{{ Summary }}` is still the count line, which is also the
          # `aws:approve` message and the subject. An approver asked to authorise "2 steps" is
          # being asked to authorise a number they cannot check; the same person asked to
          # authorise a component moving between two versions can check it in one command.
          Message = {
            Subject = {
              Data = "Authorisation needed: {{ Summary }}"
            }
            Body = {
              # THE CONSOLE LINK IS THE WHOLE MESSAGE. Unlike the upgrade approval's mail there
              # is nothing to presign and no capability to mint: the link goes to the SSM
              # console, and AWS authenticates the reader there. A recipient who is not a listed
              # approver can open it and cannot approve.
              #
              # THE TARGET KUBERNETES VERSION IS NOT A PARAMETER of this runbook, so the heading
              # names the cluster alone rather than `cluster -> Kubernetes 1.36` as the upgrade
              # mail does. `{{ Summary }}` already ends with `on <cluster> -> Kubernetes <n>`, so
              # the version is stated in the callout instead of being invented here. Putting it in
              # the heading would mean a new parameter and a matching Input in `workflow.tf`.
              Html = {
                Data = <<-EOM
                  <!DOCTYPE html>
                  <html><body style="margin:0;padding:28px 24px;background:#0f1115;
                  font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#eef1f6">
                  <div style="max-width:820px;margin:0 auto">
                    <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#f9ab00">
                      Amazon EKS &middot; authorise add-on updates
                    </div>
                    <h1 style="font-size:21px;margin:8px 0 6px;font-weight:600">{{ Cluster }}</h1>
                    <p style="font-size:14px;color:#b9c0cc;margin:0 0 4px;line-height:1.6">
                      The add-on changes below were approved on the report for this cluster, and the
                      run is now waiting for a named AWS principal to authorise the release.
                      Approving records <strong style="color:#eef1f6">your IAM principal</strong>
                      against it. <strong style="color:#eef1f6">Nothing has been changed yet.</strong>
                    </p>
                    <p style="font-size:14px;color:#eef1f6;margin:14px 0 0;line-height:1.6;
                      border-left:3px solid #f9ab00;padding:10px 14px;background:#161922;border-radius:4px">
                      <strong>{{ Changes }}</strong><br>
                      {{ Summary }}<br>
                      Only the approved steps are covered by this authorisation. Anything left
                      undecided is offered again on the next run and is never read as a refusal.
                    </p>
                    <a href="https://{{ global:REGION }}.console.aws.amazon.com/systems-manager/automation/execution/{{ automation:EXECUTION_ID }}"
                      style="display:inline-block;padding:12px 22px;margin:20px 0;
                      background:#f9ab00;color:#0f1115;text-decoration:none;border-radius:6px;
                      font-weight:600;font-size:15px">Authorise this release</a>
                    <p style="font-size:11px;color:#8f97a5;border-top:1px solid #2a2f3a;padding-top:14px;
                      line-height:1.6">
                      Approve and reject are in the Systems Manager console, where AWS authenticates
                      you. Only a principal named in advance can answer; anyone else can open the link
                      and cannot act. Rejecting, or letting the request expire, fails the run and
                      authorises nothing &mdash; the decisions already made on the report are saved
                      either way. Approved steps {{ ApprovedSteps }}.
                      <br>run {{ RunId }}
                    </p>
                  </div>
                  </body></html>
                EOM
              }
              Text = {
                Data = <<-EOM
                  Authorisation needed: {{ Summary }}

                  The add-on changes approved on the report for {{ Cluster }} are waiting for a named
                  AWS principal to authorise the release. Approving records your IAM principal against
                  it. Nothing has been changed yet.

                  What would be released: {{ Changes }}

                  Only the approved steps are covered by this authorisation. Anything left undecided is
                  offered again on the next run and is never read as a refusal.

                  Authorise this release:
                  https://{{ global:REGION }}.console.aws.amazon.com/systems-manager/automation/execution/{{ automation:EXECUTION_ID }}

                  Approve and reject are in the Systems Manager console, where AWS authenticates you.
                  Only a principal named in advance can answer; anyone else can open the link and cannot
                  act. Rejecting, or letting the request expire, fails the run and authorises nothing --
                  the decisions already made on the report are saved either way.

                  approved steps {{ ApprovedSteps }}
                  run            {{ RunId }}
                EOM
              }
            }
          }
        }
      },
      {
        name        = "approve"
        action      = "aws:approve"
        description = "Wait for a listed principal. SSM does the matching, not this repository."

        # ABORT ON REJECTION, and there is deliberately no `step:` target. A rejection makes the
        # whole automation `Failed`, which is what `Release decided?` reads as a refusal -- so
        # the refusal travels by the automation's own status rather than by a step this document
        # would have to keep in step with the state machine.
        onFailure = "Abort"

        # THE DEADLINE, ENFORCED HERE AND NOWHERE ELSE. The state machine has no competing
        # `TimeoutSeconds` on the poll loop, so there is exactly one number and no way for two
        # deadlines to disagree about when a run should have given up.
        timeoutSeconds = var.release_approval_timeout_seconds

        isEnd = true

        inputs = {
          # NO `NotificationArn`. The mail above is the notification, and an SNS topic here would
          # be a second delivery path with its own subscriber list -- a second set of people who
          # learn a cluster change is pending, declared nowhere and reviewed by nobody.
          Message = "{{ Summary }}"

          # ONE APPROVAL. Not a quorum: this pipeline has one operator, and configuring a
          # two-person rule that one person satisfies would be a claim the deployment does not
          # support. One honest approver beats a quorum of one.
          MinRequiredApprovals = 1

          # THE AUTHORISATION BOUNDARY, AND THE DOCUMENT CARRIES NO PART OF IT. The list arrives
          # as a parameter that has no default, resolved per run from the cluster's access
          # entries. So there is no value of "who may authorise a cluster change" anywhere in
          # this document, in this file, or in any tfvars -- which is strictly stronger than the
          # previous version, where the answer was a literal ARN somebody had to remember to
          # maintain.
          #
          # A BARE `{{ Approvers }}` AND NOT A LIST CONTAINING IT. SSM substitutes a StringList
          # parameter as the list when the whole value is the reference; wrapping it as
          # `["{{ Approvers }}"]` would yield a single element holding a comma-joined string,
          # which matches no principal and would pause forever.
          #
          # SSM DOES THE PRINCIPAL MATCHING. A role ARN here covers the sessions assumed from
          # that role, which is why a federated `assumed-role/Admin/<session>` identity can
          # approve against `role/Admin`. Nothing in this repository compares a caller to this
          # list -- the resolver decides who is ON it and never who signalled.
          Approvers = "{{ Approvers }}"
        }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# The role the runbook runs as
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "release_gate_automation_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ssm.amazonaws.com"]
    }

    # NO `aws:SourceAccount` CONDITION, unlike `states_assume` in workflow.tf, and the asymmetry
    # is deliberate rather than an oversight. AWS's documented trust policy for an Automation
    # service role is the bare service principal, and it is not established that
    # `ssm.amazonaws.com` populates `aws:SourceAccount` when it assumes on behalf of
    # StartAutomationExecution. An unverified condition here does not fail safe: it fails the
    # assume on every run, which is a gate that never once asks anybody -- the exact outcome this
    # whole file is arranged to avoid.
    #
    # THE NARROWING THAT IS VERIFIED SITS ON THE OTHER SIDE. The only principal that can hand
    # SSM this role is the state machine role, whose `PassTheAutomationRole` statement names this
    # one role and conditions on `iam:PassedToService = ssm.amazonaws.com`. So the confused
    # deputy this condition would guard against has no way to reach the role in the first place.
  }
}

resource "aws_iam_role" "release_gate_automation" {
  name               = "${local.name_prefix}-release-gate-automation"
  description        = "Runs the release gate runbook. Can send one email and nothing else."
  assume_role_policy = data.aws_iam_policy_document.release_gate_automation_assume.json
}

data "aws_iam_policy_document" "release_gate_automation" {
  # Mail the request. Mirrors `MailTheApprovalLink` in upgrade_approval.tf: scoped to this
  # deployment's SES sender identity ARN, with the `ses:FromAddress` condition kept as defence
  # in depth.
  dynamic "statement" {
    for_each = var.approval_email_from == "" ? [] : [var.approval_email_from]

    content {
      sid       = "MailTheReleaseRequest"
      effect    = "Allow"
      actions   = ["ses:SendEmail", "ses:SendRawEmail"]
      resources = [local.ses_sender_identity_arn]

      condition {
        test     = "StringEquals"
        variable = "ses:FromAddress"
        values   = [statement.value]
      }
    }
  }

  # DELIBERATELY ABSENT: `states:SendTaskSuccess` and `states:SendTaskFailure`. This design hands
  #   the runbook no task token, so it has nothing to resume and needs no power to resume it --
  #   the execution watches the automation rather than the automation resuming the execution.
  #   Reintroducing `.waitForTaskToken` means adding these back, and
  #   `tests/test_the_release_gate_grants_nothing_it_does_not_need.py` fails until somebody
  #   deletes a line, which is the conversation that change should have to have.
  # DELIBERATELY ABSENT: every `s3:` action. This runbook reads no document and writes none. The
  #   summary it mails is handed to it as a parameter, already derived.
  # DELIBERATELY ABSENT: every `eks:` action. It does not touch the cluster. Authorising a change
  #   and making one are different jobs, and this role may only do the first.
  # DELIBERATELY ABSENT: every `dynamodb:` action. No fact store, no token store. An upgrade
  #   authorisation is about one cluster and must not be able to write anything fleet-wide.
  # DELIBERATELY ABSENT: every `bedrock:` action. Nothing here calls a model.
  # DELIBERATELY ABSENT: every `ssm:` action. `aws:approve` needs none -- the APPROVER holds
  #   `ssm:SendAutomationSignal`, and a runbook able to signal itself would be a gate that
  #   approves itself.
}

# `count` RATHER THAN AN UNCONDITIONAL POLICY. The statement above lives in a `dynamic` block, so
# an empty `approval_email_from` yields a policy document with ZERO statements and IAM refuses
# that outright. Empty is a working configuration -- the automation still pauses and a listed
# approver still finds it in the console -- for the reason `approval_email_from`'s own docstring
# gives, so it must not be the thing that fails an apply.
resource "aws_iam_role_policy" "release_gate_automation" {
  count  = var.approval_email_from == "" ? 0 : 1
  name   = "release-gate-permissions"
  role   = aws_iam_role.release_gate_automation.id
  policy = data.aws_iam_policy_document.release_gate_automation.json
}

