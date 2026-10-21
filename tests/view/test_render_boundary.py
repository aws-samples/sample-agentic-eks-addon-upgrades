"""The import-direction guard that makes the single-source-of-truth claim checkable.

A renderer may import `view.model` and the standard library, and nothing else. Renderers that
import `..decide.ReadinessAssessment`, `..facts.FactStore` and
`..order.{StepKind, UpgradePlan}` directly are *how* each one
could re-derive a number for itself. Remove the ability to reach a stage document and the
class of defect has nowhere to start.

F5, FROM THE INDEPENDENT REVIEW: the first version of this guard was DEAD FOR ABSOLUTE
IMPORTS. It took `module.split(".")[0]` for both spellings, and for any in-package
absolute import that head is always `"eks_addon_readiness"` -- which is not a key in
`FORBIDDEN`. So `from eks_addon_readiness.order import UpgradePlan` sailed through the
guard whose entire purpose is to stop exactly that.

The predicate is now a function, `forbidden_imports`, and it is tested against source that
is known-bad and source that is known-good BEFORE it is pointed at the real modules. A
guard nobody has watched fail is not a guard.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

PACKAGE = "eks_addon_readiness"
RENDER_DIR = pathlib.Path(__file__).resolve().parents[2] / PACKAGE / "view" / "render"

# The only intra-package module a renderer may reach. `view.model` is the value set;
# everything else in the package either derives values (`build`) or is a stage document,
# and a renderer must do neither.
# `command` is itself a renderer in this directory -- it imports the standard library
# and nothing else, holds no contract and derives no value -- so a renderer reaching it
# crosses no boundary. It is named explicitly rather than the rule being relaxed to
# "anything under render/", because the point of the guard is that additions are
# deliberate.
#
# `html` is allowed on exactly the same terms, and for a reason worth stating rather than
# assuming: it derives no value, holds no contract, reaches no stage document, and imports
# only `..model`, `.command` and the standard library. So a renderer reaching it crosses no
# boundary either.
#
# `approval.py` reaches it for the cell helpers and the stylesheet, and that dependency is
# the POINT rather than a shortcut. The approval page and the run report show the same rows
# with the same columns, and two hand-maintained copies of `_recommended_cell` drift the
# moment either page changes -- which is precisely the argument `command.py` was extracted
# on. Copying them to avoid an import would satisfy this guard and defeat what it protects.
ALLOWED_RELATIVE = frozenset({"model", "command", "html"})

# Contexts a renderer must never reach into, named explicitly so the failure message says
# which rule was broken rather than merely that one was.
FORBIDDEN = {
    "decide": "a Stage 4 assessment",
    "order": "a Stage 5 plan",
    "facts": "the gap ledger or the fact store",
    "report": "the Stage 2 inventory report",
    "snapshot": "a Stage 1 snapshot",
    "storage": "object storage",
    "knowledge": "raw compatibility knowledge",
    "loop": "the Stage 7 dispatch loop",
}


def _head(module: str, level: int) -> str:
    """The context a dotted module name reaches into, for either import spelling.

    THE FIX FOR F5 IS THIS FUNCTION. A relative import inside the package names its
    context directly (`..order` -> `order`). An absolute one carries the package prefix
    (`eks_addon_readiness.order` -> `order`), and the earlier guard forgot to strip it, so
    every absolute import reduced to the harmless head `eks_addon_readiness` and matched
    nothing.
    """
    if level:
        return module.split(".")[0] if module else ""
    return module.removeprefix(f"{PACKAGE}.").split(".")[0]


def forbidden_imports(source: str, filename: str = "<test>") -> list[str]:
    """Every import in `source` a renderer is not allowed to make, as readable reasons.

    Walks the AST rather than the text, so a docstring that discusses a forbidden module
    -- and these modules' docstrings do, at length -- cannot trip it.
    """
    problems: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            head = _head(module, node.level)
            if node.level and module and head not in ALLOWED_RELATIVE:
                problems.append(
                    f"imports `{'.' * node.level}{module}`, which is not one of "
                    f"{sorted(ALLOWED_RELATIVE)}"
                )
            elif not node.level and head in FORBIDDEN:
                problems.append(f"imports `{module}`, reaching {FORBIDDEN[head]}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                head = _head(alias.name, 0)
                if head in FORBIDDEN:
                    problems.append(
                        f"imports `{alias.name}`, reaching {FORBIDDEN[head]}"
                    )
    return problems


# ---------------------------------------------------------------------------
# GUARD THE GUARD. These run before the guard is pointed at anything real.
# ---------------------------------------------------------------------------

_ABSOLUTE_CROSSING = f"from {PACKAGE}.order import UpgradePlan"
_RELATIVE_CROSSING = "from ..order import UpgradePlan"


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_ABSOLUTE_CROSSING, id="absolute"),
        pytest.param(_RELATIVE_CROSSING, id="relative"),
        pytest.param(f"import {PACKAGE}.facts", id="plain-import"),
        pytest.param("from ..decide import ReadinessAssessment", id="relative-decide"),
        pytest.param(f"from {PACKAGE}.storage import read_document", id="absolute-storage"),
    ],
)
def test_the_guard_catches_a_boundary_crossing(source: str) -> None:
    """Both spellings of crossing the boundary must be caught.

    The `absolute` case is F5: it passed the earlier guard. It is first in the list
    because it is the one that was broken.
    """
    assert forbidden_imports(source), (
        f"the guard did not catch {source!r}, so it does not protect the boundary"
    )


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from ..model import ReportView", id="the-value-set"),
        pytest.param("import html as html_escape", id="stdlib-alias"),
        pytest.param("from __future__ import annotations", id="future"),
        pytest.param('"""A docstring naming ..order and eks_addon_readiness.facts."""', id="docstring"),
    ],
)
def test_the_guard_permits_what_a_renderer_legitimately_needs(source: str) -> None:
    """And it must not be a guard that refuses everything, which would prove nothing."""
    assert not forbidden_imports(source), (
        f"the guard wrongly flagged {source!r}, which a renderer is allowed to do"
    )


# ---------------------------------------------------------------------------
# THE REAL MODULES.
# ---------------------------------------------------------------------------


def _render_modules() -> list[pathlib.Path]:
    return sorted(p for p in RENDER_DIR.glob("*.py") if p.name != "__init__.py")


def test_there_are_render_modules_to_check() -> None:
    """An empty glob would make every assertion below vacuous."""
    assert _render_modules(), f"no renderer modules found under {RENDER_DIR}"


@pytest.mark.parametrize("path", _render_modules(), ids=lambda p: p.name)
def test_a_renderer_imports_only_the_value_set(path: pathlib.Path) -> None:
    problems = forbidden_imports(path.read_text(), str(path))
    assert not problems, (
        f"{path.name} crosses the presentation boundary:\n  "
        + "\n  ".join(problems)
        + "\nA renderer takes a ReportView and nothing else; anything more lets it "
        "re-derive a value the view already computed, which is the defect this "
        "boundary exists to prevent."
    )


@pytest.mark.parametrize("path", _render_modules(), ids=lambda p: p.name)
def test_a_renderer_is_listed_in_the_renderer_ruleset(path: pathlib.Path) -> None:
    """Every renderer is fingerprinted, so a rendering change is attributable.

    A renderer absent from `RENDERER_MODULES` would change what a human reads without
    moving any digest -- the exact hole this stage was built to close.
    """
    from eks_addon_readiness.view import RENDERER_MODULES

    assert path.stem in RENDERER_MODULES, (
        f"{path.name} renders output but is not in view/ruleset.py "
        f"RENDERER_MODULES {RENDERER_MODULES}, so editing it would change the "
        "report without moving renderer_digest."
    )
