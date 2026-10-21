"""The report as Markdown. A pure function of the value set.

Imports `..model` and nothing else -- no stage document, no fact store, no
`decide`, no `order`. That restriction is the point and a test enforces it: with
`ReportView` as the only input, "this renderer computed its own total" becomes a
visible import violation rather than a helper call nobody notices.

A Markdown renderer that renders the undecided rows and then renders *all* of
`unsequenceable` with no filtering double-counts every component that appears in both
(which is every undecided one). Fixing only the HTML path leaves it live. Here there is one
list -- `view.unresolved` -- so the bug has nowhere to live.
"""

from __future__ import annotations

from ..model import (
    ABSENT,
    ApprovalControls,
    ComponentGroup,
    Provenance,
    ReportView,
    UnresolvedOrigin,
)

_GROUP_TITLES: dict[ComponentGroup, str] = {
    ComponentGroup.MANAGED_ADDON: "EKS-managed add-ons",
    ComponentGroup.HELM_RELEASE: "CNCF add-ons: Helm-installed",
    ComponentGroup.OTHER_WORKLOAD: "CNCF add-ons: other workloads",
}

_ORIGIN_NOTES: dict[UnresolvedOrigin, str] = {
    UnresolvedOrigin.BOTH: "undecided, so not sequenced",
    UnresolvedOrigin.ASSESSMENT_ONLY: "undecided",
    UnresolvedOrigin.PLAN_ONLY: "decided, but could not be sequenced",
}


def _cell(value: str | None) -> str:
    """A table cell, with pipes escaped so one value cannot break the table."""
    return (value or ABSENT).replace("|", "\\|")


def _source_cell(row) -> tuple[str, str]:
    """(source, synopsis) for one component. First source only."""
    if not row.sources:
        return ABSENT, ABSENT
    source = row.sources[0]
    star = " *" if source.official else ""
    label = f"{source.title}{star}"
    cell = f"[{label}]({source.url})" if source.url else label
    return _cell(cell), _cell(source.synopsis or ABSENT)


def to_markdown(
    view: ReportView,
    provenance: Provenance,
    approvals: ApprovalControls | None = None,
) -> str:
    """Render the value set as Markdown.

    Carries the pending approvals for the same reason the HTML does, and with the
    same wording: both views or neither. A Markdown copy that omitted the decision
    would be a second report disagreeing with the first about what the run is
    waiting on, which is exactly the divergence this package was written to remove.
    """
    controls = approvals if approvals is not None else ApprovalControls()
    totals = view.totals
    # Readiness for the target, not plan feasibility: `feasible: false` means some component has
    # no recorded version supporting the target, so the line names those components (read off
    # the same `unresolved` rows the Unresolved section lists) rather than failing the plan.
    blockers = list(dict.fromkeys(r.component for r in view.unresolved))
    if view.feasible:
        feasible = "yes"
    elif blockers:
        feasible = (
            f"{len(blockers)} unresolved - {', '.join(blockers)} (no recorded version supports "
            f"Kubernetes {view.target_kubernetes_version}; see Unresolved)"
        )
    else:
        feasible = "unresolved - a component has no recorded version that supports the target"
    lines = [
        f"# Upgrade readiness - {view.cluster.name}",
        "",
        f"Kubernetes {view.current_kubernetes_version} to "
        f"{view.target_kubernetes_version} via {' then '.join(view.hops)}.",
        "",
        f"- **Ready for Kubernetes {view.target_kubernetes_version}:** {feasible}",
        f"- **Detected:** {totals.detected} "
        f"({totals.managed_addons} managed add-ons, "
        f"{totals.helm_releases} Helm releases, "
        f"{totals.other_workloads} other workloads)",
        f"- **Compatible:** {totals.compatible}",
        f"- **Upgrade needed:** {totals.upgrade_needed}",
        f"- **Upgrade recommended:** {totals.upgrade_recommended}",
        f"- **Unresolved:** {totals.unresolved} "
        f"({totals.unresolved_undecided} undecided, "
        f"{totals.unresolved_unsequenceable_only} sequenced-blocked only)",
        f"- **Knowledge:** {totals.answered_gaps} answered, "
        f"{totals.researchable_gaps} researchable",
        "",
        "### Evidence chain",
        "",
        "| Document | Digest |",
        "|---|---|",
        f"| snapshot | `{provenance.snapshot_digest}` |",
        f"| inventory | `{provenance.report_digest}` |",
        f"| ledger | `{provenance.ledger_digest}` |",
        f"| knowledge | `{provenance.store_digest}` |",
        f"| assessment | `{provenance.assessment_digest}` |",
        f"| plan | `{provenance.plan_digest}` |",
        f"| this report | `{view.digest()}` |",
        "",
    ]

    if view.store_errors:
        lines += [
            "> **The fact store was degraded when this report was produced.** "
            "Components below may read as having no fact when the fact merely "
            "would not parse.",
            "",
        ]
        lines += [f"> - {e}" for e in view.store_errors]
        lines += [""]

    lines += ["## Components", ""]
    rendered_any = False
    for group, title in _GROUP_TITLES.items():
        rows = [r for r in view.components if r.group is group]
        if not rows:
            continue
        rendered_any = True
        lines += [
            f"### {title} ({len(rows)})",
            "",
            "| Component | Installed | Status | Confidence | Recommended "
            "| Source | What the evidence says |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in rows:
            source, synopsis = _source_cell(row)
            recommended = _cell(row.recommended_version)
            if row.stays:
                recommended += " (stay)"
            lines.append(
                f"| {_cell(row.component)} | {_cell(row.installed_version)} "
                f"| {_cell(row.verdict)} | {_cell(row.confidence)} "
                f"| {recommended} | {source} | {synopsis} |"
            )
        lines.append("")
    if not rendered_any:
        lines += ["_No components were detected._", ""]

    if view.recommended_upgrades:
        lines += [
            f"## Upgrade recommended ({totals.upgrade_recommended})",
            "",
            "_Already compatible with the target; optional moves to the newest "
            "supported version._",
            "",
            "| Component | Installed series | Recommended | Apply at |",
            "|---|---|---|---|",
        ]
        for row in view.recommended_upgrades:
            lines.append(
                f"| {_cell(row.component)} | {_cell(row.from_series)} "
                f"| {_cell(row.recommended_version)} "
                f"| Kubernetes {_cell(row.at_kubernetes)} |"
            )
        lines.append("")

    lines += [f"## Upgrade sequence ({totals.steps})", ""]
    if view.steps:
        lines += ["| # | Kind | Action | Reversible |", "|---|---|---|---|"]
        for step in view.steps:
            reversible = (
                "ONE WAY"
                if step.one_way
                else (step.reversibility or ABSENT)
            )
            lines.append(
                f"| {step.order} | {step.label} | {_cell(step.description)} "
                f"| {_cell(reversible)} |"
            )
    else:
        lines.append("_No steps could be sequenced._")
    lines.append("")

    # ONE list, ONE count. The number in the heading is the same field the HTML
    # tile reads, because there is only one field.
    lines += [f"## Unresolved ({totals.unresolved})", ""]
    if view.unresolved:
        lines += [
            "_Gaps in recorded knowledge, not upgrade blockers - verify each "
            "against its source before acting._",
            "",
            "| Component | Status | Why it is here | Reason |",
            "|---|---|---|---|",
        ]
        for row in view.unresolved:
            lines.append(
                f"| {_cell(row.component)} | {_cell(row.verdict)} "
                f"| {_ORIGIN_NOTES[row.origin]} | {_cell(row.reason)} |"
            )
    else:
        lines.append(
            "_Nothing unresolved: every component was decided and sequenced._"
        )
    lines.append("")

    lines += _approvals_section(controls)

    return "\n".join(lines)


def _approvals_section(controls: ApprovalControls) -> list[str]:
    """The decision, in Markdown. No command is rendered here, and that is deliberate.

    The HTML page shows the signed command because it is the surface a person acts
    from. This view is the machine-readable and diff-able copy, so it carries the
    two digests and the candidate URI -- everything needed to construct the command
    or to audit one afterwards -- without a copy of the command itself. One place
    that spells the request, so a change to it cannot leave a second, stale spelling
    behind in another format.
    """
    lines = [f"## Awaiting your approval ({len(controls.pending)})", ""]
    if not controls.pending:
        lines += [
            "_Nothing is awaiting approval. A component with no recorded fact "
            "stays reported as unknown rather than guessed at._",
            "",
        ]
        return lines
    lines += [
        "_A researched fact never stores itself. Approving one is a human act, "
        "recorded against the approver's own principal together with the digest "
        "of the report they read. Run the signed command from the HTML view._",
        "",
        "| Component | Series | Confidence | Direction | Candidate digest "
        "| Report digest | Candidate |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in controls.pending:
        lines.append(
            f"| {_cell(row.component)} | {_cell(row.version_series)} "
            f"| {_cell(row.confidence)} | {_cell(row.change_direction)} "
            f"| `{row.candidate_digest}` | `{row.report_digest}` "
            f"| {_cell(row.candidate_uri)} |"
        )
    lines.append("")
    for row in controls.pending:
        lines += [f"- **{_cell(row.component)}:** {_cell(row.headline)}"]
    lines.append("")
    return lines
