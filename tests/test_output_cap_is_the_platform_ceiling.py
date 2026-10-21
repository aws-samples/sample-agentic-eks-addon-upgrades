"""The per-response output cap, asserted to be the platform's ceiling rather than a number.

WHAT THIS TEST IS FOR

The standing rule on this project is that no token or output cap may be set unless the
platform imposes it, and every cap that remains must be recorded as a platform ceiling
rather than a choice. `MAX_OUTPUT_TOKENS` is the one cap on this path that shapes how much
of a correct answer can come back, so it is the one that has to be held to that rule by a
test rather than by a comment.

It had drifted the other way. `extract.py` carried 64000 under the comment *"The model's
per-response maximum, not a smaller number of my choosing"*, while an earlier decision called
the same number *"a CHOICE ... NOT a documented model maximum"* that had never been found
published. Both cannot be true, and the comment made the stronger claim.

WHAT THE PROBE SETTLED

A live read-only probe against `us.anthropic.claude-sonnet-5` on Bedrock (us-west-2, SDK
1.5.0 -- the version `scripts/build-packages.sh` pins and deploys) named the ceiling in the
service's own rejection text and bisected it exactly:

    max_tokens=999999  ->  400 max_tokens: 999999 > 128000, which is the maximum
                               allowed number of output tokens for anthropic.claude-sonnet-5
    max_tokens=128000  ->  accepted
    max_tokens=128001  ->  400 ... > 128000, which is the maximum ...

So 128000 is a real, enforced, service-named ceiling, and 64000 was half of it. The log is
a live read-only probe.

WHY THE CLIENT TIMEOUT IS PART OF THE SAME FACT

`max_tokens` cannot simply be dropped here: it is a REQUIRED argument on
`messages.create`/`.parse` (the SDK signature has no default, and `None` raises), so this
path has no unset state to return to. That makes the honest resolution "set it to the real
maximum", not "remove it".

But raising it to the real maximum runs into a second, independent platform constraint. The
SDK refuses a NON-streaming request whose `max_tokens` implies more than ten minutes of
generation, client-side, before any HTTP request is sent:

    anthropic/_base_client.py::_calculate_nonstreaming_timeout
        expected_time = 3600 * max_tokens / 128_000
        if expected_time > 600: raise ValueError("Streaming is required ...")

which trips above `128000 * 600/3600` = 21333 tokens. `extract.py` calls the non-streaming
`messages.parse`/`.create`, so the value deployed before this test (64000) could not run on
this path at all -- it raised before sending. Verified live: 21333 accepted, 21334 raised.

The guard is guarded in turn:

    if not stream and not is_given(timeout) and self._client.timeout == DEFAULT_TIMEOUT:

so a client constructed with an explicit `timeout` is exempt -- that is the SDK's own
documented way for a caller to say it accepts the longer wall clock. Verified live:
`timeout=3600` plus non-streaming `max_tokens=64000` is accepted.

That is why this test asserts over two things and not one. Asserting the ceiling alone would
pass while the call still could not be made; asserting the timeout alone would not say what
it is for. The pair is the fact.

WHY THE TIMEOUT'S PHASES ARE ASSERTED SEPARATELY

A first version of this passed `timeout=3600.0`, a bare float. Review caught two things wrong
with it and both are now pinned below. httpx expands a scalar to
`connect=read=write=pool`, so the SDK's own 5s connect timeout silently became 3600s -- a
black-holed route would block for an hour instead of failing in five seconds. And 3600s is
unreachable inside the research Lambda's 900s ceiling: the runtime kills the invocation with
no Python exception, so the run ends as `Lambda.Unknown` with no artifact, which is the exact
silent failure the cap exists to avoid. The read timeout is therefore derived from the
Lambda's budget, and `connect` is set separately.

RUNNING IT

No credentials and no `anthropic` install are needed. The ceiling is a constant, and
`build_client()` imports the SDK lazily, so a stand-in module is enough to observe how it
constructs the client.
"""

from __future__ import annotations

import importlib
import os
import sys
import types

import pytest

from eks_addon_readiness.research import extract

# The ceiling the service named in its own rejection text, bisected exactly. Not a budget,
# not a round number someone liked: 128001 is refused and 128000 is not.
BEDROCK_ENFORCED_OUTPUT_CEILING = 128000

# The SDK's non-streaming refusal threshold, derived from its own arithmetic rather than
# copied: expected_time = 3600 * max_tokens / 128_000, refused once that exceeds 600s.
SDK_NONSTREAMING_MAX_TOKENS = BEDROCK_ENFORCED_OUTPUT_CEILING * 600 // 3600  # 21333

# The research Lambda's own ceiling, which is Lambda's maximum. The read timeout must land
# under this or it can never be the thing that fires.
RESEARCH_LAMBDA_TIMEOUT_SECONDS = 900


def test_the_ceiling_constant_is_the_one_the_service_enforces() -> None:
    """The module names the probed ceiling as a constant, so it can be clamped against."""
    assert extract.CEILING_OUTPUT_TOKENS == BEDROCK_ENFORCED_OUTPUT_CEILING, (
        f"CEILING_OUTPUT_TOKENS is {extract.CEILING_OUTPUT_TOKENS}, but the ceiling "
        f"us.anthropic.claude-sonnet-5 enforces is {BEDROCK_ENFORCED_OUTPUT_CEILING} "
        "(named in the service's 400 and bisected in "
        "a live read-only probe)."
    )


def test_the_default_cap_is_the_ceiling_and_not_a_smaller_chosen_number() -> None:
    """With no override, the cap IS the ceiling -- any other default is a cap we chose.

    This reads the default rather than the resolved module constant, so that setting
    `READINESS_RESEARCH_MAX_TOKENS` for a cost-capped run does not redden the suite. An
    earlier version asserted on the resolved value and did exactly that.
    """
    resolved = _resolve_cap_with({})
    assert resolved == BEDROCK_ENFORCED_OUTPUT_CEILING, (
        f"the default MAX_OUTPUT_TOKENS is {resolved}, not the "
        f"{BEDROCK_ENFORCED_OUTPUT_CEILING} the service allows. A default below the ceiling "
        "is a cap this repository chose, which the standing rule on caps forbids."
    )


def test_the_override_cannot_raise_the_cap_past_what_the_service_accepts() -> None:
    """A ceiling the environment can walk past is not being treated as a ceiling.

    Before this, `max(1024, int(env))` clamped only the low end, so
    `READINESS_RESEARCH_MAX_TOKENS=200000` produced 200000 and earned the same 400 the
    module's own comment quotes as proof the ceiling exists -- at the far end of a research
    run rather than at startup.
    """
    resolved = _resolve_cap_with({"READINESS_RESEARCH_MAX_TOKENS": "200000"})
    assert resolved == BEDROCK_ENFORCED_OUTPUT_CEILING, (
        f"an override of 200000 produced MAX_OUTPUT_TOKENS={resolved}, past "
        f"the {BEDROCK_ENFORCED_OUTPUT_CEILING} the service accepts. The upper clamp is "
        "missing, so the ceiling is documented but not enforced."
    )


def test_the_override_can_still_lower_the_cap() -> None:
    """The clamp must not become a pin: a cost-capped run is a legitimate reason to lower it."""
    resolved = _resolve_cap_with({"READINESS_RESEARCH_MAX_TOKENS": "32000"})
    assert resolved == 32000, (
        f"an override of 32000 produced {resolved}; the clamp should bound "
        "the top end only, not override a deliberately lower value."
    )


def test_the_cap_is_high_enough_to_need_the_timeout_that_makes_it_legal() -> None:
    """The two halves are one fact: at the ceiling, the non-streaming guard would fire.

    If a future SDK raised its threshold above the ceiling, the timeout below would stop
    being load-bearing and this test would say so rather than quietly guarding nothing.
    """
    assert extract.CEILING_OUTPUT_TOKENS > SDK_NONSTREAMING_MAX_TOKENS, (
        f"the ceiling ({extract.CEILING_OUTPUT_TOKENS}) no longer exceeds the SDK's "
        f"non-streaming threshold ({SDK_NONSTREAMING_MAX_TOKENS}), so the explicit client "
        "timeout is no longer what makes this call legal. Re-derive both before editing."
    )


def test_build_client_sets_the_timeout_phases_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit timeout is what lets the call carry the ceiling -- but not as a bare float.

    Two properties, because the first version of this satisfied only one of them:

    * an explicit timeout must be passed at all, or the SDK refuses the request client-side
      before sending it; and
    * `connect` must be set separately, because httpx expands a scalar to
      `connect=read=write=pool` and the SDK's 5s connect timeout would become the read
      timeout.
    """
    captured: dict[str, object] = {}

    class _StandInTimeout:
        def __init__(self, read: float, *, connect: float | None = None, **rest: object):
            self.read = read
            self.connect = connect
            self.rest = rest

    class _StandInBedrockClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    stand_in = types.ModuleType("anthropic")
    stand_in.AnthropicBedrock = _StandInBedrockClient  # type: ignore[attr-defined]
    stand_in.Timeout = _StandInTimeout  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", stand_in)

    extract.build_client("us.anthropic.claude-sonnet-5")

    assert "timeout" in captured, (
        "build_client() constructed the SDK client with no explicit timeout. The SDK then "
        "refuses any non-streaming request above "
        f"{SDK_NONSTREAMING_MAX_TOKENS} max_tokens client-side, and this path sends "
        f"{extract.MAX_OUTPUT_TOKENS}, so every research call raises ValueError before it "
        "is sent. See anthropic/_base_client.py::_calculate_nonstreaming_timeout."
    )

    timeout = captured["timeout"]
    assert isinstance(timeout, _StandInTimeout), (
        f"timeout must be a Timeout with per-phase values, got {timeout!r}. A bare float "
        "sets connect, read, write and pool at once."
    )

    assert timeout.connect == 5.0, (
        f"connect timeout is {timeout.connect}, not the SDK's 5s. A connect timeout that "
        "inherited the read timeout would wait minutes to open a TCP connection that a "
        "black-holed route will never open. Connecting is not the slow part; generating is."
    )

    assert 0 < timeout.read < RESEARCH_LAMBDA_TIMEOUT_SECONDS, (
        f"read timeout is {timeout.read}s, outside the research Lambda's "
        f"{RESEARCH_LAMBDA_TIMEOUT_SECONDS}s ceiling. Above it the runtime kills the "
        "invocation before the SDK can raise APITimeoutError, so the run ends as "
        "Lambda.Unknown with no artifact -- the silent failure this cap exists to avoid."
    )


def _resolve_cap_with(env: dict[str, str]) -> int:
    """Re-import `extract` under a given environment and return the cap it resolved to.

    Returns the VALUE, not the module, and that is the whole point. `importlib.reload`
    mutates the existing module object in place rather than producing a new one, so a helper
    that returned the module handed back an object the `finally` block below then reloaded
    under the restored environment -- every caller read the ambient value and the override
    under test was invisible. That is what made
    `test_the_override_can_still_lower_the_cap` read 128000 where it had set 32000.

    `build_client` imports the SDK lazily, so re-importing the module needs no `anthropic`.
    """
    saved = {k: os.environ.get(k) for k in ("READINESS_RESEARCH_MAX_TOKENS",)}
    try:
        for key in saved:
            os.environ.pop(key, None)
        os.environ.update(env)
        return int(importlib.reload(extract).MAX_OUTPUT_TOKENS)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # Restores the module to the ambient environment so later tests in this file, and any
        # other module holding a reference to it, see the value they would see on a fresh run.
        importlib.reload(extract)
