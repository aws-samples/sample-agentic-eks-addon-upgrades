"""The Harness proxy must outwait the Harness, or a working answer is thrown away.

WHAT THIS CATCHES, and it is not hypothetical. An early run of the assessment
prompt failed on all three components with

    NO_EXTRACTION: ReadTimeoutError: AWSHTTPSConnectionPool(
        host='bedrock-agentcore.us-east-1.amazonaws.com', port=443): Read timed out.

while the Harness was still producing a correct extraction. botocore's default read timeout
is 60 SECONDS; one component's research is several page fetches plus a model loop over them,
which takes minutes. So the proxy was aborting calls that were working and reporting them as
components with no findable fact -- the worst shape of failure this pipeline has, because
`NO_EXTRACTION` is also what an honest "nothing published" answer looks like.

The default is silent, which is why this is a test and not a comment: nothing about
`boto3.client("bedrock-agentcore")` looks wrong, and the failure only appears against a real
Harness under a real prompt.
"""

from __future__ import annotations

from eks_addon_readiness.stages import research_harness

# The Lambda's own ceiling. Waiting past it buys nothing: the invocation dies first.
LAMBDA_TIMEOUT_SECONDS = 900

# What botocore uses when nobody says otherwise, and what this test exists to keep out.
BOTOCORE_DEFAULT_READ_TIMEOUT = 60


def test_the_harness_client_waits_longer_than_botocore_would() -> None:
    read_timeout = research_harness._HARNESS_CONFIG.read_timeout
    assert read_timeout > BOTOCORE_DEFAULT_READ_TIMEOUT * 5, (
        f"the harness client's read timeout is {read_timeout}s. A research call takes "
        "minutes, so anything near botocore's 60s default aborts a working answer and "
        "reports it as NO_EXTRACTION -- indistinguishable from a component with nothing "
        "published."
    )


def test_the_harness_client_does_not_outlive_its_own_invocation() -> None:
    """A timeout past the Lambda's ceiling is a promise the runtime cannot keep."""
    read_timeout = research_harness._HARNESS_CONFIG.read_timeout
    assert read_timeout < LAMBDA_TIMEOUT_SECONDS, (
        f"the read timeout is {read_timeout}s but the Lambda is capped at "
        f"{LAMBDA_TIMEOUT_SECONDS}s, so the wait can never complete: the invocation is "
        "killed first and the timeout never fires."
    )


def test_the_harness_call_is_never_retried() -> None:
    """A retried InvokeHarness re-runs the whole model loop.

    So a slow-but-working call would be abandoned and the work paid for twice -- and the
    second attempt would record a second set of page bodies under a second session id.
    """
    retries = research_harness._HARNESS_CONFIG.retries or {}
    assert retries.get("max_attempts") == 0, (
        f"the harness client allows retries ({retries}). Retrying a model loop duplicates "
        "the cost and the recorded evidence."
    )


def test_the_judge_client_also_beats_the_default() -> None:
    """The judge is deterministic but still a Lambda doing S3 reads over a whole session."""
    read_timeout = research_harness._JUDGE_CONFIG.read_timeout
    assert read_timeout > BOTOCORE_DEFAULT_READ_TIMEOUT, (
        f"the judge client's read timeout is {read_timeout}s, at or under botocore's "
        "default; a judge that is merely slow would be reported as a failed judge."
    )


def test_the_judge_call_is_never_retried() -> None:
    """The judge WRITES the candidate, its evidence and its report.

    A retry after a response was already produced writes a second set of objects for one
    research run, so the approval queue grows a duplicate nobody asked for.
    """
    retries = research_harness._JUDGE_CONFIG.retries or {}
    assert retries.get("max_attempts") == 0, (
        f"the judge client allows retries ({retries}), which can duplicate the candidate "
        "and report objects a single research run writes."
    )
