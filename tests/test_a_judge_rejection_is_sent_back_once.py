"""A well-formed extraction the judge REJECTED is sent back to the same session, once.

The schema re-ask covers a malformed object. This covers a sound one the checker refused: a
composed release quote, a page cited but never fetched, or no release quoted at all -- each a
distinct rejection that costs a whole extra pipeline run. The judge's summary goes back on the
same session; the second answer is judged on the same terms; a second rejection stands.
"""

from __future__ import annotations

from typing import Any

from eks_addon_readiness.stages import research_harness as rh

EVENT = {
    "component_id": "metrics-server",
    "version_series": "0.6.x",
    "installed_version": "0.6.4",
    "current_kubernetes_version": "1.35",
    "target_kubernetes_version": "1.36",
    "bucket": "b",
    "cluster_name": "c",
}


def _wire(monkeypatch, harness_answers: list[dict], judge_verdicts: list[dict]):
    calls: dict[str, list] = {"harness": [], "judge": []}

    def fake_harness(session_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        calls["harness"].append([m["role"] for m in messages])
        return harness_answers[len(calls["harness"]) - 1]

    def fake_judge(session_id: str, event: dict, extraction: dict) -> dict:
        calls["judge"].append(extraction)
        return judge_verdicts[len(calls["judge"]) - 1]

    monkeypatch.setattr(rh, "HARNESS_ARN", "arn:harness")
    monkeypatch.setattr(rh, "JUDGE_FUNCTION", "judge")
    monkeypatch.setattr(rh, "_run_harness", fake_harness)
    monkeypatch.setattr(rh, "_run_judge", fake_judge)
    monkeypatch.setattr(rh, "_prompt", lambda event, sid: "prompt")
    return calls


def test_a_rejection_is_re_asked_with_the_judges_reason_and_can_recover(monkeypatch) -> None:
    calls = _wire(
        monkeypatch,
        harness_answers=[{"try": 1}, {"try": 2}],
        judge_verdicts=[
            {"decision": "REJECTED", "summary": "1 of 16 quote(s) do not appear"},
            {"decision": "PENDING_APPROVAL", "summary": "Confidence MEDIUM"},
        ],
    )
    result = rh.handler(EVENT)
    assert result["decision"] == "PENDING_APPROVAL"
    assert result["reask"] == "recovered"
    assert len(calls["harness"]) == 2 and len(calls["judge"]) == 2
    # Same conversation, extended: prompt, the model's first answer, the checker's reason.
    assert calls["harness"][1] == ["user", "assistant", "user"]
    assert calls["judge"] == [{"try": 1}, {"try": 2}]


def test_a_second_rejection_stands(monkeypatch) -> None:
    calls = _wire(
        monkeypatch,
        harness_answers=[{"try": 1}, {"try": 2}],
        judge_verdicts=[
            {"decision": "REJECTED", "summary": "no release was quoted"},
            {"decision": "REJECTED", "summary": "still no release was quoted"},
        ],
    )
    result = rh.handler(EVENT)
    assert result["decision"] == "REJECTED"
    assert result["reask"] == "rejected again"
    assert len(calls["harness"]) == 2, "exactly one re-ask, never a loop"


def test_an_accepted_first_answer_is_not_re_asked(monkeypatch) -> None:
    calls = _wire(
        monkeypatch,
        harness_answers=[{"try": 1}],
        judge_verdicts=[{"decision": "PENDING_APPROVAL", "summary": "Confidence HIGH"}],
    )
    result = rh.handler(EVENT)
    assert result["decision"] == "PENDING_APPROVAL"
    assert "reask" not in result
    assert len(calls["harness"]) == 1


def test_the_re_ask_tells_the_agent_to_quote_less_not_more() -> None:
    text = rh._REASK_AFTER_JUDGE
    assert "verbatim span from a page you fetched" in text
    assert "bare version tag" in text
    assert "Do not invent a compatibility statement" in text
