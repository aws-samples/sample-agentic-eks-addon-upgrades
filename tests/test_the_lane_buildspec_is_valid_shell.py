"""The buildspec is shell inside YAML, and a syntax error in it is discovered on a live cluster.

WHY THIS TEST EXISTS, AND IT IS NOT HYPOTHETICAL. A YAML `- |` block scalar strips its base indent
from every line before the shell ever sees it. So a heredoc written at any depth inside one arrives
with LEADING SPACES on its terminator, and `<<` requires the terminator at column zero -- `<<-`
strips
tabs, not spaces. The heredoc then swallows the rest of the phase, and the symptom is a build that
silently stops doing anything after that point: no error, no failed command, just a phase that ends
early. On this stage that phase is the one that captures the diagnosis and rolls a component back.

That is exactly the defect this file caught while the triage call was being wired in: two inline
Python heredocs, correct in isolation, that would have arrived at the shell unterminated.

SO EVERY SHELL BLOCK IS EXTRACTED AND PARSED. `bash -n` reads a script without running any of it, so
this costs nothing and needs no credentials, no cluster and no CodeBuild. It is the cheapest
possible
check for the most expensive possible place to find a typo.

WHAT IT DOES NOT CLAIM. That the commands are correct, that the flags are right, or that the logic
holds -- `test_the_diagnosis_lands_before_the_rollback.py` and
`test_the_managed_addon_path_preserves_its_configuration.py` are what read the meaning. This only
proves the shell will parse, which every other assertion about the buildspec quietly assumes.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

# `<<` then an optionally-quoted word, and NOTHING AFTER IT captured. A heredoc opener can carry a
# redirect on the same line -- `python3 - <<'PY' > /tmp/out.json` is exactly the shape in this
# buildspec -- so splitting on `<<` and stripping quotes yields `PY' > /tmp/out.json` as the marker.
# The first version of this test did that and reported a real heredoc as unterminated.
HEREDOC_OPENER = re.compile(r"""<<-?\s*(?:'([A-Za-z_][A-Za-z0-9_]*)'|"([A-Za-z_][A-Za-z0-9_]*)")""")

BUILDSPEC = (
    Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/upgrade_buildspec.yml"
)

# How many `- |` blocks the buildspec is expected to have: one per phase. Pinned so that a phase
# disappearing -- or a block being merged into another -- is a failure rather than a quietly smaller
# set of checks.
EXPECTED_BLOCKS = 4


def _shell_blocks() -> list[str]:
    """Every `- |` block scalar, dedented the way YAML dedents it.

    Hand-parsed rather than loaded with a YAML library, because this suite has no YAML dependency by
    design -- `upgrade_lane`'s library functions take already-parsed mappings so the fence can be
    tested with PyYAML absent, and adding it here for one test would undo that.

    The rule implemented is YAML's own: the indentation of the block's first non-empty line is the
    base, and that base is removed from every line. Which is precisely the rule that breaks
    heredocs.
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


def test_the_buildspec_has_one_shell_block_per_phase() -> None:
    blocks = _shell_blocks()
    assert len(blocks) == EXPECTED_BLOCKS, (
        f"found {len(blocks)} shell block(s), expected {EXPECTED_BLOCKS} (install, pre_build, "
        "build, "
        "post_build). If a phase was added or removed on purpose, update this constant in the same "
        "change -- otherwise the checks below are silently covering less than they read as "
        "covering."
    )


def test_every_shell_block_parses() -> None:
    for number, block in enumerate(_shell_blocks(), 1):
        result = subprocess.run(
            ["bash", "-n"], input=block, text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, (
            f"shell block {number} of the buildspec does not parse:\n{result.stderr.strip()}\n"
            "This would be discovered by a build that has already created an access entry and may "
            "already have changed the cluster."
        )


def test_no_block_leaves_a_heredoc_unterminated() -> None:
    """`bash -n` WARNS about this and still exits 0, so the return code alone is not enough.

    An unterminated heredoc is the specific failure a YAML block scalar invites, and it is silent:
    the
    rest of the phase becomes heredoc content, so the commands after it simply never run.
    """
    for number, block in enumerate(_shell_blocks(), 1):
        result = subprocess.run(
            ["bash", "-n"], input=block, text=True, capture_output=True, check=False
        )
        assert "here-document" not in result.stderr, (
            f"shell block {number} has a heredoc bash could not find the end of:\n"
            f"{result.stderr.strip()}\n"
            "A YAML block scalar strips its base indent, so a terminator written at any depth "
            "arrives"
            "with leading spaces and `<<` needs column zero. Use a single-quoted `python3 -c` "
            "one-liner instead."
        )


def test_every_heredoc_terminator_survives_the_dedent() -> None:
    """The positive form of the check above: after dedenting, a terminator is at column zero.

    Asserted separately because `bash -n`'s warning depends on the heredoc running off the END
    of the
    block. A heredoc whose terminator is indented but which happens to be followed by another
    heredoc's terminator can parse and still be wrong.
    """
    for number, block in enumerate(_shell_blocks(), 1):
        # COMMENT LINES ARE SKIPPED, and this file's own header explains the heredoc trap using
        # the very syntax it forbids -- so a scan that did not skip comments would find an "opener"
        # in a sentence and demand a terminator for it. Caught by this test failing on itself.
        openers = [
            line
            for line in block.splitlines()
            if not line.lstrip().startswith("#") and ("<<'" in line or '<<"' in line)
        ]
        for opener in openers:
            found = HEREDOC_OPENER.search(opener)
            assert found, (
                f"shell block {number} has a line this test read as a heredoc opener and could not "
                f"extract a marker from: {opener!r}. Fix the pattern rather than the buildspec."
            )
            marker = found.group(1) or found.group(2)
            terminators = [
                line
                for line in block.splitlines()
                if line.rstrip() == marker or line.strip() == marker
            ]
            assert any(line == marker for line in terminators), (
                f"shell block {number} opens a heredoc with marker {marker!r} and every candidate "
                f"terminator is indented: {terminators!r}. After YAML strips the block's base "
                "indent "
                "the terminator must sit at column zero, or the heredoc swallows the rest of the "
                "phase."
            )


def test_every_block_sets_a_failure_mode_explicitly() -> None:
    """Each phase says what it does on error, not what CodeBuild's command chaining gives it.

    Two of these blocks want `set -e` and two deliberately do NOT: the `build` phase records the
    mutation's exit status instead of aborting, because `post_build` is what captures the diagnosis
    and rolls back, and a phase that aborted on the mutation would take the diagnosis with it.
    Either
    choice is fine; making no choice is not, because then the behaviour depends on how CodeBuild
    happens to chain commands.
    """
    for number, block in enumerate(_shell_blocks(), 1):
        assert "set -" in block.splitlines()[0] or any(
            line.startswith("set -") for line in block.splitlines()[:3]
        ), (
            f"shell block {number} does not open with a `set -` line, so its failure behaviour is "
            "whatever CodeBuild's command chaining happens to give it. The `build` and `post_build`"
            "phases deliberately omit `-e`; they still say so."
        )
