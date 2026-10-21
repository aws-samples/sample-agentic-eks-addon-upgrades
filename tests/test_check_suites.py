"""Runs the check-suite drivers under pytest, so `uv run pytest` covers everything.

The three drivers predate pytest here and are deliberately plain: each prints a line per
check and returns a per-lane exit code, which is what makes their output readable as evidence
in a log. Rewriting them as pytest tests would lose that, so instead pytest runs each in a
subprocess and asserts its own exit code -- the driver stays the source of truth about whether
it passed, which is the same rule as reading a build by its own log rather than the shell's
status.

A failure prints the driver's full output, so the reason is in the pytest run and not only in
a file someone has to go and find.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

DRIVERS = [
    ("stage6_checks.py", "the deterministic half: both done-conditions, then hole by hole"),
    ("loop_checks.py", "the model side: request shape, schema enforcement, sweep handler"),
    ("approval_checks.py", "the approval surface: report, HTML projection, transition"),
]


@pytest.mark.parametrize("driver,description", DRIVERS, ids=[d for d, _ in DRIVERS])
def test_check_suite(driver: str, description: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tests" / driver)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"{driver} ({description}) exited {completed.returncode}\n\n"
            f"{completed.stdout[-6000:]}\n{completed.stderr[-2000:]}"
        )


def test_frozen_ruleset_digests_are_unchanged() -> None:
    """The 16 carried rule modules are byte-frozen; their digests are the proof.

    Kept as a pytest test as well as a driver lane, because this is the one assertion whose
    failure means a carried module was edited -- and that must fail the default test run, not
    only a lane someone remembers to look at.

    STAGE 5's EXPECTED VALUE HAS MOVED FOUR TIMES, DELIBERATELY, and this is the record of
    all four.

    ONCE for a fifth module, `recommend`, so that a managed add-on whose compatibility AWS
    decided can earn the optional upgrade step `interleave` has no fact store to derive.

    AGAIN for a sixth, `dedupe`, which drops a plan step that installs the version an earlier
    step for the same component already installs. For example, keda offered twice with 2.20.2 on
    both rows -- a decision with no consequence, on a page whose entire purpose is to ask for
    consequential ones. The direct repair is one word inside `moves._series_covers`: scan the
    rules rather than return on the first whose series matches. The byte freeze forbids it, so the
    redundant step is dropped downstream instead, and `order/dedupe.py` records what that
    substitution does and does not reach.

    AGAIN for a seventh, `floor`, which drops a plan step that installs a version AT OR BELOW
    the one running. Consider a keda fact whose catalogue
    records exactly one release in 2.20.x: for a cluster on 2.20.2 the sequencer emitted a
    required move installing 2.20.0, and nothing refused it. `dedupe` keeps such a step
    correctly -- no EARLIER step installs it -- and the "strictly ahead of what is installed"
    rule existed only in `recommend` and in `view/build.py`, on neither path that produces a
    forced move. `dedupe` is edited in the same change to share its renumbering, so this
    digest moved once for the two together rather than twice.

    AGAIN for an eighth, `collapse`, which drops a plan step that installs a series the plan
    ABANDONS before the cluster hops at all. For a cluster at 1.35 targeting
    1.36, cert-manager and keda could each be moved TWICE at Kubernetes 1.35 -- 1.13.x to 1.19.x to
    1.21.x, and 2.17.x to 2.20.x to 2.21.x -- while the stored facts give 1.21.x `[1.33, 1.37)`
    and 2.21.x `[1.34, 1.37)`, each covering the cluster as it stands AND the target. So one
    move reached the target and the stop in between was installed and replaced before anything
    moved. `dedupe` keeps the pair because the two steps install different versions and `floor`
    keeps it because both are ahead of what is running; neither measures what the cluster ends
    up on. The direct repair is inside `moves.plan_moves`, which takes `candidates[0]` with no
    lookahead; the byte freeze forbids editing it, so the run is collapsed downstream and the
    reversibility
    is re-resolved over the widened span. `floor` is edited in the same change to call
    `collapse` rather than `recommend`, so it still runs last.

    Every move has the same reason: a plan whose steps changed must not carry the rules
    digest of the plan before them, and editing a carried module instead would have changed
    every plan digest ever produced rather than only those produced from here on
    (the carried-module byte freeze).

    The assertion keeps its full force. All four carried Stage 5 sources are byte-identical,
    and any edit to one of them still moves `d5()` off the value below -- only the value
    moved, not what a mismatch means. Stages 3 and 4 still equal their original ruleset
    exactly; Stage 2 moved for the chart-version-in-app-label fix (see below).
    """
    from eks_addon_readiness.decide.ruleset import ruleset_digest as d4
    from eks_addon_readiness.facts.ruleset import ruleset_digest as d3
    from eks_addon_readiness.order.ruleset import ruleset_digest as d5
    from eks_addon_readiness.report import ruleset_digest as d2

    # Stage 2 moved: inventory/{resolve,confidence}.py now take a Helm component's installed
    # version from the image tag when the `app.kubernetes.io/version` label is the chart
    # version (the kyverno chart-version-in-app-label fix). A deliberate behaviour change.
    assert d2() == "sha256:0c091b1beead7e7c21b3d0cb7f1e3f6dfc8910bc9900e534b331ab9fb49333a9"
    # Moved for a comment-only release cleanup (docstring edits in the carried modules); rules unchanged.
    assert d3() == "sha256:e21996fc26a3cc53c25b43034993d9a946a8fd2bff6074154ef7f96d4e59c32d"
    assert d4() == "sha256:d1583a353a9090e1493989ae8093cbe5529a46fd3b1a6a72298cee7c34d9c0bb"
    # Moved for a comment-only release cleanup (docstring edits in the carried modules); rules unchanged.
    assert d5() == "sha256:72f7aafd3acb72c319df02a3ec7ab6bd850a89bad91aa99a5eac53e244626d7a"
