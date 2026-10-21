"""A managed add-on update carries a version string EKS will accept, resolved from AWS's catalogue.

WHAT THIS CATCHES, AND IT IS THE DEFECT THAT RAN LIVE. The lane's `TARGET_ADDON_VERSION` is
`ReleaseStep.to_version`, which is the approved plan step's version, which is a bare
`major.minor.patch` because `PlanStep.recommended_version` is patterned `^\\d+\\.\\d+\\.\\d+$` and
cannot hold a build suffix. EKS names a managed add-on `v<major>.<minor>.<patch>-eksbuild.<n>` and
refuses every other spelling. a test run step 10 the lane issued
`--addon-version 1.23.1` and EKS answered `InvalidParameterException: Addon version specified is not
supported`; `describe-addon` still reports `v1.22.4-eksbuild.3` ACTIVE, so vpc-cni was never
modified.

WHY NO TEST CAUGHT IT. `test_the_managed_addon_path_preserves_its_configuration.py` calls
`plan_addon_update(to_version="v1.12.1-eksbuild.2")` -- it ASSUMED a well-formed version because it
was asserting about `--resolve-conflicts`, and nothing anywhere produced one or required one. There
was no formatting code on either path: the rollback happened to be right because its version comes
from `describe-addon`, which answers in EKS's own spelling. So this was never coded, not coded twice
inconsistently.

WHAT IS ASSERTED. That the resolution turns an approved version into one AWS publishes, picking the
newest build of that release; that it refuses rather than guesses when AWS publishes no such
release; that BOTH command builders now refuse a version EKS would reject, so the rollback's
correctness is a property instead of a provenance accident; and that the buildspec asks for the
resolution rather than passing the approved version straight to the API.

Deliberately no AWS call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eks_addon_readiness.upgrade_lane import __main__ as cli
from eks_addon_readiness.upgrade_lane.addon_version import (
    AddonVersionUnresolved,
    is_published_addon_version,
    published_versions,
    resolve_addon_version,
)
from eks_addon_readiness.upgrade_lane.diagnose import (
    REQUIRED_FILES_MANAGED_ADDON,
    BundleFile,
    BundleManifest,
)
from eks_addon_readiness.upgrade_lane.remediate import (
    plan_addon_rollback,
    plan_addon_update,
)

BUILDSPEC = (
    Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/upgrade_buildspec.yml"
)

# The shape `aws eks describe-addon-versions --addon-name vpc-cni` returns, trimmed to the field
# the resolution reads. The versions are the real ones from the cluster in the run above: the
# installed `v1.22.4-eksbuild.3` and the `1.23.1` series the plan approved moving to, published in
# two builds so "newest build of the approved release" is a distinguishable choice.
CATALOGUE = {
    "addons": [
        {
            "addonName": "vpc-cni",
            "addonVersions": [
                {"addonVersion": "v1.23.1-eksbuild.1"},
                {"addonVersion": "v1.23.1-eksbuild.7"},
                {"addonVersion": "v1.22.4-eksbuild.3"},
                {"addonVersion": "v1.19.5-eksbuild.1"},
            ],
        }
    ]
}

# What the release record for a test run actually carried for step 10.
APPROVED = "1.23.1"


def _complete_addon_bundle() -> BundleManifest:
    names = (*REQUIRED_FILES_MANAGED_ADDON, "logs/aws-node-2mqng.aws-node.current.log")
    return BundleManifest(
        run_id="00000000-0000-4000-8000-000000000001",
        component="vpc-cni",
        cluster="my-eks-cluster",
        phase="after",
        managed_addon=True,
        files=tuple(
            BundleFile(name=name, bytes_written=10, sha256="d" * 64, command="read")
            for name in names
        ),
        captured_at=datetime.now(UTC),
    )


def test_the_approved_version_resolves_to_one_eks_publishes() -> None:
    """The bare `1.23.1` the plan approved becomes the string the API takes."""
    resolved = resolve_addon_version(addon="vpc-cni", wanted=APPROVED, catalogue=CATALOGUE)
    assert is_published_addon_version(resolved), (
        f"resolved {resolved!r}, which EKS would refuse. The API takes "
        "`v<major>.<minor>.<patch>-eksbuild.<n>` and nothing else."
    )
    assert resolved == "v1.23.1-eksbuild.7", (
        f"resolved {resolved!r}. Among builds of the approved release the newest is the one to "
        "take: `-eksbuild.<n>` is AWS's rebuild counter for the same component version, so a lower "
        "ordinal is an older image of the release that was approved."
    )


def test_the_version_the_lane_actually_sent_would_now_be_refused() -> None:
    """The live failure, as an assertion: `1.23.1` cannot reach the API through the command builder.

    THE RED TEST FOR THIS CHANGE. Before it, `plan_addon_update` passed `to_version` through
    untouched and the only thing that rejected `1.23.1` was EKS -- after the lane had opened an
    access entry, and with the rejection then laundered into a success by the status poll.
    """
    with pytest.raises(AddonVersionUnresolved, match="not how EKS spells"):
        plan_addon_update(
            cluster="my-eks-cluster",
            region="us-east-1",
            addon="vpc-cni",
            to_version=APPROVED,
            configuration_values="",
        )


def test_the_rollback_is_held_to_the_same_shape() -> None:
    """Its version comes from `describe-addon` and was right by provenance, not by code.

    Asserting the shape on the way back is what makes that a property: a future change that sourced
    the rollback version from the plan instead would be caught here rather than on a cluster.
    """
    with pytest.raises(AddonVersionUnresolved, match="not how EKS spells"):
        plan_addon_rollback(
            cluster="my-eks-cluster",
            region="us-east-1",
            addon="vpc-cni",
            to_version="1.22.4",
            configuration_values="",
            bundle=_complete_addon_bundle(),
        )


def test_a_resolved_version_builds_the_update_argv() -> None:
    """The two halves together: resolve, then build. This is the sequence the buildspec runs."""
    resolved = resolve_addon_version(addon="vpc-cni", wanted=APPROVED, catalogue=CATALOGUE)
    argv = plan_addon_update(
        cluster="my-eks-cluster",
        region="us-east-1",
        addon="vpc-cni",
        to_version=resolved,
        configuration_values="",
    )
    assert argv[argv.index("--addon-version") + 1] == "v1.23.1-eksbuild.7"
    assert argv[argv.index("--resolve-conflicts") + 1] == "PRESERVE"


def test_a_release_aws_does_not_publish_is_refused_rather_than_approximated() -> None:
    """There is no nearest build. A version AWS never published is not a version to apply."""
    with pytest.raises(AddonVersionUnresolved, match="publishes no build"):
        resolve_addon_version(addon="vpc-cni", wanted="1.24.9", catalogue=CATALOGUE)


def test_an_already_eks_spelled_version_is_honoured_only_if_still_published() -> None:
    """A withdrawn build is not a version this lane may apply, even spelled correctly."""
    assert (
        resolve_addon_version(
            addon="vpc-cni", wanted="v1.22.4-eksbuild.3", catalogue=CATALOGUE
        )
        == "v1.22.4-eksbuild.3"
    )
    with pytest.raises(AddonVersionUnresolved, match="does not publish it"):
        resolve_addon_version(
            addon="vpc-cni", wanted="v1.23.1-eksbuild.99", catalogue=CATALOGUE
        )


def test_a_catalogue_about_another_addon_is_refused() -> None:
    """Resolving from another add-on's builds would install a different component."""
    with pytest.raises(AddonVersionUnresolved, match="answers about"):
        published_versions(CATALOGUE, addon="coredns")


def test_the_cli_prints_one_resolved_version_and_refuses_with_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The buildspec redirects stdout into a file, so stdout must be the version and nothing else.

    And a refusal must be exit 2 -- the lane's code for a deliberate decline -- because `pre_build`
    carries `set -e` and that is what stops the run before any mutation.
    """
    import json

    catalogue = tmp_path / "describe-addon-versions.json"
    catalogue.write_text(json.dumps(CATALOGUE), encoding="utf-8")

    assert (
        cli.main(
            [
                "resolve-addon-version",
                "--addon",
                "vpc-cni",
                "--wanted",
                APPROVED,
                "--catalogue",
                str(catalogue),
            ]
        )
        == 0
    )
    assert capsys.readouterr().out.strip() == "v1.23.1-eksbuild.7"

    assert (
        cli.main(
            [
                "resolve-addon-version",
                "--addon",
                "vpc-cni",
                "--wanted",
                "1.24.9",
                "--catalogue",
                str(catalogue),
            ]
        )
        == cli.REFUSAL
    ), "an unresolvable version must exit 2, the lane's code for a refusal it made deliberately"


def test_the_buildspec_resolves_rather_than_sending_the_approved_version() -> None:
    """The flags come from tested Python; so must the version that reaches them.

    A buildspec that kept passing `${TARGET_ADDON_VERSION}` to `--kind addon-update` would satisfy
    every assertion above while sending the API the one string it refuses.
    """
    text = BUILDSPEC.read_text()
    assert "resolve-addon-version" in text, (
        "the buildspec never asks `upgrade_lane resolve-addon-version` for the version EKS "
        "publishes, so the approved step's `major.minor.patch` would reach the API."
    )
    assert "describe-addon-versions" in text, (
        "the buildspec reads no add-on catalogue. The `-eksbuild.<n>` ordinal is AWS's own and "
        "cannot be derived from the approved version, so it has to be read."
    )
    update = [
        line
        for line in text.splitlines()
        if "--kind addon-update" in line or "--addon \"${COMPONENT}\" --version" in line
    ]
    assert not any("${TARGET_ADDON_VERSION}" in line for line in update), (
        f"the add-on update is still handed the approved version verbatim: {update!r}. That is the "
        "string EKS rejected as 'Addon version specified is not supported'."
    )
