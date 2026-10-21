"""The view the upgrade report page renders, and the reducers that fill it from evidence.

    in    an `UpgradeRunReport`, plus whatever the report stage could read beside it
    out   one `UpgradeReportView`, a flat description of every cell on the page

**PURE, AND DELIBERATELY SO.** Nothing here reads S3, DynamoDB or a clock. The report stage
does the reading and hands the raw payloads in; this module turns a `pods.json` into pod rows,
a log body into the lines worth showing, a checkpoint list into chips. That split is what lets
`report_page.to_upgrade_report_html` stay a pure function of its arguments -- re-render the same
view and the same bytes come out -- and what lets this file ship in the lane zip beside
`report_page.py` without dragging `storage/` in with it.

**THE VIEW IS THE CONTRACT BETWEEN THE STAGE AND THE PAGE.** The page renders what is here and
nothing else; the stage fills what it could read and leaves the rest empty. An empty field is
rendered as a sentence ("no pod state was captured"), never as a blank, so a reader can tell
"nothing to say" from "nothing was read".

**`view_from_report` IS THE FLOOR.** Given only the durable `UpgradeRunReport` -- no bundles, no
decisions, no plan -- it still produces a complete view, so a report rendered from the document
alone is a valid page rather than a crash. Everything the stage adds on top is enrichment.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from ..models import ContractModel
from .models import LaneOutcome, UpgradeRunReport, UpgradeStatus, UpgradeTriage
from .validate import notable_log_lines

Hue = Literal["good", "warning", "critical", "muted", "accent"]

# What each status says in a person's vocabulary, the hue it wears, its glyph, and the order the
# table sorts it into: what needs a person first, then what was undone, then what worked.
STATUS_LABEL: dict[UpgradeStatus, tuple[int, str, Hue, str]] = {
    UpgradeStatus.REFERRED: (0, "Action required", "critical", "!"),
    UpgradeStatus.LANE_NOT_OPENED: (1, "Not started", "critical", "!"),
    UpgradeStatus.REFUSED_BEFORE_MUTATION: (2, "Refused, unchanged", "muted", "\u2013"),
    UpgradeStatus.ROLLED_BACK: (3, "Rolled back", "warning", "\u21bb"),
    UpgradeStatus.UPGRADED: (4, "Upgraded", "good", "\u2713"),
}


class PodRow(ContractModel):
    """One pod as the page shows it: the four facts a person scans for."""

    name: str = Field(min_length=1)
    ready: bool | None = None
    restarts: int = 0
    state: str = ""
    image: str = ""
    broken: bool = False


class Chip(ContractModel):
    """One checkpoint, as a small mono chip under the result pill."""

    label: str = Field(min_length=1)
    hue: Hue = "muted"


class RollbackDetail(ContractModel):
    """The rollback half of a rolled-back row."""

    command: str = ""
    duration: str = ""
    lines: tuple[str, ...] = ()
    verdict_line: str = ""
    verdict_hue: Hue = "muted"


class AppliedDetail(ContractModel):
    """The row a `Details` toggle reveals under one applied step."""

    validation_heading: str = "Validation record"
    validation: tuple[str, ...] = ()
    verdict_line: str = ""
    verdict_hue: Hue = "muted"
    pods_heading: str = "Pod state"
    pods_note: str = ""
    pods: tuple[PodRow, ...] = ()
    state_heading: str = ""
    state_lines: tuple[str, ...] = ()
    rollback: RollbackDetail | None = None
    log_heading: str = ""
    log_lines: tuple[str, ...] = ()
    executed: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    warning: str = ""


class AppliedRow(ContractModel):
    order: int = Field(ge=1)
    component: str = Field(min_length=1)
    sub: str = ""
    before: str = ""
    before_sub: str = ""
    after: str = ""
    after_sub: str = ""
    # Set only when where the component ended up differs from what was applied.
    current_note: str = ""
    status: UpgradeStatus
    chips: tuple[Chip, ...] = ()
    detail: AppliedDetail


class DeferredRow(ContractModel):
    order: int = Field(ge=1)
    component: str = Field(min_length=1)
    sub: str = ""
    current: str = ""
    target: str = ""
    target_sub: str = ""
    reason: str = Field(min_length=1)
    required: bool = False


class AuthorizationRow(ContractModel):
    gate: str = Field(min_length=1)
    gate_sub: str = ""
    principal: str = ""
    mechanism: str = ""
    outcome: str = ""
    outcome_hue: Hue = "muted"


class InventoryRow(ContractModel):
    component: str = Field(min_length=1)
    where: str = ""
    installed: str = ""
    verdict: str = ""
    verdict_hue: Hue = "muted"
    confidence: str = ""
    recommended: str = ""
    source: str = ""


class UpgradeReportView(ContractModel):
    """Every cell on the page. The renderer adds markup and nothing else."""

    cluster: str = Field(min_length=1)
    region: str = ""
    account: str = ""
    current_kubernetes_version: str = ""
    target_kubernetes_version: str = Field(min_length=1)
    window: str = ""
    run_id: str = Field(min_length=1)

    authorized: int = 0
    upgraded: int = 0
    rolled_back: int = 0
    action_required: int = 0
    deferred_count: int = 0

    authorization_summary: str = ""
    authorization: tuple[AuthorizationRow, ...] = ()
    applied: tuple[AppliedRow, ...] = ()
    deferred_note: str = ""
    deferred: tuple[DeferredRow, ...] = ()
    inventory_sub: str = ""
    inventory_note: str = ""
    inventory: tuple[InventoryRow, ...] = ()

    footer: tuple[str, ...] = ()

    @property
    def headline_pill(self) -> tuple[str, Hue, str]:
        """The one pill in the header: what needs a person, else what was undone, else fine."""
        if self.action_required:
            return (f"{self.action_required} action required", "critical", "!")
        if self.rolled_back:
            return (f"{self.rolled_back} rolled back", "warning", "\u21bb")
        return (f"{self.upgraded} upgraded", "good", "\u2713")


# ---------------------------------------------------------------------------
# Reducers: raw evidence -> view cells. Tolerant of shape, intolerant of guessing.
# ---------------------------------------------------------------------------


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


_HASH_SEGMENT = re.compile(r"^[a-z0-9]{5}$|^[a-f0-9]{8,10}$")


def _workload_name(pod_name: str) -> str:
    """`metrics-server-59878f459f-lf95s` -> `metrics-server`; `aws-node-2mqng` -> `aws-node`.

    Strips the trailing ReplicaSet/DaemonSet hash segments Kubernetes appends. Anything that does
    not look like a hash is kept, so a workload whose name ends in five letters is not truncated.
    """
    parts = pod_name.split("-")
    while len(parts) > 1 and _HASH_SEGMENT.match(parts[-1]):
        parts.pop()
    return "-".join(parts)


def pods_from_json(payload: Any) -> tuple[PodRow, ...]:
    """`kubectl get pods -o json` -> the rows the page shows.

    Reads the same fields `validate.pods_from_kubectl` does, plus the running image and a
    `broken` flag for the API-server-stated conditions the floor treats as brokenness, so the
    page can tint exactly the rows the floor would have.
    """
    from .validate import BROKEN_PHASES, BROKEN_TERMINATED_REASONS, BROKEN_WAITING_REASONS

    if not isinstance(payload, Mapping):
        return ()
    items = payload.get("items")
    if not isinstance(items, Sequence):
        return ()
    rows: list[PodRow] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        status = item.get("status") if isinstance(item.get("status"), Mapping) else {}
        name = _str(metadata.get("name")) or "<unnamed>"
        phase = _str(status.get("phase"))
        ready: bool | None = None
        conditions = status.get("conditions")
        if isinstance(conditions, Sequence):
            ready = any(
                isinstance(c, Mapping) and c.get("type") == "Ready" and c.get("status") == "True"
                for c in conditions
            )
        restarts = 0
        state = phase
        image = ""
        broken = phase in BROKEN_PHASES
        statuses = status.get("containerStatuses")
        if isinstance(statuses, Sequence):
            # THE IMAGE SHOWN IS THE MAIN CONTAINER'S. A pod's containers come back in no useful
            # order and a sidecar (aws-node's network-policy agent) can sort first; the container
            # named like the pod's own workload is the one whose version a reader is checking.
            main = _workload_name(name)
            ordered = sorted(
                (c for c in statuses if isinstance(c, Mapping)),
                key=lambda c: 0 if _str(c.get("name")) == main else 1,
            )
            for container in ordered:
                count = container.get("restartCount")
                if isinstance(count, int):
                    restarts += count
                if not image:
                    image = _str(container.get("image")).rsplit("/", 1)[-1]
                st = container.get("state")
                if isinstance(st, Mapping):
                    waiting = st.get("waiting")
                    terminated = st.get("terminated")
                    if isinstance(waiting, Mapping) and _str(waiting.get("reason")):
                        reason = _str(waiting.get("reason"))
                        state = reason
                        broken = broken or reason in BROKEN_WAITING_REASONS
                    elif isinstance(terminated, Mapping) and _str(terminated.get("reason")):
                        reason = _str(terminated.get("reason"))
                        state = reason
                        broken = broken or reason in BROKEN_TERMINATED_REASONS
        rows.append(
            PodRow(
                name=name, ready=ready, restarts=restarts, state=state, image=image, broken=broken
            )
        )
    return tuple(sorted(rows, key=lambda r: r.name))


def log_excerpt(text: str, *, limit: int = 6) -> tuple[tuple[str, ...], str]:
    """The lines worth showing from one container log, and the one-line count under them.

    Uses `validate.notable_log_lines` -- the same idea of "worth looking at" the agent's prompt
    used -- so the page and the agent agree about which lines are notable. Nothing decides on it.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    notable = notable_log_lines(text, limit=limit)
    if not lines:
        return (), "log empty"
    if not notable:
        return (), f"0 error lines / {len(lines)}"
    return notable, f"{len(notable_log_lines(text, limit=10_000))} error lines / {len(lines)}"


def event_summary(payload: Any, *, names: Sequence[str]) -> str:
    """One sentence about the events touching the named objects."""
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), Sequence):
        return "events not captured"
    normal = 0
    warnings: dict[str, int] = {}
    for item in payload["items"]:
        if not isinstance(item, Mapping):
            continue
        involved = item.get("involvedObject")
        target = _str(involved.get("name")) if isinstance(involved, Mapping) else ""
        if names and not any(target == n or target.startswith(n) for n in names):
            continue
        count = item.get("count") if isinstance(item.get("count"), int) else 1
        if _str(item.get("type")) == "Warning":
            reason = _str(item.get("reason")) or "Warning"
            warnings[reason] = warnings.get(reason, 0) + count
        else:
            normal += 1
    if warnings:
        listed = ", ".join(f"{reason} \u00d7{n}" for reason, n in sorted(warnings.items()))
        return f"Events: {normal} Normal, Warning: {listed}."
    if normal:
        return f"Events: {normal} Normal, 0 Warning."
    return "Events: none retained for these objects."


def workload_lines(payload: Any) -> tuple[str, ...]:
    """`kubectl get deployments,daemonsets,statefulsets -o json` -> one rollout line per workload.

    The line a person reads to know the rollout finished: desired/ready/updated/available, and
    whether the controller has observed the current generation. Kubernetes omits zero-valued
    status integers, so an absent field is read as 0, not unknown.
    """
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), Sequence):
        return ()
    out: list[str] = []
    for item in payload["items"]:
        if not isinstance(item, Mapping):
            continue
        kind = _str(item.get("kind"))
        meta = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        spec = item.get("spec") if isinstance(item.get("spec"), Mapping) else {}
        status = item.get("status") if isinstance(item.get("status"), Mapping) else {}
        name = _str(meta.get("name")) or "?"

        def n(key: str, source: Mapping[str, Any] = status) -> int:
            value = source.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        if kind == "DaemonSet":
            facts = (
                f"desired {n('desiredNumberScheduled')}, current {n('currentNumberScheduled')}, "
                f"updated {n('updatedNumberScheduled')}, ready {n('numberReady')}, "
                f"available {n('numberAvailable')}"
            )
        else:
            facts = (
                f"replicas {n('readyReplicas')}/{n('replicas', spec) or n('replicas')}, "
                f"updated {n('updatedReplicas')}, available {n('availableReplicas')}"
            )
        generation = meta.get("generation")
        observed = status.get("observedGeneration")
        settled = (
            "generation observed"
            if isinstance(generation, int) and observed == generation
            else "generation not yet observed"
            if isinstance(generation, int)
            else ""
        )
        conditions = status.get("conditions")
        notable = ""
        if isinstance(conditions, Sequence):
            for condition in conditions:
                if not isinstance(condition, Mapping):
                    continue
                ctype, cstatus = _str(condition.get("type")), _str(condition.get("status"))
                reason = _str(condition.get("reason"))
                if ctype == "Progressing" and reason:
                    notable = f"Progressing={cstatus} ({reason})"
                if ctype == "Available" and cstatus != "True":
                    notable = f"Available={cstatus}"
        parts = [p for p in (facts, settled, notable) if p]
        out.append(f"{kind} {name}: " + "; ".join(parts) + ".")
    return tuple(out)


def helm_revision_line(history: Any) -> str:
    """The deployed revision, from `helm history -o json`."""
    if not isinstance(history, Sequence):
        return ""
    deployed = [
        r for r in history if isinstance(r, Mapping) and _str(r.get("status")) == "deployed"
    ]
    if not deployed:
        return ""
    rev = deployed[-1]
    return (
        f"Helm revision {rev.get('revision')} deployed (chart {_str(rev.get('chart'))}, "
        f"app {_str(rev.get('app_version'))})."
    )


def addon_state_line(describe: Any) -> str:
    """`describe-addon` -> one line: version, status, health."""
    addon = describe.get("addon") if isinstance(describe, Mapping) else None
    if not isinstance(addon, Mapping):
        return ""
    health = addon.get("health") if isinstance(addon.get("health"), Mapping) else {}
    issues = health.get("issues") if isinstance(health.get("issues"), Sequence) else []
    issue_text = f"{len(issues)} health issue(s)" if issues else "no health issues"
    return f"{_str(addon.get('addonVersion'))} {_str(addon.get('status'))}, {issue_text}."


def chips_for(outcome: LaneOutcome) -> tuple[Chip, ...]:
    """Every checkpoint, then the re-validation, as chips. `HEALTHY t+180s`."""
    chips: list[Chip] = []
    for cp in outcome.checkpoints:
        verdict = cp.verdict.value
        hue: Hue = (
            "good"
            if verdict == "HEALTHY"
            else "critical"
            if verdict in ("UNHEALTHY", "REFER")
            else "muted"
        )
        chips.append(Chip(label=f"{verdict} t+{cp.elapsed_seconds}s", hue=hue))
    if outcome.revalidation is not None:
        verdict = outcome.revalidation.verdict.value
        chips.append(
            Chip(
                label=f"{verdict} post-rollback",
                hue="good" if verdict == "HEALTHY" else "critical",
            )
        )
    return tuple(chips)


def validation_lines(triage: UpgradeTriage | None) -> tuple[tuple[str, ...], str, Hue]:
    """The agent's observations, then its reasons, then the verdict line."""
    if triage is None:
        return (), "Verdict: not judged \u2014 the agent returned no triage", "muted"
    lines = tuple(triage.observations) + tuple(f"\u2014 {r}" for r in triage.reasons)
    verdict = triage.verdict.value
    hue: Hue = "good" if verdict == "HEALTHY" else "critical" if verdict != "SETTLING" else "muted"
    tail = {
        "HEALTHY": "validation passed",
        "UNHEALTHY": "rollback initiated",
        "REFER": "referred to a person",
        "SETTLING": "not yet established",
    }.get(verdict, "")
    return lines, f"Verdict: {verdict}" + (f" \u2014 {tail}" if tail else ""), hue


# ---------------------------------------------------------------------------
# The floor: a complete view from the report document alone.
# ---------------------------------------------------------------------------


def detail_from_outcome(outcome: LaneOutcome) -> AppliedDetail:
    """What the `UpgradeRunReport` alone can say about one lane. The stage enriches this."""
    validation, verdict_line, hue = validation_lines(outcome.triage)
    if not validation and outcome.observed:
        # No triage reached the report; the lane's own observed lines are the next best account.
        validation = tuple(outcome.observed)
    rollback: RollbackDetail | None = None
    if outcome.rollback_command or outcome.status is UpgradeStatus.ROLLED_BACK:
        lines: list[str] = []
        if outcome.rollback_succeeded is True:
            lines.append("The rollback completed.")
        elif outcome.rollback_succeeded is False:
            lines.append("The rollback itself FAILED.")
        rv_line, rv_hue = "", "muted"
        if outcome.revalidation is not None:
            rv = outcome.revalidation.verdict.value
            rv_line = f"Verdict: {rv} post-rollback" + (
                " \u2014 rollback verified" if rv == "HEALTHY" else " \u2014 still unhealthy"
            )
            rv_hue = "good" if rv == "HEALTHY" else "critical"
            lines.extend(outcome.revalidation.observations)
        rollback = RollbackDetail(
            command=outcome.rollback_command,
            lines=tuple(lines),
            verdict_line=rv_line,
            verdict_hue=rv_hue,
        )
    executed = tuple(a for a in outcome.attempted if a and a != outcome.rollback_command)
    evidence = tuple(
        f"{label}: {uri}"
        for label, uri in (
            ("before", outcome.diagnosis_before_uri),
            ("after", outcome.diagnosis_after_uri),
        )
        if uri
    )
    warning = ""
    if outcome.cleanup_error:
        warning = (
            "WARNING: this step's temporary cluster access entry could not be deleted "
            f"({outcome.cleanup_error}). Check and revoke it."
        )
    heading = "Rollback trigger" if outcome.status is UpgradeStatus.ROLLED_BACK else (
        "What was tried and what was seen"
        if outcome.status is UpgradeStatus.REFERRED
        else "Validation record"
    )
    if outcome.status is UpgradeStatus.REFERRED and outcome.attempted:
        validation = tuple(f"tried: {a}" for a in outcome.attempted) + tuple(
            f"saw: {o}" for o in outcome.observed
        )
    return AppliedDetail(
        validation_heading=heading,
        validation=validation,
        verdict_line=verdict_line,
        verdict_hue=hue,
        pods_note="no pod state was captured for this page",
        rollback=rollback,
        executed=executed,
        evidence=evidence or ("no diagnosis bundle was captured",),
        warning=warning,
    )


def same_release(resulting: str, applied: str) -> bool:
    """Whether what runs is the release that was applied, across the spellings that carry it.

    `applied` is the plan's bare `0.9.0`; `resulting` is read off the cluster -- an image tag
    `v0.9.0`, or EKS's `v1.23.1-eksbuild.1`. The same release in three spellings is not a
    discrepancy worth a "current:" note; a different number is.

    >>> same_release("v0.9.0", "0.9.0")
    True
    >>> same_release("v1.23.1-eksbuild.1", "1.23.1")
    True
    >>> same_release("v0.6.4", "0.9.0")
    False
    >>> same_release("", "0.9.0")
    False
    """
    have, want = resulting.strip().lstrip("vV"), applied.strip().lstrip("vV")
    return bool(have) and bool(want) and (have == want or have.startswith(want + "-"))


def row_from_outcome(outcome: LaneOutcome, detail: AppliedDetail | None = None) -> AppliedRow:
    current_note = ""
    if outcome.resulting and not same_release(outcome.resulting, outcome.applied):
        current_note = f"current: {outcome.resulting}"
    elif outcome.status is UpgradeStatus.ROLLED_BACK:
        current_note = f"current: {outcome.installed or 'previous version'} (restored)"
    return AppliedRow(
        order=outcome.step_order,
        component=outcome.component,
        sub="EKS managed add-on" if outcome.managed_addon else "Helm release",
        before=outcome.installed or "?",
        after=outcome.applied or "?",
        current_note=current_note,
        status=outcome.status,
        chips=chips_for(outcome),
        detail=detail or detail_from_outcome(outcome),
    )


def sort_key(row: AppliedRow) -> tuple[int, int]:
    return (STATUS_LABEL.get(row.status, (9, "", "muted", ""))[0], row.order)


def view_from_report(report: UpgradeRunReport, *, rendered_at: str = "") -> UpgradeReportView:
    """A complete view from the durable document alone. Everything else is enrichment."""
    rows = tuple(sorted((row_from_outcome(o) for o in report.outcomes), key=sort_key))
    referred = len(report.referred)
    principal = ", ".join(report.approver_arns) or "not recorded"
    authorization = (
        AuthorizationRow(
            gate="Authorization",
            gate_sub="IAM-authenticated, per run",
            principal=principal,
            mechanism=(
                f"SSM Automation {report.automation_execution_id}, aws:approve"
                if report.automation_execution_id
                else "not recorded"
            ),
            outcome=f"{len(report.outcomes)} step(s) authorized",
            outcome_hue="good" if report.approver_arns else "muted",
        ),
    )
    return UpgradeReportView(
        cluster=report.cluster.name,
        region=report.cluster.region or "",
        target_kubernetes_version=report.target_kubernetes_version,
        run_id=report.run_id,
        authorized=len(report.outcomes),
        upgraded=len(report.upgraded),
        rolled_back=len(report.rolled_back),
        action_required=referred,
        authorization_summary=f"authorized by {principal}",
        authorization=authorization,
        applied=rows,
        footer=(f"generated {rendered_at}",) if rendered_at else (),
    )


def short_arn(arn: str) -> str:
    """`arn:aws:iam::1234:role/Admin` -> `role/Admin`, for a summary line."""
    return arn.rsplit(":", 1)[-1] if ":" in arn else arn
