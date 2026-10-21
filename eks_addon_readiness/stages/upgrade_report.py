"""The report stage: read every lane's own account, and hand the human one document.

    in    one lane result object per approved component, under the run's own prefix
    out   one `UpgradeRunReport`, and a count of what a person must look at

**A COMPONENT WITH NO RESULT OBJECT IS REPORTED, NOT OMITTED.** That is the whole reason this
stage reads S3 rather than the state payload. A build that died before it could write anything --
a CodeBuild agent failure, a `.sync` timeout, a lane that never opened -- leaves no result, and
omitting it would make the report say the run touched fewer components than it was authorised to.
So the approved step list is the spine and the results are joined onto it; a missing result becomes
a REFERRED row that says so.

**IT DOES NOT MUTATE AND IT DOES NOT DECIDE.** Each lane already decided its own status, in
CodeBuild, with the cluster in front of it. This stage reconciles and counts. That split is what
keeps the report readable as evidence: nothing here can turn a REFERRED lane into a green row,
because nothing here knows anything the lane did not write down.

**IT NEVER RAISES FOR A CONDITION OF THE WORLD.** An unreadable result object, a malformed one, a
prefix with nothing under it -- each becomes a REFERRED row with the reason. The report is the one
artifact the human is waiting for, so a stage that failed rather than reported would lose the very
thing it exists to produce. What it WILL refuse is a missing parameter, because that is a
deployment defect and not a state of the cluster.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..models import ClusterReference
from ..snapshot import ClusterSnapshot
from ..storage import documents
from ..storage import keys as storage_keys
from ..upgrade import Release
from ..upgrade_lane import keys
from ..upgrade_lane.applicable import catalogue_index, not_applicable_reason
from ..upgrade_lane.fence import LANE_GROUP
from ..upgrade_lane.models import (
    LaneOutcome,
    TriageVerdict,
    UpgradeRunReport,
    UpgradeStatus,
    UpgradeTriage,
)
from . import settings

Event = dict[str, Any]
Result = dict[str, Any]


def _release(event: Event, *, bucket: str, cluster: str, run_id: str) -> Release:
    """The run's authorisation, read back from the object `Release recorded` wrote.

    **READ FROM S3, NOT REBUILT FROM THE PAYLOAD, AND THAT IS THE WHOLE POINT OF HANGING THIS STAGE
    OFF `Release recorded`.** The gate payload carries step ORDERS and `approver_decisions` as JSON
    TEXT; turning that into a `Release` needs the Stage 5 plan and the Stage 2 inventory, which
    `release_prep.py` and `release_record.py` already did. Re-deriving it here would be a second
    implementation of the same join -- free to disagree with the one that was recorded, and the
    disagreement would be between two accounts of who authorised what.

    SO THE DOCUMENT IS THE AUTHORISATION. The lane planner reads the object under
    `releases/cluster=.../run=.../release.json`, validates it against this contract, and acts on
    it. A run whose authorisation could not be recorded never reaches this stage at all, because
    `Release recorded` has no `Catch` -- which makes "the lanes only act on a durable
    authorisation" a property of the graph rather than a check here.

    IT REFUSES RATHER THAN DEGRADES. A missing object, an unparseable one, or one that fails the
    contract is a `MissingParameter` in plan mode -- no lane opens -- and a reported row in report
    mode, because by then a cluster may already have changed.
    """
    uri = documents.build_uri(bucket, storage_keys.release_key(cluster, run_id))
    try:
        body = documents.read_text(uri)
    except Exception as problem:
        raise settings.MissingParameter(
            f"the release recorded at {uri} could not be read ({type(problem).__name__}), so "
            "nothing says this run was authorised or what it authorised. `Release recorded` writes "
            "it on the only path that reaches this stage."
        ) from problem
    return Release.model_validate_json(body)


def _lane_rows(release: Release, event: Event) -> list[dict[str, Any]]:
    """One row per AUTHORISED step, joined onto whatever the Map reported for it.

    THE AUTHORISED LIST IS THE SPINE, NOT THE APPROVED-INTENT LIST. `upgrade_approval` records what
    a person wanted; `Release.approved_steps` is what a named AWS principal then authorised. The
    stage may only act on the second, and using it here rather than the first is what makes that
    true of the lanes as well as of the report.

    AND NOT THE MAP'S OUTPUT EITHER, because the Map's output is exactly the thing that goes
    missing when a lane fails hard. A step with no row becomes a REFERRED component that says so.
    """
    lanes = event.get("lanes") or []
    by_order = {}
    for lane in lanes:
        if isinstance(lane, dict) and isinstance(lane.get("step_order"), int):
            by_order[lane["step_order"]] = lane
    # `approved_steps` IS A TUPLE OF `ReleaseStep` OBJECTS, NOT OF ORDERS, and it changed shape on
    # main while this stage was being built. The object carries the component, the target version
    # and the install method, so this join no longer needs the plan or the catalogue to say what
    # step 4 moves -- which is exactly the second read `ReleaseStep`'s docstring exists to remove.
    return [
        {
            "step_order": step.order,
            "component": step.component or "",
            **by_order.get(step.order, {}),
        }
        for step in release.approved_steps
    ]


def _outcome_from(
    payload: dict[str, Any], *, cluster: ClusterReference, run_id: str, step_order: int
) -> LaneOutcome:
    status = payload.get("status")
    try:
        parsed = UpgradeStatus(status)
    except ValueError:
        # A status this code does not recognise is a REFERRAL, not a guess. The lane wrote
        # something; reading it as success would be the worst available reading.
        return LaneOutcome(
            cluster=cluster,
            run_id=run_id,
            step_order=step_order,
            component=str(payload.get("component") or f"step-{step_order}"),
            status=UpgradeStatus.REFERRED,
            attempted=("the lane wrote a result object",),
            observed=(f"its status was {status!r}, which this stage does not recognise",),
            finished_at=datetime.now(UTC),
        )

    rollback_command = str(payload.get("rollback_command") or "")
    checkpoints, triage, revalidation = _checkpoints_from(
        payload.get("checkpoints"), rolled_back=bool(rollback_command)
    )
    observed = tuple(str(part) for part in payload.get("observed") or ())

    return LaneOutcome(
        cluster=cluster,
        run_id=run_id,
        step_order=step_order,
        component=str(payload.get("component") or f"step-{step_order}"),
        status=parsed,
        installed=str(payload.get("installed") or ""),
        applied=str(payload.get("applied") or ""),
        resulting=str(payload.get("resulting") or ""),
        managed_addon=bool(payload.get("managed_addon")),
        attempted=tuple(str(part) for part in payload.get("attempted") or ()),
        observed=observed,
        diagnosis_before_uri=str(payload.get("diagnosis_before_uri") or ""),
        diagnosis_after_uri=str(payload.get("diagnosis_after_uri") or ""),
        checkpoints=checkpoints,
        triage=triage,
        rollback_command=rollback_command,
        rollback_succeeded=_rollback_succeeded(observed) if rollback_command else None,
        revalidation=revalidation,
        cleanup_error=str(payload.get("cleanup_error") or ""),
        finished_at=datetime.now(UTC),
    )


def _checkpoints_from(
    raw: Any, *, rolled_back: bool
) -> tuple[tuple[UpgradeTriage, ...], UpgradeTriage | None, UpgradeTriage | None]:
    """The lane's checkpoint log as typed triages, plus which one decided and which re-validated.

    THE LANE WRITES WRAPPERS, NOT TRIAGES. Each line of `checkpoints.jsonl` is
    `{checkpoint, elapsed_seconds, verdict, proposed_action, triage, reason}` -- the triage the
    agent returned sits under `triage`, and it is `null` when the agent was unavailable or its
    answer was refused (`reason` then says why). The first end-to-end report passed the list
    through untouched, `LaneOutcome.checkpoints` refused the shape, and every row rendered as
    "not judged" while the agent's full HEALTHY reading sat in S3. This reads the shape the lane
    actually writes.

    WHICH CHECKPOINT DECIDED. On a lane that did not roll back, the last one. On a lane that did,
    the first UNHEALTHY or REFER is what fired the rollback, and anything after it is the
    re-validation the buildspec runs once the previous version is back -- so the last of those is
    `revalidation`. A wrapper with no triage contributes nothing here; its refusal reason is
    already in the lane's `observed` lines.
    """
    if not isinstance(raw, list):
        return (), None, None
    parsed: list[UpgradeTriage] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        body = entry.get("triage") if isinstance(entry.get("triage"), dict) else None
        if body is None:
            continue
        try:
            parsed.append(UpgradeTriage.model_validate(body))
        except Exception:
            continue
    if not parsed:
        return (), None, None
    if rolled_back:
        for index, triage in enumerate(parsed):
            if triage.verdict in (TriageVerdict.UNHEALTHY, TriageVerdict.REFER):
                after = parsed[index + 1 :]
                return tuple(parsed), triage, (after[-1] if after else None)
    return tuple(parsed), parsed[-1], None


def _rollback_succeeded(observed: tuple[str, ...]) -> bool | None:
    """What the lane's own observed lines say about the rollback, when they say anything."""
    text = " ".join(observed).lower()
    if "rollback itself failed" in text:
        return False
    if "rollback completed" in text or "rollback was applied" in text:
        return True
    return None




# ---------------------------------------------------------------------------
# Mode `plan`: one work item per approved component, for the Map to fan out over.
#
# EVERYTHING A LANE NEEDS IS ON THE RELEASE RECORD, and reading it is the whole job. This used
# to join the record against `upgrade_lane_catalogue`, a Terraform map somebody maintained by
# hand per component -- release name, namespace, chart repository, an app->chart version table
# -- so a component with an approved fact and no map entry was reported LANE_NOT_OPENED for a
# reason nothing upstream could see coming. `release_record.py`
# now copies onto each `ReleaseStep` the release, namespace and chart from the Stage 2
# inventory and the chart REPOSITORY from the APPROVED FACT, a quote a person approved. The
# chart version is nobody's to transcribe: the lane asks the repository which chart ships the
# approved application version, before anything moves.
#
# A HELM STEP WITHOUT A CHART REPOSITORY IS SKIPPED WITH A REASON, NOT GUESSED AT. It reaches the
# report as LANE_NOT_OPENED, which is the same outcome the IAM condition produces for a component
# with no reviewed RBAC fence -- two independent fail-closed paths that agree.
# ---------------------------------------------------------------------------








def _oci_chart_ref(reference: str, chart_name: str) -> str:
    """The `oci://` reference `helm` installs from, with the chart name exactly once.

    Projects print an OCI chart two ways: the full chart reference
    (`oci://public.ecr.aws/karpenter/karpenter`) or the registry namespace it lives under
    (`oci://registry.example/charts`), with the chart named separately. Helm wants the full
    reference, so the installed chart's name is appended only when it is not already the last
    path segment.

    >>> _oci_chart_ref("oci://public.ecr.aws/karpenter/karpenter", "karpenter")
    'oci://public.ecr.aws/karpenter/karpenter'
    >>> _oci_chart_ref("oci://registry.example/charts/", "keda")
    'oci://registry.example/charts/keda'
    """
    base = reference.rstrip("/")
    if not chart_name or base.rsplit("/", 1)[-1] == chart_name:
        return base
    return f"{base}/{chart_name}"


def _run_id_from(event: Event) -> str:
    """The run this invocation is about, from wherever the state actually carries it.

    THE STATE DOES NOT PUT `run_id` AT ITS TOP LEVEL, AND BOTH MODES ASSUMED IT DID. That assumption
    breaks when `Upgrade lanes` passes the whole run payload: `event["run_id"]`
    is absent, and the empty string goes straight into an S3 key --

        releases/cluster=my-eks-cluster/run=/release.json

    -- which the lane's IAM policy does not match, so it came back AccessDenied and the planner
    reported that nothing had authorised the run. The message named a permissions problem; the cause
    was an empty path segment. `run=` with nothing after it is the tell.

    `release.run_id` IS THE RIGHT PLACE TO READ IT, not merely an available one. `Release recorded`
    writes that document, it is the authorisation this stage may act on, and its `run_id` is by
    definition the run being authorised. Reading the id from the same document as the approved steps
    means the two cannot disagree about which run they describe.

    The explicit keys stay first so a direct invocation -- a test, an operator reproducing a
    report --
    can still name the run without assembling a release document around it.
    """
    for candidate in (event.get("run_id"), event.get("execution_name")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    for holder, *path in (("release", "run_id"), ("release_record", "document", "run_id")):
        value: Any = event.get(holder)
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _plan(event: Event) -> Result:
    """Emit one lane item per approved step, and a reason for every step that cannot run."""
    resolved = settings.from_event(event)
    run_id = _run_id_from(event)
    # THE AUTHORISED STEPS, from the validated release. NOT `upgrade_approval.approved_steps`:
    # that is what a person WANTED, and this stage may only open a lane for what a named AWS
    # principal then AUTHORISED. If the two ever disagree, the authorisation is the half that wins.
    #
    # AND THEY ARE OBJECTS NOW. `ReleaseStep` carries `component`, `to_version` and
    # `install_method`, so the lane items below are built from the authorisation itself rather than
    # joined against a decision document and a catalogue flag. Two fewer reads, and no way for the
    # catalogue to disagree with what was authorised about which API to call.
    approved = list(
        _release(
            event,
            bucket=resolved.bucket,
            cluster=resolved.cluster_name,
            run_id=run_id,
        ).approved_steps
    )
    if not (run_id and approved):
        raise settings.MissingParameter(
            "the plan mode needs a run id and a non-empty authorised-step list. Got "
            f"run_id={run_id!r}, {len(approved)} authorised step(s). Without both there is nothing "
            "to open a lane for, and guessing is not available.\n\n"
            "NOTE THAT `plan_digest` IS NO LONGER NEEDED. It was, when the components had to be "
            "looked up in the run's decision document; `ReleaseStep` now carries the component, "
            "the target version and the install method, so the authorisation answers for itself."
        )

    items: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    current_minor = str(event.get("kubernetes_version") or "").strip()
    # THE SAME CATALOGUE THE APPROVAL PAGE USED, from the same snapshot, so the two cannot
    # disagree about which steps the cluster can take today. Unreadable -> empty -> the rule falls
    # back to placement, which skips more, never less.
    addon_catalogue: dict[tuple[str, str], tuple[str, ...]] = {}
    snapshot_uri = str(event.get("snapshot_uri") or "").strip()
    if snapshot_uri:
        try:
            addon_catalogue = catalogue_index(
                documents.read_document(snapshot_uri, ClusterSnapshot).eks_addon_catalogue
            )
        except Exception as problem:
            print(f"the snapshot's add-on catalogue could not be read: {problem}")

    for step in sorted(approved, key=lambda s: s.order):
        component = step.component or ""
        # A STEP THE CLUSTER CANNOT TAKE TODAY IS NOT ATTEMPTED. The approval page withholds the
        # control for these (`upgrade_approval._withheld_until_the_cluster_moves`), so an
        # authorised one should not exist -- but the release is what this stage acts on, and a
        # release from before that change, or one approved through another path, must still not
        # send EKS a build it does not publish for the running minor. Consider kube-proxy
        # `v1.36.0-eksbuild.25` on a 1.35 cluster, which EKS refuses: without this guard the
        # run spends a lane and fails on a change that could never have applied. One rule, shared
        # with the page: `upgrade_lane.applicable`.
        reason = (
            not_applicable_reason(
                component=component,
                target_version=step.to_version,
                at_kubernetes=step.at_kubernetes,
                current_minor=current_minor,
                managed_addon=step.install_method == "EKS_MANAGED_ADDON",
                catalogue=addon_catalogue,
            )
            if component
            else ""
        )
        if reason:
            skipped.append(
                {"step_order": str(step.order), "component": component, "reason": reason}
            )
            continue
        if not component:
            # THE CLUSTER HOP MOVES NO COMPONENT, and `ReleaseStep.component` is None for it. Not a
            # skip with a complaint: there is genuinely nothing for a lane to upgrade, so it is
            # noted and passed over.
            skipped.append(
                {
                    "step_order": str(step.order),
                    "component": "",
                    "reason": (
                        f"step {step.order} is a {step.kind} and moves no component, so there is "
                        "no lane to open for it"
                    ),
                }
            )
            continue

        # HOW IT IS INSTALLED COMES FROM THE AUTHORISATION. `install_method` is copied onto the
        # release from the Stage 2 inventory, so it is what the cluster actually reported.
        managed_addon = step.install_method == "EKS_MANAGED_ADDON"
        target = step.to_version or ""
        if not managed_addon and not (step.chart_repository and step.chart_name):
            # THE CHART REPOSITORY COMES FROM THE APPROVED FACT VIA THE RECORD, and its
            # absence is not filled in: `helm repo add` with a guessed URL installs from a
            # source nobody approved. Which chart VERSION ships `to_version` is not needed
            # here at all -- the lane asks the repository (`helm search repo`) before it moves.
            skipped.append(
                {
                    "step_order": str(step.order),
                    "component": component,
                    "reason": (
                        f"the approved fact for {component!r} carries no chart repository, so "
                        "the lane has nothing to `helm repo add`. Re-research the component: "
                        "the research prompt asks for the repository when the task names a "
                        "Helm release, and the approver sees it beside the recommended version."
                    ),
                }
            )
            continue

        oci = (step.chart_repository or "").startswith("oci://")
        items.append(
            {
                # EVERY VALUE IS A STRING. These become CodeBuild environment-variable overrides and
                # CodeBuild takes only strings; an int here is refused by the SDK integration at the
                # start of the lane rather than inside it.
                "step_order": str(step.order),
                "component": component,
                "release": step.release_name or component,
                "namespace": step.namespace or "kube-system",
                # The group the lane's access entry maps the build role into. A CONSTANT, NOT
                # DERIVED FROM THE COMPONENT -- it was `upgrade-agent-<component>`, one group per
                # component bound to a Role naming that release's own objects. No role binds it now;
                # what it does is satisfy the `ForAllValues:StringEquals` condition on
                # `eks:CreateAccessEntry`, which is what stops an entry reaching `system:masters`.
                "group": LANE_GROUP,
                "managed_addon": "true" if managed_addon else "false",
                # THE LOCAL ALIAS `helm repo add` GIVES THE REPOSITORY IS THE COMPONENT'S NAME, and
                # the chart reference is `<alias>/<chart>`. The alias is the lane's own and never
                # leaves it, so the component id is as good a name as any and cannot collide
                # within one build.
                "chart_repo_name": "" if managed_addon or oci else component,
                "chart_repo_url": "" if managed_addon or oci else (step.chart_repository or ""),
                # AN OCI CHART IS ADDRESSED DIRECTLY, with no `helm repo add` and no alias: the
                # reference is the chart. See `_oci_chart_ref` for how the chart name is placed.
                "chart_ref": (
                    ""
                    if managed_addon
                    else _oci_chart_ref(step.chart_repository or "", step.chart_name or "")
                    if oci
                    else f"{component}/{step.chart_name}"
                ),
                "target_addon_version": target if managed_addon else "",
                # The approved application version for either kind. The Helm lane resolves the
                # chart version that ships it from the repository, the way the add-on lane
                # resolves the `-eksbuild.N` build from EKS.
                "target_version": target,
                # `from_version` IS ABSENT FROM THE RELEASE RECORD, deliberately -- it carries
                # `from_series`, not the exact installed version. So the build reads the installed
                # version off the cluster at lane start, and this carries the SERIES only, as
                # context for the agent's prompt.
                "installed": step.from_series or "",
                # The line the approver actually read, verbatim, so what was authorised and what the
                # agent is told cannot differ.
                "summary": step.summary,
                "addon_pod_selector": step.pod_selector or "",
                "diagnosis_prefix": keys.diagnosis_prefix(
                    resolved.cluster_name, run_id, component
                ),
                "lane_result_key": keys.lane_result_key(
                    resolved.cluster_name, run_id, component
                ),
            }
        )

    return {"items": items, "skipped": skipped}


def handler(event: Event, _context: Any = None) -> Result:
    """Plan the lanes, or report on them. `mode` says which.

    ONE FUNCTION AND TWO MODES, for `upgrade_brief.py`'s reason: both halves read and write the
    same documents in the same bucket, so splitting them would be two Lambdas, two roles and two
    log groups holding identical grants. The MODE arrives on the payload from the state, never
    from the environment, so which half runs is visible in the state machine rather than in a
    deployment.
    """
    mode = str(event.get("mode") or "report").strip()
    # The state passes the whole run payload nested under `run`, because naming fields would fail
    # the state on any path where one is absent -- the trap `Upgrade approval` documents for
    # `brief_uri`. Unwrapped here so both modes read the payload they expect.
    run = event.get("run")
    payload: Event = run if isinstance(run, dict) else event
    if mode == "plan":
        return _plan(payload)
    return _report(payload)


def _planner_skip_reason(event: Event, order: int) -> str:
    """The planner's reason for not opening a lane for this step, or "" when it opened one.

    Read off `upgrade_lanes.skipped`, which `_plan` returned on the same execution and the state
    carried under `$.upgrade_lanes`. Absent or unreadable means "not skipped", so a payload that
    lost the list degrades to the lane-result read below rather than to a wrong claim.
    """
    lanes = event.get("upgrade_lanes")
    skipped = lanes.get("skipped") if isinstance(lanes, dict) else None
    if not isinstance(skipped, list):
        return ""
    for entry in skipped:
        if isinstance(entry, dict) and str(entry.get("step_order")) == str(order):
            return str(entry.get("reason") or "the planner skipped this step without a reason")
    return ""


def _report(event: Event) -> Result:
    """Assemble the run's report from the lanes' own result objects."""
    resolved = settings.from_event(event)
    run_id = _run_id_from(event)
    if not run_id:
        raise settings.MissingParameter(
            "run_id (or execution_name) is required: the report is keyed on the run and there is "
            "no other way to find this run's lane results."
        )

    cluster = ClusterReference(name=resolved.cluster_name, region=resolved.region)

    # A RELEASE THIS STAGE CANNOT VALIDATE STILL PRODUCES A REPORT, because by the time this runs a
    # cluster has already been changed and the report is the only account of it. So the refusal is
    # recorded as the report's own body rather than raised: `release_error` says what was wrong, the
    # approver fields stay empty rather than being filled with something plausible, and the run
    # fails at `Anything referred?` because the outcome list is empty and therefore unaccounted for.
    release: Release | None = None
    release_error = ""
    try:
        release = _release(
            event, bucket=resolved.bucket, cluster=resolved.cluster_name, run_id=run_id
        )
    except Exception as problem:
        release_error = f"{type(problem).__name__}: {problem}"

    outcomes: list[LaneOutcome] = []
    for step in (_lane_rows(release, event) if release is not None else []):
        order = int(step["step_order"])
        component = str(step.get("component") or "")
        cleanup_error = str(step.get("cleanup_error") or "")

        if not component:
            # The Map iteration itself could not be read, so there is no component name to look a
            # result up by. Reported rather than skipped.
            outcomes.append(
                LaneOutcome(
                    cluster=cluster,
                    run_id=run_id,
                    step_order=order,
                    component=f"step-{order}",
                    status=UpgradeStatus.REFERRED,
                    attempted=("the lane's own payload was read",),
                    observed=(
                        "it named no component, so no lane result could be located for this "
                        "approved step",
                    ),
                    finished_at=datetime.now(UTC),
                )
            )
            continue

        skipped_reason = _planner_skip_reason(event, order)
        if skipped_reason:
            # THE PLANNER NEVER OPENED THIS LANE, AND SAID WHY. Reading it as a missing lane result
            # produced "not readable (AccessDenied) ... its CloudWatch log is the next place to
            # look" about a step no build ever ran -- true, and pointing a person at a log that
            # does not exist. The planner's own reason is the account, so it is the row.
            outcomes.append(
                LaneOutcome(
                    cluster=cluster,
                    run_id=run_id,
                    step_order=order,
                    component=component,
                    status=UpgradeStatus.LANE_NOT_OPENED,
                    attempted=("the lane planner considered this authorised step",),
                    observed=(skipped_reason,),
                    cleanup_error=cleanup_error,
                    finished_at=datetime.now(UTC),
                )
            )
            continue

        uri = documents.build_uri(
            resolved.bucket, keys.lane_result_key(resolved.cluster_name, run_id, component)
        )
        try:
            payload = documents.read_json(uri)
        # A BROAD except, deliberately: see the module docstring. Any failure to read a lane
        # result becomes a REFERRED row, because the report is the one artifact a person is
        # waiting for and a stage that raised would lose it.
        except Exception as problem:
            outcomes.append(
                LaneOutcome(
                    cluster=cluster,
                    run_id=run_id,
                    step_order=order,
                    component=component,
                    status=UpgradeStatus.REFERRED,
                    attempted=(f"read the lane result at {uri}",),
                    observed=(
                        f"it is not readable ({type(problem).__name__}), so this lane's outcome "
                        "is unknown. The build either never wrote one or failed before it could; "
                        "its CloudWatch log is the next place to look.",
                    ),
                    cleanup_error=cleanup_error,
                    finished_at=datetime.now(UTC),
                )
            )
            continue

        if not isinstance(payload, dict):
            payload = {"status": None, "component": component}
        outcome = _outcome_from(
            payload, cluster=cluster, run_id=run_id, step_order=order
        )
        if cleanup_error:
            # THE ACCESS ENTRY DID NOT COME OFF. Recorded on the outcome so the report says so in
            # the row rather than in a footnote: an undeleted access entry is a standing
            # cluster-write grant nobody asked for.
            outcome = outcome.model_copy(update={"cleanup_error": cleanup_error})
        outcomes.append(outcome)

    if release is None:
        # ONE ROW SAYING SO, rather than an empty report. An empty outcome list reads as "this run
        # touched nothing", and the lanes may well have changed the cluster before this stage ran.
        outcomes.append(
            LaneOutcome(
                cluster=cluster,
                run_id=run_id,
                step_order=1,
                component="(the run's authorisation)",
                status=UpgradeStatus.REFERRED,
                attempted=("read the release gate's payload into the Release contract",),
                observed=(
                    f"it was refused: {release_error}. Which steps were authorised, and by whom, "
                    "cannot be established from this payload -- so what the lanes did cannot be "
                    "attributed. Every lane result is still under upgrade-lanes/ and every "
                    "diagnosis bundle under upgrade-diagnosis/ for this run id.",
                ),
                finished_at=datetime.now(UTC),
            )
        )

    report = UpgradeRunReport(
        cluster=cluster,
        run_id=run_id,
        target_kubernetes_version=resolved.target_kubernetes_version,
        automation_execution_id=release.automation_execution_id if release else "",
        approver_arns=release.approver_arns if release else (),
        outcomes=tuple(outcomes),
        finished_at=datetime.now(UTC),
    )

    report_uri = documents.build_uri(
        resolved.bucket, keys.upgrade_report_key(resolved.cluster_name, run_id)
    )
    # THE DOCUMENT IS WRITTEN BEFORE THE MAIL IS BUILT, and the order is the point. The report is
    # the run's only account of a cluster that has already been changed, so it becomes durable
    # first; the mail is a delivery of it, and a delivery that fails must not be able to discard
    # the thing it was delivering.
    documents.write_document(report_uri, report)

    # THE PAGE, BESIDE THE DOCUMENT. Rendered from the document that was just written, so the two
    # cannot disagree -- and the page cites the document's URI, so a reader who distrusts the page
    # can fetch what it was rendered from.
    page_uri = ""
    try:
        from ..upgrade_lane.report_page import to_upgrade_report_html

        page_uri = documents.build_uri(
            resolved.bucket,
            keys.upgrade_report_key(resolved.cluster_name, run_id).replace(
                "report.json", "report.html"
            ),
        )
        # THE VIEW IS ENRICHMENT AND THE DOCUMENT IS THE RECORD. `build_view` reads the decisions,
        # the plan, the inventory and the diagnosis bundles beside the report; every one of those
        # reads is optional inside it, so a bundle that cannot be read leaves a sentence on the page
        # rather than failing the render. If building the view itself fails, the page is rendered
        # from the document alone, which is a complete page with less on it.
        from .upgrade_report_view import build_view

        try:
            view = build_view(
                report, event=event, release=release, bucket=resolved.bucket
            )
        except Exception as problem:
            print(f"the report view could not be enriched: {type(problem).__name__}: {problem}")
            view = None
        documents.write_text(
            page_uri,
            to_upgrade_report_html(
                report,
                rendered_at=report.finished_at.isoformat(),
                report_uri=report_uri,
                view=view,
            ),
            "text/html; charset=utf-8",
        )
    except Exception as problem:
        # THE PAGE IS A VIEW AND THE DOCUMENT IS THE RECORD. A renderer that failed must not be able
        # to discard the account of a cluster that has already been changed, so this is an absence
        # noted on the result rather than a raise.
        page_uri = ""
        print(f"the report page could not be rendered: {type(problem).__name__}: {problem}")

    mail_message_id = ""
    mail_error = ""
    try:
        from ..notify.presign import presign_s3_get
        from ..notify.upgrade_report_email import (
            RunFacts,
            build_report_email,
            send_report_email,
        )

        # THE FOUR FACTS THE REPORT DOCUMENT DOES NOT CARRY, off the payload this state
        # already receives. `UpgradeRunReport` is an account of the lanes and nothing else,
        # so the mail cannot say what Kubernetes minor the cluster is on, how many changes a
        # person left alone, where the page is, or -- crucially -- what app version was
        # authorised. None of it costs a read: `release` is the document validated at the top
        # of this function and the rest is on `event`.
        #
        # `upgrade_approval` ARRIVES DOUBLE-NESTED on the live payload
        # (`{"upgrade_approval": {"upgrade_approval": {...}}}`), which is the decision
        # watcher's ResultPath landing inside the key it is named after. Unwrapped rather
        # than relied on, so a flattened payload works too.
        decided = event.get("upgrade_approval") or {}
        if isinstance(decided.get("upgrade_approval"), dict):
            decided = decided["upgrade_approval"]

        mail_message_id = send_report_email(
            build_report_email(
                report,
                report_uri=report_uri,
                facts=RunFacts(
                    current_kubernetes_version=str(event.get("kubernetes_version") or ""),
                    undecided_steps=len(decided.get("undecided_steps") or ()),
                    # The page is written immediately above, so it can be signed here. No
                    # page means a mail with no button rather than a dead one.
                    page_url=presign_s3_get(page_uri) if page_uri else "",
                    # THE APP VERSIONS COME FROM THE AUTHORISATION, NOT FROM THE LANE.
                    # `LaneOutcome.applied` holds the CHART version for a Helm lane, so the
                    # mail this replaces told a reader metrics-server had moved to 3.14.0 --
                    # a chart version, and not a version of metrics-server at all.
                    # `ReleaseStep.to_version` is the app version a named principal
                    # released. See the builder's docstring.
                    authorised={
                        step.order: (
                            step.from_series or "",
                            step.to_version or "",
                            step.install_method or "",
                        )
                        for step in (release.approved_steps if release else ())
                    },
                ),
            )
        )
    except Exception as problem:  # the report is already durable; see the module docstring.
        # A BOUNCED MAIL IS A RECORDED ABSENCE, NOT A FAILED STAGE. The execution still fails on a
        # referral, every diagnosis bundle is still in S3, and the report is still at the URI
        # above -- so the honest outcome is to say the notification did not arrive and carry on.
        # Raising here would turn a mail server's bad afternoon into a lost account of a cluster
        # change.
        mail_error = f"{type(problem).__name__}: {problem}"

    return {
        "upgrade_report_uri": report_uri,
        "upgrade_report_page_uri": page_uri,
        "upgrade_report_digest": report.digest(),
        "upgrade_summary": report.summary(),
        # WHAT `Anything referred?` BRANCHES ON. A count rather than a list, because a Choice on a
        # list's first element is how this file's release-gate neighbour has to spell
        # "non-empty" -- and a count that is zero is readable in the console.
        "upgrade_referred_count": len(report.referred),
        "upgrade_referred": list(report.referred),
        "upgrade_upgraded": list(report.upgraded),
        "upgrade_rolled_back": list(report.rolled_back),
        # WHETHER THE PERSON WAS ACTUALLY TOLD. `""` with no error means mail is not configured,
        # which is a supported deployment; an error means it is configured and did not work, and
        # those are different things a reader needs to be able to tell apart.
        "upgrade_report_mailed": bool(mail_message_id),
        "upgrade_report_mail_error": mail_error,
    }
