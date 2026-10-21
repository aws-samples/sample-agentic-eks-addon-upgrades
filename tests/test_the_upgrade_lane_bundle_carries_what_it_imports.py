"""Every module the shipped upgrade lane imports is in the zip, and nothing drags a package in.

WHAT THIS CATCHES, AND IT REACHED THE ACCOUNT. `scripts/build-packages.sh` assembles the upgrade
lane from an ENUMERATED file list rather than a `cp -R`, deliberately: the lane is the one artifact
in this system holding a cluster-write identity, and its own comment says a recursive copy "would
sweep in every stage and the harness proxies -- code with no business holding a
cluster-write identity, shipped into the one artifact that does". It then calls that list "the
review surface".

A review surface only works if somebody looks at it. `addon_version.py` was added to the package and
not to the list, while the shipped `__main__.py` and `remediate.py` both imported it -- so the
deployed lane raised ImportError on the first `python3 -m eks_addon_readiness.upgrade_lane` --
EARLIER than the malformed `--addon-version` that change had fixed. A managed add-on upgrade was
worse off than before it, entirely from packaging. Found by unzipping the live
`upgrade-lane/source.zip` out of S3, by a person, not by any test.

SO THE CHECK IS GENERAL RATHER THAN A LIST OF THE FILES THAT WERE MISSING. It walks the import
closure of the lane's ENTRY POINT -- the `python -m` target the buildspec invokes -- and requires
the file list to cover it. Any module added to the lane, and any new dependency of one already
there, is caught the same way without this test being edited.

AND IT HOLDS THE LINE IN THE OTHER DIRECTION. `test_the_bundle_pulls_in_no_wider_package` fails if
the closure ever reaches `scan`, `stages`, `research` and their like. Not hypothetical either: one
`from ..order.recommend import catalogue_version_key` in `addon_version.py` put forty modules in the
closure with `scan` -- the cluster reader -- among them. Widening the file list to match would have
been the wrong repair, and this is the test that says so.

Deliberately no AWS call, no zip build and no `cp`. The script's own list is the thing under test.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "eks_addon_readiness"
SCRIPT = REPO / "scripts/build-packages.sh"

# What the buildspec invokes. Everything the lane can run is reachable from here, because
# `python -m` on the package executes `__main__.py`.
ENTRY_POINT = "eks_addon_readiness.upgrade_lane.__main__"

# Packages the lane must never pull in. Each is named in the packaging comment's own argument or is
# a transport: the lane holds a cluster-write identity and has no business carrying the cluster
# reader, the document stages or the research pipeline.
FORBIDDEN_PACKAGES = (
    "eks_addon_readiness.scan",
    "eks_addon_readiness.stages",
    "eks_addon_readiness.research",
    "eks_addon_readiness.brief",
    "eks_addon_readiness.loop",
    "eks_addon_readiness.notify",
    "eks_addon_readiness.storage",
    "eks_addon_readiness.view",
)

# `report_page.py` ships in the zip and imports `..view.render.html`, which does not, so importing
# it inside the bundle would fail. Nothing the lane RUNS imports it, so this is latent, not live.
# Recorded as an expectation instead of asserted away: if anything ever does reach it from the lane,
# the failure should be this test rather than a build against a cluster.
KNOWN_UNIMPORTABLE = {
    "eks_addon_readiness.upgrade_lane.report_page": ["eks_addon_readiness.view.render.html"],
}


def _bundled_modules() -> frozenset[str]:
    """The module names the packaging script copies into `upgrade-lane.zip`.

    PARSED FROM THE SCRIPT, because the script is the review surface this test replaces. Building
    the zip would be a truer read and is deliberately not done: it needs `zip`, it writes into
    `build/`, and this test has to be runnable by anyone in a second. A parse that stops matching
    the script fails loudly below rather than quietly checking nothing.
    """
    text = SCRIPT.read_text(encoding="utf-8")
    # Only the upgrade-lane section. `assemble` above it uses `cp -R`, so its packages are complete
    # by construction and are not what this test is about.
    section = text.split("UPGRADE_DIR=", 1)
    assert len(section) == 2, (
        "build-packages.sh no longer has an `UPGRADE_DIR=` section. This test parses that section "
        "to learn what ships in the lane; find where the lane is assembled now and fix the parse."
    )
    loops = re.findall(
        r"for f in (.*?); do\s*\n\s*cp \"\$\{ROOT\}/(eks_addon_readiness(?:/upgrade_lane)?)/",
        section[1],
        re.S,
    )
    assert len(loops) == 2, (
        f"expected two `for f in ...` copy loops in the lane section (the package root's modules "
        f"and `upgrade_lane`'s), found {len(loops)}. If the script changed shape, fix this parse: "
        "an unparsed list means this test is checking nothing."
    )
    modules: set[str] = set()
    for names, directory in loops:
        package = directory.replace("/", ".")
        for name in names.replace("\\\n", " ").split():
            assert name.endswith(".py"), f"unexpected entry in the copy list: {name!r}"
            stem = name[: -len(".py")]
            modules.add(package if stem == "__init__" else f"{package}.{stem}")
    return frozenset(modules)


def _module_path(module: str) -> Path | None:
    """The file backing a dotted module name, or None when it is not part of this package."""
    relative = module.split(".")
    if relative[0] != PACKAGE.name:
        return None
    direct = REPO.joinpath(*relative).with_suffix(".py")
    if direct.is_file():
        return direct
    package_init = REPO.joinpath(*relative) / "__init__.py"
    return package_init if package_init.is_file() else None


def _imported_modules(module: str) -> set[str]:
    """Every in-package module `module` imports, with relative imports resolved.

    Both the module a `from X import y` names AND `X.y` itself are yielded, because `from . import
    models` and `from .models import Thing` are one dependency written two ways and only the second
    names the module directly.
    """
    path = _module_path(module)
    if path is None:
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                parts = owner.split(".")
                if node.level > 1:
                    parts = parts[: -(node.level - 1)]
                target = ".".join(parts + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target.startswith(PACKAGE.name):
                found.add(target)
                found.update(f"{target}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(
                alias.name for alias in node.names if alias.name.startswith(PACKAGE.name)
            )
    return found


def _runtime_closure(entry: str = ENTRY_POINT) -> frozenset[str]:
    """Every in-package module that importing `entry` executes, transitively.

    A PACKAGE'S `__init__.py` RUNS WHEN A SUBMODULE IS IMPORTED, so every ancestor package is in the
    closure too. Missing that is how a closure walk reports success for a bundle carrying the leaf
    module but not the package it lives in.
    """
    seen: set[str] = set()
    pending = [entry]
    while pending:
        module = pending.pop()
        if module in seen or _module_path(module) is None:
            continue
        seen.add(module)
        parts = module.split(".")
        ancestors = {".".join(parts[:index]) for index in range(1, len(parts))}
        for target in _imported_modules(module) | ancestors:
            if target not in seen and _module_path(target) is not None:
                pending.append(target)
    return frozenset(seen)


def test_the_bundle_carries_every_module_the_lane_imports() -> None:
    """The defect as an assertion. A module in the closure and not in the zip is an ImportError."""
    missing = sorted(_runtime_closure() - _bundled_modules())
    assert not missing, (
        "the upgrade lane imports modules that `scripts/build-packages.sh` does not ship:\n  "
        + "\n  ".join(missing)
        + "\n\nThe deployed lane raises ImportError on the first `python3 -m "
        "eks_addon_readiness.upgrade_lane`, before it reaches the cluster at all -- and it does so "
        "after a person has approved the upgrade and the state machine has opened an access entry. "
        "Either add the module to the copy loop in that script, or remove the import."
    )


def test_the_bundle_pulls_in_no_wider_package() -> None:
    """Adding to the file list is not always the right repair, and this says why.

    One `from ..order.recommend import ...` in `addon_version.py` put forty modules in the closure,
    `scan` -- the cluster reader -- among them. Satisfying the test above by shipping all forty
    would have put the cluster reader inside the artifact that holds the cluster-write identity,
    which is the outcome the enumerated list exists to prevent.
    """
    closure = _runtime_closure()
    reached = sorted(
        module
        for module in closure
        for forbidden in FORBIDDEN_PACKAGES
        if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert not reached, (
        "the upgrade lane's import closure reaches packages that must not ship in it:\n  "
        + "\n  ".join(reached)
        + "\n\nThe lane is the one artifact holding a cluster-write identity. Do NOT fix this by "
        "adding these to the copy list -- break the import instead, the way "
        "`addon_version.addon_version_key` does. See that function's docstring."
    )


def test_the_bundle_ships_nothing_it_cannot_import() -> None:
    """The reverse read: a module in the zip whose own imports are absent is a trap for the next
    edit.

    Only `report_page.py` is expected, and only because nothing the lane runs imports it.
    """
    bundled = _bundled_modules()
    unsatisfiable = {
        module: absent
        for module in sorted(bundled)
        if (
            absent := sorted(
                target
                for target in _imported_modules(module)
                if _module_path(target) is not None and target not in bundled
            )
        )
    }
    assert unsatisfiable == KNOWN_UNIMPORTABLE, (
        f"the set of shipped-but-unimportable modules changed: {unsatisfiable!r}. A NEW entry "
        "means a module in the zip cannot be imported inside it; an entry that DISAPPEARED means "
        "somebody fixed it, which is welcome -- update KNOWN_UNIMPORTABLE in the same change."
    )


def test_the_entry_point_this_test_walks_is_the_one_the_buildspec_invokes() -> None:
    """A closure walk from the wrong entry point proves nothing about the deployed lane."""
    buildspec = (REPO / "infra/terraform/modules/platform/upgrade_buildspec.yml").read_text(encoding="utf-8")
    invoked = set(re.findall(r"python3 -m (eks_addon_readiness\.upgrade_lane)\b", buildspec))
    assert invoked == {"eks_addon_readiness.upgrade_lane"}, (
        f"the buildspec invokes {invoked!r}, which is not the module this test walks from. The "
        "closure has to start where the lane actually starts."
    )
    assert _module_path(ENTRY_POINT) is not None, (
        f"{ENTRY_POINT} does not exist, so `python -m eks_addon_readiness.upgrade_lane` has no "
        "entry point at all."
    )


def test_the_reconciliation_ships_where_it_is_actually_imported() -> None:
    """`naming.py` is NOT in the lane bundle, and that is correct rather than the same bug again.

    It carries the add-on-name to workload-identity reconciliation, and the module that imports it
    is `stages/upgrade_triage.py` -- the triage LAMBDA. Lambda packages are assembled by `cp -R` of
    the whole package, so it ships there without being enumerated anywhere. Asserted rather than
    assumed, because "shipping it is necessary but not sufficient" cuts both ways: if the lane ever
    does import it, the test above fails and this one explains where it belongs.
    """
    importers = sorted(
        path.relative_to(REPO).as_posix()
        for path in PACKAGE.rglob("*.py")
        if path.name != "naming.py"
        and re.search(r"\bfrom \.*(?:upgrade_lane\.)?naming import\b", path.read_text())
    )
    assert importers == ["eks_addon_readiness/stages/upgrade_triage.py"], (
        f"the reconciliation is imported by {importers!r}. If the upgrade LANE now imports it, add "
        "`naming.py` to the copy loop in build-packages.sh; if a new Lambda does, nothing is "
        "needed."
    )
    assert "eks_addon_readiness.upgrade_lane.naming" not in _runtime_closure(), (
        "the lane's own closure now reaches naming.py, so it must be added to the copy loop in "
        "build-packages.sh -- otherwise this is the ImportError that shipped once already."
    )
    assert "cp -R \"${ROOT}/eks_addon_readiness\"" in SCRIPT.read_text(encoding="utf-8"), (
        "Lambda packages are no longer assembled by `cp -R` of the package, so naming.py is not "
        "guaranteed to reach the triage Lambda any more. Check how that package is built now."
    )


@pytest.mark.parametrize(
    "version",
    [
        "v1.23.1-eksbuild.1",
        "v1.23.1-eksbuild.7",
        "v1.22.4-eksbuild.3",
        "v1.14.3-eksbuild.16",
        "v1.14.3-eksbuild.9",
        "v1.4.0-eksbuild.2",
        "1.3.10-eksbuild.3",
        "1.13.2",
        "v1.13.2",
        "alpine",
        "",
        None,
    ],
)
def test_the_lanes_version_ordering_agrees_with_the_plans(version: str | None) -> None:
    """The two implementations are pinned together, since the lane cannot import the plan's.

    `addon_version.addon_version_key` duplicates `order.recommend.catalogue_version_key` because
    importing it drags `scan` into the cluster-writing artifact -- see above, and that function's
    docstring. A duplicate with nothing holding it to the original is how "coded twice
    inconsistently" starts, so this is what makes the duplication safe rather than merely expedient.
    """
    from eks_addon_readiness.order.recommend import catalogue_version_key
    from eks_addon_readiness.upgrade_lane.addon_version import addon_version_key

    assert addon_version_key(version) == catalogue_version_key(version), (
        f"the lane orders {version!r} differently from the plan. The lane's copy exists only "
        "because it cannot import the plan's; the moment they disagree, a version the plan "
        "recommends and a version the lane resolves are two different versions."
    )
