"""A managed add-on moves with `resolveConflicts=PRESERVE` and its own configuration carried
forward.

WHAT THIS CATCHES. `eks update-addon` defaults to `NONE`, and `OVERWRITE` is the flag somebody
reaches for when an update is refused because a field was changed on the cluster. Either of those
replaces an operator's setting with the new version's default -- silently, because the add-on
reports
ACTIVE afterwards. That is the same loss the Helm path avoids by re-supplying `helm get values`, and
it is the managed-add-on expression of the human's requirement: read the existing configuration,
move the version, re-apply the same configuration.

`configurationValues` IS THE SECOND HALF AND IS EASY TO FORGET. `PRESERVE` resolves CONFLICTS in
favour of the cluster; it does not re-send the configuration. An update that passed PRESERVE and
omitted the add-on's recorded `configurationValues` would drop custom config that the add-on's own
spec carries. So both are asserted, on the way forward and on the way back.

THE COMMAND IS BUILT BY `remediate.py` AND PRINTED BY THE CLI, which is why this test can exist at
all: the buildspec asks Python for the argv rather than spelling `aws eks update-addon` itself, so
the flags are under test instead of being a shell line nothing parses.

Deliberately no AWS call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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

# A real shape of `configurationValues`: the coredns add-on's compute-type and replica settings, as
# an operator would have set them. Kept as JSON text because that is what the API takes and what
# `describe-addon` returns.
CONFIGURATION = '{"replicaCount":3,"computeType":"Fargate"}'


def _complete_addon_bundle() -> BundleManifest:
    names = (*REQUIRED_FILES_MANAGED_ADDON, "logs/coredns-abc.coredns.current.log")
    return BundleManifest(
        run_id="toast-191932",
        component="coredns",
        cluster="my-eks-cluster",
        phase="after",
        managed_addon=True,
        files=tuple(
            BundleFile(name=name, bytes_written=10, sha256="c" * 64, command="read")
            for name in names
        ),
        captured_at=datetime.now(UTC),
    )


def test_the_forward_update_passes_preserve() -> None:
    argv = plan_addon_update(
        cluster="my-eks-cluster",
        region="us-east-1",
        addon="coredns",
        to_version="v1.12.1-eksbuild.2",
        configuration_values=CONFIGURATION,
    )
    assert "--resolve-conflicts" in argv, f"no --resolve-conflicts at all: {argv!r}"
    assert argv[argv.index("--resolve-conflicts") + 1] == "PRESERVE", (
        f"the add-on update does not pass PRESERVE: {argv!r}. NONE refuses the update when a field "
        "was changed on the cluster, and OVERWRITE replaces that field with the new version's "
        "default -- silently, with the add-on reporting ACTIVE afterwards."
    )


def test_the_forward_update_carries_the_addons_own_configuration() -> None:
    """PRESERVE resolves conflicts; it does not re-send the configuration. Both are needed."""
    argv = plan_addon_update(
        cluster="my-eks-cluster",
        region="us-east-1",
        addon="coredns",
        to_version="v1.12.1-eksbuild.2",
        configuration_values=CONFIGURATION,
    )
    assert "--configuration-values" in argv, (
        f"the add-on update sends no configuration: {argv!r}. PRESERVE alone does not carry the "
        "add-on's own `configurationValues` forward."
    )
    assert argv[argv.index("--configuration-values") + 1] == CONFIGURATION


def test_an_addon_with_no_configuration_omits_the_flag_rather_than_sending_empty() -> None:
    """An empty `--configuration-values ""` is not the same as not sending one.

    The API reads an explicit empty string as "no configuration", which on an add-on that HAS
    configuration would clear it -- the exact loss PRESERVE exists to prevent, arriving through the
    flag meant to prevent it.
    """
    argv = plan_addon_update(
        cluster="my-eks-cluster",
        region="us-east-1",
        addon="kube-proxy",
        to_version="v1.35.0-eksbuild.1",
        configuration_values="",
    )
    assert "--configuration-values" not in argv, (
        f"an empty configuration is sent explicitly: {argv!r}. That reads as 'clear it'."
    )


def test_the_rollback_also_preserves_and_re_sends_the_configuration() -> None:
    """Rolling back with OVERWRITE would return the old VERSION with the NEW version's defaults.

    Which is not where the add-on started, and the whole reason PRESERVE is used going forward is
    that this cluster's add-ons carry configuration somebody chose.
    """
    argv = plan_addon_rollback(
        cluster="my-eks-cluster",
        region="us-east-1",
        addon="coredns",
        to_version="v1.11.4-eksbuild.2",
        configuration_values=CONFIGURATION,
        bundle=_complete_addon_bundle(),
    )
    assert argv[argv.index("--resolve-conflicts") + 1] == "PRESERVE", (
        f"the add-on rollback does not pass PRESERVE: {argv!r}"
    )
    assert argv[argv.index("--configuration-values") + 1] == CONFIGURATION
    assert argv[argv.index("--addon-version") + 1] == "v1.11.4-eksbuild.2", (
        f"the rollback does not name the recorded previous version: {argv!r}. The version is read "
        "from `describe-addon` before the update; there is no 'previous' the API can infer."
    )


def test_a_rollback_with_no_recorded_version_is_refused() -> None:
    """There is no `--addon-version previous`. An unrecorded version is a capture defect."""
    from eks_addon_readiness.upgrade_lane.remediate import RollbackRefused

    with pytest.raises(RollbackRefused, match="no previous version"):
        plan_addon_rollback(
            cluster="my-eks-cluster",
            region="us-east-1",
            addon="coredns",
            to_version="",
            configuration_values=CONFIGURATION,
            bundle=_complete_addon_bundle(),
        )


def test_every_addon_command_names_its_region_explicitly() -> None:
    """A region-scoped EKS call against the wrong default region returns EMPTY with no error.

    So an omitted `--region` does not fail -- it reports that the add-on does not exist, on the one
    path where that reading is wrong. This shell's default is `us-west-2`; the cluster is in
    `us-east-1`.
    """
    for argv in (
        plan_addon_update(
            cluster="my-eks-cluster",
            region="us-east-1",
            addon="coredns",
            to_version="v1.12.1-eksbuild.2",
            configuration_values="",
        ),
        plan_addon_rollback(
            cluster="my-eks-cluster",
            region="us-east-1",
            addon="coredns",
            to_version="v1.11.4-eksbuild.2",
            configuration_values="",
            bundle=_complete_addon_bundle(),
        ),
    ):
        assert "--region" in argv, f"no explicit --region: {argv!r}"
        assert argv[argv.index("--region") + 1] == "us-east-1"


def test_the_buildspec_never_spells_its_own_addon_update() -> None:
    """The flags have to come from the tested code, not from a shell line nothing parses.

    A buildspec that wrote `aws eks update-addon ... --resolve-conflicts OVERWRITE` would pass every
    assertion above, because those test a function the buildspec had stopped calling.
    """
    text = BUILDSPEC.read_text()
    assert "--kind addon-update" in text, (
        "the buildspec does not ask `upgrade_lane command --kind addon-update` for the add-on "
        "update argv, so the PRESERVE assertions above test code that no longer runs."
    )
    assert "OVERWRITE" not in text, (
        "the buildspec mentions OVERWRITE. It replaces an operator's setting with the new version's"
        "default and the add-on reports ACTIVE afterwards, so nothing surfaces the loss."
    )
    # The only place `update-addon` may appear as a literal is the polling read afterwards, which is
    # a `describe-addon`. A literal mutation in the shell is what this asserts against.
    mutations = [
        line
        for line in text.splitlines()
        if "eks update-addon" in line and "--kind" not in line and not line.lstrip().startswith("#")
    ]
    assert not mutations, (
        f"the buildspec spells its own add-on update: {mutations!r}. Every mutation's argv comes "
        "from `remediate.py` so that the flags carrying this design are under test."
    )


def test_the_addon_path_is_polled_to_a_terminal_status() -> None:
    """`update-addon` returning 0 says nothing about the add-on: the update is asynchronous.

    A lane that treated the API call's exit code as the outcome would report UPGRADED for an add-on
    heading into DEGRADED -- and would then validate pods that had not been replaced yet.
    """
    text = BUILDSPEC.read_text()
    for status in ("ACTIVE", "DEGRADED", "UPDATE_FAILED"):
        assert status in text, (
            f"the buildspec does not read the add-on status {status!r}. An add-on update is "
            "asynchronous; the call's exit code is not the outcome."
        )
