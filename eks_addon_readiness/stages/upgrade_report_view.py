"""Fill the upgrade report's view from the evidence beside the report document.

    in    the `UpgradeRunReport` just written, the run payload, the validated `Release`
    out   one `UpgradeReportView` for `report_page.render_view`

**EVERY READ IS OPTIONAL AND EVERY FAILURE IS A SENTENCE.** The report document is already
durable when this runs, so nothing here may fail the stage: a bundle that cannot be read leaves
the pod table saying so, a decisions document that is missing leaves the approval row saying
"not recorded". The page degrades toward `report_view.view_from_report`, never toward a raise.

**WHAT IS READ, AND WHERE IT COMES FROM.**

    release            `Release.approved_steps`      the authorised moves: kind, to_version, method
    decisions          `RunDecisions`                who approved which step, what was deferred
    plan               `UpgradePlan`                 the steps that were NOT authorised -> Deferred
    inventory          `InventoryReport`             installed versions, namespaces -> Components
    assessment         `ReadinessAssessment`         verdict per component -> Components
    snapshot           `ClusterSnapshot`             insights, collection gaps -> Components sub
    bundles            `upgrade-diagnosis/.../before|after/`
                                                     pods.json, logs/, events.json, helm-history,
                                                     describe-addon, mutation.log -> Details rows

None of these are re-derived. The plan is read for the steps the release did not include, the
inventory for the version a component was on, and the bundle for what the lane captured -- so the
page cannot disagree with the documents a person can fetch themselves.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..decide import ReadinessAssessment
from ..models import CompatibilityVerdict, Confidence
from ..order import UpgradePlan, catalogue_recommendation
from ..report import InventoryReport
from ..snapshot import ClusterSnapshot
from ..storage import documents
from ..storage import keys as storage_keys
from ..upgrade import Decision, Release, RunDecisions
from ..upgrade_lane.models import LaneOutcome, UpgradeRunReport, UpgradeStatus
from ..upgrade_lane.report_view import (
    AppliedDetail,
    AppliedRow,
    AuthorizationRow,
    DeferredRow,
    InventoryRow,
    UpgradeReportView,
    addon_state_line,
    chips_for,
    detail_from_outcome,
    event_summary,
    helm_revision_line,
    log_excerpt,
    pods_from_json,
    same_release,
    short_arn,
    workload_lines,
)

Event = dict[str, Any]

_ADDON_VERSION = re.compile(r"--addon-version\s+(\S+)")


def _try(read: Callable[[], Any], *, default: Any = None) -> Any:
    """One optional read. Anything that goes wrong is the default, not a raise."""
    try:
        return read()
    except Exception:
        return default


def _hhmm(moment: datetime | str | None) -> str:
    if moment is None:
        return ""
    if isinstance(moment, str):
        parsed = _try(lambda: datetime.fromisoformat(moment.replace("Z", "+00:00")))
        if parsed is None:
            return moment
        moment = parsed
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.strftime("%H:%M UTC")


def _window(started: str, finished: datetime) -> str:
    start = _try(lambda: datetime.fromisoformat(started.replace("Z", "+00:00")))
    if start is None:
        return ""
    start = start.astimezone(UTC)
    end = finished.astimezone(UTC)
    return f"{start:%Y-%m-%d %H:%M}\u2013{end:%H:%M} UTC"


def _account(arns: tuple[str, ...]) -> str:
    for arn in arns:
        parts = arn.split(":")
        if len(parts) > 4 and parts[4].isdigit():
            return parts[4]
    return ""


# ---------------------------------------------------------------------------
# Bundle evidence -> one lane's details.
# ---------------------------------------------------------------------------


class _Bundle:
    """The files of one diagnosis bundle, read lazily and never twice."""

    def __init__(self, prefix_uri: str) -> None:
        self.prefix = prefix_uri
        self._keys: list[str] | None = None

    @property
    def keys(self) -> list[str]:
        if self._keys is None:
            self._keys = _try(lambda: documents.list_uris(self.prefix), default=[]) or []
        return self._keys

    @property
    def present(self) -> bool:
        return bool(self.prefix) and bool(self.keys)

    def json(self, name: str) -> Any:
        uri = self.prefix + name
        return _try(lambda: documents.read_json(uri)) if uri in self.keys else None

    def text(self, name: str) -> str:
        uri = self.prefix + name
        if uri not in self.keys:
            return ""
        return _try(lambda: documents.read_text(uri), default="") or ""

    def logs(self) -> list[tuple[str, str]]:
        """`(file name, body)` for every current-container log, largest first."""
        out: list[tuple[str, str]] = []
        for uri in self.keys:
            name = uri[len(self.prefix):]
            if name.startswith("logs/") and name.endswith(".current.log"):
                body = _try(lambda u=uri: documents.read_text(u), default="") or ""
                out.append((name[len("logs/"):-len(".current.log")], body))
        return sorted(out, key=lambda pair: -len(pair[1]))


def _log_block(bundle: _Bundle) -> tuple[str, tuple[str, ...], str]:
    """The one log worth showing: the container with the most notable lines.

    Returns `(heading, lines, one-line summary for the state list)`.
    """
    logs = bundle.logs()
    if not logs:
        return "", (), ""
    best: tuple[str, tuple[str, ...], str] | None = None
    clean: list[str] = []
    for name, body in logs:
        lines, summary = log_excerpt(body)
        if lines and (best is None or len(lines) > len(best[1])):
            best = (name, lines, summary)
        elif not lines:
            clean.append(f"{name}: {summary}")
    if best is not None:
        name, lines, summary = best
        return f"Container log \u00b7 {name} \u00b7 {summary}", lines, ""
    return "", (), "Container logs: " + "; ".join(clean[:3]) + (" \u2026" if len(clean) > 3 else "")


def _current_from(bundle: _Bundle, *, managed_addon: bool) -> str:
    """The version the component is on per the bundle's own read, or "" when it has none."""
    if not bundle.present:
        return ""
    if managed_addon:
        describe = bundle.json("describe-addon.json")
        addon = describe.get("addon") if isinstance(describe, dict) else None
        return str(addon.get("addonVersion") or "") if isinstance(addon, dict) else ""
    history = bundle.json("helm-history.json")
    deployed = [
        r for r in (history or []) if isinstance(r, dict) and r.get("status") == "deployed"
    ]
    if not deployed:
        return ""
    app = deployed[-1].get("app_version")
    return str(app) if isinstance(app, str) else ""


def _detail(
    outcome: LaneOutcome, *, before_bundle: _Bundle, after_bundle: _Bundle, before: str, after: str
) -> AppliedDetail:
    """Enrich the floor detail with what the bundles hold."""
    base = detail_from_outcome(outcome)

    # WHICH POD STATE THE PAGE SHOWS. The `after` bundle is captured when validation refused, before
    # any rollback -- so on a rolled-back row it IS the state that caused the rollback. On a healthy
    # row only `before` exists, and the heading says so rather than implying it is post-upgrade.
    pods_heading, pods_note, pods = base.pods_heading, base.pods_note, ()
    if after_bundle.present and after_bundle.json("pods.json") is not None:
        pods = pods_from_json(after_bundle.json("pods.json"))
        pods_heading = f"Pod state at {after}"
        pods_note = (
            "post-upgrade capture"
            if outcome.status is UpgradeStatus.UPGRADED
            else "pre-rollback capture"
            if outcome.rollback_command
            else "captured at refusal"
        )
    elif before_bundle.present and before_bundle.json("pods.json") is not None:
        pods = pods_from_json(before_bundle.json("pods.json"))
        pods_heading = f"Pod state at {before}"
        pods_note = "pre-upgrade capture; post-upgrade state is in the validation record"

    evidence_bundle = after_bundle if after_bundle.present else before_bundle
    state_lines: list[str] = list(workload_lines(evidence_bundle.json("workloads.json")))
    if outcome.managed_addon:
        line = addon_state_line(evidence_bundle.json("describe-addon.json"))
        if line:
            state_lines.append(
                ("Add-on: " if not after_bundle.present else "Add-on at refusal: ") + line
            )
    else:
        line = helm_revision_line(evidence_bundle.json("helm-history.json"))
        if line:
            state_lines.append(line + (" (pre-upgrade)" if not after_bundle.present else ""))
    pod_names = [p.name for p in pods] or [outcome.component]
    state_lines.append(event_summary(evidence_bundle.json("events.json"), names=pod_names))
    log_heading, log_lines, log_summary = _log_block(evidence_bundle)
    if log_summary:
        state_lines.append(log_summary)

    validation = list(base.validation)
    verdict_line, verdict_hue = base.verdict_line, base.verdict_hue
    if outcome.status is not UpgradeStatus.UPGRADED:
        # WHAT THE MUTATION ITSELF SAID, when the lane captured it. This is the line that turned a
        # 900-second referral into "the update-addon call was rejected before anything changed"
        # on one test run, and it belongs at the top of the record rather than in a bucket.
        mutation = after_bundle.text("logs/mutation.log").strip()
        if mutation:
            validation.insert(0, f"Mutation output: {mutation.splitlines()[0][:300]}")
        # WHETHER THE CLUSTER ACTUALLY CHANGED. A referral over a component that is on the version
        # it started on is a very different afternoon from one over a broken component, and the
        # two bundles can settle it without anybody's judgement.
        was = _current_from(before_bundle, managed_addon=outcome.managed_addon)
        now = _current_from(after_bundle, managed_addon=outcome.managed_addon)
        if was and now and was == now:
            validation.insert(
                1 if mutation else 0,
                f"No change was applied: the component is on {now}, the version it started on.",
            )
        if outcome.triage is None and outcome.status is UpgradeStatus.REFERRED:
            verdict_line, verdict_hue = "Verdict: REFERRED \u2014 action required", "critical"

    return AppliedDetail(
        validation_heading=base.validation_heading,
        validation=tuple(validation),
        verdict_line=verdict_line,
        verdict_hue=verdict_hue,
        pods_heading=pods_heading,
        pods_note=pods_note,
        pods=pods,
        state_heading="Add-on state" if outcome.managed_addon else "Release state",
        state_lines=tuple(state_lines),
        rollback=base.rollback,
        log_heading=log_heading,
        log_lines=log_lines,
        executed=base.executed,
        evidence=base.evidence,
        warning=base.warning,
    )


# ---------------------------------------------------------------------------
# The view.
# ---------------------------------------------------------------------------


def _applied_after(outcome: LaneOutcome, step: Any) -> tuple[str, str]:
    """`(after, after_sub)`: the app version that was authorised, and how it was delivered."""
    if outcome.managed_addon:
        for line in outcome.attempted:
            match = _ADDON_VERSION.search(line)
            if match and not match.group(1).startswith("v" + (outcome.installed or "\0")):
                return match.group(1), ""
        return (step.to_version if step is not None and step.to_version else outcome.applied), ""
    app = step.to_version if step is not None and step.to_version else ""
    chart = outcome.applied
    if app:
        return app, f"chart {chart}" if chart else ""
    return chart, "chart version"


def build_view(
    report: UpgradeRunReport,
    *,
    event: Event,
    release: Release | None,
    bucket: str,
) -> UpgradeReportView:
    """Everything the page can say about this run, from the documents beside the report."""
    cluster = report.cluster.name
    target = report.target_kubernetes_version
    current = str(event.get("kubernetes_version") or "")

    plan_digest = str(event.get("plan_digest") or "")
    decisions: RunDecisions | None = (
        _try(
            lambda: documents.read_document(
                documents.build_uri(
                    bucket,
                    storage_keys.run_decisions_key(cluster, target, plan_digest, report.run_id),
                ),
                RunDecisions,
            )
        )
        if plan_digest
        else None
    )
    plan: UpgradePlan | None = (
        _try(lambda: documents.read_document(str(event["plan_uri"]), UpgradePlan))
        if event.get("plan_uri")
        else None
    )
    inventory: InventoryReport | None = (
        _try(lambda: documents.read_document(str(event["inventory_uri"]), InventoryReport))
        if event.get("inventory_uri")
        else None
    )
    assessment: ReadinessAssessment | None = (
        _try(lambda: documents.read_document(str(event["assessment_uri"]), ReadinessAssessment))
        if event.get("assessment_uri")
        else None
    )
    snapshot: ClusterSnapshot | None = (
        _try(lambda: documents.read_document(str(event["snapshot_uri"]), ClusterSnapshot))
        if event.get("snapshot_uri")
        else None
    )

    installed_by_identity: dict[str, Any] = {}
    if inventory is not None:
        for component in inventory.components:
            if component.identity:
                installed_by_identity.setdefault(component.identity, component)

    # WHAT THE APPROVAL PAGE SHOWED WITHOUT A CONTROL, and why. `upgrade_approval` arrives
    # double-nested on the live payload (the watcher's ResultPath lands inside the key it is named
    # after); unwrapped rather than relied on, as the report stage does for the mail.
    decided: Any = event.get("upgrade_approval") or {}
    if isinstance(decided, dict) and isinstance(decided.get("upgrade_approval"), dict):
        decided = decided["upgrade_approval"]
    withheld: dict[str, str] = (
        {str(k): str(v) for k, v in decided.get("withheld_steps", {}).items()}
        if isinstance(decided, dict) and isinstance(decided.get("withheld_steps"), dict)
        else {}
    )
    steps_by_order = {s.order: s for s in (release.approved_steps if release else ())}
    decision_by_order = {d.step_order: d for d in (decisions.decisions if decisions else ())}

    # ---- Applied -------------------------------------------------------------------------
    applied: list[AppliedRow] = []
    for outcome in report.outcomes:
        step = steps_by_order.get(outcome.step_order)
        component = installed_by_identity.get(outcome.component)
        # The namespace off the release record (copied there from the inventory at approval
        # time), then the inventory itself for a record from before that field existed.
        namespace = str(
            (step.namespace if step is not None else "")
            or (component.namespace if component else "")
            or ""
        )
        kind = step.kind.value if step is not None else ""
        need = (
            f"required for {target}"
            if kind == "COMPONENT_MOVE"
            else "recommended (optional)"
            if kind == "COMPONENT_UPGRADE_RECOMMENDED"
            else ""
        )
        sub = " \u00b7 ".join(
            part
            for part in (
                "EKS managed add-on" if outcome.managed_addon else "Helm release",
                f"ns {namespace}" if namespace else "",
                need,
            )
            if part
        )
        before_bundle = _Bundle(outcome.diagnosis_before_uri)
        after_bundle = _Bundle(outcome.diagnosis_after_uri)
        before = (
            component.version if component and component.version else outcome.installed
        ) or "?"
        before_sub = ""
        if not outcome.managed_addon:
            history = before_bundle.json("helm-history.json")
            deployed = [
                r for r in (history or []) if isinstance(r, dict) and r.get("status") == "deployed"
            ]
            if deployed and isinstance(deployed[-1].get("chart"), str):
                before_sub = "chart " + deployed[-1]["chart"].rsplit("-", 1)[-1]
        after, after_sub = _applied_after(outcome, step)
        current_note = ""
        now = _current_from(after_bundle, managed_addon=outcome.managed_addon)
        if outcome.status is UpgradeStatus.ROLLED_BACK:
            current_note = f"current: {now or before} (restored)"
        elif outcome.resulting and not same_release(outcome.resulting, outcome.applied):
            current_note = f"current: {outcome.resulting}"
        elif outcome.status is not UpgradeStatus.UPGRADED and now and now != after:
            current_note = f"current: {now}"
        applied.append(
            AppliedRow(
                order=outcome.step_order,
                component=outcome.component,
                sub=sub,
                before=before,
                before_sub=before_sub,
                after=after,
                after_sub=after_sub,
                current_note=current_note,
                status=outcome.status,
                chips=chips_for(outcome),
                detail=_detail(
                    outcome,
                    before_bundle=before_bundle,
                    after_bundle=after_bundle,
                    before=before,
                    after=after,
                ),
            )
        )

    # ---- Deferred ------------------------------------------------------------------------
    deferred: list[DeferredRow] = []
    outstanding: list[str] = [
        o.component for o in report.outcomes if o.status is not UpgradeStatus.UPGRADED
        and steps_by_order.get(o.step_order) is not None
        and steps_by_order[o.step_order].kind.value == "COMPONENT_MOVE"
    ]
    aws_recommended: dict[str, str] = {}
    if assessment is not None:
        for d in assessment.decisions:
            aws_version, _at = catalogue_recommendation(d)
            if aws_version and d.identity:
                aws_recommended[d.identity] = aws_version
    for step in (plan.steps if plan is not None else ()):
        if step.order in steps_by_order:
            continue
        if step.kind.value == "CLUSTER_UPGRADE":
            deferred.append(
                DeferredRow(
                    order=step.order,
                    component="cluster",
                    sub="control plane",
                    current=step.from_kubernetes or current,
                    target=step.to_kubernetes or target,
                    reason="Out of scope. Control-plane upgrades are not performed by this stage.",
                )
            )
            continue
        name = step.component or f"step-{step.order}"
        component = installed_by_identity.get(name)
        managed = bool(component and component.install_method == "EKS_MANAGED_ADDON")
        required = step.kind.value == "COMPONENT_MOVE"
        decision = decision_by_order.get(step.order)
        withheld_reason = withheld.get(str(step.order))
        if withheld_reason:
            head = "Not applicable yet."
        elif decision is not None and decision.decision is Decision.REJECTED:
            head = "Rejected by approver."
        elif decisions is not None and step.order in decisions.offered:
            head = "Deferred by approver."
        elif decisions is None:
            head = "Not executed."
        else:
            head = "Not offered on the readiness report."
        if withheld_reason:
            tail = withheld_reason
            if required:
                outstanding.append(name)
        elif required:
            held = step.from_series or 'the installed series'
            tail = f"Required \u2014 {held} is unsupported on Kubernetes {target}."
            outstanding.append(name)
        else:
            tail = f"Optional \u2014 current version is compatible with {target}"
            tail += " (AWS Insights)." if managed else "."
        deferred.append(
            DeferredRow(
                order=step.order,
                component=name,
                sub=" \u00b7 ".join(
                    p
                    for p in (
                        "EKS managed add-on" if managed else "Helm release",
                        f"ns {component.namespace}" if component and component.namespace else "",
                        f"required for {target}" if required else "recommended (optional)",
                    )
                    if p
                ),
                current=(
                    component.version if component and component.version else step.from_series
                )
                or "?",
                target=(
                    decision.recommended
                    if decision and decision.recommended
                    else aws_recommended.get(name)
                    or step.recommended_version
                    or step.to_series
                    or "?"
                ),
                target_sub=f"at Kubernetes {step.at_kubernetes}" if step.at_kubernetes else "",
                reason=f"{head} {tail}",
                required=required,
            )
        )
    deferred.sort(key=lambda r: r.order)
    outstanding = sorted(dict.fromkeys(outstanding))
    deferred_note = "A deferral is not recorded as a rejection."
    if outstanding:
        deferred_note += (
            f" {len(outstanding)} required change(s) remain outstanding before the control plane "
            f"can move to {target}: {', '.join(outstanding)}."
        )

    # ---- Authorization -------------------------------------------------------------------
    authorization: list[AuthorizationRow] = []
    approver_email = decisions.decisions[0].approver if decisions and decisions.decisions else ""
    if decisions is not None:
        approved = ", ".join(str(o) for o in decisions.approved) or "none"
        rejected = ", ".join(str(o) for o in decisions.rejected)
        offered = ", ".join(str(o) for o in decisions.offered)
        outcome_bits = [f"Steps {approved} approved"]
        if rejected:
            outcome_bits.append(f"{rejected} rejected")
        if offered:
            outcome_bits.append(f"{offered} deferred")
        outcome_bits.append(f"Done at {_hhmm(decisions.finished_at)}")
        first_at = min((d.decided_at for d in decisions.decisions), default=None)
        authorization.append(
            AuthorizationRow(
                gate="Approval",
                gate_sub="intent, per step",
                principal=approver_email or "not recorded",
                mechanism="Readiness report, single-use link"
                + (f" \u00b7 {_hhmm(first_at)}" if first_at else ""),
                outcome=" \u00b7 ".join(outcome_bits),
                outcome_hue="good" if decisions.approved else "muted",
            )
        )
    else:
        authorization.append(
            AuthorizationRow(
                gate="Approval",
                gate_sub="intent, per step",
                principal="not recorded",
                mechanism="the run's decisions document could not be read",
                outcome="unknown",
            )
        )
    arns = report.approver_arns
    authorization.append(
        AuthorizationRow(
            gate="Authorization",
            gate_sub="IAM-authenticated, per run",
            principal=", ".join(arns) or "not recorded",
            mechanism=(
                f"SSM Automation {report.automation_execution_id}, aws:approve \u00b7 eligibility: "
                "AmazonEKSClusterAdminPolicy on this cluster"
                if report.automation_execution_id
                else "not recorded"
            ),
            outcome=(
                f"Approved {_hhmm(release.approved_at)} \u00b7 steps "
                + ", ".join(str(s.order) for s in release.approved_steps)
                if release is not None
                else "no release record"
            ),
            outcome_hue="good" if arns else "critical",
        )
    )
    not_revoked = [o.component for o in report.outcomes if o.cleanup_error]
    authorization.append(
        AuthorizationRow(
            gate="Cluster access",
            gate_sub="per step, just-in-time",
            principal="EKS access entry (cluster-admin, lane role)",
            mechanism=(
                "created by the state machine before each step, deleted after it on every path"
            ),
            outcome=(
                "NOT REVOKED for " + ", ".join(not_revoked) + " \u2014 check and remove"
                if not_revoked
                else f"revoked after each of {len(report.outcomes)} step(s) \u00b7 none outstanding"
            ),
            outcome_hue="critical" if not_revoked else "good",
        )
    )
    summary_bits = []
    if approver_email:
        summary_bits.append(f"approved by {approver_email}")
    if arns:
        summary_bits.append(f"authorized by {short_arn(arns[0])}")
    summary_bits.append(
        "ACCESS ENTRY NOT REVOKED" if not_revoked else "no cluster access outstanding"
    )

    # ---- Components ----------------------------------------------------------------------
    inventory_rows: list[InventoryRow] = []
    inventory_sub = ""
    inventory_note = ""
    if inventory is not None:
        verdicts = {}
        if assessment is not None:
            verdicts = {d.workload_ref: d for d in assessment.decisions}
        recommended_by_component = {
            s.component: (s.recommended_version or s.to_series or "")
            for s in (plan.steps if plan is not None else ())
            if s.component
        }
        # A MANAGED ADD-ON'S RECOMMENDATION IS AWS'S FULL BUILD STRING. The plan's frozen
        # `recommended_version` cannot hold `-eksbuild.N`, so the approval page reads it off the
        # assessment through `catalogue_recommendation`; the same call here keeps the two in step.
        for d in verdicts.values():
            aws_version, _at = catalogue_recommendation(d)
            if aws_version and d.identity:
                recommended_by_component[d.identity] = aws_version
        for d in decision_by_order.values():
            if d.recommended:
                recommended_by_component[d.component] = d.recommended
        label = {
            CompatibilityVerdict.COMPATIBLE: ("Compatible", "good"),
            CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE: ("Upgrade required", "warning"),
            CompatibilityVerdict.INCOMPATIBLE: ("Incompatible", "critical"),
            CompatibilityVerdict.UNRESOLVED: ("Unresolved", "muted"),
        }
        dots = {
            Confidence.HIGH: "\u25cf\u25cf\u25cf High",
            Confidence.MEDIUM: "\u25cf\u25cf Medium",
            Confidence.LOW: "\u25cf Low",
            Confidence.UNRESOLVED: "Unresolved",
        }
        for component in inventory.components:
            name = component.identity or component.workload_ref
            decision = verdicts.get(component.workload_ref)
            verdict_label, hue = ("Not assessed", "muted")
            source = ""
            if decision is not None:
                verdict_label, hue = label[decision.verdict]
                first = decision.hops[0]
                source = {
                    "eks-insight": "AWS EKS Insights (this cluster, next minor)",
                    "stored-fact": "Upstream compatibility matrix"
                    + (
                        f" ({decision.knowledge_base_revision})"
                        if decision.knowledge_base_revision
                        else ""
                    ),
                }.get(first.decided_from, "no compatibility source")
            kind_of = component.workload_ref.split('/')[0]
            inventory_rows.append(
                InventoryRow(
                    component=name,
                    where=f"{component.namespace or '-'} \u00b7 {kind_of}",
                    installed=component.version or "unresolved",
                    verdict=verdict_label,
                    verdict_hue=hue,  # type: ignore[arg-type]
                    confidence=dots.get(component.confidence, str(component.confidence)),
                    recommended=recommended_by_component.get(name, "\u2014"),
                    source=source,
                )
            )
        addons = sum(1 for c in inventory.components if c.install_method == "EKS_MANAGED_ADDON")
        helm = sum(1 for c in inventory.components if c.install_method == "HELM")
        bits = [f"{addons} EKS managed add-ons, {helm} Helm releases"]
        if snapshot is not None:
            gaps = len(snapshot.collection_gaps)
            bits.append("no collection gaps" if not gaps else f"{gaps} collection gap(s)")
            statuses = {i.status or "UNKNOWN" for i in snapshot.insights}
            if snapshot.insights:
                bits.append(
                    f"{len(snapshot.insights)} AWS Insights: "
                    + ("PASSING" if statuses == {"PASSING"} else ", ".join(sorted(statuses)))
                )
        inventory_sub = " \u00b7 ".join(bits)
        captured = str(event.get("snapshot_captured_at") or inventory.snapshot_captured_at)
        inventory_note = (
            f"Snapshot taken at {_hhmm(captured)}, prior to execution. Identical to the inventory "
            "on the readiness report."
        )

    footer = []
    if plan_digest:
        footer.append(f"plan {plan_digest.removeprefix('sha256:')[:12]}")
    snap = str(event.get("snapshot_digest") or "")
    if snap:
        footer.append(f"snapshot {snap.removeprefix('sha256:')[:12]}")
    footer.append(f"release record {storage_keys.release_key(cluster, report.run_id)}")

    return UpgradeReportView(
        cluster=cluster,
        region=report.cluster.region or str(event.get("region") or ""),
        account=_account(arns),
        current_kubernetes_version=current,
        target_kubernetes_version=target,
        window=_window(str(event.get("snapshot_captured_at") or ""), report.finished_at),
        run_id=report.run_id,
        authorized=len(report.outcomes),
        upgraded=len(report.upgraded),
        rolled_back=len(report.rolled_back),
        action_required=len(report.referred),
        deferred_count=len(deferred),
        authorization_summary=" \u00b7 ".join(summary_bits),
        authorization=tuple(authorization),
        applied=tuple(applied),
        deferred_note=deferred_note,
        deferred=tuple(deferred),
        inventory_sub=inventory_sub,
        inventory_note=inventory_note,
        inventory=tuple(inventory_rows),
        footer=tuple(footer),
    )
