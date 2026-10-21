"""The lane's command line. What the buildspec calls, so the tested code is the code that runs.

    python -m eks_addon_readiness.upgrade_lane fence-check    --component C --release R ...
    python -m eks_addon_readiness.upgrade_lane validate       --pods P --logs D ...
    python -m eks_addon_readiness.upgrade_lane bundle-seal    --dir D --phase after ...
    python -m eks_addon_readiness.upgrade_lane command        --kind helm-upgrade ...
    python -m eks_addon_readiness.upgrade_lane resolve-addon-version --addon A --wanted V ...
    python -m eks_addon_readiness.upgrade_lane resolve-chart-version --chart-ref R ...
    python -m eks_addon_readiness.upgrade_lane running-version --pods pods.json

**WHY THE BUILDSPEC ASKS FOR THE COMMAND INSTEAD OF SPELLING IT.** `command` prints the argv that
`remediate.py` built. So the flags that matter -- `--resolve-conflicts PRESERVE`, the absence of
`--atomic`, the named rollback revision, `-f values.yaml` -- are decided by tested Python and not
by a shell line in a YAML file that no test parses. A buildspec that spelled its own `helm
upgrade` would be a second implementation of requirement 4, free to drift from the one under test.

**EVERY FAILURE EXITS NON-ZERO WITH THE REASON ON STDERR.** CodeBuild fails a phase on a non-zero
exit, and a phase failure is what the state machine reads as a refusal. So there is no path here
that reports a problem and exits 0 -- that would be the silent skip this project keeps naming.

EXIT CODES. 0 success; 2 a refusal this code made deliberately (the fence said no, the bundle is
incomplete, a rollback was asked for without evidence, a version is not one EKS publishes); 1
anything else, including an unreadable input. Two codes rather than one because a refusal is a
decision and a crash is not, and the report says different things about them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .addon_version import AddonVersionUnresolved, resolve_from_file
from .chart_version import ChartVersionUnresolved, resolve_oci_from_file
from .chart_version import resolve_from_file as resolve_chart_from_file
from .diagnose import BundleFile, BundleManifest, IncompleteBundle, require_complete
from .fence import (
    LaneFence,
    OffLaneObject,
    image_tag,
    refuse_a_manifest_that_does_not_ship,
    refuse_a_render_this_code_cannot_read,
)
from .remediate import (
    RollbackRefused,
    plan_addon_rollback,
    plan_addon_update,
    plan_rollback,
    plan_upgrade,
)
from .validate import floor_for, notable_log_lines, pods_from_kubectl

REFUSAL = 2


def _load_yaml_documents(path: Path) -> list[dict[str, Any]]:
    """Every non-empty document in a multi-document YAML file.

    `yaml` IS IMPORTED HERE RATHER THAN AT MODULE SCOPE, AND IT IS THE ONLY YAML IMPORT IN THIS
    PACKAGE. `fence.py` and `validate.py` take already-parsed mappings, so the whole fence is
    exercised by the unit suite in an environment with no PyYAML at all -- which is what keeps this
    stage out of `pyproject.toml`: no dependency is added, no extra is declared, and `uv.lock` does
    not move. PyYAML is pinned and installed by the buildspec's `install` phase, beside the pinned
    pydantic, because CodeBuild is the only place it is ever needed.
    """
    # Imported inside the function, deliberately -- see the docstring above.
    import yaml

    documents: list[dict[str, Any]] = []
    for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
        # `helm template` emits an empty document for every template that renders to nothing.
        # Those are dropped HERE and nowhere else: `refuse_a_render_this_code_cannot_read` treats a
        # mapping with no `kind` as a refusal, deliberately, so the drop has to happen where the
        # emptiness is unambiguous rather than inside the fence.
        if isinstance(document, dict) and document:
            documents.append(document)
    return documents


def _cmd_fence_check(args: argparse.Namespace) -> int:
    """Refuse a rendered manifest this code cannot read, or that does not ship the approved version.

    THE LANE ARRIVES ON FLAGS, NOT IN A FILE. There used to be a committed `lanes/<component>.json`
    per component, generated from that release's own manifest, which also produced the RBAC. Nothing
    per-component is left to commit: a lane IS its release name and namespace, both of which the
    state machine already knows. Which kinds, labels or namespaces the render carries is not judged
    here -- see `fence.py` for what bounds the lane instead.
    """
    fence = LaneFence(
        component=args.component,
        release=args.release,
        namespace=args.namespace,
        managed_addon=args.managed_addon,
    )
    documents = _load_yaml_documents(Path(args.manifest))
    if not documents:
        print(
            f"refused: {args.manifest} rendered no objects at all. An upgrade that would apply "
            "nothing is not an upgrade, and proceeding would report success for a move that did "
            "not happen.",
            file=sys.stderr,
        )
        return REFUSAL
    refuse_a_render_this_code_cannot_read(documents)
    kinds = sorted({str(d.get("kind")) for d in documents})
    print(
        f"{len(documents)} object(s) rendered for the {fence.component} lane: {', '.join(kinds)}."
    )
    if args.application_version:
        # THE MANIFEST MUST SHIP WHAT WAS APPROVED. Rendered with the release's existing values,
        # a pinned `image.tag` would move the chart and leave the component where it is -- and
        # everything downstream would call that healthy. Refused here, before the mutation.
        image = refuse_a_manifest_that_does_not_ship(documents, args.application_version)
        print(f"-- {image} carries application version {args.application_version}")
    return 0


def _cmd_running_version(args: argparse.Namespace) -> int:
    """Print the application version(s) the component's pods are actually running.

    THE REPORT'S `resulting`, READ OFF THE CLUSTER rather than echoed from the request. `applied`
    says what was asked for; this says what runs, from the image tags of the containers in the
    captured `pods.json`. When they agree the report shows one version; when they do not -- a
    rollback, a pinned image the pre-mutation check somehow missed, a partial rollout -- the
    report shows both, which is exactly the case a person needs to see.

    ONE LINE ON STDOUT: the distinct tags, comma-separated, or nothing when no pod carries one.
    Exits 0 either way; this is an observation, not a verdict.
    """
    payload = json.loads(Path(args.pods).read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else None
    tags: list[str] = []
    for pod in items or ():
        spec = pod.get("spec") if isinstance(pod, dict) else None
        for container in (spec or {}).get("containers") or ():
            image = container.get("image") if isinstance(container, dict) else None
            tag = image_tag(image) if isinstance(image, str) else ""
            if tag and tag not in tags:
                tags.append(tag)
    print(",".join(tags))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    """The FLOOR: what the cluster states outright about these pods. Not a verdict.

    THE AGENT JUDGES HEALTH; THIS DOES NOT. `validate.py`'s header gives the argument in full. What
    comes out here is a short list of facts the API server has already decided -- a container in
    CrashLoopBackOff, an OOMKill, a pod in phase Failed, no pods where the manifest declares a
    workload -- plus the counts the agent is shown as evidence.

    EXITS 0 EITHER WAY, unlike everywhere else in this file: a broken component is a finding this
    stage handles, not a build that failed. A crash reading the inputs still exits 1.
    """
    pods_readable = True
    pods: tuple[Any, ...] = ()
    try:
        payload = json.loads(Path(args.pods).read_text(encoding="utf-8"))
        pods = pods_from_kubectl(payload)
    except (OSError, json.JSONDecodeError) as problem:
        pods_readable = False
        print(f"pod list unreadable: {problem}", file=sys.stderr)

    log_text = ""
    logs_dir = Path(args.logs)
    if logs_dir.is_dir():
        for log_file in sorted(logs_dir.rglob("*.log")):
            try:
                log_text += log_file.read_text(encoding="utf-8", errors="replace")
            except OSError as problem:
                print(f"log unreadable: {log_file}: {problem}", file=sys.stderr)

    floor = floor_for(
        pods=pods,
        restarts_before=args.restarts_before,
        pods_readable=pods_readable,
        expect_pods=not args.no_pods_expected,
    )
    payload = json.loads(floor.model_dump_json())
    # The lines worth putting in front of the agent. CARRIED, NOT BRANCHED ON -- see
    # `validate.NOTABLE_LOG_PATTERNS`: the moment something decides on this it has become a verdict
    # again, which is the mistake this whole module was rewritten to undo.
    payload["notable_log_lines"] = list(notable_log_lines(log_text))
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_bundle_seal(args: argparse.Namespace) -> int:
    """Index every file in a bundle directory and write the manifest that licenses a rollback.

    THE MANIFEST IS WRITTEN LAST AND IT IS WRITTEN HERE, which is what makes the ordering a
    property rather than a habit: the buildspec cannot produce a manifest before it has captured
    the files, because this command builds the manifest FROM the files on disk.
    """
    root = Path(args.dir)
    if not root.is_dir():
        print(f"no bundle directory at {root}", file=sys.stderr)
        return 1

    commands: dict[str, str] = {}
    statuses: dict[str, int] = {}
    receipts = root / "commands.json"
    if receipts.is_file():
        # The buildspec appends one row per read: name, command, exit status. Read here so a
        # DENIED read stays in the manifest as a file with a non-zero status rather than becoming
        # an absence that reads as "there was nothing there".
        try:
            rows = json.loads(receipts.read_text(encoding="utf-8"))
            for row in rows:
                commands[str(row["name"])] = str(row["command"])
                statuses[str(row["name"])] = int(row.get("exit_status", 0))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as problem:
            print(f"commands.json unreadable: {problem}", file=sys.stderr)
            return 1

    files: list[BundleFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in ("manifest.json", "commands.json"):
            continue
        name = str(path.relative_to(root))
        body = path.read_bytes()
        files.append(
            BundleFile(
                name=name,
                bytes_written=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                command=commands.get(name, "unrecorded"),
                exit_status=statuses.get(name, 0),
            )
        )

    if not files:
        print(
            f"refused: {root} holds no captured files, so there is nothing to seal. A bundle "
            "with no contents must not become a manifest that licenses a rollback.",
            file=sys.stderr,
        )
        return REFUSAL

    manifest = BundleManifest(
        run_id=args.run_id,
        component=args.component,
        cluster=args.cluster,
        phase=args.phase,
        managed_addon=args.managed_addon,
        files=tuple(files),
        captured_at=datetime.now(UTC),
    )
    (root / "manifest.json").write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    if args.require_complete:
        require_complete(manifest)
    print(
        f"sealed {len(files)} file(s) into the {manifest.phase} bundle for {manifest.component}"
        + (f"; reads that failed: {list(manifest.failed_reads)}" if manifest.failed_reads else "")
    )
    return 0


def _cmd_command(args: argparse.Namespace) -> int:
    """Print the argv for one mutation, built by `remediate.py`. One line, shell-quoted."""
    # Only this subcommand needs it.
    import shlex

    bundle = None
    if args.bundle:
        bundle = BundleManifest.model_validate_json(Path(args.bundle).read_text(encoding="utf-8"))

    if args.kind == "helm-upgrade":
        argv: Sequence[str] = plan_upgrade(
            release=args.release,
            chart=args.chart,
            version=args.version,
            namespace=args.namespace,
            values_file=args.values_file,
            history_max=args.history_max,
        )
    elif args.kind == "helm-rollback":
        argv = plan_rollback(
            release=args.release,
            namespace=args.namespace,
            to_revision=args.to_revision,
            bundle=bundle,
        )
    elif args.kind == "addon-update":
        argv = plan_addon_update(
            cluster=args.cluster,
            region=args.region,
            addon=args.addon,
            to_version=args.version,
            configuration_values=args.configuration_values,
        )
    elif args.kind == "addon-rollback":
        argv = plan_addon_rollback(
            cluster=args.cluster,
            region=args.region,
            addon=args.addon,
            to_version=args.version,
            configuration_values=args.configuration_values,
            bundle=bundle,
        )
    else:  # pragma: no cover -- argparse `choices` already refused it.
        print(f"unknown command kind {args.kind!r}", file=sys.stderr)
        return 1

    print(" ".join(shlex.quote(part) for part in argv))
    return 0


def _cmd_resolve_addon_version(args: argparse.Namespace) -> int:
    """Print the add-on version EKS publishes for the release the approved step named.

    ONE LINE ON STDOUT AND NOTHING ELSE, because the buildspec redirects it into a file the build
    phase reads. A refusal goes to stderr with a non-zero exit, which in the `pre_build` phase --
    the one that carries `set -e` -- stops the run BEFORE any mutation. That is the fail-closed
    direction: a version that cannot be resolved must not reach the API, where it costs a rejected
    call, a full health window and a rollback on a component nothing touched.
    """
    print(resolve_from_file(addon=args.addon, wanted=args.wanted, catalogue_path=args.catalogue))
    return 0


def _cmd_resolve_chart_version(args: argparse.Namespace) -> int:
    """Print the chart version the repository publishes for the approved application version.

    `_cmd_resolve_addon_version`'s twin for Helm: one line on stdout for the buildspec to read
    into a file, a refusal on stderr with exit 2 -- which in `pre_build` stops the run before
    `helm template`, the fence and the mutation.
    """
    print(
        resolve_chart_from_file(
            chart_ref=args.chart_ref,
            application_version=args.application_version,
            search_path=args.search,
        )
    )
    return 0


def _cmd_resolve_oci_chart_version(args: argparse.Namespace) -> int:
    """`_cmd_resolve_chart_version` for an OCI chart: confirm the pulled `Chart.yaml` ships it."""
    print(
        resolve_oci_from_file(
            chart_ref=args.chart_ref,
            application_version=args.application_version,
            chart_yaml_path=args.chart_yaml,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eks_addon_readiness.upgrade_lane")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("fence-check", help="refuse a manifest that leaves the lane")
    check.add_argument("--component", required=True)
    check.add_argument("--release", required=True)
    check.add_argument("--namespace", required=True)
    check.add_argument("--managed-addon", action="store_true")
    check.add_argument("--manifest", required=True)
    check.add_argument(
        "--application-version", help="refuse unless a rendered workload image carries it"
    )

    running = sub.add_parser(
        "running-version", help="the image tag(s) the component's pods run, from pods.json"
    )
    running.add_argument("--pods", required=True)
    running.set_defaults(handler=_cmd_running_version)
    check.set_defaults(handler=_cmd_fence_check)

    validate = sub.add_parser("validate", help="pod status and pod logs into one verdict")
    validate.add_argument("--pods", required=True)
    validate.add_argument("--logs", required=True)
    validate.add_argument("--restarts-before", type=int, default=0)
    validate.add_argument("--no-pods-expected", action="store_true")
    validate.set_defaults(handler=_cmd_validate)

    seal = sub.add_parser("bundle-seal", help="write the manifest that licenses a rollback")
    seal.add_argument("--dir", required=True)
    seal.add_argument("--run-id", required=True)
    seal.add_argument("--component", required=True)
    seal.add_argument("--cluster", required=True)
    seal.add_argument("--phase", required=True, choices=["before", "after"])
    seal.add_argument("--managed-addon", action="store_true")
    seal.add_argument("--require-complete", action="store_true")
    seal.set_defaults(handler=_cmd_bundle_seal)

    resolve = sub.add_parser(
        "resolve-addon-version", help="the version EKS publishes for an approved release"
    )
    resolve.add_argument("--addon", required=True)
    resolve.add_argument("--wanted", required=True)
    resolve.add_argument("--catalogue", required=True)
    resolve.set_defaults(handler=_cmd_resolve_addon_version)

    chart = sub.add_parser(
        "resolve-chart-version",
        help="the chart version the repository publishes for an approved application version",
    )
    chart.add_argument("--chart-ref", required=True)
    chart.add_argument("--application-version", required=True)
    chart.add_argument("--search", required=True, help="`helm search repo -o json` output file")
    chart.set_defaults(handler=_cmd_resolve_chart_version)

    oci = sub.add_parser(
        "resolve-oci-chart-version",
        help="confirm an OCI chart's Chart.yaml ships the approved application version",
    )
    oci.add_argument("--chart-ref", required=True)
    oci.add_argument("--application-version", required=True)
    oci.add_argument("--chart-yaml", required=True, help="`helm show chart` output file")
    oci.set_defaults(handler=_cmd_resolve_oci_chart_version)

    command = sub.add_parser("command", help="print the argv for one mutation")
    command.add_argument(
        "--kind",
        required=True,
        choices=["helm-upgrade", "helm-rollback", "addon-update", "addon-rollback"],
    )
    command.add_argument("--release", default="")
    command.add_argument("--chart", default="")
    command.add_argument("--version", default="")
    command.add_argument("--namespace", default="")
    command.add_argument("--values-file", default="values.yaml")
    command.add_argument("--history-max", type=int, default=20)
    command.add_argument("--to-revision", type=int, default=0)
    command.add_argument("--cluster", default="")
    command.add_argument("--region", default="")
    command.add_argument("--addon", default="")
    command.add_argument("--configuration-values", default="")
    command.add_argument("--bundle", default="")
    command.set_defaults(handler=_cmd_command)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        OffLaneObject,
        IncompleteBundle,
        RollbackRefused,
        AddonVersionUnresolved,
        ChartVersionUnresolved,
    ) as refusal:
        # A DELIBERATE REFUSAL, distinguished from a crash by its exit code. Every one of these
        # means the lane declined to mutate the cluster, which is the outcome the report calls
        # REFUSED_BEFORE_MUTATION or REFERRED -- not an error in this program.
        print(f"refused: {refusal}", file=sys.stderr)
        return REFUSAL


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
