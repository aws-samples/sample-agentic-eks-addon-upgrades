"""The version string EKS will accept for a managed add-on, resolved from AWS's own catalogue.

    in    the version a plan approved (`1.23.1`) and `describe-addon-versions` for that add-on
    out   the version EKS publishes (`v1.23.1-eksbuild.1`), or a refusal naming both

**THE PLAN AND THE EKS API DO NOT SPELL A VERSION THE SAME WAY, AND NOTHING RECONCILED THEM.**
`PlanStep.recommended_version` is patterned `^\\d+\\.\\d+\\.\\d+$` -- `order/recommend.py` says so
outright, and calls it a limitation bounded by the frozen contract -- so an approved step carries
`1.23.1`, `ReleaseStep.to_version` carries `1.23.1`, and the lane's `TARGET_ADDON_VERSION` is that
same string. EKS versions a managed add-on as `v<major>.<minor>.<patch>-eksbuild.<n>` and refuses
anything else outright. Measured on one test run's step 10:
`--addon-version 1.23.1` answered `InvalidParameterException: Addon version specified is not
supported`, and vpc-cni stayed on `v1.22.4-eksbuild.3`, ACTIVE, never modified.

**THE BUILD ORDINAL IS NOT DERIVABLE, WHICH IS WHY THIS IS A RESOLUTION AND NOT A FORMAT.**
`-eksbuild.<n>` is AWS's own rebuild counter for one add-on release; nothing about `1.23.1` says
whether the published build is `eksbuild.1` or `eksbuild.7`, and a lane that guessed would be
inventing a version. So the catalogue AWS publishes is READ, and the numeric core is matched
against it.

**THE ROLLBACK PATH NEVER HAD THIS BUG AND THAT WAS ITS SOURCE, NOT ITS CODE.** Its version comes
from `describe-addon`, which answers in EKS's own spelling, so it was right by provenance. There was
no formatting code anywhere in the lane -- one path read a value EKS had produced and the other
passed the plan's value through untouched. So this was never coded rather than coded twice, and
`plan_addon_update`/`plan_addon_rollback` now BOTH refuse a version that is not EKS-shaped, which
makes the correct spelling on the way back a property instead of a coincidence.

**COMPATIBILITY REMAINS EKS'S CALL, NOT THIS MODULE'S.** The catalogue is read for the add-on
without a `--kubernetes-version` filter and this resolves only the spelling. If the resolved version
turns out to be incompatible with the cluster, EKS refuses the update -- and that refusal now
survives the status poll, so the lane diagnoses and rolls back instead of reporting a success.
Deciding compatibility here would stand a second policy beside Stage 4's, which owns the question.

**NEWEST BUILD FOR THE REQUESTED RELEASE.** When AWS publishes several builds of one release,
`eksbuild.7` is the one to take: the ordinal is a rebuild of the same component version, so a lower
one is an older image of the release that was approved. That matches `order/recommend.py`'s policy
for an optional upgrade -- newest AWS lists -- and the required-move policy of "lowest series that
clears the hop" is about SERIES, which this does not touch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# The numbers out of a version that may or may not carry the `v` prefix and the build ordinal --
# an approved step's bare `1.23.1` and AWS's own `v1.23.1-eksbuild.7` respectively.
_LOOSE_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-eksbuild\.(\d+))?")


class AddonVersionUnresolved(Exception):
    """A managed add-on version could not be turned into one EKS publishes. Always names both."""


def addon_version_key(version: str | None) -> tuple[int, int, int, int] | None:
    """Order an EKS add-on version. `None` when it is not a version at all.

    THE ORDERING HAS TO BE VERSION-AWARE. Sorting AWS's list as text gets three things wrong at
    once: `v1.4.0-eksbuild.2` sorts above `v1.14.3-eksbuild.16` (4 above 1 in the second field),
    `eksbuild.9` sorts above `eksbuild.16`, and the `v` prefix shifts everything.

    **THIS DUPLICATES `order.recommend.catalogue_version_key`, AND THE DUPLICATION IS THE PACKAGING
    BOUNDARY RATHER THAN AN OVERSIGHT.** Importing that function is what the first version of this
    module did, and it is wrong for a reason visible in neither module: the upgrade lane ships as an
    ENUMERATED file list in `scripts/build-packages.sh`, not as the whole package, and that one
    import puts FORTY modules in the lane's runtime closure -- `order`, `facts`, `decide`,
    `inventory`, `report` and `scan`. `scan` is the cluster READER. The lane is the one artifact in
    this system holding a cluster-write identity, and that script says outright that sweeping in
    code with no business holding one is what the enumerated list exists to prevent.

    So the ordering is reimplemented here over the standard library alone, and
    `tests/test_the_upgrade_lane_bundle_carries_what_it_imports.py` pins this function to
    `catalogue_version_key` across a shared table of versions. The two cannot drift without a test
    failing, which is the property the import was there to provide -- obtained without widening the
    artifact that performs every mutation.

    Editing `order/recommend.py` to import from HERE was the other option and is worse: its source
    text is hashed into the Stage 5 ruleset digest, so it would move the digest of every plan ever
    produced, for a change that alters no plan.

    >>> addon_version_key("v1.14.3-eksbuild.16")
    (1, 14, 3, 16)
    >>> addon_version_key("v1.4.0-eksbuild.2") < addon_version_key("v1.14.3-eksbuild.9")
    True
    >>> addon_version_key("v1.14.3-eksbuild.9") < addon_version_key("v1.14.3-eksbuild.16")
    True
    >>> addon_version_key("1.3.10-eksbuild.3") < addon_version_key("v1.4.0-eksbuild.1")
    True
    >>> addon_version_key("1.13.2")
    (1, 13, 2, 0)
    >>> addon_version_key("alpine") is None
    True
    >>> addon_version_key(None) is None
    True
    """
    if not version:
        return None
    found = _LOOSE_VERSION.match(version.strip())
    if found is None:
        return None
    major, minor, patch, build = found.groups()
    return (int(major), int(minor), int(patch), int(build) if build else 0)


def is_published_addon_version(text: str) -> bool:
    """Whether `text` is spelled the way EKS spells a managed add-on version.

    NOT A GUESS AT WHAT EKS ACCEPTS -- a shape check. `v1.23.1-eksbuild.1` is the only shape the
    API has ever taken, and `1.23.1` is the shape that was rejected live. The check is here so the
    refusal can happen where the command is built rather than at the API.

    >>> is_published_addon_version("v1.23.1-eksbuild.1")
    True
    >>> is_published_addon_version("v1.22.4-eksbuild.3")
    True
    >>> is_published_addon_version("1.23.1")
    False
    >>> is_published_addon_version("1.23.1-eksbuild.1")
    False
    >>> is_published_addon_version("v1.23.1")
    False
    >>> is_published_addon_version("")
    False
    """
    core, separator, build = text.partition("-eksbuild.")
    if not separator or not build.isdigit():
        return False
    if not core.startswith("v"):
        return False
    parts = core[1:].split(".")
    return len(parts) == 3 and all(part.isdigit() for part in parts)


def published_versions(catalogue: Any, *, addon: str) -> tuple[str, ...]:
    """Every `addonVersion` AWS publishes for `addon`, from a `describe-addon-versions` response.

    THE WHOLE RESPONSE IS TAKEN rather than a `--query`-narrowed list, so the add-on name AWS
    answered about is checked against the one asked for. A response about another add-on would
    otherwise resolve this lane's version from another component's builds.

    >>> response = {"addons": [{"addonName": "vpc-cni", "addonVersions": [
    ...     {"addonVersion": "v1.23.1-eksbuild.1"}, {"addonVersion": "v1.22.4-eksbuild.3"}]}]}
    >>> published_versions(response, addon="vpc-cni")
    ('v1.23.1-eksbuild.1', 'v1.22.4-eksbuild.3')
    """
    addons = catalogue.get("addons") if isinstance(catalogue, dict) else None
    if not isinstance(addons, list) or not addons:
        raise AddonVersionUnresolved(
            f"the add-on catalogue read for {addon!r} lists no add-ons at all, so there is no "
            "published version to resolve against. `aws eks describe-addon-versions "
            f"--addon-name {addon}` answering nothing means the name is not one EKS publishes."
        )

    versions: list[str] = []
    for entry in addons:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("addonName") or "")
        if name and name != addon:
            raise AddonVersionUnresolved(
                f"the add-on catalogue read for {addon!r} answers about {name!r}. Resolving this "
                "lane's version from another add-on's builds would install a different component."
            )
        for published in entry.get("addonVersions") or ():
            if isinstance(published, dict):
                version = str(published.get("addonVersion") or "")
                if version:
                    versions.append(version)
    if not versions:
        raise AddonVersionUnresolved(
            f"the add-on catalogue read for {addon!r} carries no `addonVersion` at all. Without "
            "AWS's own list there is no honest way to obtain the `-eksbuild.<n>` ordinal, and "
            "guessing it would install a version nobody approved."
        )
    return tuple(versions)


def resolve_addon_version(*, addon: str, wanted: str, catalogue: Any) -> str:
    """The published add-on version for the release `wanted` names, or a refusal.

    `wanted` is the approved step's version, so usually a bare `major.minor.patch`. A `wanted` that
    is ALREADY an EKS spelling is honoured rather than re-resolved -- but only if AWS still
    publishes it, because a build that has been withdrawn is not a version this lane may apply.

    >>> response = {"addons": [{"addonName": "vpc-cni", "addonVersions": [
    ...     {"addonVersion": "v1.23.1-eksbuild.1"}, {"addonVersion": "v1.23.1-eksbuild.7"},
    ...     {"addonVersion": "v1.22.4-eksbuild.3"}]}]}
    >>> resolve_addon_version(addon="vpc-cni", wanted="1.23.1", catalogue=response)
    'v1.23.1-eksbuild.7'
    >>> resolve_addon_version(addon="vpc-cni", wanted="v1.22.4-eksbuild.3", catalogue=response)
    'v1.22.4-eksbuild.3'
    """
    if not wanted.strip():
        raise AddonVersionUnresolved(
            f"no target version reached the lane for add-on {addon!r}, so there is nothing to "
            "resolve. The version comes from the authorised release step; its absence is a defect "
            "in the lane payload, not something to default."
        )
    wanted = wanted.strip()
    published = published_versions(catalogue, addon=addon)

    if is_published_addon_version(wanted):
        if wanted in published:
            return wanted
        raise AddonVersionUnresolved(
            f"{wanted!r} is spelled as an EKS add-on version but AWS does not publish it for "
            f"{addon!r}. Published: {list(published)!r}."
        )

    asked = addon_version_key(wanted)
    if asked is None:
        raise AddonVersionUnresolved(
            f"{wanted!r} is not a version at all, so no published build of {addon!r} can be "
            "matched to it. An approved step carries `major.minor.patch`; anything else means the "
            "release record and this lane disagree about what was authorised."
        )

    best: tuple[int, int, int, int] | None = None
    best_version = ""
    for candidate in published:
        key = addon_version_key(candidate)
        # Same release, any build. The first three fields are the component version AWS and the
        # plan agree on; the fourth is the ordinal only AWS knows.
        if key is None or key[:3] != asked[:3]:
            continue
        if best is None or key > best:
            best, best_version = key, candidate

    if not best_version:
        raise AddonVersionUnresolved(
            f"AWS publishes no build of {addon!r} for version {wanted!r}. EKS names a managed "
            "add-on `v<major>.<minor>.<patch>-eksbuild.<n>` and refuses any other spelling, and "
            "the `-eksbuild.<n>` ordinal is AWS's own and cannot be derived. Published: "
            f"{list(published)!r}."
        )
    return best_version


def resolve_from_file(*, addon: str, wanted: str, catalogue_path: str | Path) -> str:
    """`resolve_addon_version` over a `describe-addon-versions` response on disk.

    The buildspec writes the response into the `before` bundle and passes the path, so the
    catalogue the resolution used is part of the evidence rather than a read nobody can revisit.
    """
    path = Path(catalogue_path)
    try:
        catalogue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as problem:
        raise AddonVersionUnresolved(
            f"the add-on catalogue for {addon!r} could not be read from {path}: {problem}. "
            "Without it the `-eksbuild.<n>` ordinal is unknown and the update would be refused by "
            "the API after the lane had already opened."
        ) from problem
    return resolve_addon_version(addon=addon, wanted=wanted, catalogue=catalogue)
