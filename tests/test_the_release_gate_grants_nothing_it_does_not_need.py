"""The role that runs the release gate must be able to send one email and nothing else.

WHAT THIS CATCHES. The release gate is the last thing standing between a plan a person
approved and an executor that will change a cluster. Its runbook runs under an IAM role, and
the cheapest way to make that role dangerous is to hand it a permission "for later" -- an
`s3:GetObject` so the mail could quote a document, an `eks:DescribeCluster` so it could check
something first. Any one of those turns the gate from a thing that ASKS into a thing that can
also ACT, and nothing about the approval flow would look different afterwards.

THE ABSENCE IS THE DESIGN, so it is asserted rather than left to the comments that state it.
`release_gate.tf` carries a block of `DELIBERATELY ABSENT:` notes; this file is what makes
them true. A note is a claim about a policy and this is the policy.

WHY `states:` IS ON THE LIST WITH THE OTHERS, and it is the one a reader is most likely to
think belongs. Under this design the automation does NOT resume the execution -- the state
machine polls `GetAutomationExecution` and decides for itself. So the runbook needs no power
over the execution at all, which is strictly less than `upgrade_decision_watcher`'s role
holds. If someone reintroduces `.waitForTaskToken` they will have to delete a line here, and
that deletion is the conversation this test exists to force.

Deliberately no AWS call: the Terraform is the source, so this runs with no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

TERRAFORM = Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/release_gate.tf"

# Every action prefix that must not appear. `states:` is included -- see the module docstring.
FORBIDDEN_PREFIXES = ("s3:", "eks:", "dynamodb:", "bedrock:", "states:", "iam:", "lambda:")

# The only two actions this role may hold. SES needs both: `SendEmail` is what the runbook
# calls, and `SendRawEmail` is granted alongside it exactly as `upgrade_approval.tf` grants
# the pair, so the two policies do not differ in a way a reader has to explain.
EXPECTED_ACTIONS = {"ses:SendEmail", "ses:SendRawEmail"}


def _without_comments(text: str) -> str:
    """`text` with every `#` comment line removed.

    LOAD-BEARING, NOT TIDINESS. The policy document this test reads ends in a block of
    `DELIBERATELY ABSENT: every s3: action` notes, which name every forbidden prefix in prose.
    Scanning the raw text would match those comments and fail on a policy that is exactly
    right -- a red test proving nothing, which is worse here than a green one. So the comments
    come out first and only real HCL is scanned.
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
        f'could not find {kind} "{type_name}" "{name}" in {TERRAFORM.name}. If it was '
        "renamed, fix this test rather than deleting it -- the invariant it guards is still "
        "real."
    )
    return _without_comments(match.group(1))


def _actions(body: str) -> set[str]:
    """Every quoted `service:Action` string inside `actions = [...]` lists in `body`."""
    found: set[str] = set()
    for block in re.findall(r"actions\s*=\s*\[(.*?)\]", body, re.S):
        found |= set(re.findall(r'"([a-z0-9-]+:[A-Za-z0-9*]+)"', block))
    return found


def test_the_automation_role_holds_no_action_that_could_change_anything() -> None:
    body = _block("data", "aws_iam_policy_document", "release_gate_automation")
    actions = _actions(body)
    assert actions, (
        "found no actions at all in the release gate automation policy. The `actions = [...]` "
        "shape this test matches has probably changed -- fix the pattern rather than deleting "
        "the test."
    )
    offenders = sorted(
        action
        for action in actions
        if action.lower().startswith(FORBIDDEN_PREFIXES)
    )
    assert not offenders, (
        f"the release gate automation role holds {offenders}. This role asks a person for "
        "authorisation; it must not be able to read a document, reach a cluster, touch a "
        "store, call a model, pass a role or resume an execution. Authorising a change and "
        "making one are different jobs and this role may only do the first."
    )


def test_the_automation_role_holds_exactly_the_two_ses_actions() -> None:
    """A denylist alone would pass a role granted `ses:*` plus something new and unlisted.

    So the positive half is asserted too: the set is exactly the two SES actions, which means
    a fourth permission fails this test even if nobody thought to add its prefix above.
    """
    body = _block("data", "aws_iam_policy_document", "release_gate_automation")
    assert _actions(body) == EXPECTED_ACTIONS, (
        f"the release gate automation role holds {sorted(_actions(body))}, not "
        f"{sorted(EXPECTED_ACTIONS)}. Sending the request is the only thing this runbook does."
    )


def test_the_mail_grant_is_bound_to_one_from_address() -> None:
    """The mail grant targets this deployment's SES sender identity ARN, never `*`.

    `ses:SendEmail` supports the `identity` resource type, so `*` would let the role mail as any
    verified identity in the account. The `ses:FromAddress` condition stays as defence in depth,
    mirroring `upgrade_approval.tf`.
    """
    body = _block("data", "aws_iam_policy_document", "release_gate_automation")
    assert "local.ses_sender_identity_arn" in body, (
        "the release gate's mail grant is not scoped to `local.ses_sender_identity_arn`. SES "
        "send actions support identity ARNs, so `*` is not acceptable here."
    )
    assert "ses:FromAddress" in body, (
        "the release gate's mail grant carries no `ses:FromAddress` condition, so the role "
        "can send as any SES identity this account has verified. Mirror the "
        "`MailTheApprovalLink` statement in upgrade_approval.tf."
    )
    assert "var.approval_email_from" in body, (
        "the `ses:FromAddress` condition does not read `var.approval_email_from`, so the "
        "address the role may send as can drift from the address the runbook actually sends "
        "from -- which is a denied send at the one moment a person is waiting for mail."
    )


