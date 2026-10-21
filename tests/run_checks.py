"""Run every check lane and report a per-lane exit code.

    python3 tests/run_checks.py

**Judge this by the per-lane exit codes below, not by the shell's status.** A runner that
returns 0 over a failed lane is a runner that lies, so each lane's own result is printed and
the summary is the arithmetic over them. The process exit code is non-zero if any lane
failed, but the log is the evidence.

Lanes:

    1  imports      every module imports with only pydantic installed
    2  doctests     the parsing and key-shape contracts
    3  stage 6      the deterministic half: both done-conditions, then hole by hole
    4  loop         the model side: request shape, schema enforcement, sweep handler
    5  digests      the four stage ruleset digests still equal their expected values

Lane 1 matters more than it looks. The four document stages must carry no model client and
no transport, so a module that imports one at module scope shows up here as a failure rather
than as a 40 MB deployment package.
"""

from __future__ import annotations

import doctest
import importlib
import pkgutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Modules that legitimately import a model client or a transport at module scope, and are
# therefore excluded from lane 1. Each is a shell module that only the runtime that needs it
# ever loads -- listed explicitly so adding one is a deliberate act.
_NEEDS_EXTRA_DEPS = {
    "eks_addon_readiness.scan.adapters.eks_mcp",
}

# Modules whose doctests are part of the contract.
_DOCTEST_MODULES = (
    # The AWS add-on version comparator. Registered because its doctests ARE the
    # contract: they pin that `v1.4.0-eksbuild.2` orders below `v1.14.3-eksbuild.9`
    # and that `eksbuild.9` orders below `eksbuild.16` -- three ways a string
    # comparison gets AWS's version list wrong, and the reason the recommended
    # version was the installed one. Doctests nobody runs are the same vacuous pass
    # this list already carries a warning about.
    "eks_addon_readiness.order.recommend",
    "eks_addon_readiness.storage.keys",
    "eks_addon_readiness.storage.documents",
    "eks_addon_readiness.storage.marks",
    # The upgrade click key and its parser. Registered because the parser REFUSING a fact
    # decision key is the property that keeps the two watchers apart, and a doctest is where
    # that is cheapest to state.
    "eks_addon_readiness.storage.decisions",
    "eks_addon_readiness.scan.collect.sanitize",
    "eks_addon_readiness.scan.helm_labels",
    "eks_addon_readiness.scan.workloads",
    "eks_addon_readiness.inventory.versions",
    "eks_addon_readiness.inventory.identity",
    "eks_addon_readiness.inventory.classify",
    "eks_addon_readiness.research.bounds",
    "eks_addon_readiness.research.verify",
    "eks_addon_readiness.research.authority",
    "eks_addon_readiness.research.acceptance",
    "eks_addon_readiness.research.freshness",
    "eks_addon_readiness.research.schema",
    "eks_addon_readiness.research.extract",
    "eks_addon_readiness.research.sources.web_docs",
    # The signed approve/reject command, and the region it signs for. Contracts because
    # their exact bytes are what a human copies into a terminal: a wrong region or a
    # mangled quote surfaces as a signature mismatch whose message names neither the
    # region nor the endpoint, so the spelling is pinned here rather than discovered on
    # stage.
    #
    # THESE TWO NAMES, not `view.model`, and the correction is worth recording. This
    # list named `view.model` while the command lived there; the command then moved to
    # `view/render/command.py` (so that editing it moves the RENDERER digest rather than
    # the ruleset one) and `region_of` moved to `view/build.py` (because it derives a
    # value stored on the document). The lane went on reporting `view.model: 0/0 passed`
    # -- a registered module with no doctests in it, which is a vacuous pass wearing the
    # word "ok". Lane 2 was covering nothing here for two commits.
    "eks_addon_readiness.view.build",
    "eks_addon_readiness.view.render.command",
)

# STAGE 5's EXPECTED VALUE IS NO LONGER ITS ORIGINAL BASELINE, deliberately, and it moved as
# `order/RULESET_MODULES` grew. It gained a fifth module, `recommend`, so the plan can carry the
# optional upgrade an AWS-decided managed add-on has earned; then a sixth, `dedupe`, so it
# stops carrying a step that installs the version an earlier step for the same component
# already installs; then a seventh, `floor`, so it stops carrying a step that installs a
# version AT OR BELOW the one running, which would move a cluster backwards; then an eighth,
# `collapse`, so it stops carrying a step that installs a series the plan ABANDONS before the
# cluster hops at all -- a component can otherwise be moved TWICE at one Kubernetes minor
# to land where one move would have put it; then a ninth, `latest`, so the plan recommends
# from the APPROVED FACT by one policy the approval page and the report also use -- the newest
# covered series, or the newest recorded release when the fact is silent above the installed
# one -- and `recommend` gives an AWS-decided add-on that is BEHIND a required step instead of
# leaving it unsequenceable.
# It moved again, for comments only, when internal project-history narrative was removed from
# the carried modules' docstrings for publication.
# Either way a plan whose steps changed must not carry the rules digest of the plan before them. All four carried Stage 5 sources are
# byte-identical, so a mismatch here still means one of them was edited. Stage 4
# still equals its original baseline exactly; Stage 2 moved for the chart-version-in-app-label fix (see below),
# Stage 3 and Stage 5 moved for the comment-only publication cleanup, and the lane says which
# is which rather than claiming baseline equality for all four.
_EXPECTED_DIGESTS = {
    # Stage 2 moved for the chart-version-in-app-label fix: inventory/{resolve,confidence}.py
    # now take a Helm component's installed version from the image tag (not the
    # `app.kubernetes.io/version` label) when that label is the chart version. A deliberate
    # behaviour change, so the Stage 2 digest moved off its original baseline.
    "stage 2": "sha256:0c091b1beead7e7c21b3d0cb7f1e3f6dfc8910bc9900e534b331ab9fb49333a9",
    # Stage 3 moved for a comment-only release cleanup (internal project-history edits in the
    # carried modules' docstrings); the rules did not change.
    "stage 3": "sha256:e21996fc26a3cc53c25b43034993d9a946a8fd2bff6074154ef7f96d4e59c32d",
    "stage 4": "sha256:d1583a353a9090e1493989ae8093cbe5529a46fd3b1a6a72298cee7c34d9c0bb",
    # Stage 5 moved again for a comment-only release cleanup (internal project-history
    # edits in the carried modules' docstrings); the rules did not change.
    "stage 5": "sha256:72f7aafd3acb72c319df02a3ec7ab6bd850a89bad91aa99a5eac53e244626d7a",
}


def lane_imports() -> int:
    print("\n===== LANE 1: import every module =====")
    package = importlib.import_module("eks_addon_readiness")
    names = ["eks_addon_readiness"] + sorted(
        m.name
        for m in pkgutil.walk_packages(package.__path__, "eks_addon_readiness.")
    )
    imported = failed = skipped = 0
    for name in names:
        if name in _NEEDS_EXTRA_DEPS:
            print(f"  skip  {name}  (needs an optional extra by design)")
            skipped += 1
            continue
        try:
            importlib.import_module(name)
            print(f"  ok    {name}")
            imported += 1
        except Exception as exc:
            print(f"  FAIL  {name}  {type(exc).__name__}: {exc}")
            failed += 1
    print(f"LANE 1: {imported} imported, {failed} failed, {skipped} skipped by design")
    print(f"LANE 1 EXIT={1 if failed else 0}")
    return 1 if failed else 0


def lane_doctests() -> int:
    print("\n===== LANE 2: doctests (the parsing and key-shape contracts) =====")
    total = failed = 0
    for name in _DOCTEST_MODULES:
        module = importlib.import_module(name)
        result = doctest.testmod(module, verbose=False)
        total += result.attempted
        failed += result.failed
        mark = "ok  " if result.failed == 0 else "FAIL"
        print(
            f"  {mark}  {name}: {result.attempted - result.failed}/{result.attempted} passed"
        )
    print(f"LANE 2: {total - failed}/{total} doctests passed, {failed} failed")
    print(f"LANE 2 EXIT={1 if failed else 0}")
    return 1 if failed else 0


def _subprocess_lane(number: int, title: str, script: str) -> int:
    print(f"\n===== LANE {number}: {title} =====")
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tests" / script)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    body = completed.stdout.strip()
    # The lane's own output is reproduced verbatim, minus its banner, so this log is the
    # evidence rather than a summary of one.
    for line in body.splitlines():
        if line.startswith(("STAGE 6 CHECKS", "LOOP AND SCHEMA CHECKS")) and "EXIT" in line:
            continue
        print(line)
    if completed.stderr.strip():
        print(completed.stderr.strip())
    print(f"LANE {number} EXIT={completed.returncode}")
    return completed.returncode


# Which of the four expected values is still its original baseline, so the lane's output
# cannot claim an equality that is no longer true. Stage 2 moved for the chart-version-in-
# app-label fix, and Stages 3 and 5 moved for the comment-only publication cleanup; see
# above. Only stage 4 still equals its original baseline.
_EQUALS_V1 = {"stage 4"}


def _provenance(name: str) -> str:
    return "  (equals original baseline)" if name in _EQUALS_V1 else "  (moved from baseline on purpose: see above)"


def lane_digests() -> int:
    print("\n===== LANE 5: frozen ruleset digests unchanged =====")
    from eks_addon_readiness.decide.ruleset import ruleset_digest as d4
    from eks_addon_readiness.facts.ruleset import ruleset_digest as d3
    from eks_addon_readiness.order.ruleset import ruleset_digest as d5
    from eks_addon_readiness.report import ruleset_digest as d2

    failed = 0
    for name, fn in (("stage 2", d2), ("stage 3", d3), ("stage 4", d4), ("stage 5", d5)):
        actual = fn()
        if actual == _EXPECTED_DIGESTS[name]:
            print(f"  ok    {name}: MATCHES EXPECTED  {actual}{_provenance(name)}")
        else:
            failed += 1
            print(f"  FAIL  {name}: MOVED")
            print(f"          expected {_EXPECTED_DIGESTS[name]}")
            print(f"          actual   {actual}")
    print(
        f"LANE 5: {'PASS - all four match; stage 4 still equals its original baseline, stage 2 moved for the chart-in-app-label fix, stages 3 and 5 for the comment-only cleanup' if not failed else 'FAIL'}"
    )
    print(f"LANE 5 EXIT={1 if failed else 0}")
    return 1 if failed else 0


def main() -> int:
    print("STAGE 6 BUILD CHECK")
    print(f"date: {datetime.now(UTC).isoformat()}")
    print(f"interpreter: Python {sys.version.split()[0]}")
    print(f"worktree: {ROOT}")
    print(
        "deps present: pydantic only (boto3/anthropic/mcp/strands NOT installed, so any\n"
        "              module importing them at module scope shows up in lane 1)"
    )

    codes = {
        1: lane_imports(),
        2: lane_doctests(),
        3: _subprocess_lane(3, "stage 6 deterministic checks", "stage6_checks.py"),
        4: _subprocess_lane(4, "model loop and schema checks", "loop_checks.py"),
        5: lane_digests(),
    }

    print("\n===== SUMMARY (judge by these, not by the shell) =====")
    for number, code in sorted(codes.items()):
        print(f"  LANE {number} EXIT={code}  {'PASS' if code == 0 else 'FAIL'}")
    worst = max(codes.values())
    print(f"OVERALL EXIT={worst}  {'ALL LANES PASS' if worst == 0 else 'AT LEAST ONE LANE FAILED'}")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
