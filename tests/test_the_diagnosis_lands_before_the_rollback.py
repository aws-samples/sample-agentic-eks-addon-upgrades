"""A rollback destroys the evidence, so it does not exist as a command until the evidence is sealed.

WHAT THIS CATCHES. The human's instruction was explicit: the agent may roll back without asking,
but "we need to make sure is capture why it failed". A rollback replaces the pods, so the current
logs go; it moves the release revision, so `helm status` changes; for a managed add-on it clears
`health.issues`, which is the one field that said what EKS thought was wrong. If the rollback runs
first, the component comes back and NOBODY CAN EVER SAY WHY IT BROKE -- and the run looks like a
success with a rollback in it.

An instruction to capture first is worth nothing the first time somebody reorders two lines of a
buildspec. So the order is a data dependency instead: `bundle-seal` builds the manifest FROM the
files on disk, and `plan_rollback` refuses unless it is handed a sealed, COMPLETE `after` manifest.
There is therefore no way to obtain the rollback command before the diagnosis exists.

THREE LAYERS ARE ASSERTED HERE because each can regress alone: the refusal in `remediate.py`, the
completeness gate in `diagnose.py`, and the ordering in the buildspec that calls them.

Deliberately no AWS call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eks_addon_readiness.upgrade_lane.diagnose import (
    REQUIRED_FILES,
    REQUIRED_FILES_MANAGED_ADDON,
    BundleFile,
    BundleManifest,
    IncompleteBundle,
    missing_from,
    require_complete,
)
from eks_addon_readiness.upgrade_lane.remediate import (
    RollbackRefused,
    plan_addon_rollback,
    plan_rollback,
)

BUILDSPEC = (
    Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/upgrade_buildspec.yml"
)


def _commands() -> str:
    """The buildspec with every `#` comment line removed.

    LOAD-BEARING, NOT TIDINESS, and the first version of this file got it wrong in both directions.
    The buildspec's header explains that `--atomic` is deliberately ABSENT and that
    `command --kind helm-rollback` refuses an unsealed bundle -- so a scan of the raw text finds
    `--atomic` in a sentence saying it is not used, and finds `--kind helm-rollback` in a comment
    ABOVE the seal it is supposed to follow. Both failed against a buildspec that was exactly
    right, which is a red test proving nothing: worse here than a green one, because the next person
    fixes the buildspec.

    So the comments come out and only the shell is read.
    `test_the_absence_of_atomic_is_still_explained`
    keeps the prose honest separately, which is the right division: one test reads what RUNS, the
    other reads what a person is TOLD.
    """
    return "\n".join(
        line for line in BUILDSPEC.read_text().splitlines() if not line.lstrip().startswith("#")
    )


def _file(name: str) -> BundleFile:
    return BundleFile(
        name=name,
        bytes_written=len(name),
        sha256="a" * 64,
        command=f"kubectl get {name}",
    )


def _manifest(
    *,
    phase: str = "after",
    managed_addon: bool = False,
    names: tuple[str, ...] | None = None,
) -> BundleManifest:
    if names is None:
        required = REQUIRED_FILES_MANAGED_ADDON if managed_addon else REQUIRED_FILES
        names = (*required, "logs/metrics-server-abc-def.metrics-server.current.log")
    return BundleManifest(
        run_id="toast-191932",
        component="metrics-server",
        cluster="my-eks-cluster",
        phase=phase,  # type: ignore[arg-type]
        managed_addon=managed_addon,
        files=tuple(_file(name) for name in names),
        captured_at=datetime.now(UTC),
    )


def test_a_rollback_with_no_bundle_is_refused() -> None:
    """The case the whole gate exists for, and the refusal names what should have been captured."""
    with pytest.raises(RollbackRefused) as refusal:
        plan_rollback(
            release="metrics-server",
            namespace="kube-system",
            to_revision=1,
            bundle=None,
        )
    message = str(refusal.value)
    for expected in ("previous container", "events", "helm history"):
        assert expected in message, (
            f"the refusal does not mention {expected!r}: {message!r}. A refusal that does not say "
            "what to capture leaves the next person guessing at the thing that blocked them."
        )


def test_a_rollback_offered_the_before_bundle_is_refused() -> None:
    """The `before` bundle cannot show what went wrong, so it does not license a rollback.

    The subtle version of the defect: a buildspec that captured a bundle at the start of the run and
    then handed THAT to the rollback would satisfy a naive "is there a bundle" check while the
    evidence of the failure was still uncaptured.
    """
    with pytest.raises(RollbackRefused, match="'before' bundle"):
        plan_rollback(
            release="metrics-server",
            namespace="kube-system",
            to_revision=1,
            bundle=_manifest(phase="before"),
        )


def test_a_rollback_offered_an_incomplete_bundle_is_refused() -> None:
    """Sealed is not enough: the bundle has to hold what a person needs to read.

    And the two failures are kept DISTINCT -- this raises `IncompleteBundle`, not
    `RollbackRefused` -- because they want different fixes. Collapsing them would send somebody to
    look at the rollback when the capture is what broke.
    """
    with pytest.raises(IncompleteBundle) as refusal:
        plan_rollback(
            release="metrics-server",
            namespace="kube-system",
            to_revision=1,
            bundle=_manifest(names=("pods.json",)),
        )
    assert "logs/" in str(refusal.value), (
        "the refusal does not name the missing pod logs. The human asked for pod status AND pod "
        f"logs; a bundle with only status has not answered: {refusal.value}"
    )


def test_a_complete_after_bundle_licenses_the_rollback_and_names_the_revision() -> None:
    """The positive case, and it pins the revision rather than letting Helm choose.

    `helm rollback <release>` with no revision means "whatever is previous NOW", and by this point
    the lane has already added a revision -- possibly two, if the upgrade half-failed. Naming the
    revision recorded before the mutation is the only way back to the version a person approved
    moving away from.
    """
    argv = plan_rollback(
        release="metrics-server",
        namespace="kube-system",
        to_revision=1,
        bundle=_manifest(),
    )
    assert argv[:4] == ("helm", "rollback", "metrics-server", "1"), (
        f"the rollback argv is {argv!r}. The revision must be positional and explicit."
    )
    assert "--namespace" in argv and "kube-system" in argv
    assert "--wait" in argv, (
        "the rollback does not wait. On the way back the only question is whether the component "
        "came home, and a rollback that returned before the pods were up would report ROLLED_BACK "
        "without knowing it."
    )


def test_revision_zero_is_refused() -> None:
    """`0` is how "let Helm decide" is spelled, and it is not available here."""
    with pytest.raises(RollbackRefused, match="not a revision"):
        plan_rollback(
            release="metrics-server",
            namespace="kube-system",
            to_revision=0,
            bundle=_manifest(),
        )


def test_the_managed_addon_rollback_is_gated_identically() -> None:
    """An add-on update replaces its pods and clears its health issues, so the evidence goes too.

    Asserted separately because it is a different function, and a gate on one of the two paths is a
    gate on neither -- the add-on path is the one that touches coredns and vpc-cni.
    """
    with pytest.raises(RollbackRefused):
        plan_addon_rollback(
            cluster="my-eks-cluster",
            region="us-east-1",
            addon="coredns",
            to_version="v1.11.4-eksbuild.2",
            configuration_values="",
            bundle=None,
        )


def test_the_managed_addon_bundle_must_carry_the_addons_own_health() -> None:
    """`describe-addon.json` is where `health.issues` lives, and it is the add-on path's "
    "evidence."""
    manifest = _manifest(
        managed_addon=True, names=("pods.json", "events.json", "logs/coredns.coredns.current.log")
    )
    assert "describe-addon.json" in missing_from(manifest), (
        "a managed add-on bundle without `describe-addon.json` is treated as complete. "
        "`health.issues` is the one field that says what EKS thought was wrong, and PRESERVE can "
        "leave a configuration the new version needs changed -- that is where it shows up."
    )
    with pytest.raises(IncompleteBundle):
        require_complete(manifest)


def test_a_bundle_with_no_pod_logs_is_incomplete() -> None:
    """Pod status and pod logs, both, because that is what was asked for."""
    manifest = _manifest(names=REQUIRED_FILES)
    assert missing_from(manifest) == ("logs/",), (
        f"a bundle with no pod log at all is reported as {missing_from(manifest)!r}. The logs are "
        "half of the requirement, and a crash-looping container's reason is only in the previous "
        "container's log."
    )


def test_a_failed_read_stays_in_the_bundle_as_a_file_with_a_status() -> None:
    """A DENIED read and an EMPTY one must not look the same.

    An absent file reads as "there was nothing there", which on a diagnosis is a lie. So a read that
    returned non-zero is still a member of the manifest, and `failed_reads` names it.
    """
    manifest = BundleManifest(
        run_id="toast-191932",
        component="metrics-server",
        cluster="my-eks-cluster",
        phase="after",
        files=(
            _file("pods.json"),
            BundleFile(
                name="events.json",
                bytes_written=120,
                sha256="b" * 64,
                command="kubectl get events -n kube-system",
                exit_status=1,
            ),
        ),
        captured_at=datetime.now(UTC),
    )
    assert manifest.failed_reads == ("events.json",)
    assert "events.json" in manifest.names, (
        "a failed read was dropped from the manifest. An absence reads as 'nothing happened', "
        "which on the run where the events are the whole story is the worst available reading."
    )


def test_the_buildspec_seals_the_bundle_before_it_asks_for_the_rollback() -> None:
    """The ordering, where it actually lives.

    The two library gates above are unreachable if the buildspec never calls them, and a reordered
    buildspec would still pass every test in this file up to here. So the text is read: the seal of
    the `after` bundle must appear before the `helm-rollback` command is requested.
    """
    text = _commands()
    seal = text.index("--phase after")
    rollback = text.index("--kind helm-rollback")
    assert seal < rollback, (
        "the buildspec asks for the rollback command before it seals the `after` diagnosis bundle. "
        "The library would refuse it -- `plan_rollback` requires a sealed manifest -- but the run "
        "would then report REFERRED for a component it could have returned, so the order is wrong "
        "in the buildspec and not only in principle."
    )
    assert "--require-complete" in text[:rollback], (
        "the `after` bundle is sealed without `--require-complete`, so an incomplete bundle would "
        "license a rollback that leaves nothing to read."
    )


def test_the_buildspec_never_passes_atomic_to_helm_upgrade() -> None:
    """`--atomic` rolls back inside Helm, before anything has read the pods.

    It is the one flag that would defeat this entire file: the component would come back and the
    diagnosis would never have existed. Asserted against the buildspec AND the library, because the
    flag could be reintroduced in either.
    """
    from eks_addon_readiness.upgrade_lane.remediate import plan_upgrade

    argv = plan_upgrade(
        release="metrics-server",
        chart="metrics-server/metrics-server",
        version="3.14.0",
        namespace="kube-system",
        values_file="/tmp/values.yaml",
        history_max=20,
    )
    assert "--atomic" not in argv, (
        f"`plan_upgrade` emits --atomic: {argv!r}. It rolls back automatically on failure, inside "
        "Helm, before any diagnosis runs -- which destroys the evidence this stage was asked to "
        "capture."
    )
    assert "--atomic" not in _commands(), (
        "the buildspec passes --atomic. It rolls back inside Helm, on failure, before any diagnosis"
        "runs -- so the component comes back and nobody can ever say why it broke."
    )
    assert "--values" in argv and "/tmp/values.yaml" in argv, (
        f"`plan_upgrade` does not re-supply the release's own values: {argv!r}. That is the whole "
        "of requirement 4 -- the version moves and no parameter is lost."
    )
    assert "--reuse-values" not in argv and "--reset-values" not in argv, (
        f"`plan_upgrade` emits a values-reuse flag: {argv!r}. `--reuse-values` makes the values a "
        "function of what Helm remembers rather than of the file this lane wrote, and "
        "`--reset-values` discards the user-supplied values the stage exists to preserve."
    )


def test_the_absence_of_atomic_is_still_explained_in_the_buildspec() -> None:
    """The flag is absent; the REASON has to be present, or somebody will add it back.

    `--atomic` is the obvious thing to reach for when an upgrade fails and the operator wants the
    component back. It is also the one flag that defeats this entire file. A comment naming it is
    what turns "we forgot" into "we decided", and it is asserted because the comment is the only
    thing standing between the next reader and a one-word change.
    """
    text = BUILDSPEC.read_text()
    assert "--atomic" in text, (
        "the buildspec no longer mentions `--atomic` at all. Its absence is deliberate and needs to"
        "say so: it rolls back inside Helm before the diagnosis is captured, which is exactly what "
        "this stage was asked not to do."
    )
