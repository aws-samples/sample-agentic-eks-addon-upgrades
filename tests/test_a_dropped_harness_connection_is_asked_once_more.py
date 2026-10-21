"""A dropped connection to the research Harness is retried once; a slow call still is not.

One failure mode: kyverno comes back `NO_EXTRACTION: ProtocolError: Response ended
prematurely`. The stream closed before any answer arrived, nothing was duplicated by asking
again, and the component cost a whole extra pipeline run. `retries.max_attempts=0` on the
client stays: that is about not abandoning a WORKING call. This is the other case.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from urllib3.exceptions import ProtocolError

from eks_addon_readiness.stages import research_harness as rh


def _stream(text: str) -> dict[str, Any]:
    return {"stream": [{"contentBlockDelta": {"delta": {"text": text}}}]}


class _Drops:
    """Drops the connection N times, then answers."""

    def __init__(self, drops: int, answer: str) -> None:
        self.drops = drops
        self.answer = answer
        self.calls = 0

    def invoke_harness(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.drops:
            raise ProtocolError("Response ended prematurely")
        return _stream(self.answer)


def test_one_drop_is_retried_and_the_answer_is_read() -> None:
    client = _Drops(drops=1, answer=json.dumps({"ok": True}))
    text = rh._invoke_once_retrying_a_dropped_connection(client, "s", [])
    assert json.loads(text) == {"ok": True}
    assert client.calls == 2


def test_a_second_drop_is_reported_not_retried_forever() -> None:
    client = _Drops(drops=2, answer="never")
    with pytest.raises(ProtocolError):
        rh._invoke_once_retrying_a_dropped_connection(client, "s", [])
    assert client.calls == 2


def test_the_client_still_does_not_retry_a_slow_call_itself() -> None:
    """The botocore retry stays off: a retried working call is abandoned and charged twice."""
    assert rh._HARNESS_CONFIG.retries == {"max_attempts": 0}
