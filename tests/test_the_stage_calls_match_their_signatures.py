"""A keyword a caller passes and a callee does not accept fails only when the stage RUNS.

WHAT THIS CATCHES, TWICE OVER, because it happened twice and the second time reached a
deployed execution.

`build_approval_email` lost its `nonces` and `done_nonce` parameters when the mail stopped
carrying per-row action links. The call site in `approval_gate.py` kept passing them. Nothing
failed at import, nothing failed in the suite, `terraform apply` succeeded, and the defect
surfaced as a Step Functions execution FAILING mid-stage:

    TypeError: build_approval_email() got an unexpected keyword argument 'nonces'
    approval_gate.py line 186, in handler

The run had already written its approval page and stored five valid grants by then, so the
visible symptom was a page that worked, an execution that had failed, and no email -- three
facts that do not obviously belong to one cause.

The first instance was `build_report`, where the judge passed `assessment` and `approve` did
not, and every approval was refused as a digest mismatch. That one has its own guard
(`test_the_two_report_builders_agree.py`) because the invariant there is that TWO CALLERS
agree with each other. This is the more general case: a caller agreeing with its CALLEE.

WHY NOT JUST CALL THE HANDLERS IN A TEST. Because exercising `approval_gate.handler` needs
S3, DynamoDB, SES and a Step Functions task token -- most of a deployment. The mismatch is
visible in the source without any of that: compare the keywords at each call site against the
signature of the function being called. Anything a callee cannot accept is a `TypeError`
waiting for a run.

WHAT IT DELIBERATELY DOES NOT CHECK: whether the ARGUMENT VALUES are right, or whether a
required parameter is missing at a call site that supplies it positionally. Both are real and
neither is what bit twice; keeping the check narrow is what keeps its failure message
unambiguous.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# (module holding the call, dotted import of the callee, its bare name at the call site).
# Extended when a stage starts calling something new across a package boundary; these are the
# calls where a signature change and a call site live in different files and different reviews.
CHECKED_CALLS: tuple[tuple[str, str, str], ...] = (
    (
        "eks_addon_readiness/stages/approval_gate.py",
        "eks_addon_readiness.notify.email:build_approval_email",
        "build_approval_email",
    ),
    (
        "eks_addon_readiness/stages/approval_gate.py",
        "eks_addon_readiness.view.render.approval:to_approval_html",
        "to_approval_html",
    ),
    (
        "eks_addon_readiness/stages/approval_gate.py",
        "eks_addon_readiness.storage.decisions:build_decision_form",
        "build_decision_form",
    ),
    (
        # The listing rule the approval gate offers from and `stages/resolve.py` suppresses on.
        # Registered because a stranded candidate's visibility now depends on its keyword-only
        # `now` and `client`, and a rename of either raises TypeError inside a
        # `.waitForTaskToken` handler -- which leaves an execution paused with no page, rather
        # than failing anywhere a suite would notice.
        "eks_addon_readiness/stages/approval_gate.py",
        "eks_addon_readiness.storage.candidates:list_pending",
        "list_pending",
    ),
    (
        "eks_addon_readiness/stages/upgrade_approval.py",
        "eks_addon_readiness.view.build:build_document",
        "build_document",
    ),
    (
        "eks_addon_readiness/stages/decision_watcher.py",
        "eks_addon_readiness.storage.tokens:build_token_store",
        "build_token_store",
    ),
)


def _accepted(dotted: str) -> tuple[set[str], bool]:
    """The keyword names a callable accepts, and whether it takes arbitrary `**kwargs`."""
    module_path, _, attr = dotted.partition(":")
    module = __import__(module_path, fromlist=[attr])
    signature = inspect.signature(getattr(module, attr))
    names = {
        name
        for name, param in signature.parameters.items()
        if param.kind
        in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    }
    takes_kwargs = any(
        param.kind is param.VAR_KEYWORD for param in signature.parameters.values()
    )
    return names, takes_kwargs


def _passed(source_path: Path, func_name: str) -> set[str]:
    """Every keyword name passed to `func_name(...)` anywhere in one module."""
    tree = ast.parse(source_path.read_text())
    passed: set[str] = set()
    seen = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (
            target.id
            if isinstance(target, ast.Name)
            else getattr(target, "attr", None)
        )
        if name != func_name:
            continue
        seen = True
        passed |= {kw.arg for kw in node.keywords if kw.arg}
    assert seen, (
        f"no call to {func_name}(...) found in {source_path.name}. If it was renamed or the "
        "call moved, update CHECKED_CALLS rather than deleting the entry -- an unchecked "
        "cross-file call is how this defect reached a deployed run."
    )
    return passed


@pytest.mark.parametrize(
    ("caller", "callee", "name"),
    CHECKED_CALLS,
    ids=[f"{c[2]}" for c in CHECKED_CALLS],
)
def test_the_caller_passes_only_what_the_callee_accepts(
    caller: str, callee: str, name: str
) -> None:
    path = ROOT / caller
    assert path.exists(), f"{caller} is gone; fix CHECKED_CALLS rather than the assertion"

    accepted, takes_kwargs = _accepted(callee)
    if takes_kwargs:
        pytest.skip(f"{name} accepts **kwargs, so no keyword can be rejected")

    passed = _passed(path, name)
    unknown = passed - accepted

    assert not unknown, (
        f"{caller} passes {sorted(unknown)} to {name}(), which does not accept "
        f"{'it' if len(unknown) == 1 else 'them'}. This raises TypeError only when the stage "
        f"RUNS -- import succeeds, the suite passes, terraform apply succeeds, and a Step "
        f"Functions execution fails mid-stage. {name} accepts: {sorted(accepted)}."
    )
