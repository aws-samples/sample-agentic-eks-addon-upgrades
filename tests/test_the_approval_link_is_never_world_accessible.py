"""The approval endpoint must never be reachable without AWS authenticating the caller.

WHAT THIS CATCHES, and it is not hypothetical: the first version of this endpoint was a
Lambda function URL with `authorization_type = "NONE"` and a resource policy granting
`lambda:InvokeFunctionUrl` to `Principal: "*"`. That is the shape AWS publishes as its own
Step Functions "Human Approval" sample, and the single-use token in the query string was the
only gate. A security scanner flagged the function as world-accessible -- correctly -- and an organisation
guardrail refused every request with

    HTTP 403 {"Message":"Forbidden. For troubleshooting Function URL authorization ..."}

while the function's own resource policy still said `Allow`. So the design was both a
security finding and non-functional in an account with that guardrail.

It is replaced by an `AWS_IAM` URL reached by a SigV4-presigned link, which satisfies the
guardrail and is strictly stronger: AWS verifies the signature before the function runs, and
the signature is bound to one method, path and query string. The nonce is kept on top.

THE REGRESSION THIS GUARDS IS A ONE-WORD EDIT. Flipping `authorization_type` back to `"NONE"`
would make the endpoint world-accessible again, and everything downstream would keep working
in a bare checkout -- the tokens still function, the tests still pass, the plan still applies.
Nothing would notice until a security scanner raised it a second time. So the invariant is asserted
here rather than left to a comment.

Deliberately no AWS call: the Terraform is the source, so this runs with no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

TERRAFORM = Path(__file__).resolve().parent.parent / "infra/terraform/modules/research/approval_stage.tf"


def _resource(kind: str, name: str) -> str:
    """The body of one `resource "<kind>" "<name>"` block."""
    text = TERRAFORM.read_text()
    match = re.search(
        rf'resource "{re.escape(kind)}" "{re.escape(name)}" \{{(.*?)\n\}}', text, re.S
    )
    assert match, (
        f'could not find resource "{kind}" "{name}" in {TERRAFORM.name}. If it was renamed, '
        "fix this test rather than deleting it -- the invariant it guards is still real."
    )
    return match.group(1)


def test_the_link_url_requires_aws_to_authenticate_the_caller() -> None:
    body = _resource("aws_lambda_function_url", "approve_link")
    auth = re.search(r'authorization_type\s*=\s*"([A-Z_]+)"', body)
    assert auth, "the approve_link function URL declares no authorization_type"
    assert auth.group(1) == "AWS_IAM", (
        f'the approve_link function URL is {auth.group(1)!r}. "NONE" makes it world-'
        "accessible: anyone on the internet can invoke it, which a security scanner raises as a "
        "finding and this account's org guardrail refuses outright with a 403. Links are "
        "SigV4-presigned instead -- see notify/presign.py."
    )


def test_no_statement_grants_the_world_permission_to_invoke_it() -> None:
    """`AWS_IAM` on the URL and `Principal: "*"` on the policy would still be open.

    The two are separate controls and both were wrong in the first version. Asserting only
    the auth type would let a `*` principal survive, which is the statement a security scanner actually
    named.
    """
    body = _resource("aws_lambda_permission", "approve_link_url")
    principal = re.search(r"principal\s*=\s*(.+)", body)
    assert principal, "the approve_link permission declares no principal"
    value = principal.group(1).strip()
    assert value != '"*"', (
        'the approve_link permission grants lambda:InvokeFunctionUrl to Principal "*", '
        "which is exactly what made this function world-accessible. Name roles instead."
    )

    # THE PERMISSION IS `for_each` OVER AN ALLOWLIST, so the principal is `each.value` and the
    # names live in the local that builds the map. Asserting on `each.value` alone would prove
    # nothing, so the allowlist itself is what gets checked: it must be seeded with the
    # approval stage's role and extended only from a variable, never from a literal.
    assert value == "each.value", (
        f"the approve_link permission names {value}; this resource is expected to iterate an "
        "allowlist so that extra principals come from a variable rather than being hardcoded."
    )

    text = TERRAFORM.read_text()
    allowlist = re.search(r"approve_link_principals\s*=\s*merge\((.*?)\n  \)", text, re.S)
    assert allowlist, "could not find the approve_link_principals allowlist"
    listed = allowlist.group(1)
    assert "aws_iam_role.approval.arn" in listed, (
        "the allowlist does not include the approval stage's role, which is the principal "
        "that mints and signs the links -- without it the stage cannot reach its own endpoint."
    )
    assert "var.approve_link_role_arns" in listed, (
        "the allowlist does not read var.approve_link_role_arns, so extra principals would "
        "have to be hardcoded and a published copy would carry someone else's role ARN."
    )
    assert '"*"' not in listed, (
        'the allowlist contains a literal "*", which reintroduces world access by the back '
        "door even though the permission resource itself looks scoped."
    )


def test_the_signed_link_cannot_outlive_the_approval_window() -> None:
    """A link valid for longer than the run it resumes is a capability with no owner.

    The grant itself lives in the token store for 7 days, so before presigning a link could
    outlive its execution by six days and still write a fact. `X-Amz-Expires` is what bounds
    it, and AWS enforces that rather than this code.
    """
    from eks_addon_readiness.notify import presign

    # The Step Functions wait, from infra/terraform/modules/platform/variables.tf.
    approval_window_seconds = 7200
    assert approval_window_seconds >= presign.DEFAULT_EXPIRY_SECONDS, (
        f"a presigned link is valid for {presign.DEFAULT_EXPIRY_SECONDS}s while the "
        f"execution waits {approval_window_seconds}s. The link would outlive the run it "
        "resumes, so clicking it would write a fact for an execution nobody is waiting on."
    )


def test_an_unsignable_link_degrades_rather_than_failing_the_run() -> None:
    """No credentials must not mean no email -- and must not mean no pipeline.

    A local render or a test has no credentials. Raising there would make the approval stage
    unable to build its message at all, failing a run over a presentation concern.
    """
    from eks_addon_readiness.notify.presign import presign_get

    plain = "https://example.lambda-url.us-east-1.on.aws/?r=run&t=nonce"

    class _NoCredentials:
        region_name = "us-east-1"

        def get_credentials(self):
            return None

    assert presign_get(plain, session=_NoCredentials()) == plain, (
        "presign_get must return the plain URL when it cannot sign, so a credential-less "
        "render still produces a message."
    )
