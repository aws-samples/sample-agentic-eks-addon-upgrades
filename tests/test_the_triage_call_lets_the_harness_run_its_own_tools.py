"""The shape of the `InvokeHarness` call, held because getting it wrong is silent and expensive.

THIS IS THE TEST THAT DID NOT EXIST. The upgrade proxy drove its own tool loop for the whole life of
this stage and no test ever called it. It failed on every checkpoint of a real lane -- four
`unavailable` readings, a 900-second window spent, and a healthy metrics-server rolled back -- and
the reason was entirely in the call shape, which is to say entirely in code that was never exercised.

TWO FACTS ABOUT THE HARNESS, BOTH MEASURED AGAINST THE LIVE SERVICE, and both of them invisible from
reading the SDK:

1. **An invoke-time `tools` parameter SUPPRESSES the tools configured on the harness resource.** Same
   prompt, same harness: inputTokens 6615 with no parameter, 5557 with ten inline definitions. The
   four Gateway tools were not offered. So sending `tools` does not ADD to the surface, it REPLACES
   it -- and the replacement here was a path that could not complete.

2. **A session is stateful and an inline-function call is a pending HANDOFF on it.** Re-sending the
   conversation on the same session id returns one event, `{"messageStop": {"stopReason":
   "tool_use"}}`, with no content. Using a fresh session id instead names the rule outright:
   `runtimeClientError: Inline function callback has no pending handoff in this session.`

So the rules this file enforces are: send no `tools`, send one user message, and never reuse a
session id across attempts. None of that is checkable by reading the function's output -- the failure
mode is a well-formed empty answer -- which is why these assert on the REQUEST.

WHAT THIS TEST CANNOT DO: prove the live service still behaves as measured. A fake client cannot tell
you AWS changed its mind. It pins OUR side of the contract so a future edit cannot quietly reintroduce
the loop, and the measurements above are recorded so the next person knows what to re-measure.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from eks_addon_readiness.stages import upgrade_triage
from eks_addon_readiness.upgrade_lane.models import TriageVerdict


class _FakeHarness:
    """Records every `invoke_harness` call and replays scripted event streams."""

    def __init__(self, streams: list[list[dict[str, Any]]]) -> None:
        self._streams = streams
        self.calls: list[dict[str, Any]] = []

    def invoke_harness(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self._streams) - 1)
        return {"stream": iter(self._streams[index])}


def _turn(text: str) -> list[dict[str, Any]]:
    """One assistant turn that says `text`."""
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "end_turn"}},
    ]


def _verdict(component: str = "metrics-server") -> str:
    return json.dumps(
        {
            "component": component,
            "verdict": "HEALTHY",
            "proposed_action": "NONE",
            "observations": ["the single pod is Running and Ready with no restarts"],
            "reasons": ["the rollout completed and the new ReplicaSet holds the live pod"],
        }
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """`run_agent` with boto3 replaced, so the call shape is observable."""

    def _install(streams: list[list[dict[str, Any]]]) -> _FakeHarness:
        fake = _FakeHarness(streams)

        class _Boto:
            @staticmethod
            def client(service: str, **_kwargs: Any) -> Any:
                assert service == "bedrock-agentcore", (
                    f"run_agent built a {service!r} client. The only client it should need is the "
                    "harness one -- a lambda client here means the tool loop is back."
                )
                return fake

        import sys
        import types

        module = types.ModuleType("boto3")
        module.client = _Boto.client  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "boto3", module)
        monkeypatch.setattr(upgrade_triage, "HARNESS_ARN", "arn:aws:bedrock-agentcore:x:1:harness/h")
        return fake

    return _install


def test_no_tools_parameter_is_sent(patched) -> None:
    """Sending one would switch the Gateway's four tools off, as measured."""
    fake = patched([_turn(_verdict())])
    upgrade_triage.run_agent(session_id="s", prompt="p", component="metrics-server")

    assert fake.calls, "the harness was never invoked"
    for call in fake.calls:
        assert "tools" not in call, (
            "a `tools` parameter suppresses the tools configured on the harness resource, which "
            "here are the only read path the agent has"
        )
        assert "allowedTools" not in call, (
            "`allowedTools` filters harness-resource tools, so naming anything excludes the Gateway"
        )


def test_one_user_message_per_call_and_no_conversation_is_replayed(patched) -> None:
    """The harness holds the conversation; re-sending it is what returned an empty stream."""
    fake = patched([_turn(_verdict())])
    upgrade_triage.run_agent(session_id="s", prompt="p", component="metrics-server")

    for call in fake.calls:
        messages = call["messages"]
        assert len(messages) == 1, f"sent {len(messages)} messages; the harness restores its own"
        assert messages[0]["role"] == "user"
        assert list(messages[0]["content"][0]) == ["text"], (
            "content must be text only -- a toolUse or toolResult block here means this proxy is "
            "trying to answer a handoff it does not own"
        )


def test_a_re_ask_uses_a_new_session_id(patched) -> None:
    """Two asks on one session id is the collision that produced a bare `messageStop`."""
    fake = patched([_turn("{}"), _turn(_verdict())])
    triage = upgrade_triage.run_agent(session_id="s", prompt="p", component="metrics-server")

    assert len(fake.calls) == 2, "expected one re-ask after the unvalidatable first answer"
    first, second = (call["runtimeSessionId"] for call in fake.calls)
    assert first != second, (
        f"both attempts used session {first!r}. The harness restores messages for a session it has "
        "seen, so the second ask would collide with the first."
    )
    assert triage.verdict is TriageVerdict.HEALTHY


def test_the_re_ask_still_names_the_component_and_the_clock(patched) -> None:
    """A fresh session knows nothing, so the correction has to carry the original prompt."""
    fake = patched([_turn("{}"), _turn(_verdict())])
    upgrade_triage.run_agent(
        session_id="s", prompt="ORIGINAL-SITUATION", component="metrics-server"
    )

    re_ask = fake.calls[1]["messages"][0]["content"][0]["text"]
    assert "ORIGINAL-SITUATION" in re_ask, (
        "the re-ask dropped the situation. On a new session the model would be asked to fix a field "
        "in an answer about a cluster it was no longer told the name of."
    )


def test_no_text_names_the_stop_reason(patched) -> None:
    """`max_iterations_exceeded` and a silent model are different faults."""
    fake = patched(
        [
            [
                {"messageStart": {"role": "assistant"}},
                {"messageStop": {"stopReason": "max_iterations_exceeded"}},
            ]
        ]
    )
    with pytest.raises(upgrade_triage.TriageUnavailable) as caught:
        upgrade_triage.run_agent(session_id="s", prompt="p", component="metrics-server")

    assert "max_iterations_exceeded" in str(caught.value), (
        "the reason reaches the lane's report verbatim, and 'the Harness returned no text' alone "
        "took four runs to tell apart from an IAM denial and a parsing bug"
    )
    assert fake.calls


class TestTheFinalTurnIsWhatGetsParsed:
    """`_final_turn_text`, which is where the verdict was being lost in commentary."""

    def test_narration_between_tool_calls_is_not_part_of_the_answer(self) -> None:
        events = [
            *_turn("All events are Normal, no warnings."),
            *_turn("The old ReplicaSet has 0 desired replicas."),
            *_turn(_verdict()),
        ]
        text = upgrade_triage._final_turn_text(events)
        assert json.loads(text)["verdict"] == "HEALTHY"
        assert "All events are Normal" not in text, (
            "concatenating every turn is what handed the parser 761 characters of prose from a "
            "reading that had gone perfectly"
        )

    def test_a_trailing_empty_turn_does_not_hide_the_answer(self) -> None:
        """`max_iterations_exceeded` ends the stream with a turn that has no content."""
        events = [
            *_turn(_verdict()),
            {"messageStart": {"role": "assistant"}},
            {"messageStop": {"stopReason": "max_iterations_exceeded"}},
        ]
        assert json.loads(upgrade_triage._final_turn_text(events))["verdict"] == "HEALTHY"

    def test_a_stream_with_no_text_at_all_is_empty_not_an_error(self) -> None:
        events = [
            {"messageStart": {"role": "assistant"}},
            {"messageStop": {"stopReason": "tool_use"}},
        ]
        assert upgrade_triage._final_turn_text(events) == ""


def test_the_deleted_loop_stays_deleted() -> None:
    """Names, not behaviour: their return would mean the suppression bug is back."""
    for gone in ("_tool_definitions", "_tool_calls", "_run_tool", "MAX_TOOL_ROUNDS"):
        assert not hasattr(upgrade_triage, gone), (
            f"{gone} is back. The proxy executing tools means it is sending `tools`, which means "
            "the Gateway is suppressed."
        )
