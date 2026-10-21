"""A rejected mutation stays failed. The status poll may not write success over it.

THE MORE DANGEROUS HALF OF THE MANAGED-ADD-ON DEFECT, and this test RUNS the real shell rather than
reading it. a test run step 10 the `update-addon` was rejected --
exit 254, "Addon version specified is not supported" -- and the phase then polled the add-on's own
status and wrote THAT into `/tmp/mutation-status`. A rejected API call never moves an add-on off
ACTIVE, so the poll wrote 0 over the call's 254.

WHAT THAT COST. `post_build` already carries the correct guard: "if MUTATION_STATUS != 0 ...
skipping the health window". It could not fire, because it had been told the mutation worked. So the
900-second window ran on a component nothing had touched -- checkpoints at 180, 420, 660 and 900
seconds, the window expiring with no verdict, an automatic rollback, then two re-validation
checkpoints at 1198 and 1416 seconds. About twenty-four minutes, and the human's question "why did
the pipeline go on to the agent and wait so long after the command had failed" is answered entirely
by this one variable.

THE RULE THE FIX IMPLEMENTS. A poll that reports on the COMPONENT cannot also be the record of
whether the API accepted the CALL. Those are two facts and the phase now keeps two:
`mutation-call-status` and `addon-settle-status`. The call's status wins; the poll may only ADD a
failure. No new guard was introduced -- the existing one is made to see the truth.

WHY THIS EXECUTES THE BUILDSPEC. The bug was not in Python; it was in one shell line. A test that
grepped for `echo 0 > /tmp/mutation-status` would pass the moment the line moved. So the `build`
phase's shell block is extracted exactly as `test_the_lane_buildspec_is_valid_shell.py` extracts it,
`/tmp` is redirected into a temporary directory, `aws` is stubbed to answer the way the live API
answered, and the resulting status file is read. `python3` is NOT stubbed: it shims to this
interpreter, so the real command builder runs.

No AWS call, no cluster, no CodeBuild.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUILDSPEC = REPO / "infra/terraform/modules/platform/upgrade_buildspec.yml"

# The approved step's version, resolved. `pre_build` writes this file; the `build` phase reads it.
RESOLVED_VERSION = "v1.23.1-eksbuild.7"

# Real `describe-addon` output for vpc-cni on the cluster in the run above, trimmed to what the
# phase reads. The version is the one the add-on never left.
ADDON_CONFIGURATION = ""


def _shell_blocks() -> list[str]:
    """Every `- |` block, dedented the way YAML dedents it.

    Same hand-parse as `test_the_lane_buildspec_is_valid_shell.py`, and for the same reason: this
    suite has no YAML dependency, deliberately, so that the fence can be exercised in an
    environment with PyYAML absent.
    """
    lines = BUILDSPEC.read_text().splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != "- |":
            index += 1
            continue
        base: int | None = None
        body: list[str] = []
        index += 1
        while index < len(lines):
            line = lines[index]
            if not line.strip():
                body.append("")
                index += 1
                continue
            indent = len(line) - len(line.lstrip())
            if base is None:
                base = indent
            if indent < base:
                break
            body.append(line[base:])
            index += 1
        blocks.append("\n".join(body))
    return blocks


def _build_phase() -> str:
    """The third block: install, pre_build, build, post_build.

    Identified by what it does rather than by its index alone, so a phase added above it fails this
    with a readable message instead of silently running the wrong shell.
    """
    blocks = _shell_blocks()
    assert len(blocks) == 4, f"expected 4 shell blocks, found {len(blocks)}"
    block = blocks[2]
    assert "mutation-status" in block and "addon status" in block, (
        "block 3 of the buildspec is not the `build` phase any more. Find the phase that performs "
        "the mutation and points the poll at it, and update this selector in the same change."
    )
    return block


_AWS_STUB = """#!/usr/bin/env bash
# The live API's two answers, selectable by environment variable.
case "$*" in
  *"eks update-addon"*)
    if [ "${STUB_UPDATE_EXIT}" != "0" ]; then
      echo "aws: [ERROR]: An error occurred (InvalidParameterException) when calling the \
UpdateAddon operation: Addon version specified is not supported" >&2
      exit "${STUB_UPDATE_EXIT}"
    fi
    echo '{"update": {"status": "InProgress"}}'
    exit 0
    ;;
  *"eks describe-addon"*)
    echo "${STUB_ADDON_STATUS}"
    exit 0
    ;;
esac
exit 0
"""


def _harness(tmp_path: Path) -> tuple[str, dict[str, str], Path]:
    """The `build` phase rewritten onto `tmp_path`, with `aws` stubbed and `python3` shimmed."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "target-addon-version").write_text(RESOLVED_VERSION + "\n", encoding="utf-8")
    (state / "addon-config").write_text(ADDON_CONFIGURATION, encoding="utf-8")

    stubs = tmp_path / "bin"
    stubs.mkdir()
    aws = stubs / "aws"
    aws.write_text(_AWS_STUB, encoding="utf-8")
    aws.chmod(0o755)
    # NOT A STUB. The real command builder must run: the point of asking Python for the argv is
    # that the argv is under test, and a faked `python3` here would test nothing.
    python3 = stubs / "python3"
    python3.write_text(
        f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8"
    )
    python3.chmod(0o755)

    script = _build_phase().replace("/tmp/", f"{state}/")
    environment = {
        "PATH": f"{stubs}:{os.environ.get('PATH', '')}",
        "PYTHONPATH": str(REPO),
        "MANAGED_ADDON": "true",
        "CLUSTER_NAME": "my-eks-cluster",
        "TARGET_REGION": "us-east-1",
        "COMPONENT": "vpc-cni",
        "RELEASE": "vpc-cni",
        "RELEASE_NAMESPACE": "kube-system",
        "CHART_REF": "",
        "TARGET_CHART_VERSION": "",
        "TARGET_ADDON_VERSION": "1.23.1",
        "HISTORY_MAX": "20",
        "STUB_UPDATE_EXIT": "254",
        "STUB_ADDON_STATUS": "ACTIVE",
    }
    return script, environment, state


def _run(script: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], env=environment, text=True, capture_output=True, check=False
    )


@pytest.fixture(autouse=True)
def _bash_is_available() -> None:
    if shutil.which("bash") is None:  # pragma: no cover - every supported platform has it
        pytest.skip("bash is required to execute the buildspec's own shell")


def test_a_rejected_call_is_not_overwritten_by_the_addons_steady_state(tmp_path: Path) -> None:
    """The live failure, executed. EKS refuses the update; the add-on reads ACTIVE throughout."""
    script, environment, state = _harness(tmp_path)
    result = _run(script, environment)

    status = (state / "mutation-status").read_text(encoding="utf-8").strip()
    assert status != "0", (
        f"the phase recorded mutation-status {status!r} after a call EKS REJECTED. A rejected "
        "update never moves the add-on off ACTIVE, so a poll that writes its own reading here "
        "reports success for a mutation that never happened -- and `post_build`'s guard, which is "
        "correct, is then told the mutation worked. Phase output:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert (state / "mutation-call-status").read_text(encoding="utf-8").strip() == "254", (
        "the API call's own exit status is not recorded as its own fact, so nothing downstream can "
        "tell a rejected call from an unhealthy component."
    )


def test_the_poll_is_not_even_run_when_the_call_failed(tmp_path: Path) -> None:
    """There is no update in flight to settle, and the ACTIVE it would read is what misled the lane.

    Also the cheap half of the cost: the poll sleeps 15 seconds per read, up to sixty reads.
    """
    script, environment, state = _harness(tmp_path)
    result = _run(script, environment)

    assert "-- addon status" not in result.stdout, (
        "the phase polled the add-on's status after a call that was rejected. Output:\n"
        f"{result.stdout}"
    )
    assert (state / "addon-settle-status").exists(), (
        "the poll's conclusion is not recorded separately, so it can still only live in the same "
        "variable as the call's status -- which is the defect."
    )


def test_an_accepted_call_that_settles_active_is_a_success(tmp_path: Path) -> None:
    """The fix must not turn every managed-add-on upgrade into a failure."""
    script, environment, state = _harness(tmp_path)
    environment["STUB_UPDATE_EXIT"] = "0"
    environment["STUB_ADDON_STATUS"] = "ACTIVE"
    result = _run(script, environment)

    assert (state / "mutation-status").read_text(encoding="utf-8").strip() == "0", (
        "an accepted update whose add-on settled ACTIVE was recorded as a failure. Output:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "-- addon status ACTIVE" in result.stdout, (
        "the add-on was never polled on the path where polling is the point: the update is "
        "asynchronous, so the call returning 0 says nothing about the component."
    )


def test_an_accepted_call_whose_addon_degrades_is_a_failure(tmp_path: Path) -> None:
    """The poll's own finding reaches the guard. It may ADD a failure; it may not clear one."""
    script, environment, state = _harness(tmp_path)
    environment["STUB_UPDATE_EXIT"] = "0"
    environment["STUB_ADDON_STATUS"] = "DEGRADED"
    _run(script, environment)

    assert (state / "mutation-status").read_text(encoding="utf-8").strip() != "0", (
        "an add-on that went DEGRADED was recorded as a successful mutation."
    )


def test_a_refused_command_is_a_failed_mutation_rather_than_an_empty_eval(
    tmp_path: Path,
) -> None:
    """`command` exits 2 on a version EKS would refuse, which leaves the argv EMPTY.

    And `eval ""` exits 0. So the ask's refusal has to be captured too, or the same laundering
    happens one step earlier: nothing applied, status 0, the health window runs.
    """
    script, environment, state = _harness(tmp_path)
    # The unresolved approved version, as it would arrive if `pre_build`'s resolution were skipped.
    (state / "target-addon-version").write_text("1.23.1\n", encoding="utf-8")
    result = _run(script, environment)

    status = (state / "mutation-status").read_text(encoding="utf-8").strip()
    assert status != "0", (
        f"a refused command produced mutation-status {status!r}. `eval` on an empty string exits "
        f"0, so an unrecorded refusal reads as a mutation that worked. Output:\n{result.stdout}\n"
        f"{result.stderr}"
    )
    assert "no mutation was attempted" in (
        state / "mutation-command"
    ).read_text(encoding="utf-8"), (
        "the recorded command claims something was attempted when nothing was."
    )


def test_post_build_still_reads_the_one_status_file_and_skips_the_window(tmp_path: Path) -> None:
    """The guard is the one that was already there. This asserts it was not replaced by a new one.

    The human's instruction was to make the existing guard see the truth rather than add a second
    one, so the check is that `post_build` still branches on `MUTATION_STATUS` read from the same
    file, and still says it is skipping the health window.
    """
    post_build = _shell_blocks()[3]
    assert 'MUTATION_STATUS="$(cat /tmp/mutation-status' in post_build, (
        "`post_build` no longer reads /tmp/mutation-status, so the combination the build phase "
        "writes reaches nothing."
    )
    assert 'if [ "${MUTATION_STATUS}" != "0" ]; then' in post_build, (
        "the existing guard on a failed mutation is gone. It was correct; what was wrong was the "
        "value it read."
    )
    assert "skipping the health window" in post_build


def test_the_lane_result_still_records_what_was_attempted(tmp_path: Path) -> None:
    """A rejected call must remain visible in the record, not vanish because nothing changed."""
    script, environment, state = _harness(tmp_path)
    _run(script, environment)

    attempted = (state / "mutation-command").read_text(encoding="utf-8")
    assert "update-addon" in attempted and RESOLVED_VERSION in attempted, (
        f"the attempted command was not recorded verbatim: {attempted!r}. The report's `attempted` "
        "list is how a reader learns what the lane asked the API for."
    )
    log = (state / "mutation.log").read_text(encoding="utf-8")
    assert "not supported" in log, (
        f"the API's own refusal is not in the mutation log: {log!r}. It is the only place the "
        "reason survives."
    )


def test_a_failed_mutation_that_changed_nothing_is_not_rolled_back() -> None:
    """The remaining unnecessary wait: a rollback of a component nobody touched.

    A refused command, and a request EKS validated and rejected, both leave the component where
    it was. The lane still rolled it "back" -- an update to the version it was already on -- and
    then ran two re-validation checkpoints, each a whole model loop, around 200 seconds apiece on
    the live run. Minutes of work to restore something unchanged, reported as ROLLED_BACK.

    `REFUSED_BEFORE_MUTATION` already existed in `UpgradeStatus`, in the report page's row table and
    in the notification's wording; the buildspec had simply never emitted it. Asserted on the shell
    because that is where the decision is made.
    """
    post_build = _shell_blocks()[3]
    assert "nothing_was_applied()" in post_build, (
        "`post_build` has no check for whether the mutation took effect, so a failed mutation "
        "still rolls back a component that was never modified."
    )
    assert "REFUSED_BEFORE_MUTATION" in post_build, (
        "the lane cannot report REFUSED_BEFORE_MUTATION. Its own comment in models.py says why it "
        "is not a kind of REFERRED: 'we did not touch the cluster' is the most important thing a "
        "reader can know, and a ROLLED_BACK row about an unchanged component says the opposite."
    )
    # THE CHECK READS THE COMPONENT rather than the call's exit code, because an exit code cannot
    # tell a validated refusal from an accepted call that was lost on the way back.
    assert "rollback-addon-version" in post_build and "addon.addonVersion" in post_build, (
        "the check does not compare the recorded pre-mutation version with what the add-on says "
        "now, so it is inferring from an exit code rather than reading the component."
    )
    nothing_applied_row = post_build.split('elif [ "${DECISION}" = "nothing-applied" ]')
    assert len(nothing_applied_row) == 2, "the act table has no row for a mutation that did nothing"
    assert 'elif [ "${SEALED}" != "yes" ]' in nothing_applied_row[1], (
        "the nothing-applied row must be checked BEFORE the unsealed-bundle row, whose message "
        "says a rollback was skipped to preserve evidence -- the wrong reason for this case."
    )


def _nothing_was_applied_function() -> str:
    """The `nothing_was_applied` shell function, extracted so it can be executed on its own."""
    post_build = _shell_blocks()[3]
    assert "nothing_was_applied()" in post_build, (
        "`post_build` has no check for whether the mutation took effect."
    )
    body = post_build.split("nothing_was_applied()", 1)[1].split("\n}", 1)[0]
    return "nothing_was_applied()" + body + "\n}\n"


@pytest.mark.parametrize(
    ("reported_now", "expected_rc", "why"),
    [
        (
            "v1.22.4-eksbuild.3",
            0,
            "the add-on is on the version recorded before the mutation, so nothing was applied and "
            "there is nothing to roll back",
        ),
        (
            "v1.23.1-eksbuild.7",
            1,
            "the add-on moved, so the mutation DID take effect and the rollback is necessary",
        ),
        (
            "UNREADABLE",
            1,
            "an unreadable version is not evidence the component is unchanged, so it must fall "
            "through to the rollback -- an unnecessary rollback costs minutes, while a skipped "
            "necessary one leaves a component on a broken version",
        ),
    ],
)
def test_the_did_anything_change_check_reads_the_component(
    tmp_path: Path, reported_now: str, expected_rc: int, why: str
) -> None:
    """Executed, not read: all three answers the add-on can give, and what each decides."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "rollback-addon-version").write_text("v1.22.4-eksbuild.3\n", encoding="utf-8")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    aws = stubs / "aws"
    aws.write_text(
        '#!/usr/bin/env bash\ncase "$*" in *"describe-addon"*) echo "${STUB_NOW}";; esac\nexit 0\n',
        encoding="utf-8",
    )
    aws.chmod(0o755)

    script = _nothing_was_applied_function().replace("/tmp/", f"{state}/")
    result = subprocess.run(
        ["bash", "-c", f"{script}\nnothing_was_applied"],
        env={
            "PATH": f"{stubs}:{os.environ.get('PATH', '')}",
            "MANAGED_ADDON": "true",
            "TARGET_REGION": "us-east-1",
            "CLUSTER_NAME": "my-eks-cluster",
            "COMPONENT": "vpc-cni",
            "STUB_NOW": reported_now,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_rc, (
        f"with the add-on reporting {reported_now!r} the check returned {result.returncode}, "
        f"expected {expected_rc}: {why}. Output:\n{result.stdout}\n{result.stderr}"
    )


def test_the_status_files_hold_nothing_but_an_exit_code(tmp_path: Path) -> None:
    """`post_build` compares them as strings, so a stray line would make every comparison false."""
    script, environment, state = _harness(tmp_path)
    _run(script, environment)

    for name in ("mutation-status", "mutation-call-status", "addon-settle-status"):
        body = (state / name).read_text(encoding="utf-8")
        assert body.strip().isdigit() and len(body.strip().splitlines()) == 1, (
            f"{name} holds {body!r}, which is not a single exit code. `post_build` reads it with "
            "`cat` and compares it to '0'."
        )
