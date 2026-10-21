"""The rollback: what it is allowed to be, and what has to exist before it may run.

    in    one failed upgrade, and the diagnosis bundle captured from it
    out   one argv the build executes, or a refusal

**THE AGENT DECIDES A ROLLBACK AND DOES NOT ASK FIRST.** That is the human's instruction and it
is implemented literally: nothing in this path waits for a person. What is NOT left to judgement
is the ORDER -- `plan_rollback` refuses unless it is handed a complete diagnosis bundle, so the
one thing a rollback can destroy is on disk before the rollback exists as a command.

**THE ROLLBACK IS A COMMAND, RETURNED, NOT A CALL.** These functions build argv and never execute
it. Two reasons, and the second is the one that matters: the buildspec executes it, so the
mutation stays in the one place this design says mutations happen; and a returned argv is
assertable in a test, which is how "the managed add-on path carries PRESERVE" becomes a check
rather than a claim about a YAML file nobody parses.

**`helm rollback` TARGETS A NAMED REVISION, NEVER `0`.** `helm rollback <release>` with no
revision rolls back to "the previous release", which is whatever Helm's history says it is at the
moment the command runs -- and by then this lane has already added a revision, and a failed
upgrade may have added two. Naming the revision the lane recorded BEFORE it mutated anything is
the difference between returning to the version a person approved moving away from and returning
to an arbitrary point in the release's history.
"""

from __future__ import annotations

from .addon_version import AddonVersionUnresolved, is_published_addon_version
from .diagnose import BundleManifest, require_complete


class RollbackRefused(Exception):
    """A rollback was asked for and this code will not build one. Always names why."""


def _refuse_a_version_eks_would_not_take(*, addon: str, to_version: str, direction: str) -> None:
    """Refuse an add-on version that is not spelled the way EKS spells one.

    THE GUARD THAT MAKES THE RESOLUTION A PROPERTY RATHER THAN A HABIT, and it is on BOTH
    directions. The forward path was handed `ReleaseStep.to_version` -- the plan's bare
    `major.minor.patch` -- and passed it through, so `--addon-version 1.23.1` reached the API and
    was rejected: `InvalidParameterException: Addon version specified is not supported`. The
    rollback path was correct only because its value comes from `describe-addon`, which answers in
    EKS's own spelling; there was no formatting code on either side to be inconsistent.

    So the shape is asserted where the argv is built. A caller holding a plan version must resolve
    it against AWS's catalogue first -- `addon_version.resolve_addon_version` -- because the
    `-eksbuild.<n>` ordinal is AWS's own and no string transformation produces it.
    """
    if not is_published_addon_version(to_version):
        raise AddonVersionUnresolved(
            f"{to_version!r} is not how EKS spells a managed add-on version, so the {direction} "
            f"for {addon!r} would be refused by the API as 'Addon version specified is not "
            "supported' -- after the lane had opened and with nothing changed. EKS publishes "
            "`v<major>.<minor>.<patch>-eksbuild.<n>`; an approved step carries "
            "`major.minor.patch`, and the `-eksbuild.<n>` ordinal is AWS's own and is not "
            "derivable. Resolve it against `describe-addon-versions` first."
        )


def plan_rollback(
    *,
    release: str,
    namespace: str,
    to_revision: int,
    bundle: BundleManifest | None,
    timeout: str = "10m",
) -> tuple[str, ...]:
    """The `helm rollback` argv for one release, or a refusal.

    `bundle` IS REQUIRED AND MAY NOT BE `None`, and the signature takes it as an optional
    parameter anyway so the refusal happens HERE with a message rather than at a call site with a
    `TypeError`. A caller that has no bundle has not captured the diagnosis, which is the one
    condition under which this stage must not roll back.
    """
    if bundle is None:
        raise RollbackRefused(
            f"no diagnosis bundle was captured for {release!r}, so there is nothing to read "
            "after the rollback replaces the pods and moves the release revision. Capture the "
            "bundle first -- pod status, the pods' own logs including the previous container, "
            "the namespace's events, helm history and status, and the values and manifest "
            "actually applied -- then roll back."
        )
    if bundle.phase != "after":
        raise RollbackRefused(
            f"the bundle offered for {release!r} is the {bundle.phase!r} bundle. A rollback is "
            "licensed by the diagnosis of the FAILURE, not by the snapshot taken before the "
            "upgrade -- the `before` bundle cannot show what went wrong."
        )
    # Raises `IncompleteBundle` with the gap named. Deliberately NOT caught and re-raised as a
    # RollbackRefused: the two failures want different fixes, and collapsing them would send
    # somebody to look at the rollback when the capture is what broke.
    require_complete(bundle)

    if to_revision < 1:
        raise RollbackRefused(
            f"revision {to_revision} is not a revision. `helm rollback` with no revision means "
            "'whatever is previous now', which after a failed upgrade is not the version a "
            "person approved moving away from."
        )

    return (
        "helm",
        "rollback",
        release,
        str(to_revision),
        "--namespace",
        namespace,
        # WAIT HERE, unlike on the upgrade. On the way forward `--wait` would force pod
        # list/watch into the lane's grant and would delay the diagnosis; on the way back the
        # only question is whether the component came home, and a rollback that returns before
        # the pods are up would report ROLLED_BACK without knowing it.
        "--wait",
        "--timeout",
        timeout,
    )


def plan_addon_rollback(
    *,
    cluster: str,
    region: str,
    addon: str,
    to_version: str,
    configuration_values: str,
    bundle: BundleManifest | None,
) -> tuple[str, ...]:
    """The `eks update-addon` argv that returns a managed add-on to its recorded version.

    SAME GATE AS THE HELM PATH. An add-on update replaces the add-on's pods exactly as a Helm
    rollback does, so the evidence is just as gone, and `describe-addon`'s `health.issues` --
    the one field that says what EKS thought was wrong -- is cleared by a successful update.

    `PRESERVE` AND THE RECORDED `configurationValues`, ON THE WAY BACK TOO. Rolling back with
    `OVERWRITE` would return the add-on to the old VERSION and to the new version's DEFAULTS,
    which is not where it started -- and the whole reason PRESERVE is used on the way forward is
    that this cluster's add-ons carry configuration somebody chose.
    """
    if bundle is None:
        raise RollbackRefused(
            f"no diagnosis bundle was captured for add-on {addon!r}. An add-on update replaces "
            "its pods and clears its health issues, so the evidence goes with it."
        )
    if bundle.phase != "after":
        raise RollbackRefused(
            f"the bundle offered for add-on {addon!r} is the {bundle.phase!r} bundle, which "
            "cannot show what went wrong."
        )
    require_complete(bundle)

    if not to_version:
        raise RollbackRefused(
            f"no previous version was recorded for add-on {addon!r}, so there is no version to "
            "return to. The version is read from `describe-addon` before the update and carried "
            "on the lane payload; its absence is a defect in the capture."
        )
    _refuse_a_version_eks_would_not_take(
        addon=addon, to_version=to_version, direction="rollback"
    )

    argv = [
        "aws",
        "eks",
        "update-addon",
        # EXPLICIT REGION, ALWAYS. A region-scoped EKS call against the wrong default region
        # returns an EMPTY result with no error, so an omitted `--region` does not fail -- it
        # reports that the add-on does not exist, on the one path where that reading is wrong.
        "--region",
        region,
        "--cluster-name",
        cluster,
        "--addon-name",
        addon,
        "--addon-version",
        to_version,
        "--resolve-conflicts",
        "PRESERVE",
    ]
    if configuration_values:
        argv += ["--configuration-values", configuration_values]
    return tuple(argv)


def plan_addon_update(
    *,
    cluster: str,
    region: str,
    addon: str,
    to_version: str,
    configuration_values: str,
) -> tuple[str, ...]:
    """The forward `eks update-addon` argv. No bundle gate: nothing has been destroyed yet.

    `--resolve-conflicts PRESERVE` AND THE ADD-ON'S OWN `configurationValues`, CARRIED FORWARD.
    This is the managed-add-on expression of requirement 4 -- read the existing configuration,
    move the version, re-apply the same configuration. `OVERWRITE` would replace a field an
    operator set on the cluster with the new version's default, silently, which is precisely the
    loss the Helm path avoids by re-supplying `helm get values`.

    THE COST OF `PRESERVE`, STATED HERE SO IT IS NOT DISCOVERED LATER: if the new add-on version
    REQUIRES a changed default, PRESERVE keeps the old value and the add-on may not work. That
    shows up as `health.issues` from `describe-addon`, which is why that read is in the diagnosis
    bundle rather than being left to a console visit.

    `to_version` MUST ALREADY BE A VERSION EKS PUBLISHES. This function used to take whatever the
    lane payload carried, which is the approved step's `major.minor.patch`, and that is the one
    string the API will not accept. See `_refuse_a_version_eks_would_not_take`.
    """
    _refuse_a_version_eks_would_not_take(
        addon=addon, to_version=to_version, direction="update"
    )
    argv = [
        "aws",
        "eks",
        "update-addon",
        "--region",
        region,
        "--cluster-name",
        cluster,
        "--addon-name",
        addon,
        "--addon-version",
        to_version,
        "--resolve-conflicts",
        "PRESERVE",
    ]
    if configuration_values:
        argv += ["--configuration-values", configuration_values]
    return tuple(argv)


def plan_upgrade(
    *,
    release: str,
    chart: str,
    version: str,
    namespace: str,
    values_file: str,
    history_max: int,
    timeout: str = "10m",
) -> tuple[str, ...]:
    """The forward `helm upgrade` argv: the same values, a moved version, and nothing else.

    `-f <values_file>` IS THE WHOLE OF REQUIREMENT 4. The file holds `helm get values` for the
    release, verbatim, so every parameter the release already had is re-supplied and only
    `--version` moves. On this cluster that carries `args: [--kubelet-insecure-tls]` forward for
    metrics-server, which is the difference between a working component and a broken one.

    NO `--atomic`, AND ITS ABSENCE IS LOAD-BEARING. `--atomic` rolls back automatically when the
    upgrade fails, INSIDE Helm, before anything has read the pods or their logs -- so it would
    destroy the evidence the human specifically asked to have captured. The rollback in this
    module does the same job in the right order.

    NO `--wait`, for a smaller reason: it makes Helm watch Pods and ReplicaSets, which would put
    pod `list`/`watch` into the lane's RBAC grant for no gain -- readiness is validated afterwards
    by the read-only identity that already holds those verbs.

    NO `--reset-values` AND NO `--reuse-values`. `--reuse-values` would make the values a
    function of what Helm remembers rather than of the file this lane wrote, and `--reset-values`
    would DISCARD the user-supplied values this stage exists to preserve. Supplying `-f`
    explicitly is the only one of the three that does what requirement 4 asks.

    `--history-max` MATCHES THE FENCE. The lane's RBAC names release-secret revisions up to a
    bound; letting Helm keep more history than the fence names would leave a revision the lane
    cannot prune, and pruning is a `delete` on a name the fence must already carry.
    """
    if history_max < 1:
        raise RollbackRefused(
            "history_max must be at least 1: Helm needs one stored revision to roll back to."
        )
    return (
        "helm",
        "upgrade",
        release,
        chart,
        "--version",
        version,
        "--namespace",
        namespace,
        "--values",
        values_file,
        "--history-max",
        str(history_max),
        "--timeout",
        timeout,
    )
