"""A caller that gives up before its callee turns a real write into a reported failure.

WHAT THIS CATCHES, and it is the worst failure shape in this pipeline rather than a
theoretical one. `approve_link` consumes the one-time grant and THEN invokes `approve`
synchronously, so `approve`'s whole execution happens inside `approve_link`'s budget. Both
Lambdas were 60s and botocore's default read timeout is also 60s, so all three raced. A slow
`approve` meant:

  * `approve_link` hit its ceiling and rendered "That did not go through";
  * the grant was already spent, so the link could not be retried;
  * and `approve` carried on and WROTE THE FACT.

A person told the approval failed, holding a dead link, with the fact silently stored. This
project has already paid for the same class of bug once -- `research_harness.py` carries an
explicit `read_timeout=890` because botocore's silent 60s default was aborting Harness calls
that were working and reporting them as components with nothing published.

The inequality below is the fix, and it is load-bearing in both directions: the client must
wait longer than `approve` can possibly run, and the Lambda must outlive the client so there
is still time to render a page saying what happened.

Deliberately no AWS call: the Lambda ceiling is read out of the Terraform, so this runs in
the standard suite with no credentials.
"""

from __future__ import annotations

import re
from pathlib import Path

from eks_addon_readiness.stages import approve_link

# `approve`'s own ceiling, from infra/terraform/modules/research/approval.tf. A Lambda cannot exceed its
# configured timeout, so this is the longest `approve` can possibly take -- which is what
# makes waiting longer than it a complete fix rather than a mitigation.
APPROVE_CEILING_SECONDS = 60

BOTOCORE_DEFAULT_READ_TIMEOUT = 60

TERRAFORM = Path(__file__).resolve().parent.parent / "infra/terraform/modules/research/approval_stage.tf"


def _link_lambda_timeout() -> int:
    """The `approve-link` Lambda's configured timeout, read from the Terraform.

    Parsed from the resource block rather than hardcoded, so raising the Lambda without
    raising the client -- or the reverse -- fails here instead of in production.
    """
    text = TERRAFORM.read_text()
    block = re.search(
        r'resource "aws_lambda_function" "approve_link" \{(.*?)\n\}', text, re.S
    )
    assert block, (
        f"could not find the approve_link function in {TERRAFORM.name}. If it was renamed, "
        "fix this test rather than deleting it -- the nesting it guards is still real."
    )
    timeout = re.search(r"^\s*timeout\s*=\s*(\d+)", block.group(1), re.M)
    assert timeout, "the approve_link function declares no explicit timeout"
    return int(timeout.group(1))


def test_the_client_waits_longer_than_approve_can_possibly_run() -> None:
    """The whole point: we must always SEE approve's answer, success or death."""
    read_timeout = approve_link._INVOKE_CONFIG.read_timeout
    assert read_timeout > APPROVE_CEILING_SECONDS, (
        f"approve_link waits {read_timeout}s for a function that may run "
        f"{APPROVE_CEILING_SECONDS}s. A slow approve would be abandoned here AFTER the "
        "grant was consumed, so the person sees a failure, the link is spent, and the fact "
        "is written anyway."
    )


def test_the_client_beats_the_silent_botocore_default() -> None:
    """An unset read timeout is 60s, which exactly ties approve's ceiling."""
    read_timeout = approve_link._INVOKE_CONFIG.read_timeout
    assert read_timeout > BOTOCORE_DEFAULT_READ_TIMEOUT, (
        f"the invoke client waits {read_timeout}s, at or under botocore's silent default. "
        "That default is what made the two functions race in the first place."
    )


def test_the_lambda_outlives_its_own_client() -> None:
    """There must be time left to render a page about a timeout, not just to hit one."""
    lambda_timeout = _link_lambda_timeout()
    read_timeout = approve_link._INVOKE_CONFIG.read_timeout
    assert lambda_timeout > read_timeout, (
        f"the approve-link Lambda is capped at {lambda_timeout}s while its own client waits "
        f"{read_timeout}s, so the invocation is killed before the timeout can be handled "
        "and the person gets a blank 502 instead of an explanation."
    )


def test_the_invoke_is_never_retried() -> None:
    """A retried invoke runs the fact write twice.

    `approve` refuses a non-pending candidate, so the second attempt 409s rather than
    double-writing -- but paying for it and logging it as an error is worse than not doing
    it.
    """
    retries = approve_link._INVOKE_CONFIG.retries or {}
    assert retries.get("max_attempts") == 0, (
        f"the invoke client allows retries ({retries}), which re-runs the only code path "
        "that writes a fact."
    )
