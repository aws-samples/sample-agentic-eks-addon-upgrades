"""The two identities in this stage, and the actions whose absence is the design.

WHAT THIS CATCHES. `eks:CreateAccessEntry` on a cluster is, unconditioned, a PRIVILEGE ESCALATION:
the holder can map any principal into any Kubernetes group, `system:masters` included. The whole
just-in-time design rests on three IAM condition keys narrowing it -- `eks:principalArn`,
`eks:kubernetesGroups` and `eks:accessEntryType` -- and deleting any one of them leaves a policy
that
still works, still passes every run, and quietly holds cluster-admin-minting power. Nothing about
the pipeline's behaviour would change.

`eks:AssociateAccessPolicy` WAS FORBIDDEN HERE AND IS NOW EXPECTED, AND THIS TEST IS WHY THAT
HAPPENED DELIBERATELY. It used to be in `FORBIDDEN_ANYWHERE` on the reasoning that an association
grants Kubernetes permissions through an AWS-managed ClusterRole, so one call would hand the lane
`AmazonEKSEditPolicy` and the `resourceNames` fence would become decorative. That was correct, and
the fact it depended on is gone: there is no `resourceNames` fence any more. Two attempts at
enumerating what an arbitrary Helm chart needs failed in three separate places, each found only by
running it, so the lane now holds `AmazonEKSClusterAdminPolicy` for the life of one access entry.

WHICH MAKES THE ASSERTION BELOW MORE IMPORTANT, NOT LESS. The grant is wide, so the things that
bound it have to be exact: it is conditioned on ONE principal, the entry it hangs off is deleted on
every exit path, and `eks:DisassociateAccessPolicy` and `eks:UpdateAccessEntry` stay forbidden. This
test moved a line from one list to the other; it did not stop watching.

So this file asserts the narrowings positively and the absences negatively, for both roles.

Deliberately no AWS call: the Terraform is the source, so this runs with no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

TERRAFORM = (
    Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/upgrade_stage.tf"
)

# What the state machine gains for this stage, and nothing else. Read as a set so a NEW action is a
# failure even if nobody thought to add its prefix to the denylist below.
EXPECTED_WORKFLOW_ACTIONS = {
    "eks:CreateAccessEntry",
    "eks:DeleteAccessEntry",
    # CLUSTER-ADMIN FOR THE LIFE OF ONE LANE. Conditioned on `eks:principalArn` = the build role. Not
    # conditioned on the policy ARN: that condition key could not be verified, and an unverified
    # condition key denies silently instead of failing loudly. See `Lane grant` in the Terraform.
    "eks:AssociateAccessPolicy",
    "codebuild:StartBuild",
    "codebuild:StopBuild",
    "codebuild:BatchGetBuilds",
    "events:PutTargets",
    "events:PutRule",
    "events:DescribeRule",
    "lambda:InvokeFunction",
}

# What the build gains. Note what is NOT here: no access-entry action of any kind, so the build
# cannot open its own lane, widen it, or keep it open past the run.
EXPECTED_BUILD_ACTIONS = {
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
    "s3:GetObject",
    "s3:GetObjectVersion",
    "s3:PutObject",
    "s3:AbortMultipartUpload",
    "eks:DescribeCluster",
    "eks:DescribeAddon",
    "eks:UpdateAddon",
    "eks:DescribeAddonVersions",
    # ASK THE UPGRADE AGENT. One function ARN, and the statement exists only when
    # `upgrade_triage_function_arn` is set -- so a deployment without the agent grants nothing here
    # and the lane rolls back deterministically. Listed as expected because the build is the only
    # thing that knows WHEN to ask: after validation refuses the component and after the `after`
    # bundle is sealed, and before the rollback.
    "lambda:InvokeFunction",
}

# Actions that must appear NOWHERE in this file, whichever role is being granted.
FORBIDDEN_ANYWHERE = (
    # `eks:AssociateAccessPolicy` WAS HERE. It moved to EXPECTED_WORKFLOW_ACTIONS when the lane took
    # cluster-admin; the docstring records why and what replaced the bound it used to provide.
    #
    # Its REVERSE stays forbidden, and that is not symmetry for its own sake. An association is keyed
    # by (cluster, principalArn) -- the access entry's identity -- so deleting the entry revokes it.
    # Granting a disassociate as well would add a second call that must succeed on the way out, and
    # the one property this stage cannot afford to lose is that revocation is a single action.
    "eks:DisassociateAccessPolicy",
    # An update can add groups to an existing entry: the create fence with an extra step.
    "eks:UpdateAccessEntry",
    # The just-in-time part is the ACCESS ENTRY, not the role. Nothing here makes an identity.
    "iam:CreateRole",
    "iam:PutRolePolicy",
    "iam:AttachRolePolicy",
    "iam:PassRole",
    # A lane's build log is the evidence of what it did.
    "codebuild:BatchDeleteBuilds",
    # The build has no power over the execution that started it: `.sync` means Step Functions
    # watches the build, not the other way round.
    "states:SendTaskSuccess",
    "states:SendTaskFailure",
    # No fact store, no token store, no model, and no cluster-wide EKS mutation.
    "dynamodb:PutItem",
    "bedrock:InvokeModel",
    "eks:UpdateClusterVersion",
    "eks:UpdateClusterConfig",
    "eks:CreateAddon",
    "eks:DeleteAddon",
)


def _without_comments(text: str) -> str:
    """`text` with every `#` comment line removed.

    LOAD-BEARING, NOT TIDINESS, exactly as in
    `test_the_release_gate_grants_nothing_it_does_not_need`:
    this file's Terraform ends its policies with blocks of `DELIBERATELY ABSENT:` notes that name
    every forbidden action in prose. Scanning the raw text would fail on a policy that is exactly
    right -- a red test proving nothing, which is worse here than a green one.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def _block(kind: str, type_name: str, name: str) -> str:
    """The body of one top-level `<kind> "<type>" "<name>" { ... }` block, comments stripped."""
    text = TERRAFORM.read_text()
    match = re.search(
        rf'{re.escape(kind)} "{re.escape(type_name)}" "{re.escape(name)}" \{{(.*?)\n\}}',
        text,
        re.S,
    )
    assert match, (
        f'could not find {kind} "{type_name}" "{name}" in {TERRAFORM.name}. If it was renamed, fix '
        "this test rather than deleting it -- the invariant it guards is still real."
    )
    return _without_comments(match.group(1))


def _actions(body: str) -> set[str]:
    found: set[str] = set()
    for block in re.findall(r"actions\s*=\s*\[(.*?)\]", body, re.S):
        found |= set(re.findall(r'"([a-z0-9-]+:[A-Za-z0-9*]+)"', block))
    return found


def test_the_state_machine_gains_exactly_these_actions_and_no_others() -> None:
    body = _block("data", "aws_iam_policy_document", "workflow_upgrade_stage")
    actions = _actions(body)
    assert actions, (
        "found no actions in the upgrade stage's workflow policy. The `actions = [...]` shape this "
        "test matches has probably changed -- fix the pattern rather than deleting the test."
    )
    assert actions == EXPECTED_WORKFLOW_ACTIONS, (
        f"the state machine's upgrade-stage grant is {sorted(actions)}, not "
        f"{sorted(EXPECTED_WORKFLOW_ACTIONS)}.\n"
        f"added: {sorted(actions - EXPECTED_WORKFLOW_ACTIONS)}\n"
        f"removed: {sorted(EXPECTED_WORKFLOW_ACTIONS - actions)}"
    )


def test_the_build_gains_exactly_these_actions_and_no_others() -> None:
    body = _block("data", "aws_iam_policy_document", "upgrade_build")
    actions = _actions(body)
    assert actions == EXPECTED_BUILD_ACTIONS, (
        f"the build role holds {sorted(actions)}, not {sorted(EXPECTED_BUILD_ACTIONS)}.\n"
        f"added: {sorted(actions - EXPECTED_BUILD_ACTIONS)}\n"
        f"removed: {sorted(EXPECTED_BUILD_ACTIONS - actions)}"
    )


def test_the_build_holds_no_access_entry_action() -> None:
    """The build must not be able to open its own lane, extend it, or keep it open.

    Called out separately from the exact-set check so a regression names the actual problem: a build
    that could create its own access entry would make the state machine's conditions irrelevant,
    because the narrowing lives entirely on the caller that opens the lane.
    """
    body = _block("data", "aws_iam_policy_document", "upgrade_build")
    offenders = sorted(
        action for action in _actions(body) if "AccessEntry" in action or "AccessPolicy" in action
    )
    assert not offenders, (
        f"the build role holds {offenders}. The just-in-time grant is created and deleted by the "
        "state machine, between the two states that bracket this build; a build that could mint one"
        "could keep one."
    )


def test_no_forbidden_action_appears_anywhere_in_the_stage() -> None:
    text = _without_comments(TERRAFORM.read_text())
    offenders = sorted(action for action in FORBIDDEN_ANYWHERE if f'"{action}"' in text)
    assert not offenders, (
        f"the upgrade stage grants {offenders}. Each of these is absent for a stated reason in "
        f"{TERRAFORM.name}'s header; if one is genuinely needed, delete its line here in the same "
        "change and say why in the commit."
    )


def test_the_create_access_entry_grant_carries_all_three_narrowings() -> None:
    """Without these, this one statement is the power to mint cluster-admin.

    Each is asserted by name because each fails silently: the policy still applies, every run still
    works, and the escalation is invisible until somebody audits the policy rather than the
    behaviour.
    """
    body = _block("data", "aws_iam_policy_document", "workflow_upgrade_stage")
    statement = body[body.index("eks:CreateAccessEntry") :]
    # The statement runs to the next `sid`, which is the delete grant.
    statement = statement[: statement.index("sid")] if "sid" in statement else statement

    for variable, why in (
        (
            "eks:principalArn",
            "without it this role can create an access entry for ANY principal, including one a "
            "person controls",
        ),
        (
            "eks:kubernetesGroups",
            "without it this role can name ANY group, `system:masters` included, and the "
            "resourceNames fence never enters the picture",
        ),
        (
            "eks:accessEntryType",
            "without it a non-STANDARD entry type can be created, whose authorisation model is not "
            "the one this design reasoned about",
        ),
    ):
        assert variable in statement, (
            f"the `eks:CreateAccessEntry` statement carries no `{variable}` condition -- {why}."
        )

    assert "ForAllValues:StringEquals" in statement, (
        "the group condition does not use `ForAllValues:StringEquals`. A single-valued test against"
        "a multi-valued key does not constrain the other values, so an entry naming the reviewed "
        "group plus `system:masters` would be permitted."
    )
    assert re.search(r'test\s*=\s*"Null"', statement), (
        "there is no `Null` condition requiring `eks:kubernetesGroups` to be present. "
        "`ForAllValues` evaluates TRUE for an absent key, so without this an entry can be created "
        "with no groups at all -- one this stage did not intend and whose cleanup path keys on the "
        "principal rather than the groups."
    )
    assert "var.upgrade_lane_groups" in statement, (
        "the group condition does not read `var.upgrade_lane_groups`, so the set IAM enforces can "
        "drift from the set whose RBAC fences were reviewed and applied. That drift is a component "
        "upgraded with no fence."
    )


def test_the_delete_grant_is_scoped_to_one_principals_entry() -> None:
    """A wildcard here would let the state machine delete the read role's entry, or `Admin`'s."""
    body = _block("data", "aws_iam_policy_document", "workflow_upgrade_stage")
    assert "local.upgrade_access_entry_arn" in body, (
        "the `eks:DeleteAccessEntry` grant does not use `local.upgrade_access_entry_arn`, which is "
        "the one ARN shape that pins the delete to this stage's own principal on this stage's own "
        "cluster."
    )
    text = TERRAFORM.read_text()
    arn = re.search(r"upgrade_access_entry_arn = \(\s*\n\s*\"([^\"]+)\"", text)
    assert arn, "could not read `local.upgrade_access_entry_arn` to check its shape"
    value = arn.group(1)
    # FOLLOWED THROUGH THE LOCAL, because the name is reached via `local.upgrade_build_role_name`
    # rather than directly. That indirection exists because the whole stage is behind a `count` and
    # a null inside a string template is an error rather than an empty segment -- so the test
    # resolves the local and then checks it names the role.
    assert "local.upgrade_build_role_name" in value or "aws_iam_role.upgrade_build" in value, (
        f"the access-entry ARN does not reach the build role's name: {value!r}. Without it the "
        "grant covers every access entry on the cluster."
    )
    if "local.upgrade_build_role_name" in value:
        assert "aws_iam_role.upgrade_build[*].name" in text, (
            "`local.upgrade_build_role_name` does not resolve to the build role's actual name, so "
            "the access-entry ARN could name a role that is not the one a lane runs as."
        )
    assert value.endswith("/*"), (
        f"the access-entry ARN does not end in the uuid wildcard: {value!r}. An access entry's ARN "
        "carries a uuid that does not exist until the entry does, so the trailing segment is the "
        "one part that cannot be pinned -- and everything before it must be."
    )


def test_the_codebuild_grant_carries_the_eventbridge_rule_the_sync_pattern_needs() -> None:
    """Omitting it does not fail the apply. It fails every RUN, at the start.

    AWS's published policy for `startBuild.sync` includes three `events:` actions on one managed
    rule, because the `.sync` pattern watches the build through EventBridge. A stage missing them
    starts, is denied, and reports an error that names EventBridge rather than this policy.
    """
    body = _block("data", "aws_iam_policy_document", "workflow_upgrade_stage")
    for action in ("events:PutTargets", "events:PutRule", "events:DescribeRule"):
        assert action in body, f"the `.sync` grant is missing {action}"
    assert "StepFunctionsGetEventForCodeBuildStartBuildRule" in body, (
        "the `events:` grant does not name `StepFunctionsGetEventForCodeBuildStartBuildRule`, which"
        "is the one managed rule the `.sync` pattern creates. A wildcard here would let the state "
        "machine write any EventBridge rule in the account."
    )


def test_the_report_stage_may_mail_only_as_the_configured_sender() -> None:
    """The mail grant targets this deployment's SES sender identity ARN, never `*`.

    `ses:SendEmail` supports the `identity` resource type, so `*` would let the report stage send
    as any identity this account has verified. The `ses:FromAddress` condition stays as defence
    in depth. Mirrors `test_the_release_gate_grants_nothing_it_does_not_need`'s assertion on the
    gate's own mail grant.
    """
    body = _block("data", "aws_iam_policy_document", "upgrade_report")
    assert "local.ses_sender_identity_arn" in body, (
        "the report's mail grant is not scoped to `local.ses_sender_identity_arn`. SES send "
        "actions support identity ARNs, so `*` is not acceptable here."
    )
    assert "ses:SendEmail" in body, (
        "the report stage cannot mail. The report is the one artifact a person is waiting for "
        "after "
        "a cluster has been changed; writing it to S3 and telling nobody is half a deliverable."
    )
    assert "ses:FromAddress" in body, (
        "the report's mail grant carries no `ses:FromAddress` condition, so the role can send as "
        "any "
        "SES identity this account has verified. Mirror `MailTheApprovalLink` in "
        "upgrade_approval.tf."
    )
    assert "var.approval_email_from" in body, (
        "the condition does not read `var.approval_email_from`, so the address the role may send "
        "as "
        "can drift from the address it actually sends from -- a denied send at the one moment a "
        "person is waiting to hear what happened to their cluster."
    )


def test_the_build_has_no_vpc_configuration() -> None:
    """The cluster endpoint is public, so a VPC would add an ENI and a NAT to the one fragile step.

    Asserted rather than left implicit, because adding a `vpc_config` "to be safe" is the obvious
    change to make and it would make the lane depend on subnet capacity at the moment a person is
    waiting for it.
    """
    body = _block("resource", "aws_codebuild_project", "upgrade")
    assert "vpc_config" not in body, (
        "the upgrade build declares a `vpc_config`. This cluster's Kubernetes endpoint is public "
        "(0.0.0.0/0) with private access off, read from the cluster, so a VPC buys nothing and adds"
        "an ENI, a subnet and a NAT dependency to the one step that must not become more fragile."
    )


def test_the_stage_is_off_by_default() -> None:
    """A stage that CHANGES a cluster must not arrive because a branch merged.

    The operator is deploying every other component and watching each stage behave; a stage present
    in the machine because of a default would make its absence indistinguishable from its silence.
    """
    text = TERRAFORM.read_text()
    match = re.search(
        r'variable "upgrade_stage_enabled" \{.*?default\s*=\s*(\w+)', text, re.S
    )
    assert match, "could not read `upgrade_stage_enabled`'s default"
    assert match.group(1) == "false", (
        f"`upgrade_stage_enabled` defaults to {match.group(1)}. It must default to false: with it "
        "false `Release approved` still names `Done` and not one of this stage's states is emitted."
    )
