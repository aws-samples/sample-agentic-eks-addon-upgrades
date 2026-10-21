"""Every Lambda must declare arm64, because the wheels in every zip are aarch64.

WHAT THIS CATCHES, AND IT IS NOT HYPOTHETICAL -- IT BROKE THE FIRST REAL UPGRADE RUN.
`build-packages.sh` fetches wheels with `--python-platform aarch64-manylinux2014`, so
`pydantic_core` in every deployment zip is an aarch64 ELF shared object. `architectures` on
`aws_lambda_function` DEFAULTS TO x86_64 when omitted, and the mismatch does not surface as a
warning or a slow path. It surfaces as:

    Runtime.ImportModuleError: Unable to import module 'entrypoints.upgrade_harness':
    No module named 'pydantic_core._pydantic_core'

which reads like a packaging bug in the zip. The zip is fine. The function disagreed with it.

WHY IT SURVIVED REVIEW. `upgrade_report` omitted the line while `upgrade_tools` and
`upgrade_triage` -- which load THE SAME ZIP -- both set it. Two of the three consumers of one
artifact were right, so nothing about the zip looked wrong, and the one that was wrong was wrong
in an attribute that is absent rather than incorrect. An omission is invisible in a diff.

WHAT IT COST. `upgrade_report` serves BOTH `Upgrade lanes` (plan mode) and `Upgrade report` (report
mode), so one missing line failed twice in a single execution. The planner failing meant no lane
opened, so the run then reached `Anything referred?` with no `$.upgrade.referred_count` and died on
an invalid choice path: three failures, one cause.

THE ASSERTION IS ON EVERY FUNCTION, NOT ON THE ONE THAT BROKE. A test naming `upgrade_report` would
pass the moment it was fixed and say nothing about the next function somebody adds. The property is
"no Lambda in this deployment may disagree with the wheels it loads", and it holds for all of them.

`*.tf` IS PARSED AS TEXT, not evaluated: this suite runs with no Terraform. Brace matching is
enough because `terraform fmt` keeps these blocks well formed.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERRAFORM = ROOT / "infra" / "terraform" / "modules"

# What `build-packages.sh` asks uv for. If this ever changes, the Lambdas must change WITH it, and
# this constant is the one place that records which way round the dependency runs.
WHEEL_PLATFORM = "aarch64-manylinux2014"
REQUIRED_ARCHITECTURE = "arm64"


def _lambda_blocks() -> list[tuple[Path, str, str]]:
    """Every `aws_lambda_function` block in the repo, as (file, name, body).

    ANCHORED ON A CLOSING BRACE AT COLUMN 0, which is what `terraform fmt` guarantees for a
    top-level block. The obvious alternative -- counting braces -- is WRONG here and was tried
    first: `"${path.module}/../../build/research.zip"` and every `jsonencode({...})` put braces
    inside strings, so a counter runs past the end of one resource and swallows the next. The
    symptom was functions being attributed to a zip they do not load, which made this test report a
    disagreement between `sweep` and `research.zip` that does not exist.
    """
    found: list[tuple[Path, str, str]] = []
    for path in sorted(TERRAFORM.rglob("*.tf")):
        text = path.read_text()
        for match in re.finditer(
            r'resource\s+"aws_lambda_function"\s+"([^"]+)"\s*\{(.*?)\n\}',
            text,
            re.S,
        ):
            found.append((path, match.group(1), match.group(2)))
    return found


def test_the_build_still_fetches_aarch64_wheels() -> None:
    """The premise of every assertion below. If the build changes platform, they must all change."""
    script = (ROOT / "scripts" / "build-packages.sh").read_text()
    assert WHEEL_PLATFORM in script, (
        f"build-packages.sh no longer requests {WHEEL_PLATFORM!r} wheels. Every Lambda in this "
        f"deployment declares {REQUIRED_ARCHITECTURE}, so changing the wheel platform without "
        "changing them produces `No module named 'pydantic_core._pydantic_core'` at import time -- "
        "an error that reads like a broken zip rather than an architecture mismatch."
    )


def _variable_default(module: Path, name: str) -> str | None:
    """The literal `default` of one variable in one module, or None."""
    for candidate in sorted(module.glob("*.tf")):
        text = candidate.read_text()
        block = re.search(rf'variable "{re.escape(name)}" \{{(.*?)\n\}}', text, re.S)
        if block:
            default = re.search(r'default\s*=\s*"([^"]+)"', block.group(1))
            return default.group(1) if default else None
    return None


def _resolved_architecture(path: Path, body: str) -> tuple[str, ...] | None:
    """What this function's `architectures` actually evaluates to, or None if absent.

    TWO SPELLINGS ARE IN USE AND BOTH ARE CORRECT: a literal `["arm64"]` and
    `[var.lambda_architecture]`, whose default is arm64. An earlier version of this test compared
    quoted strings only, so every variable-based function read as `[]` -- it reported five false
    failures across modules that were perfectly fine, and would have trained somebody to ignore it.
    Resolving the variable is what makes the assertion about the deployed architecture rather than
    about how it happens to be written.
    """
    match = re.search(r"architectures\s*=\s*\[([^\]]*)\]", body)
    if not match:
        return None
    inner = match.group(1).strip()
    literals = re.findall(r'"([^"]+)"', inner)
    if literals:
        return tuple(literals)
    reference = re.fullmatch(r"var\.([A-Za-z0-9_]+)", inner)
    if reference:
        resolved = _variable_default(path.parent, reference.group(1))
        return (resolved,) if resolved else ()
    return ()


def test_every_lambda_declares_the_architecture_its_wheels_were_built_for() -> None:
    """An OMITTED `architectures` is the failure mode, not a wrong one. It defaults to x86_64."""
    blocks = _lambda_blocks()
    assert blocks, "found no aws_lambda_function blocks at all, so this test proves nothing"

    for path, name, body in blocks:
        resolved = _resolved_architecture(path, body)
        assert resolved is not None, (
            f"aws_lambda_function.{name} in {path.name} does not set `architectures`, so it "
            f"defaults to x86_64 while its zip carries {WHEEL_PLATFORM} wheels. The symptom is "
            "`Runtime.ImportModuleError: No module named 'pydantic_core._pydantic_core'`, which "
            "looks like a packaging fault in the zip and is not one."
        )
        assert resolved == (REQUIRED_ARCHITECTURE,), (
            f"aws_lambda_function.{name} in {path.name} resolves to {resolved}, not "
            f"({REQUIRED_ARCHITECTURE!r},). Every zip in this deployment is built for "
            f"{WHEEL_PLATFORM}. If this is a `var.` reference, check that variable's default."
        )


def test_functions_sharing_one_zip_agree_about_its_architecture() -> None:
    """The specific shape that hid the defect: one artifact, three consumers, one disagreeing.

    `upgrade_report` and `upgrade_triage` all load `upgrade_harness.zip`. Two set
    arm64 and one did not, so the zip looked correct from every angle except the one that mattered.
    Grouping by filename makes that disagreement the thing being asserted rather than something a
    reviewer has to notice.
    """
    by_zip: dict[str, list[tuple[str, tuple[str, ...] | None]]] = {}
    for path, name, body in _lambda_blocks():
        filename = re.search(r"filename\s*=\s*[^\n]*?([A-Za-z0-9_.-]+\.zip)", body)
        if not filename:
            continue
        by_zip.setdefault(filename.group(1), []).append(
            (name, _resolved_architecture(path, body))
        )

    for zip_name, consumers in sorted(by_zip.items()):
        architectures = {declared for _name, declared in consumers}
        assert len(architectures) == 1, (
            f"the functions loading {zip_name} do not agree about its architecture: "
            f"{sorted(consumers)}. One zip cannot be two architectures, so whichever function "
            "disagrees will fail at import -- and the zip will look innocent."
        )
