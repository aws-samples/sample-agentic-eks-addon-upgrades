# The upgrade stage: the states that change a cluster, the build that changes it, and the one
# grant that opens a lane.
#
# WHERE IT HANGS. `Release recorded` used to name `Done`. It now names `Upgrade lanes` when this
# stage is enabled, and still names `Done` when it is not -- so the whole stage is behind ONE
# variable that defaults to FALSE. That is deliberate beyond the usual gating idiom: the operator
# is deploying every other component and watching each stage behave, and a stage that appeared in
# the machine because a branch merged would make its absence indistinguishable from its silence.
# `workflow.tf` therefore carries exactly two lines of this stage -- `local.after_release_recorded`
# and one line in the `States` merge -- and everything else is here.
#
# THE JUST-IN-TIME PART IS THE ACCESS ENTRY, NOT THE ROLE. `aws_iam_role.upgrade_build` is
# standing and has NO cluster access: no access entry, no `aws-auth` row, nothing. At `Lane open`
# the state machine calls `eks:CreateAccessEntry` for that one role with one
# `kubernetesGroups` member; at `Lane cleanup` it deletes it. So the pipeline never holds
# `iam:CreateRole` or `iam:PutRolePolicy`, and a leaked grant is a leaked ACCESS ENTRY -- one API
# call to inspect and one to revoke -- rather than a role nobody remembers creating.
#
# WHAT MAKES THAT GRANT SAFE IS AN IAM CONDITION, NOT A COMMENT. `eks:CreateAccessEntry` supports
# `eks:principalArn`, `eks:kubernetesGroups` and `eks:accessEntryType` as condition keys, so the
# statement below CANNOT create an entry for any principal but the build role, cannot name a group
# outside the reviewed list, and cannot mint a non-STANDARD entry. In particular it cannot create a
# `system:masters` entry, which is the escalation a bare `eks:CreateAccessEntry` on a cluster would
# be. And because the group list is exactly the components whose RBAC fence has been applied, A
# COMPONENT WITH NO REVIEWED FENCE CANNOT BE UPGRADED AT ALL: `Lane open` is denied and the lane
# reports LANE_NOT_OPENED.
#
# NOW GRANTED, AND THIS COMMENT USED TO FORBID IT. Corrected in place rather than deleted, because the
#   original reasoning was right and what changed is a fact it rested on. It said: an access policy
#   association bypasses the `resourceNames` fence entirely, since the lane would then hold the
#   policy's permissions whatever the pre-created Role says. Still true -- and there is no
#   `resourceNames` fence left to bypass. Two attempts at enumerating what Helm needs failed in three
#   separate places, so the lane now holds `AmazonEKSClusterAdminPolicy` for the life of one access
#   entry, and the approved chart's render -- with Helm refusing another release's objects -- is the
#   component boundary; `fence-check` only confirms that render ships the approved version. The
#   `Lane grant` state carries the full reasoning and what bounds the grant.
#
#   CONDITIONED ON THE PRINCIPAL, NOT ON THE POLICY, which is a real weakness and is stated on the
#   statement itself: a condition key for a policy ARN could not be verified against AWS's reference,
#   and an unverified condition key does not fail loudly -- it matches nothing and denies everything.
# DELIBERATELY STILL ABSENT: `eks:DisassociateAccessPolicy`. An association is keyed by (cluster,
#   principalArn) -- the access entry's own identity -- so deleting the entry leaves it nowhere to
#   live. `CloseTheLane` stays the single revocation, which keeps the number of calls that must
#   succeed on the way out at one rather than two.
# DELIBERATELY ABSENT: `eks:UpdateAccessEntry`. Updating an existing entry can add groups to it,
#   which is the create fence with an extra step. There is no update path: an entry is created,
#   used, and deleted.
# DELIBERATELY ABSENT: `iam:CreateRole`, `iam:PutRolePolicy`, `iam:AttachRolePolicy`. The identity
#   is standing and declared here; nothing at run time makes one.
# DELIBERATELY ABSENT from the BUILD role: every `eks:*AccessEntry` action. The build cannot open
#   its own lane, extend it, or keep it open past the run -- only the state machine can, and only
#   between the two states that bracket the build.

variable "upgrade_stage_enabled" {
  description = <<-EOT
    Whether a run may PROCEED past the readiness report into approval, release and the lanes.

    FALSE BY DEFAULT. This is a switch on the STATE MACHINE, not on the infrastructure: with it
    false the machine ends at `Readiness report` and not one of the approval, release or lane
    states is emitted, so nothing can write to a cluster. The CodeBuild project, its role, the
    report Lambda and their logs are built regardless (see `local.upgrade_built`) -- turning the
    flow off must not delete the logs of the lanes that already ran, and turning it back on must
    be a definition change, not a rebuild.
  EOT
  type        = bool
  default     = false
}

variable "upgrade_target_cluster" {
  description = <<-EOT
    The ONE cluster this stage may write to. No default: a stage that could invent which cluster
    to change is worse than one that fails to apply.

    Every grant below is scoped to it by ARN, so widening the blast radius to a second cluster is
    a visible change to this variable and a visible change in the plan.
  EOT
  type        = string
  default     = ""
}

variable "upgrade_lane_groups" {
  description = <<-EOT
    The Kubernetes groups a lane's access entry may name. ONE ENTRY, NOT ONE PER COMPONENT -- it was
    per-component when each had its own generated RBAC fence, and neither the fence nor the per-component
    group survives.

    WHAT IT STILL DOES, NOW THAT NO ROLE BINDS THE GROUP. The lane's Kubernetes permissions come from
    `AmazonEKSClusterAdminPolicy`, associated at `Lane grant` and revoked with the entry, so this group
    grants nothing by itself. The list remains because it is a `ForAllValues:StringEquals` condition on
    `eks:CreateAccessEntry`: it pins which group an entry may be created into, and that is what stops
    this stage minting an entry into `system:masters`. An inert group is the point -- it is a name the
    IAM condition can be written against.

    Held in step with `fence.py`'s constants by
    `tests/test_the_lane_grant_is_bounded_where_rbac_no_longer_bounds_it.py`.
  EOT
  type        = list(string)
  default     = []
}

variable "upgrade_build_image" {
  description = "CodeBuild image for the lane. Standard AL2023; helm and kubectl are installed and checksummed by the buildspec."
  type        = string
  default     = "aws/codebuild/amazonlinux-x86_64-standard:5.0"
}

variable "upgrade_helm_version" {
  description = "Pinned helm version the lane installs, e.g. v3.16.4."
  type        = string
  default     = "v3.16.4"
}

variable "upgrade_helm_sha256" {
  description = <<-EOT
    SHA256 of the pinned helm tarball. The buildspec verifies it and FAILS the build on a
    mismatch, before any identity is used -- so a substituted binary stops the run rather than
    performing the upgrade.

    Empty is not a working configuration here, unlike most empty defaults in this project: the
    precondition on the CodeBuild project refuses an apply without it.
  EOT
  type        = string
  default     = ""
}

variable "upgrade_kubectl_version" {
  description = "Pinned kubectl version, e.g. v1.35.0. Within one minor of the cluster."
  type        = string
  default     = "v1.35.0"
}

variable "upgrade_kubectl_sha256" {
  description = "SHA256 of the pinned kubectl binary. Verified by the buildspec; empty fails the apply."
  type        = string
  default     = ""
}

variable "upgrade_build_timeout_minutes" {
  description = <<-EOT
    How long one lane's build may run. Bounds the window in which the access entry is live, which
    is the number that actually matters: the entry is deleted by the state that follows the build,
    so a build that hangs is a grant that stays open until this timeout fires.
  EOT
  type        = number
  default     = 30
}

variable "upgrade_history_max" {
  description = <<-EOT
    `helm --history-max`. MUST NOT EXCEED the `release_secret_revisions` the fences enumerate:
    Helm keeping more history than the fence names leaves a release secret the lane cannot prune,
    and pruning is a `delete` on a name the fence must already carry.
  EOT
  type        = number
  default     = 20
}

variable "upgrade_triage_function_arn" {
  description = <<-EOT
    ARN of the upgrade agent's triage proxy, from `infra/terraform/modules/upgrade`'s
    `upgrade_triage_function_arn` output.

    NULL BY DEFAULT, AND THE STAGE WORKS WITHOUT IT. With it unset the lane never calls out: an
    unhealthy component has its diagnosis captured and sealed and is then rolled back
    deterministically, and the report says triage was unavailable. So wiring this turns a
    deterministic rollback into a triaged one, and leaving it unwired DEGRADES rather than breaks.

    That asymmetry is deliberate and it is the same one `infra/terraform/modules/brief` has: nothing advisory may
    gate a run. A model that is throttled, unreachable or answering badly must not be able to leave a
    broken component running, so the code's default is the safe action and the agent can only redirect
    it to REFER.
  EOT
  type        = string
  default     = null
}

variable "upgrade_health_deadline_seconds" {
  description = <<-EOT
    How long the agent is given to establish that an upgraded component is healthy.

    A DEADLINE, NOT A SLEEP. A rollout may legitimately be unhealthy while it settles -- a pod
    pulling an image, a readiness probe that has not passed its first check -- so the lane asks at
    several checkpoints and ends the moment the agent says HEALTHY. A component that comes up in
    forty-five seconds costs forty-five seconds.

    WHEN IT EXPIRES AND THE COMPONENT IS STILL NOT HEALTHY, THE ROLLBACK FIRES AUTOMATICALLY. That
    is the number this variable really sets: how long we are willing to let a possibly-broken
    component keep running before returning it to the version it was on.

    BOUNDED BY `upgrade_build_timeout_minutes`, by precondition. The build has to fit this window
    plus the rollback, the re-validation and the install phase -- otherwise CodeBuild kills the build
    and the deadline is silently truncated instead of enforced.
  EOT
  type        = number
  default     = 600
}

variable "upgrade_health_checkpoints" {
  description = <<-EOT
    Elapsed seconds at which the agent is asked, measured from the moment the change returned.

    THE LAST ONE MUST EQUAL `upgrade_health_deadline_seconds`: that is the call on which `SETTLING`
    is withdrawn, so a list whose last entry is short of the deadline would leave the window running
    with nobody asked at the end of it. A precondition refuses that.

    FIVE BY DEFAULT, front-loaded: most components are up within a minute and the early checkpoints
    are what make the common case cheap. The later ones exist for the slow legitimate rollout.
  EOT
  type        = list(number)
  default     = [45, 120, 240, 420, 600]
}

variable "upgrade_revalidation_checkpoints" {
  description = <<-EOT
    Seconds to wait between re-validation asks AFTER a rollback, as a list of gaps.

    SHORTER THAN THE FORWARD WINDOW, deliberately: a rollback goes to a version that was running
    minutes ago, so if it is not healthy quickly it is not going to be -- and that case is the worst
    outcome this stage reaches, so reaching it sooner is better than waiting politely.
  EOT
  type        = list(number)
  default     = [30, 60]
}


# ---------------------------------------------------------------------------
# What the stage is made of
# ---------------------------------------------------------------------------

locals {
  # TWO SWITCHES, DELIBERATELY. `upgrade_built` says whether the stage's resources EXIST; it is
  # true whenever a target cluster is named, because every resource here is scoped to that one
  # cluster and none is a risk in itself -- the CodeBuild role holds no cluster access until a
  # lane grants it. `upgrade_enabled` says whether a RUN may reach them: it gates only the state
  # machine. Off, a run ends at the readiness report and the resources sit idle with their logs
  # intact. They used to be one flag, and turning the flow off destroyed the build log group and
  # every lane's log with it.
  upgrade_built   = var.upgrade_target_cluster != ""
  upgrade_enabled = var.upgrade_stage_enabled

  # Where `Release recorded` goes. ONE LOCAL, referenced once in workflow.tf, so enabling this
  # stage is a variable rather than an edit to a file another lane is applying.
  #
  # `Release recorded` AND NOT `Release approved`, which is where this hooked before the release
  # record landed on main. That state writes the authorisation to S3, so hanging the lanes off it
  # means a lane opens only after the authorisation it acts on is durable -- and a run that could
  # not record its authorisation never reaches a lane.
  after_release_recorded = local.upgrade_enabled ? "Upgrade lanes" : "Done"

  # SKIPPING THE RELEASE GATE WHEN THIS STAGE IS OFF WAS ASKED FOR AND CANNOT BE DONE FROM HERE.
  # Recorded rather than silently dropped, because the reasoning was sound and the next person to
  # want it should not spend the same hour finding out why.
  #
  # THE IDEA. The gate exists to get a NAMED AWS PRINCIPAL to authorise a cluster change. With this
  # stage off no cluster change can happen, so the gate wakes a person, has them assume a role and
  # approve in the SSM console to authorise something nothing will do -- the same mistake
  # `Anything approved?` already avoids for an empty list.
  #
  # WHY IT DOES NOT WORK. Routing `Anything approved?` to `Done` leaves `Release gate` and the four
  # states after it with no inbound edge, and Step Functions REFUSES THE WHOLE DEFINITION:
  #
  #     ERROR (MISSING_TRANSITION_TARGET): State "Release gate" is not reachable.
  #
  # Unreachable states are not tolerated, so the skip would have to REMOVE those five states -- and
  # they are declared unconditionally in `workflow.tf` by the release-gate lane. Gating another
  # lane's states on this stage's flag is the coupling this file exists to avoid; `after_release_
  # approved` is two lines in their file precisely so that this stage can be added and removed
  # without touching their states.
  #
  # WHAT IT WOULD TAKE, for whoever decides to do it: make `local.release_gate_state` and the four
  # states around it conditional on the same flag, in `workflow.tf`, owned by that lane. That is a
  # cross-lane change and a deliberate one, not something to slip in here.

  upgrade_cluster_arn = (
    "arn:${data.aws_partition.current.partition}:eks:${var.region}:${local.account_id}:cluster/${var.upgrade_target_cluster}"
  )

  # The access entry the state machine may delete, by ARN. The UUID is the only wildcard: an
  # access-entry ARN is
  # `access-entry/<cluster>/<identity type>/<account>/<name>/<uuid>` and the uuid does not exist
  # until the entry does. So the grant is pinned to ONE principal's entry on ONE cluster -- the
  # state machine cannot delete the read role's entry, `Admin`'s, or the node group's.
  # A `coalesce` ON THE NAME, because a local is NOT lazy enough to rely on. This is referenced only
  # from the gated policy document, and Terraform still evaluates it when the stage is off -- and a
  # null inside a string template is an error rather than an empty segment. The placeholder is never
  # used: with the stage off, nothing consumes this local's value.
  upgrade_build_role_name = coalesce(
    one(aws_iam_role.upgrade_build[*].name),
    "${local.name_prefix}-upgrade-build",
  )

  upgrade_access_entry_arn = (
    "arn:${data.aws_partition.current.partition}:eks:${var.region}:${local.account_id}:access-entry/${var.upgrade_target_cluster}/role/${local.account_id}/${local.upgrade_build_role_name}/*"
  )

  # EVERY MANAGED ADD-ON ON THE ONE CLUSTER, not a committed list of names. This was
  # `upgrade_managed_addons`, four names typed into tfvars from `aws eks list-addons` on one day,
  # so a fifth add-on installed later would be inventoried, researched, planned, approved -- and
  # then denied by IAM in its lane, for a reason nothing upstream could have shown. The same defect
  # shape as the deleted per-chart lane catalogue. What bounds an update is the RELEASE RECORD, the
  # approved step a named principal authorised; IAM's job is to bound the cluster, and it does.
  upgrade_addon_arns = [
    "arn:${data.aws_partition.current.partition}:eks:${var.region}:${local.account_id}:addon/${var.upgrade_target_cluster}/*"
  ]

}

# ---------------------------------------------------------------------------
# The build's identity: standing, and with no cluster access of its own
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "upgrade_build_assume" {
  count = local.upgrade_built ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }

    # Both narrowings AWS documents for a CodeBuild service role, and they matter more here than
    # in most places: this is the role an access entry is minted for, so a confused deputy able to
    # assume it would be a confused deputy able to write to the cluster during a lane.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:codebuild:${var.region}:${local.account_id}:project/${local.name_prefix}-upgrade"]
    }
  }
}

resource "aws_iam_role" "upgrade_build" {
  count = local.upgrade_built ? 1 : 0

  name = "${local.name_prefix}-upgrade-build"
  description = join(" ", [
    "Runs one component's upgrade in CodeBuild.",
    "Holds NO cluster access of its own: the EKS access entry that grants it anything is created",
    "at the start of a lane and deleted at the end of it.",
  ])
  assume_role_policy = data.aws_iam_policy_document.upgrade_build_assume[0].json
}

data "aws_iam_policy_document" "upgrade_build" {
  count = local.upgrade_built ? 1 : 0

  statement {
    sid    = "WriteOwnBuildLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "${aws_cloudwatch_log_group.upgrade_build[0].arn}",
      "${aws_cloudwatch_log_group.upgrade_build[0].arn}:*",
    ]
  }

  # Read the source zip Terraform uploaded. NOT a wildcard on the bucket: the lane may read the
  # artifact that IS the lane and nothing else in the bucket, which is where every stage document
  # and every approval decision lives.
  statement {
    sid    = "ReadOwnSource"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.documents.arn}/${aws_s3_object.upgrade_source[0].key}"]
  }

  # Write the diagnosis bundles, the lane results, and nothing else. THREE PREFIXES, NAMED, for
  # the reason the read above is narrow: a lane that could write anywhere in this bucket could
  # rewrite the plan it was authorised against.
  statement {
    sid    = "WriteOwnEvidence"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
    ]
    resources = [
      "${aws_s3_bucket.documents.arn}/upgrade-diagnosis/*",
      "${aws_s3_bucket.documents.arn}/upgrade-lanes/*",
    ]
  }

  # Reach the cluster's API. `eks:DescribeCluster` is what `aws eks update-kubeconfig` needs; the
  # API server call itself is authorised by the access entry, not by IAM, which is the whole point
  # of the just-in-time design.
  statement {
    sid    = "ReachOneClustersEndpoint"
    effect = "Allow"
    actions = [
      "eks:DescribeCluster",
    ]
    resources = [local.upgrade_cluster_arn]
  }

  # The managed add-on path: read the add-on's current version, configuration and health, and
  # update it. Scoped to the one cluster; WHICH add-on moves is the approved release step's to say,
  # and the planner opens a lane only for a step a named principal authorised.
  statement {
    sid    = "UpdateThisClustersAddons"
    effect = "Allow"
    actions = [
      "eks:DescribeAddon",
      "eks:UpdateAddon",
    ]
    resources = local.upgrade_addon_arns
  }

  statement {
    sid       = "ReadTheGlobalAddonCatalogue"
    effect    = "Allow"
    actions   = ["eks:DescribeAddonVersions"]
    resources = ["*"]
  }

  # ASK THE UPGRADE AGENT, and only it. One function ARN, and the grant exists at all only when the
  # agent is deployed -- with `upgrade_triage_function_arn` null there is no statement, which is the
  # honest expression of a lane that does not call out.
  #
  # THE BUILD IS WHAT ASKS, rather than the state machine, because the triage has to happen at a
  # precise moment inside the lane: after validation refuses the component and after the `after`
  # bundle is SEALED, and before the rollback. Only the build knows when that is, and moving it into
  # the state machine would need a second build to act on the answer with the access entry open across
  # both.
  dynamic "statement" {
    for_each = var.upgrade_triage_function_arn == null ? [] : [var.upgrade_triage_function_arn]

    content {
      sid       = "AskTheUpgradeAgent"
      effect    = "Allow"
      actions   = ["lambda:InvokeFunction"]
      resources = [statement.value]
    }
  }

  # DELIBERATELY ABSENT: every `eks:*AccessEntry` action. The build cannot open its own lane,
  #   widen it, or keep it open. Only the state machine can, and only for the duration of the two
  #   states that bracket this build.
  # DELIBERATELY ABSENT: `eks:AccessKubernetesApi`. Access-entry authentication does not use it --
  #   it is the grant the AWS console needs to render Kubernetes objects. If the first acceptance
  #   run shows a denial that names it, this is the line to add, and it should be added with that
  #   evidence rather than pre-emptively.
  # DELIBERATELY ABSENT: `sts:AssumeRole` of any kind. An earlier shape of this design had the
  #   diagnosis reads run under the standing read-only role, which needed this action and a trust
  #   policy change in `infra/terraform/cluster-access`. The lane reads under its own identity instead
  #   and the fence carries the read verbs -- see `fence.py::VALIDATION_READ_RULES`.
  # DELIBERATELY ABSENT: every `s3:GetObject` outside the source key. The lane is told what to do
  #   by environment variables; it does not read the plan, the decisions or the release.
  # DELIBERATELY ABSENT: every `dynamodb:` action. No fact store, no token store. An upgrade must
  #   not be able to write anything fleet-wide.
  # DELIBERATELY ABSENT: every `bedrock:` action. No model runs in the build.
  # DELIBERATELY ABSENT: every `states:` action. The build cannot resume, fail or signal the
  #   execution that started it. `.sync` means Step Functions watches the build; the build has no
  #   power over Step Functions.
}

resource "aws_iam_role_policy" "upgrade_build" {
  count = local.upgrade_built ? 1 : 0

  name   = "upgrade-build-permissions"
  role   = aws_iam_role.upgrade_build[0].id
  policy = data.aws_iam_policy_document.upgrade_build[0].json
}

# ---------------------------------------------------------------------------
# What the state machine gains. A SECOND inline policy on the existing workflow role rather than
# statements added to `aws_iam_role_policy.workflow`: that policy is in `workflow.tf`, which
# another lane is applying, and a role carries many inline policies without collision. So this
# stage's grants arrive and leave with this stage.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "workflow_upgrade_stage" {
  count = local.upgrade_built ? 1 : 0

  # THE JIT GRANT, AND ITS THREE CONDITIONS ARE THE DESIGN. Without them, `eks:CreateAccessEntry`
  # on a cluster is a privilege escalation: any principal, any group, including `system:masters`.
  statement {
    sid       = "OpenOneLaneForOnePrincipal"
    effect    = "Allow"
    actions   = ["eks:CreateAccessEntry"]
    resources = [local.upgrade_cluster_arn]

    condition {
      test     = "StringEquals"
      variable = "eks:principalArn"
      values   = [aws_iam_role.upgrade_build[0].arn]
    }

    # The EXACT group set, not a prefix pattern. A prefix would let a future payload name
    # `upgrade-agent-anything`; this names the components whose fence exists.
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "eks:kubernetesGroups"
      values   = var.upgrade_lane_groups
    }

    # `ForAllValues` IS TRUE FOR AN ABSENT KEY, which would let an entry through with no groups at
    # all. That entry would grant nothing -- but it would also be an access entry this stage did
    # not intend, and the cleanup path keys on the principal rather than on the groups. Requiring
    # the key is one line and removes the case.
    condition {
      test     = "Null"
      variable = "eks:kubernetesGroups"
      values   = ["false"]
    }

    condition {
      test     = "StringEquals"
      variable = "eks:accessEntryType"
      values   = ["STANDARD"]
    }
  }

  # THE CLEANUP GRANT. Scoped by ARN to the one principal this stage mints for.
  #
  # ONE ARN, AND IT WAS TWO. The agent had its own read-only entry, opened by `Lane open reader` and
  # deleted by a cleanup state of its own; the agent reads through an AgentCore Gateway holding a
  # standing `AmazonEKSViewPolicy` entry now, so the lane mints nothing for it. This is the action
  # that must never fail for want of a permission, because a lane that cannot close leaves a
  # standing cluster grant -- and the one that remains is the cluster-admin one.
  statement {
    sid       = "CloseTheLane"
    effect    = "Allow"
    actions   = ["eks:DeleteAccessEntry"]
    resources = [local.upgrade_access_entry_arn]
  }

  # CLUSTER-ADMIN, FOR ONE LANE, FOR ONE PRINCIPAL. The widest grant in this deployment, and the
  # reasoning for it is on the `Lane grant` state rather than repeated here.
  #
  # SCOPED TO THE ACCESS ENTRY, NOT THE CLUSTER, AND THE FIRST VERSION OF THIS GOT IT WRONG. IAM
  # evaluates `eks:AssociateAccessPolicy` against the ENTRY being granted, not the cluster it lives
  # on. Granting the cluster ARN denied every call:
  #
  #     AccessDeniedException: User: .../<prefix>-workflow/... is not authorized to
  #     perform: eks:AssociateAccessPolicy on resource:
  #     arn:aws:eks:...:access-entry/my-eks-cluster/role/.../upgrade-build/...
  #
  # `Lane grant` failed five seconds into the lane, so CodeBuild never started and nothing reached the
  # cluster. The correct value was already in this file, two statements up: `CloseTheLane` uses
  # `local.upgrade_access_entry_arn` for exactly this resource shape.
  #
  # IT IS ALSO THE NARROWER GRANT, which is why this is a correction and not a trade. The cluster ARN
  # would have covered associating a policy to ANY principal's entry on that cluster; this covers one
  # principal's entry and nothing else.
  #
  # CONDITIONED ON THE PRINCIPAL AND NOT ON THE POLICY, still a stated weakness but a smaller one than
  # it was when the resource was the whole cluster: this statement permits associating ANY access
  # policy -- not only cluster-admin -- to THAT ONE ENTRY. The reason is not laziness: a condition key
  # for a policy ARN could not be verified against AWS's own reference, and an unverified condition key
  # is the worst thing to guess at in IAM. It does not fail loudly; it matches nothing, denies every
  # call, and presents as a broken lane rather than as a bad policy. So the narrowing that IS reliable
  # is applied instead -- the policy ARN is a literal in the committed state machine definition, and
  # `states.amazonaws.com` is the only principal that can assume this role.
  #
  # NO `eks:DisassociateAccessPolicy`, DELIBERATELY. An association is keyed by (cluster,
  # principalArn) -- the access entry's own identity -- so deleting the entry leaves it nowhere to
  # live. `CloseTheLane` above is what revokes this grant, which keeps the number of things that must
  # succeed on the way out at one rather than two. If a run ever shows an association surviving its
  # entry, this comment is wrong and the cleanup needs a second call.
  statement {
    sid       = "GrantTheLaneClusterAdminForItsLifetime"
    effect    = "Allow"
    actions   = ["eks:AssociateAccessPolicy"]
    resources = [local.upgrade_access_entry_arn]

    condition {
      test     = "StringEquals"
      variable = "eks:principalArn"
      values   = [aws_iam_role.upgrade_build[0].arn]
    }
  }

  # `startBuild.sync`, exactly as AWS's own template for that pattern specifies: the three
  # CodeBuild actions on the project, and the three EventBridge actions on the ONE managed rule the
  # `.sync` pattern creates. Omitting the EventBridge half does not fail the apply -- it fails
  # every run, at the start, with an error that names EventBridge and not this policy.
  statement {
    sid    = "RunOneLaneBuild"
    effect = "Allow"
    actions = [
      "codebuild:StartBuild",
      "codebuild:StopBuild",
      "codebuild:BatchGetBuilds",
    ]
    resources = [aws_codebuild_project.upgrade[0].arn]
  }

  statement {
    sid    = "WatchTheBuildForCompletion"
    effect = "Allow"
    actions = [
      "events:PutTargets",
      "events:PutRule",
      "events:DescribeRule",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:events:${var.region}:${local.account_id}:rule/StepFunctionsGetEventForCodeBuildStartBuildRule",
    ]
  }

  statement {
    sid       = "InvokeTheReportStage"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.upgrade_report[0].arn, "${aws_lambda_function.upgrade_report[0].arn}:*"]
  }

  # DELIBERATELY ABSENT: `eks:AssociateAccessPolicy` and `eks:UpdateAccessEntry`. See this file's
  #   header -- either one would defeat the `resourceNames` fence the access entry exists to hand
  #   authority to.
  # DELIBERATELY ABSENT: `codebuild:BatchDeleteBuilds`. A lane's build log is the evidence of what
  #   it did; nothing in this pipeline may delete one.
  # DELIBERATELY ABSENT: `iam:PassRole`. A CodeBuild project carries its service role on the
  #   project, not on the start call, so nothing is passed.
}

resource "aws_iam_role_policy" "workflow_upgrade_stage" {
  count  = local.upgrade_built ? 1 : 0
  name   = "upgrade-stage-permissions"
  role   = aws_iam_role.workflow.id
  policy = data.aws_iam_policy_document.workflow_upgrade_stage[0].json
}

# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "upgrade_build" {
  count = local.upgrade_built ? 1 : 0

  name              = "/aws/codebuild/${local.name_prefix}-upgrade"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.log_kms_key_arn
}

# THE MUTATION CODE AS A VERSION-PINNED OBJECT, BUILT BY `scripts/build-packages.sh`.
#
# NOT `archive_file`, AND THE REASON IS A SHARED FILE RATHER THAN A PREFERENCE. An `archive_file`
# data source makes this module depend on `hashicorp/archive`, which would add a provider to
# `infra/terraform/modules/platform/.terraform.lock.hcl` -- a COMMITTED file that another lane is applying from
# right now. So the zip is assembled by the same script that builds every other artifact here,
# with the same deterministic sort-and-`zip -X`, and this resource only uploads it.
#
# The file list is IN that script, enumerated, for the reason its own comment gives: this is the
# artifact that performs every mutation, so what is in it is what can run against a cluster.
resource "aws_s3_object" "upgrade_source" {
  count = local.upgrade_built ? 1 : 0

  bucket = aws_s3_bucket.documents.id
  key    = "upgrade-lane/source.zip"
  source = "${path.module}/../../../../build/upgrade-lane.zip"
  # `etag` IS WHAT MAKES A CODE CHANGE VISIBLE IN THE PLAN. Without it Terraform compares only the
  # key and a changed zip uploads nothing -- the lane would keep running the previous bytes while
  # the diff said the code had moved, which is the worst available failure for the one artifact
  # that changes a cluster.
  etag = filemd5("${path.module}/../../../../build/upgrade-lane.zip")
}

resource "aws_codebuild_project" "upgrade" {
  count = local.upgrade_built ? 1 : 0

  name          = "${local.name_prefix}-upgrade"
  description   = "One component's upgrade: helm upgrade or eks update-addon, validated, with a diagnosis bundle."
  service_role  = aws_iam_role.upgrade_build[0].arn
  build_timeout = var.upgrade_build_timeout_minutes
  # A LANE THAT CANNOT START PROMPTLY IS A LANE THAT SHOULD NOT START. The access entry is created
  # before the build; a build sitting in a queue is an open grant doing nothing.
  queued_timeout = 15

  source {
    # AN S3 SOURCE, NOT `NO_SOURCE` WITH AN INLINE BUILDSPEC. The code that performs the mutation
    # has to be a reviewed artifact rather than a string in a Terraform file, and an S3 source is
    # how CodeBuild pins one: the buildspec, the lane library and the fences arrive together in
    # one object whose version this resource records.
    type     = "S3"
    location = "${aws_s3_bucket.documents.id}/${aws_s3_object.upgrade_source[0].key}"
  }

  source_version = aws_s3_object.upgrade_source[0].version_id

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type         = "LINUX_CONTAINER"
    compute_type = "BUILD_GENERAL1_SMALL"
    image        = var.upgrade_build_image

    # Deployment facts. What THIS RUN is about arrives as per-build overrides from the state
    # machine, so one project serves every component and every run.
    environment_variable {
      name  = "TARGET_REGION"
      value = var.region
    }
    environment_variable {
      name  = "CLUSTER_NAME"
      value = var.upgrade_target_cluster
    }
    environment_variable {
      name  = "EVIDENCE_BUCKET"
      value = aws_s3_bucket.documents.id
    }
    environment_variable {
      name  = "HELM_VERSION"
      value = var.upgrade_helm_version
    }
    environment_variable {
      name  = "HELM_SHA256"
      value = var.upgrade_helm_sha256
    }
    environment_variable {
      name  = "KUBECTL_VERSION"
      value = var.upgrade_kubectl_version
    }
    environment_variable {
      name  = "KUBECTL_SHA256"
      value = var.upgrade_kubectl_sha256
    }
    environment_variable {
      name  = "HISTORY_MAX"
      value = tostring(var.upgrade_history_max)
    }
    environment_variable {
      name  = "HEALTH_DEADLINE_SECONDS"
      value = tostring(var.upgrade_health_deadline_seconds)
    }
    # SPACE-SEPARATED, because the buildspec iterates them with a shell `for`. A JSON list would
    # need a parser in the middle of the one block that must stay readable.
    environment_variable {
      name  = "HEALTH_CHECKPOINTS"
      value = join(" ", var.upgrade_health_checkpoints)
    }
    environment_variable {
      name  = "REVALIDATION_CHECKPOINTS"
      value = join(" ", var.upgrade_revalidation_checkpoints)
    }
    # EMPTY WHEN THE AGENT IS NOT DEPLOYED, and the buildspec reads an empty value as "do not call
    # out". A variable that was absent rather than empty would make the shell's `${VAR}` expansion
    # fail under `set -u`, which would fail the phase that decides whether to roll a component back --
    # the one place a missing configuration must not become a refusal.
    environment_variable {
      name = "TRIAGE_FUNCTION"
      # A CONDITIONAL AND NOT `coalesce`, and the plan is what caught it. Terraform's `coalesce`
      # returns the first non-null AND NON-EMPTY argument, so `coalesce(x, "")` with `x` null has
      # no valid answer and FAILS THE PLAN -- which would have made a deployment without the agent
      # component unappliable, the exact opposite of the degrade-gracefully property this variable
      # exists to provide.
      #
      # EMPTY RATHER THAN ABSENT, still: the buildspec reads an empty value as "do not call out",
      # and a variable that was absent entirely would make `${TRIAGE_FUNCTION}` fail under `set -u`
      # in the phase that decides whether to roll a component back -- the one place a missing
      # configuration must not become a refusal.
      value = var.upgrade_triage_function_arn == null ? "" : var.upgrade_triage_function_arn
    }
  }

  # NO `vpc_config`, AND THE ABSENCE IS VERIFIED RATHER THAN ASSUMED. This cluster's Kubernetes
  # endpoint is public (`0.0.0.0/0`) with private access off, read from the cluster. A VPC
  # configuration would add an ENI, a subnet and a NAT dependency to the one step in this system
  # that must not become more fragile, and would buy nothing.

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.upgrade_build[0].name
    }
  }

  lifecycle {
    # FAIL THE APPLY, NOT THE RUN. Each of these produces a build that starts, reaches the one
    # moment that matters and refuses -- which is the most expensive place to discover a missing
    # value, because a person has already approved and authorised by then.
    precondition {
      condition     = !var.upgrade_stage_enabled || local.upgrade_built
      error_message = "upgrade_stage_enabled is true but upgrade_target_cluster is empty, so the machine would reach lanes that were never built. Name the one cluster this stage may write to."
    }
    precondition {
      condition     = !local.upgrade_built || length(var.upgrade_lane_groups) > 0
      error_message = "upgrade_target_cluster is set but upgrade_lane_groups is empty, so eks:CreateAccessEntry's condition would permit no group and every lane would refuse to open."
    }
    precondition {
      condition     = !local.upgrade_built || (var.upgrade_helm_sha256 != "" && var.upgrade_kubectl_sha256 != "")
      error_message = "upgrade_target_cluster is set but a tool checksum is empty. The buildspec verifies the helm and kubectl downloads before it uses any identity; an unset checksum turns that check into a no-op."
    }
    precondition {
      condition = (
        !local.upgrade_built ||
        length(var.upgrade_health_checkpoints) == 0 ||
        var.upgrade_health_checkpoints[length(var.upgrade_health_checkpoints) - 1] == var.upgrade_health_deadline_seconds
      )
      error_message = "the last health checkpoint must equal upgrade_health_deadline_seconds: that is the call on which SETTLING is withdrawn, so a shorter last checkpoint would leave the window running with nobody asked at the end of it."
    }
    precondition {
      # THE LONGEST OPEN WINDOW IS THE BUILD TIMEOUT, NOT THE DEADLINE, and that is the number to
      # look at. The build has to fit the health window plus the rollback, the re-validation and the
      # install phase; if it does not, CodeBuild kills the build and the deadline is silently
      # truncated rather than enforced -- with two access entries open until `Lane cleanup` runs.
      condition = (
        !local.upgrade_built ||
        var.upgrade_build_timeout_minutes * 60 >= var.upgrade_health_deadline_seconds + 600
      )
      error_message = "upgrade_build_timeout_minutes leaves under ten minutes of margin over upgrade_health_deadline_seconds. The build must also fit the rollback, its re-validation and the install phase, or CodeBuild truncates the health window instead of the deadline enforcing it."
    }
    precondition {
      condition     = var.upgrade_history_max <= 20
      error_message = "upgrade_history_max exceeds the release-secret revisions the committed fences enumerate, so Helm would keep a revision the lane has no permission to prune."
    }
  }
}

# ---------------------------------------------------------------------------
# The two Lambda-served states: plan the lanes, then report on them. ONE FUNCTION, invoked twice
# with a different `mode`, for `upgrade_brief.py`'s reason -- the two halves read and write the
# same documents and splitting them would be two roles with the same grants.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "upgrade_report" {
  count = local.upgrade_built ? 1 : 0

  name              = "/aws/lambda/${local.name_prefix}-upgrade-report"
  retention_in_days = var.log_retention_days
  kms_key_id        = local.log_kms_key_arn
}

data "aws_iam_policy_document" "upgrade_report_assume" {
  count = local.upgrade_built ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "upgrade_report" {
  count = local.upgrade_built ? 1 : 0

  name               = "${local.name_prefix}-upgrade-report"
  description        = "Plans one lane per approved component, then reports what the lanes did. No cluster access."
  assume_role_policy = data.aws_iam_policy_document.upgrade_report_assume[0].json
}

data "aws_iam_policy_document" "upgrade_report" {
  count = local.upgrade_built ? 1 : 0

  statement {
    sid    = "WriteOwnLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.upgrade_report[0].arn}:*"]
  }

  # THE AUTHORISATION AND THE LANE RESULTS. Exactly the two prefixes this stage reads, and the list
  # was WRONG IN BOTH DIRECTIONS until the first end-to-end run exposed it.
  #
  # `releases/*` WAS MISSING, and that is what the run failed on. The planner used to rebuild the
  # release from `upgrade-decisions/*` plus `plans/*`; it now reads the document `Release recorded`
  # wrote, because re-deriving the authorisation here would be a second implementation free to
  # disagree with the recorded one. The read moved and this policy did not, so `_release()` got
  # AccessDenied on an object that exists -- surfaced as "nothing says this run was authorised", which
  # points a reader at the approval gate rather than at an IAM statement.
  #
  # `upgrade-decisions/*` AND `plans/*` ARE GONE, because that same change stopped reading them.
  # Keeping them would leave two standing grants on a run's inputs for a stage that never opens them,
  # and a dead grant is indistinguishable in review from a live one.
  statement {
    sid    = "ReadTheAuthorisationAndTheLaneResults"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.documents.arn}/releases/*",
      "${aws_s3_bucket.documents.arn}/upgrade-lanes/*",
    ]
  }
  # THE EVIDENCE THE REPORT PAGE RENDERS INLINE. Read-only, and every one of these reads is optional
  # inside `stages/upgrade_report_view.py`: a denied or missing object leaves a sentence on the page
  # ("no pod state was captured"), never a failed stage. The report DOCUMENT needs none of them --
  # it is assembled from the two prefixes above and written first -- so a regression here degrades
  # the page and cannot lose the record.
  #
  #   upgrade-diagnosis/*   pods.json, logs/, events.json, helm-history / describe-addon,
  #                         mutation.log -> the Details row under each applied step
  #   upgrade-decisions/*   who approved which step and what was deferred -> Authorization, Deferred
  #   plans/*               the steps the release did NOT include -> Deferred
  #   inventories/*, decisions/*, snapshots/*
  #                         installed versions, verdicts, insights -> Components
  #
  # `upgrade-decisions/*` AND `plans/*` COME BACK FOR A DIFFERENT REASON THAN THEY LEFT. The comment
  # above records their removal when the planner stopped re-deriving the authorisation from them.
  # That reasoning still holds: the release is still the only authorisation this stage acts on. They
  # are read again to SHOW what a person deferred and why, which the release cannot say.
  statement {
    sid    = "ReadTheEvidenceThePageRenders"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions = ["s3:GetObject"]
    resources = [
      "${aws_s3_bucket.documents.arn}/upgrade-diagnosis/*",
      "${aws_s3_bucket.documents.arn}/upgrade-decisions/*",
      "${aws_s3_bucket.documents.arn}/plans/*",
      "${aws_s3_bucket.documents.arn}/inventories/*",
      "${aws_s3_bucket.documents.arn}/decisions/*",
      "${aws_s3_bucket.documents.arn}/snapshots/*",
    ]
  }
  # LISTING, FOR THE ONE PREFIX WHOSE OBJECT NAMES ARE NOT KNOWABLE IN ADVANCE. A diagnosis bundle
  # holds one log file per pod per container per stream, named after pods that did not exist when
  # the lane was planned. `s3:ListBucket` is a bucket-level action, so the prefix is bound with a
  # condition rather than a resource path.
  statement {
    sid       = "ListTheDiagnosisBundles"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["upgrade-diagnosis/*", "upgrade-decisions/*"]
    }
  }

  # WRITE THE REPORT, AND READ IT BACK. `GetObject` is here for the MAIL LINK, not for the stage:
  # the page URL in the report mail is an S3 presigned GET signed with THIS role's credentials,
  # and a presigned URL grants exactly what its signer could do. With `PutObject` alone the mail
  # carried a link that opened to `AccessDenied ... not authorized to perform: s3:GetObject` --
  # which is what the recipient would see. The approval stage's role has the same pair for
  # the same reason (`PresignTheEvidencePage`).
  statement {
    sid    = "WriteTheReport"
    effect = "Allow"
    # Scoped to named prefixes in this sample's own bucket; kics ba2ed23b cannot resolve the ARN reference.
    # kics-scan ignore-line
    actions   = ["s3:PutObject", "s3:GetObject"]
    resources = ["${aws_s3_bucket.documents.arn}/upgrade-reports/*"]
  }

  # MAIL THE REPORT. Mirrors `MailTheApprovalLink` in upgrade_approval.tf and
  # `MailTheReleaseRequest` in release_gate.tf: scoped to this deployment's SES sender identity
  # ARN, with the `ses:FromAddress` condition kept as defence in depth.
  #
  # THE SAME ADDRESS THE APPROVALS USE, and the stage reads the same two environment variables --
  # one deployment, one place a person's address is configured. Two would mean a run whose approvals
  # arrive and whose report does not, with nothing saying why.
  #
  # `dynamic` SO AN UNCONFIGURED DEPLOYMENT IS STILL VALID. An empty `approval_email_from` yields no
  # statement, `send_report_email` returns "" and the stage records that mail is not configured --
  # which is a supported deployment, not a failure, exactly as it is for the two approvals.
  dynamic "statement" {
    for_each = var.approval_email_from == "" ? [] : [var.approval_email_from]

    content {
      sid       = "MailTheUpgradeReport"
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

  # DELIBERATELY ABSENT: every `eks:` action. This function reports on cluster changes and cannot
  #   make one, cannot read a cluster, and cannot open a lane.
  # DELIBERATELY ABSENT: every `codebuild:` action. It reads what the lanes WROTE, not what the
  #   builds did -- a report assembled from build metadata would say a build succeeded where the
  #   lane's own account says it rolled a component back.
  # DELIBERATELY ABSENT: `s3:PutObject` on any prefix but `upgrade-reports/`. It cannot rewrite a
  #   lane result, which is the one thing that would let a report disagree with the evidence.
  # DELIBERATELY ABSENT: every `dynamodb:` and `bedrock:` action.
}

resource "aws_iam_role_policy" "upgrade_report" {
  count = local.upgrade_built ? 1 : 0

  name   = "upgrade-report-permissions"
  role   = aws_iam_role.upgrade_report[0].id
  policy = data.aws_iam_policy_document.upgrade_report[0].json
}

resource "aws_lambda_function" "upgrade_report" {
  count = local.upgrade_built ? 1 : 0

  function_name = "${local.name_prefix}-upgrade-report"
  description   = "Plan one lane per approved component; report what the lanes did."
  role          = aws_iam_role.upgrade_report[0].arn
  handler       = "entrypoints.upgrade_harness.stage_upgrade_report"
  runtime       = "python3.12"

  # arm64, AND ITS ABSENCE HERE BROKE THE FIRST REAL RUN. `build-packages.sh` fetches wheels with
  # `--python-platform aarch64-manylinux2014`, so `pydantic_core` in every zip is an aarch64 ELF
  # object. This function omitted `architectures`, which defaults to x86_64, and the mismatch is not
  # a load-time warning -- it is:
  #
  #     Runtime.ImportModuleError: Unable to import module 'entrypoints.upgrade_harness':
  #     No module named 'pydantic_core._pydantic_core'
  #
  # Measured: it failed twice in one execution, because this one function serves BOTH `Upgrade lanes`
  # (plan mode) and `Upgrade report` (report mode). The planner failing meant no lane opened at all,
  # so the run reached `Anything referred?` with no `$.upgrade.referred_count` and died on an invalid
  # path -- three failures whose single cause was this missing line.
  #
  # Every other function in this stage sets it, including `upgrade_triage`, which shares this exact
  # zip; that is what made the omission hard to see in review -- the zip is correct, and only one of
  # its consumers disagreed with it.
  #
  # THROUGH THE VARIABLE RATHER THAN A LITERAL, which is what the rest of this deployment does and
  # what makes the build script and the functions move together: `var.lambda_architecture` defaults
  # to arm64 and is the single place that has to agree with `--python-platform`.
  architectures = [var.lambda_architecture]

  timeout     = 120
  memory_size = 512

  # THIS STAGE'S OWN ZIP, NOT `stages.zip`, and the reason is what a plan showed. With the handler
  # in `entrypoints/stages.py` every existing stage Lambda's `source_code_hash` moved the moment this branch
  # existed -- nine unrelated functions redeployed by a stage that was switched OFF. See
  # `entrypoints.upgrade_harness.py`'s header. It also carries a boto3 new enough for `InvokeHarness`,
  # which the triage proxy in the same zip needs.
  filename         = "${path.module}/../../../../build/upgrade_harness.zip"
  source_code_hash = filebase64sha256("${path.module}/../../../../build/upgrade_harness.zip")

  environment {
    variables = {
      DOCUMENT_BUCKET = aws_s3_bucket.documents.id
      CLUSTER_NAME    = var.upgrade_target_cluster
      # The same pair the approvals use, so one address configures the whole deployment.
      APPROVAL_EMAIL_FROM = var.approval_email_from
      APPROVAL_EMAIL_TO   = join(",", var.approval_email_to)
    }
  }

  depends_on = [aws_cloudwatch_log_group.upgrade_report]
}

# ---------------------------------------------------------------------------
# The states.
#
# FOUR AT THE TOP LEVEL AND FOUR INSIDE THE MAP, and the shape of the inner four is the whole
# safety argument of this stage:
#
#   Lane open     -> Lane upgrade    createAccessEntry
#                 -> Lane cleanup    on any error
#   Lane upgrade  -> Lane cleanup    codebuild:startBuild.sync
#                 -> Lane cleanup    on any error
#   Lane cleanup  -> Lane outcome    deleteAccessEntry
#                 -> Lane outcome    on any error
#   Lane outcome  -> End
#
# `Lane cleanup` CANNOT BE SKIPPED, AND THAT IS A PROPERTY OF THE GRAPH RATHER THAN OF THE ORDER.
# Both of `Lane open`'s edges and both of `Lane upgrade`'s edges name it, and it is the only state
# with an edge to `Lane outcome` -- so no path from `Lane open` to an end exists that does not
# traverse it. There is NO `Fail` state inside this Iterator at all, which is stronger than putting
# the cleanup in front of one: a Fail is a terminal somebody could later reach directly.
#
# `Lane open`'s FAILURE GOES TO THE CLEANUP TOO, which looks redundant and is not. A
# `States.Timeout` on `createAccessEntry` can leave the entry created and the state failed, and an
# entry created by a call the state machine believes failed is exactly the leak this design exists
# to prevent. Deleting an entry that was never created fails with a not-found, which the cleanup's
# own Catch carries onward.
#
# A LANE THAT FAILED IS A ROW IN THE REPORT, NOT A FAILED EXECUTION. A Fail inside the Map would
# abandon the components after it and skip the report -- and the report is the deliverable. The
# execution still ends red when anything was referred, at `Anything referred?`, AFTER the report is
# durable in S3. Same reasoning `Release rejected` gives: failing loses the run and never the
# answers.
# ---------------------------------------------------------------------------

locals {
  # `one(...)` RATHER THAN `[0]` THROUGHOUT THE STATE LOCALS BELOW, and the plan is what taught it.
  # A conditional expression makes Terraform evaluate BOTH branches for type unification, so a
  # `local.upgrade_enabled ? { ... } : {}` states map still resolves every reference inside the true
  # branch even when the stage is OFF -- and `[0]` against a zero-count resource is an error rather
  # than a null. `one()` yields null instead, which nothing reads because the map is `{}` anyway.
  upgrade_lane_iterator = {
    StartAt = "Lane open"
    States = {
      "Lane open" = {
        Type     = "Task"
        Comment  = "Create the access entry. This is the entire just-in-time grant."
        Resource = "arn:${data.aws_partition.current.partition}:states:::aws-sdk:eks:createAccessEntry"
        Parameters = {
          "ClusterName.$" = "$.cluster"
          PrincipalArn    = one(aws_iam_role.upgrade_build[*].arn)
          # ONE GROUP, THE LANE'S OWN. `States.Array` because the API takes a list and a bare
          # string is refused by the SDK integration -- the same shape `Release gate` documents
          # for SSM's parameter map.
          "KubernetesGroups.$" = "States.Array($.lane.group)"
          # STANDARD, named rather than defaulted, because the IAM condition requires it and a
          # request that omitted it would be denied with an error about a condition rather than
          # about a type.
          Type = "STANDARD"
        }
        # The response carries the entry's ARN and creation time. Kept: it is the handle a human
        # uses to check the entry really went away.
        ResultSelector = {
          "principal_arn.$" = "$.AccessEntry.PrincipalArn"
          "created_at.$"    = "$.AccessEntry.CreatedAt"
        }
        ResultPath = "$.access_entry"
        Next       = "Lane grant"
        # RETRIED, unlike the build below. This call has not changed the cluster, so a transport
        # failure can be retried without the risk that makes `Lane upgrade` unretryable.
        Retry = local.transient_retry
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.lane_error"
          Next        = "Lane cleanup"
        }]
      }

      # THE GRANT ITSELF, AND IT IS CLUSTER-ADMIN FOR THE LIFE OF ONE LANE. Said plainly because it
      # is the widest thing this system does.
      #
      # WHY, AFTER THREE ATTEMPTS AT ENUMERATING IT. Helm's permission needs are not knowable ahead
      # of time for an arbitrary chart, and every gap costs a human approval and a pipeline run to
      # find. Measured on this cluster, in order: a `create` on secrets that RBAC could not match
      # because it carried `resourceNames`; then `replicasets` list, which `helm rollback --wait`
      # needs; with `pods` and `events` behind it. Each was one line, and each surfaced only by
      # running it against a real chart.
      #
      # AND NO PREDEFINED POLICY SHORT OF THIS ONE CAN DO IT. `AmazonEKSAdminPolicy` carries no
      # `apiregistration.k8s.io`, no `clusterroles`/`clusterrolebindings` and no
      # `apiextensions.k8s.io` -- so it cannot upgrade metrics-server, which owns an APIService, two
      # ClusterRoles and two ClusterRoleBindings, at ANY scope. Access scope widens namespaces; it
      # never adds API groups. `AmazonEKSClusterAdminPolicy` is `*`/`*`/`*`.
      #
      # WHAT BOUNDS IT, SINCE RBAC NO LONGER DOES:
      #   * TIME. The association lives ON the access entry -- it is keyed by (cluster, principalArn),
      #     which is that entry's identity -- and `Lane cleanup` deletes the entry on every exit path
      #     including failure and timeout. No entry, no subject, no grant.
      #   * WHO. Only the CodeBuild role. The agent holds `upgrade-reader`: read-only RBAC, no
      #     secrets, no exec, and never this. The split is deliberate -- the identity whose needs
      #     cannot be enumerated gets the wide grant, the identity we distrust keeps the narrow one.
      #   * WHAT IT RUNS. A buildspec committed to this repo and pinned by S3 object version, with no
      #     model-decided input anywhere on the write path.
      #   * WHICH OBJECTS. `helm template` of the approved chart with this release's values is what
      #     Helm applies, and Helm refuses to adopt an object another release holds. `fence-check`
      #     refuses the whole run, before the mutation, if that render does not ship the approved
      #     version -- which is why the buildspec proves it against a render that must be refused
      #     instead of trusting it.
      "Lane grant" = {
        Type     = "Task"
        Comment  = "Associate cluster-admin with the entry, for the life of this lane only."
        Resource = "arn:${data.aws_partition.current.partition}:states:::aws-sdk:eks:associateAccessPolicy"
        Parameters = {
          "ClusterName.$" = "$.cluster"
          PrincipalArn    = one(aws_iam_role.upgrade_build[*].arn)
          # PINNED IN THE COMMITTED DEFINITION rather than taken from the payload. The IAM statement
          # conditions on the principal but NOT on the policy: the condition key for a policy ARN is
          # not one this code could verify, and an unverified condition key does not fail loudly --
          # it matches nothing and denies. So the narrowing that is actually reliable here is that
          # this string lives in git and the state machine is the only caller.
          PolicyArn = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
          AccessScope = {
            Type = "cluster"
          }
        }
        ResultPath = "$.access_policy"
        Next       = "Lane upgrade"
        Retry      = local.transient_retry
        # A FAILURE HERE STILL CLEANS UP. The entry already exists by this point, so skipping the
        # cleanup would leave a principal mapped into the cluster with no run to justify it.
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.lane_error"
          Next        = "Lane cleanup"
        }]
      }

      "Lane upgrade" = {
        Type    = "Task"
        Comment = "The one mutation. Blocks until the build finishes; no polling loop, no task token."
        # `.sync` -- AWS's table lists StartBuild as supporting Run a Job. So this state returns
        # when the build completes, and the IAM it needs is the three CodeBuild actions plus the
        # three EventBridge actions for the managed rule the pattern creates.
        Resource = "arn:${data.aws_partition.current.partition}:states:::codebuild:startBuild.sync"
        Parameters = {
          ProjectName = one(aws_codebuild_project.upgrade[*].name)
          # EVERY VALUE IS A STRING, including the step order, for the type-unification reason this
          # file's neighbour documents at length: one non-string in a list of objects makes
          # Terraform unify the element types and the definition is refused whole.
          EnvironmentVariablesOverride = [
            { Name = "RUN_ID", Type = "PLAINTEXT", "Value.$" = "$.run_id" },
            { Name = "STEP_ORDER", Type = "PLAINTEXT", "Value.$" = "$.lane.step_order" },
            { Name = "COMPONENT", Type = "PLAINTEXT", "Value.$" = "$.lane.component" },
            { Name = "RELEASE", Type = "PLAINTEXT", "Value.$" = "$.lane.release" },
            { Name = "RELEASE_NAMESPACE", Type = "PLAINTEXT", "Value.$" = "$.lane.namespace" },
            { Name = "MANAGED_ADDON", Type = "PLAINTEXT", "Value.$" = "$.lane.managed_addon" },
            { Name = "CHART_REPO_NAME", Type = "PLAINTEXT", "Value.$" = "$.lane.chart_repo_name" },
            { Name = "CHART_REPO_URL", Type = "PLAINTEXT", "Value.$" = "$.lane.chart_repo_url" },
            { Name = "CHART_REF", Type = "PLAINTEXT", "Value.$" = "$.lane.chart_ref" },
            { Name = "TARGET_ADDON_VERSION", Type = "PLAINTEXT", "Value.$" = "$.lane.target_addon_version" },
            # The approved application version for either kind. The Helm path resolves the chart
            # version that ships it from the repository before it renders anything, as the add-on
            # path resolves the `-eksbuild.<n>` build from EKS.
            { Name = "TARGET_VERSION", Type = "PLAINTEXT", "Value.$" = "$.lane.target_version" },
            { Name = "INSTALLED_VERSION", Type = "PLAINTEXT", "Value.$" = "$.lane.installed" },
            { Name = "ADDON_POD_SELECTOR", Type = "PLAINTEXT", "Value.$" = "$.lane.addon_pod_selector" },
            # THE KEYS, COMPUTED BY THE PLAN STAGE rather than assembled here. `upgrade_lane/keys.py`
            # holds the shapes with doctests, and the report stage lists them back -- two spellings
            # of one key would be a report that cannot find the results it summarises.
            { Name = "DIAGNOSIS_PREFIX", Type = "PLAINTEXT", "Value.$" = "$.lane.diagnosis_prefix" },
            { Name = "LANE_RESULT_KEY", Type = "PLAINTEXT", "Value.$" = "$.lane.lane_result_key" },
          ]
        }
        # THE BUILD OBJECT IS ENORMOUS AND THE MAP CARRIES ONE PER ITERATION, against a 256 KiB
        # payload ceiling. Two fields are enough: the report reads the lane's own result object from
        # S3, not the build's metadata.
        ResultSelector = {
          "status.$" = "$.Build.BuildStatus"
          "id.$"     = "$.Build.Id"
        }
        ResultPath = "$.build"
        # NO `Retry`, AND THE ABSENCE IS DELIBERATE. Every other Task in this machine carries
        # `local.transient_retry`. Retrying a build that may already have changed the cluster turns
        # one approved upgrade into two applies and two rollbacks, with the first one's diagnosis
        # overwritten by the second. A transport failure here is a refusal.
        Next = "Lane cleanup"
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.lane_error"
          Next        = "Lane cleanup"
        }]
      }

      # THE ONE CLEANUP, AND IT USED TO BE TWO. There was a `Lane cleanup` that deleted the AGENT's
      # read-only entry and then handed to this one, so that a failed reader delete could not strand
      # the write grant. The agent has no entry of its own now -- it reads through a Gateway whose
      # `AmazonEKSViewPolicy` entry is standing and Terraform-managed -- so the lane mints one
      # principal and deletes one, and this state is the only route to the iteration's end.
      "Lane cleanup" = {
        Type     = "Task"
        Comment  = "Delete the lane's write access entry. The only route to this iteration's end."
        Resource = "arn:${data.aws_partition.current.partition}:states:::aws-sdk:eks:deleteAccessEntry"
        Parameters = {
          "ClusterName.$" = "$.cluster"
          PrincipalArn    = one(aws_iam_role.upgrade_build[*].arn)
        }
        ResultPath = "$.cleanup"
        # RETRIED HARD, because the cost of giving up here is a standing cluster-admin grant -- this
        # is the entry the access policy hangs off. But retried on NAMED errors, for the reason the
        # reader cleanup above records at length: a service exception is a task failure, so
        # `States.ALL` and `States.TaskFailed` both retry a not-found that can never succeed.
        #
        # AND A NOT-FOUND HERE IS SUCCESS, NOT FAILURE. It means the entry is gone, which is the only
        # thing this state is trying to achieve. Catching it separately keeps it out of
        # `cleanup_error`, so the report does not raise a finding about a grant that does not exist.
        Retry = [{
          ErrorEquals = [
            "Eks.ServerException",
            "Eks.ThrottlingException",
            "States.Timeout",
          ]
          IntervalSeconds = 2
          MaxAttempts     = 6
          BackoffRate     = 2
        }]
        Next = "Lane outcome"
        Catch = [{
          ErrorEquals = ["Eks.ResourceNotFoundException"]
          ResultPath  = "$.cleanup_absent"
          Next        = "Lane outcome"
          },
          {
            ErrorEquals = ["States.ALL"]
            # THE ONE ERROR THAT REACHES THE REPORT AS A FINDING RATHER THAN A ROW. A lane whose
            # entry did not come off has left a grant nobody asked for, and `UpgradeRunReport.referred`
            # counts any outcome carrying this -- so the execution ends red and a person is told which
            # principal to check.
            ResultPath = "$.cleanup_error"
            Next       = "Lane outcome"
        }]
      }

      "Lane outcome" = {
        Type    = "Pass"
        Comment = "The lane's whole payload, unchanged. The only End in this Iterator."
        # NO `Parameters`, AND THAT IS LOAD-BEARING rather than lazy. A `"...$"` selector naming an
        # absent path FAILS the state, and on the error paths `$.build` does not exist -- the exact
        # trap `Upgrade approval` documents for `brief_uri`. Passing the payload through means the
        # Map's result carries `lane_error` or `cleanup_error` when they happened and nothing when
        # they did not, which is precisely what the report stage reads.
        End = true
      }
    }
  }

  # FIVE SEPARATE LOCALS AND A `merge`, NOT ONE CONDITIONAL MAP, and this is not a style choice.
  # `local.upgrade_enabled ? { five states } : {}` is REFUSED by Terraform: the two result
  # expressions must have consistent types, and a Task, a Map, a Choice and a Fail have four
  # different shapes, so there is no element type an empty object could unify with. The error names
  # the first attribute it cannot match and says nothing about why.
  #
  # `workflow.tf` is already arranged this way -- seven separate locals for the release gate's seven
  # states -- for this reason and for the related one its `States` comment gives at length: mixing
  # shapes in one comprehension is how `End = true` became the string `"true"` and the definition
  # was refused whole. One state per local keeps every value's type its own.
  upgrade_lanes_state = local.upgrade_enabled ? {
    "Upgrade lanes" = {
      Type     = "Task"
      Comment  = "One work item per approved component. No cluster access; reads the decisions."
      Resource = "arn:${data.aws_partition.current.partition}:states:::lambda:invoke"
      Parameters = {
        FunctionName = aws_lambda_function.upgrade_report[0].arn
        Payload = {
          mode    = "plan"
          "run.$" = "$"
        }
      }
      ResultSelector = {
        "items.$"   = "$.Payload.items"
        "skipped.$" = "$.Payload.skipped"
      }
      ResultPath = "$.upgrade_lanes"
      Next       = "Upgrade lane"
      Retry      = local.transient_retry
      # A PLANNING FAILURE STILL REPORTS. There is nothing to clean up -- no lane has opened -- and
      # the report stage reads the release's approved steps, so it can say "these components were
      # authorised and no lane ran" rather than the execution vanishing into a Fail.
      Catch = [{
        ErrorEquals = ["States.ALL"]
        ResultPath  = "$.upgrade_lanes_error"
        Next        = "Upgrade report"
      }]
    }

  } : {}

  upgrade_lane_map_state = local.upgrade_enabled ? {
    "Upgrade lane" = {
      Type    = "Map"
      Comment = "One component at a time. The concurrency limit is part of the fence."
      # ONE AT A TIME, AND IT IS NOT A THROTTLE. Sequencing makes the lane an EXECUTION boundary as
      # well as an RBAC one: one access entry exists at a time, one component is ever in flight, and
      # two upgrades cannot interleave their diagnosis. It does NOT replace the `resourceNames`
      # fence -- sequencing bounds WHEN a write happens and the fence bounds WHAT it may reach, and
      # every managed add-on on this cluster shares `kube-system` with every other.
      MaxConcurrency = 1
      ItemsPath      = "$.upgrade_lanes.items"
      Parameters = {
        "lane.$"    = "$$.Map.Item.Value"
        "run_id.$"  = "$.release.run_id"
        "cluster.$" = "$.release.cluster"
      }
      Iterator   = local.upgrade_lane_iterator
      ResultPath = "$.lanes"
      Next       = "Upgrade report"
      # THE MAP'S CATCH LEADS ONWARD, and unlike the brief's catches that is not because this stage
      # is advisory. It is because each iteration has already closed its own access entry and
      # written its own result object, so the report can still be assembled -- and a Map failure
      # that skipped the report would destroy the only account of a cluster that was just changed.
      Catch = [{
        ErrorEquals = ["States.ALL"]
        ResultPath  = "$.lanes_error"
        Next        = "Upgrade report"
      }]
    }

  } : {}

  upgrade_report_state = local.upgrade_enabled ? {
    "Upgrade report" = {
      Type     = "Task"
      Comment  = "Read every lane's own account of itself and write the run's one report."
      Resource = "arn:${data.aws_partition.current.partition}:states:::lambda:invoke"
      Parameters = {
        FunctionName = aws_lambda_function.upgrade_report[0].arn
        Payload = {
          mode    = "report"
          "run.$" = "$"
        }
      }
      ResultSelector = {
        "report_uri.$"     = "$.Payload.upgrade_report_uri"
        "summary.$"        = "$.Payload.upgrade_summary"
        "referred_count.$" = "$.Payload.upgrade_referred_count"
        "referred.$"       = "$.Payload.upgrade_referred"
      }
      ResultPath = "$.upgrade"
      Next       = "Anything referred?"
      Retry      = local.transient_retry
      # A REPORT THAT COULD NOT BE WRITTEN IS ITSELF A REFERRAL. Routed to the Choice rather than to
      # the Fail directly, so there is one place that decides how a run ends.
      Catch = [{
        ErrorEquals = ["States.ALL"]
        ResultPath  = "$.upgrade_error"
        Next        = "Anything referred?"
      }]
    }

  } : {}

  anything_referred_state = local.upgrade_enabled ? {
    "Anything referred?" = {
      Type    = "Choice"
      Comment = "Did anything need a person. An unreadable answer counts as yes."
      # A POSITIVE TEST FOR ZERO, NOT `IsPresent` ON A LIST. `IsPresent = false` on
      # `referred[0]` would also match a payload with no `$.upgrade` at all -- the unreadable case --
      # and would route it to `Done`, which is the one reading that must not win here. A numeric
      # equality matches only when the report actually said nobody, so every other state of the
      # world falls through to the Default.
      Choices = [{
        Variable      = "$.upgrade.referred_count"
        NumericEquals = 0
        Next          = "Done"
      }]
      # DEFAULT IS THE FAIL, which is the opposite of `Anything approved?` two states earlier and
      # for the mirror-image reason. That Choice defaults to `Done` because `Done` authorises
      # nothing; this one defaults to the Fail because by the time it runs A CLUSTER HAS ALREADY
      # BEEN CHANGED, and a run that quietly went green while a component needed a person would be
      # indistinguishable from one that worked. The report is already durable in S3, so failing
      # loses the green tick and never the answers.
      Default = "Upgrade referred"
    }

  } : {}

  upgrade_referred_state = local.upgrade_enabled ? {
    "Upgrade referred" = {
      Type    = "Fail"
      Comment = "A component needs a person. The report says which, and what was tried and seen."
      Error   = "UpgradeReferred"
      Cause   = "One or more approved components could not be completed by this stage, or an access entry could not be deleted. The run's report is written under upgrade-reports/ and names, per component, what was attempted and what was observed. Every diagnosis bundle is under upgrade-diagnosis/ for the same run id. Nothing about this failure loses a decision, an authorisation or a diagnosis."
    }
  } : {}

  # The one name `workflow.tf` merges. `merge` does NOT unify its arguments' element types, which
  # is exactly why the five above can be five maps of differently-shaped states and this can be one
  # value.
  upgrade_stage_states = merge(
    local.upgrade_lanes_state,
    local.upgrade_lane_map_state,
    local.upgrade_report_state,
    local.anything_referred_state,
    local.upgrade_referred_state,
  )
}


# THE BUILD ROLE'S ARN, for `infra/terraform/modules/upgrade`'s resource policy. An output rather than
# a value an operator transcribes, for `release_gate.tf`'s reason: Terraform cannot let a reference
# go stale and can very easily let a string go stale. The deploy root wires it straight across.
output "upgrade_build_role_arn" {
  description = "ARN of the upgrade lane's CodeBuild service role, or null when the stage is off."
  value       = one(aws_iam_role.upgrade_build[*].arn)
}
