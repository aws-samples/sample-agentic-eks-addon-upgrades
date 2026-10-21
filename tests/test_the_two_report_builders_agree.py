"""`build_report` has two callers, and a keyword either of them forgets refuses approvals.

WHAT THIS CATCHES, MEASURED ON LIVE DATA rather than imagined. Stage 6 builds the approval
report and stores it; `approve` REBUILDS it and compares digests, so that an approval proves
what the person was actually shown rather than that a file on S3 happened to match. The two
builds must therefore be the same call.

They stopped being the same call when `assessment` was added to the report for the
assessment work: the judge passed it, `approve._rebuild_report` did not. Consequence, seen
against the candidate `metrics-server 0.6.x`:

    stored report digest        sha256:2495d466...
    rebuild WITHOUT assessment  sha256:b7caa374...
    rebuild WITH assessment     sha256:2495d466...

and every approval was refused with

    409 the approval names a report digest that does not match the report this candidate
        renders. Submitted sha256:2495d466..., expected sha256:b7caa374...

EVERY approval, for EVERY component, from the moment the assessment was added. Nothing caught
it: the digest check is the last step of the write path, so no test and no run reached it --
the only way to see it was to approve something, and until the approval stage existed nobody
had. The message even reads like tampering, which is the opposite of the truth.

WHY THIS IS A STRUCTURAL TEST AND NOT A FIXTURE. Comparing two digests needs a candidate, a
stored report, an extraction and a fact store, which is most of a run. The invariant that
actually matters is narrower and checkable from the source: THE TWO CALL SITES PASS THE SAME
KEYWORDS. A new report input added to one and not the other reproduces this bug exactly, and
this test fails the moment that happens.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JUDGE = ROOT / "eks_addon_readiness/stages/judge_extraction.py"
APPROVE = ROOT / "eks_addon_readiness/stages/approve.py"


def _build_report_keywords(path: Path) -> set[str]:
    """Every keyword passed to `build_report(...)` in one module."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    calls = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func
        if isinstance(name, ast.Name) and name.id == "build_report":
            calls += 1
            found |= {kw.arg for kw in node.keywords if kw.arg}
    assert calls, (
        f"no build_report(...) call found in {path.name}. If it was renamed or moved, fix "
        "this test rather than deleting it -- the two builders drifting is what it guards."
    )
    return found


def test_both_builders_pass_the_same_report_inputs() -> None:
    judge = _build_report_keywords(JUDGE)
    approve = _build_report_keywords(APPROVE)

    missing_in_approve = judge - approve
    missing_in_judge = approve - judge

    assert not missing_in_approve, (
        f"judge_extraction passes {sorted(missing_in_approve)} to build_report and approve "
        "does not, so the report approve rebuilds differs from the one the person was shown "
        "and EVERY approval is refused as a digest mismatch -- reported as though the "
        "request had been tampered with."
    )
    assert not missing_in_judge, (
        f"approve passes {sorted(missing_in_judge)} to build_report and judge_extraction "
        "does not. The stored report would then lack an input the rebuild includes, which is "
        "the same mismatch in the other direction."
    )


def test_the_assessment_is_one_of_those_inputs() -> None:
    """The specific keyword whose absence caused the outage, named so a revert is loud.

    Guarding only "the sets are equal" would pass if someone removed `assessment` from BOTH
    call sites, which silently drops the model's conclusions out of the report a person
    approves from -- a different defect with the same shape.
    """
    for path in (JUDGE, APPROVE):
        assert "assessment" in _build_report_keywords(path), (
            f"{path.name} no longer passes `assessment` to build_report. The report a person "
            "approves from is what carries the model's recommendation, its confidence "
            "rationale and its cited sources; without it they are approving a version number "
            "with no stated basis."
        )
