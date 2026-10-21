"""The upgrade report: ONE page for the whole run, in the approval page's own idiom.

    in    one `UpgradeReportView` (or, at minimum, one `UpgradeRunReport`)
    out   one HTML page

**THE SHAPE IS THE APPROVAL PAGE'S, ON PURPOSE.** Header, tiles, then one collapsed card per
question -- Authorization, Applied, Deferred, Components -- each holding one table. A row's
`Details` toggle reveals the evidence under it, the way the approval page reveals a brief. The
human asked for exactly this after two drafts: "look at the upgrade approval, we have so much
data there but we presented it well". So the page reuses `_CSS` and `_ADVICE_CSS` from the
presentation renderer rather than inventing markup the stylesheet has never heard of, and adds
only the rules the evidence rows need.

**EVERY CARD STARTS CLOSED.** The card summaries carry the headline -- "2 upgraded, 1 rolled
back", "approved by ... authorized by ..." -- so the page is readable without opening anything,
and a reader opens the one card they came for.

**IT RENDERS A VIEW, NOT A DOCUMENT.** `report_view.UpgradeReportView` is a flat description of
every cell; the report stage fills it from the durable `UpgradeRunReport` plus whatever evidence
it could read (bundles, decisions, plan, inventory). Given only the report, `view_from_report`
produces the floor and this page still renders. That keeps this function pure -- no S3, no
clock, `rendered_at` is passed in -- so the same view renders to the same bytes.

**NO CONTROLS.** Nothing is being decided here. No form, no nonce, no task token, which is what
makes the page safe to mail a link to.

**WHY IT LIVES HERE AND NOT IN `view/render/`.** Two tests in `tests/view/test_render_boundary.py`
refused it there, and both were right: a module in `view/render/` joins `renderer_digest`, a
field on the PRESENTATION document, and this page has nothing to do with that document. So it
belongs to the upgrade stage and imports the stylesheet, not the other way round.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..view.render.html import _ADVICE_CSS, _CSS, _e
from .models import UpgradeRunReport
from .report_view import (
    STATUS_LABEL,
    AppliedDetail,
    AppliedRow,
    AuthorizationRow,
    DeferredRow,
    Hue,
    InventoryRow,
    PodRow,
    UpgradeReportView,
    sort_key,
    view_from_report,
)

_HUE_VAR: dict[Hue, str] = {
    "good": "var(--status-good)",
    "warning": "var(--status-warning)",
    "critical": "var(--status-critical)",
    "muted": "var(--text-muted)",
    "accent": "var(--accent)",
}

# Only what the evidence rows need on top of the two shared stylesheets.
_REPORT_CSS = """
.evd { display:grid; grid-template-columns:1fr 1fr; gap:1rem 1.4rem; }
@media (max-width:900px) { .evd { grid-template-columns:1fr; } }
.evd h4 { margin:0 0 .35rem; font-size:.7rem; text-transform:uppercase; letter-spacing:.07em;
  color:var(--text-secondary); font-weight:700; }
.evd h4 .n { color:var(--text-muted); font-weight:400; text-transform:none; letter-spacing:0; }
.evd h4 + h4, .evd ul + h4, .evd table + h4 { margin-top:.8rem; }
.evd .full { grid-column:1 / -1; }
table.mini { width:100%; border-collapse:collapse; font-size:.8rem; table-layout:auto; }
table.mini th { font-size:.66rem; text-transform:uppercase; letter-spacing:.05em;
  color:var(--text-muted); text-align:left; padding:.25rem .5rem;
  border-bottom:1px solid var(--border); background:transparent; }
table.mini td { padding:.3rem .5rem; border-bottom:1px solid var(--border); vertical-align:top; }
table.mini tr:last-child td { border-bottom:0; }
ul.read { margin:0; padding-left:1rem; font-size:.82rem; color:var(--text-secondary); }
ul.read li { margin:.15rem 0; }
ul.read li.verdict { font-weight:700; }
.h-good { color:var(--status-good); } .h-critical { color:var(--status-critical); }
.h-warning { color:var(--status-warning); } .h-muted { color:var(--text-muted); }
pre.log { margin:.3rem 0 0; padding:.55rem .7rem; background:var(--surface-1);
  border:1px solid var(--border); border-radius:8px; font-family:var(--mono); font-size:.72rem;
  line-height:1.45; max-height:11rem; overflow:auto; color:var(--text-secondary);
  white-space:pre-wrap; }
.evlinks { margin-top:.8rem; font-size:.76rem; color:var(--text-muted); overflow-wrap:anywhere; }
.evlinks code { font-size:.72rem; }
.cps { display:inline-flex; gap:.3rem; flex-wrap:wrap; margin-top:.2rem; }
.cps span { font-family:var(--mono); font-size:.68rem; border:1px solid var(--border-strong);
  border-radius:6px; padding:0 .35rem; color:var(--text-secondary); }
.cps .h-good { border-color:var(--status-good); }
.cps .h-critical { border-color:var(--status-critical); }
.now { font-family:var(--mono); font-size:.76rem; color:var(--status-warning);
  margin-top:.15rem; }
.warnbox { margin:.8rem 0 0; padding:.6rem .8rem; border-left:3px solid var(--status-critical);
  background:color-mix(in srgb, var(--status-critical) 10%, var(--surface-1)); font-size:.82rem; }
table.authz-t td:nth-child(1), table.authz-t th:nth-child(1) { width:16%; }
table.authz-t td:nth-child(2), table.authz-t th:nth-child(2) { width:26%; overflow-wrap:anywhere; }
table.authz-t td:nth-child(3), table.authz-t th:nth-child(3) { width:34%; }
table.authz-t td:nth-child(4), table.authz-t th:nth-child(4) { width:24%; }
/* THE APPLIED AND DEFERRED TABLES RE-PACE `table.seq` FOR VERSIONS THIS LONG. The approval page
   set Before/After at 12%/15% for `1.13.2` -> `1.21.2`; this page shows managed add-ons as EKS
   spells them, `1.3.10-eksbuild.3` -> `v1.4.0-eksbuild.2`, and under `table-layout:fixed` a
   `nowrap` pill that does not fit is not clipped, it is DRAWN OVER THE NEXT COLUMN -- run
   d789a1d3's report read "1.13.2-eksbuild.21 v1.14.6-eksbuild.4" as one string. So both version
   columns take 17%, paid for by the result column (a pill and chips) and the details column (one
   button), and the pill may break inside itself if a build suffix is longer still. The same
   lesson `table.cols6` learned on the fact-approval page, applied to the table that forgot it. */
table.seq tbody td:nth-child(2), table.seq thead th:nth-child(2) { width:20%; }
table.seq tbody td:nth-child(3), table.seq thead th:nth-child(3) { width:17%; }
table.seq tbody td:nth-child(4), table.seq thead th:nth-child(4) { width:17%; }
table.seq tbody td:nth-child(5), table.seq thead th:nth-child(5) { width:28%; }
table.seq tbody td:nth-child(6), table.seq thead th:nth-child(6) { width:14%; }
table.seq tbody td:nth-child(3) .ver, table.seq tbody td:nth-child(4) .ver {
  white-space:normal; overflow-wrap:anywhere; }
"""


_LEAD = (
    "Post-upgrade validation was performed by the upgrade agent through read-only Kubernetes "
    "API access. Expand a row's <em>Details</em> for the validation record."
)
_FOOTNOTE = (
    "Post-upgrade validation was performed by the upgrade agent through read-only Kubernetes "
    "API access; rollback was executed automatically on an UNHEALTHY verdict. This report is "
    "read-only."
)


def _pill(label: str, hue: Hue, glyph: str) -> str:
    return (
        f'<span class="pill" style="--hue:{_HUE_VAR[hue]};">'
        f'<span class="pill-glyph" aria-hidden="true">{_e(glyph)}</span>'
        f"<span>{_e(label)}</span></span>"
    )


def _tile(n: int, label: str, hue: Hue) -> str:
    return (
        f'<div class="tile" style="--hue:{_HUE_VAR[hue]}"><div class="tile-n">{n}</div>'
        f'<div class="tile-l">{_e(label)}</div></div>'
    )


def _sub(text: str) -> str:
    return f'<div class="sub">{_e(text)}</div>' if text else ""


def _items(lines: Iterable[str]) -> str:
    return "".join(f"<li>{_e(line)}</li>" for line in lines)


def _pods(rows: tuple[PodRow, ...], note: str) -> str:
    if not rows:
        return f'<p class="nodet">{_e(note or "no pod state was captured")}</p>'
    body = []
    for pod in rows:
        bad = ' class="h-critical"' if pod.broken else ""
        ready = "" if pod.ready is None else ("True" if pod.ready else "False")
        ready_cls = ' class="h-critical"' if pod.ready is False else ""
        body.append(
            f"<tr><td>{_e(pod.name)}</td><td{ready_cls}>{_e(ready)}</td>"
            f"<td{bad}>{pod.restarts}</td><td{bad}>{_e(pod.state)}</td><td>{_e(pod.image)}</td></tr>"
        )
    return (
        '<table class="mini"><thead><tr><th>Pod</th><th>Ready</th><th>Restarts</th><th>State</th>'
        f'<th>Image</th></tr></thead><tbody>{"".join(body)}</tbody></table>'
    )


def _detail(detail: AppliedDetail) -> str:
    left = [f"<h4>{_e(detail.validation_heading)}</h4>"]
    if detail.validation or detail.verdict_line:
        left.append(
            '<ul class="read">'
            + _items(detail.validation)
            + (
                f'<li class="verdict h-{detail.verdict_hue}">{_e(detail.verdict_line)}</li>'
                if detail.verdict_line
                else ""
            )
            + "</ul>"
        )
    else:
        left.append('<p class="nodet">no validation record was captured</p>')

    right = [
        f"<h4>{_e(detail.pods_heading)}"
        + (
            f' <span class="n">&middot; {_e(detail.pods_note)}</span>'
            if detail.pods and detail.pods_note
            else ""
        )
        + "</h4>",
        _pods(detail.pods, detail.pods_note),
    ]
    if detail.rollback is not None:
        rb = detail.rollback
        right.append(
            "<h4>Post-rollback state"
            + (
                f' <span class="n">&middot; <code>{_e(rb.command)}</code>'
                + (f", {_e(rb.duration)}" if rb.duration else "")
                + "</span>"
                if rb.command
                else ""
            )
            + "</h4>"
        )
        right.append(
            '<ul class="read">'
            + _items(rb.lines)
            + (
                f'<li class="verdict h-{rb.verdict_hue}">{_e(rb.verdict_line)}</li>'
                if rb.verdict_line
                else ""
            )
            + "</ul>"
        )
    elif detail.state_lines:
        right.append(f"<h4>{_e(detail.state_heading or 'State')}</h4>")
        right.append('<ul class="read">' + _items(detail.state_lines) + "</ul>")

    full: list[str] = []
    if detail.log_lines or detail.log_heading:
        full.append(f"<h4>{_e(detail.log_heading or 'Container log')}</h4>")
        if detail.log_lines:
            body = "\n".join(_e(line) for line in detail.log_lines)
            full.append(f'<pre class="log">{body}</pre>')
    tail = []
    if detail.executed:
        commands = " &middot; ".join(f"<code>{_e(c)}</code>" for c in detail.executed)
        tail.append(f"Executed: {commands}")
    if detail.evidence:
        uris = " &middot; ".join(f"<code>{_e(u)}</code>" for u in detail.evidence)
        tail.append(f"Evidence: {uris}")
    if tail:
        full.append('<div class="evlinks">' + " &middot; ".join(tail) + "</div>")
    if detail.warning:
        full.append(f'<p class="warnbox"><strong>WARNING</strong> {_e(detail.warning)}</p>')

    return (
        '<div class="evd"><div>'
        + "".join(left)
        + "</div><div>"
        + "".join(right)
        + '</div><div class="full">'
        + "".join(full)
        + "</div></div>"
    )


def _applied_row(row: AppliedRow) -> str:
    _, label, hue, glyph = STATUS_LABEL.get(row.status, (9, str(row.status), "muted", ""))
    chips = "".join(f'<span class="h-{c.hue}">{_e(c.label)}</span>' for c in row.chips)
    after_cls = "ver ver-move" if hue == "good" else "ver"
    return (
        '<tr class="step">'
        f'<td class="num">{row.order}</td>'
        f'<td><span class="name">{_e(row.component)}</span>{_sub(row.sub)}</td>'
        f'<td><span class="ver">{_e(row.before)}</span>{_sub(row.before_sub)}</td>'
        f'<td><span class="{after_cls}">{_e(row.after)}</span>{_sub(row.after_sub)}'
        + (f'<div class="now">{_e(row.current_note)}</div>' if row.current_note else "")
        + "</td>"
        f"<td>{_pill(label, hue, glyph)}"
        + (f'<div class="cps">{chips}</div>' if chips else "")
        + "</td>"
        '<td><details class="det"><summary class="detbtn">Details</summary></details></td>'
        "</tr>"
        f'<tr class="briefrow"><td colspan="6">{_detail(row.detail)}</td></tr>'
    )


def _deferred_row(row: DeferredRow) -> str:
    reason = _e(row.reason)
    if row.required:
        reason = reason.replace("Required", '<span class="h-warning">Required</span>', 1)
    return (
        '<tr class="step">'
        f'<td class="num">{row.order}</td>'
        f'<td><span class="name">{_e(row.component)}</span>{_sub(row.sub)}</td>'
        f'<td><span class="ver">{_e(row.current)}</span></td>'
        f'<td><span class="ver">{_e(row.target)}</span>{_sub(row.target_sub)}</td>'
        f'<td class="nodet">{reason}</td><td></td>'
        "</tr>"
    )


def _authz_row(row: AuthorizationRow) -> str:
    return (
        f'<tr><td><span class="name">{_e(row.gate)}</span>{_sub(row.gate_sub)}</td>'
        f"<td><code>{_e(row.principal)}</code></td>"
        f'<td class="nodet">{_e(row.mechanism)}</td>'
        f'<td class="nodet h-{row.outcome_hue}">{_e(row.outcome)}</td></tr>'
    )


def _inventory_row(row: InventoryRow) -> str:
    glyph = {"good": "\u2713", "warning": "\u2191", "critical": "\u2717"}.get(row.verdict_hue, "?")
    return (
        f'<tr><td><span class="name">{_e(row.component)}</span>{_sub(row.where)}</td>'
        f'<td><span class="ver">{_e(row.installed)}</span></td>'
        f"<td>{_pill(row.verdict, row.verdict_hue, glyph)}</td>"
        f"<td>{_e(row.confidence)}</td>"
        f'<td><span class="ver">{_e(row.recommended)}</span></td>'
        f'<td class="src">{_e(row.source)}</td></tr>'
    )


def _card(title: str, sub: str, count: int | None, body: str, *, note: str = "") -> str:
    return (
        '<details class="card"><summary>'
        f'<span class="sum-title">{_e(title)}</span>'
        f'<span class="sum-sub">{_e(sub)}</span>'
        + (f'<span class="count">{count}</span>' if count is not None else "")
        + "</summary>"
        + (f'<p class="note">{note}</p>' if note else "")
        + f'<div class="scroll">{body}</div></details>'
    )


def _table(cls: str, head: tuple[str, ...], rows: str) -> str:
    return (
        f'<table class="{cls}"><thead><tr>'
        + "".join(f"<th>{_e(h)}</th>" for h in head)
        + f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def render_view(view: UpgradeReportView, *, rendered_at: str, report_uri: str) -> str:
    """The whole page from a view. A pure function of its arguments."""
    headline, hue, glyph = view.headline_pill
    hop = [
        "<span>Kubernetes</span>",
        f'<span class="k">{_e(view.current_kubernetes_version or "?")}</span>',
        '<span class="arrow">&rarr;</span>',
        f'<span class="k">{_e(view.target_kubernetes_version)}</span>',
        f"<span>&middot;</span> {_pill(headline, hue, glyph)}",
        "<span>&middot;</span> "
        f"<span>{view.authorized} applied, {view.deferred_count} deferred</span>",
    ]
    if view.region:
        hop.append(f"<span>&middot;</span> <span>{_e(view.region)}</span>")
    if view.window:
        hop.append(f"<span>&middot;</span> <span>{_e(view.window)}</span>")

    tiles = "".join(
        (
            _tile(view.authorized, "Authorized", "accent"),
            _tile(view.upgraded, "Upgraded", "good"),
            _tile(view.rolled_back, "Rolled back", "warning"),
            _tile(view.action_required, "Action required", "critical"),
            _tile(view.deferred_count, "Deferred", "muted"),
        )
    )

    authorization = _card(
        "Authorization",
        view.authorization_summary,
        None,
        _table(
            "cols4 authz-t",
            ("Gate", "Principal", "Mechanism", "Outcome"),
            "".join(_authz_row(r) for r in view.authorization),
        ),
    )

    applied_rows = sorted(view.applied, key=sort_key)
    applied_sub = (
        f"{view.upgraded} upgraded, {view.rolled_back} rolled back"
        + (f", {view.action_required} action required" if view.action_required else "")
        + " \u00b7 sequential, in plan order"
    )
    applied = _card(
        "Applied",
        applied_sub,
        len(applied_rows),
        _table(
            "seq",
            ("#", "Component", "Before", "After", "Result", ""),
            "".join(_applied_row(r) for r in applied_rows),
        ),
    )

    deferred = ""
    if view.deferred:
        deferred = _card(
            "Deferred",
            f"{view.deferred_count} step(s) not executed",
            len(view.deferred),
            _table(
                "seq",
                ("#", "Component", "Current", "Target", "Reason", ""),
                "".join(_deferred_row(r) for r in view.deferred),
            ),
            note=_e(view.deferred_note),
        )

    inventory = ""
    if view.inventory:
        inventory = (
            f"<h2>Cluster inventory &middot; {len(view.inventory)} components</h2>"
            + (f'<p class="lead">{_e(view.inventory_note)}</p>' if view.inventory_note else "")
            + _card(
                f"Components on Kubernetes {view.current_kubernetes_version or '?'}",
                view.inventory_sub,
                len(view.inventory),
                _table(
                    "cols6",
                    ("Component", "Installed", f"Compatibility ({view.target_kubernetes_version})",
                     "Confidence", "Recommended", "Evidence source"),
                    "".join(_inventory_row(r) for r in view.inventory),
                ),
            )
        )

    footer_bits = [f"Run <code>{_e(view.run_id)}</code>", *[_e(b) for b in view.footer]]
    footer_bits.append(f"generated {_e(rendered_at)} from <code>{_e(report_uri)}</code>")

    return f"""<!DOCTYPE html>
<html lang="en" data-page="upgrade-report">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Upgrade report - {_e(view.cluster)}</title>
<style>{_CSS}{_ADVICE_CSS}{_REPORT_CSS}</style>
</head>
<body>
<header><div class="inner">
  <div class="eyebrow">Amazon EKS &middot; Add-on upgrade &middot; Change report</div>
  <h1>{_e(view.cluster)}</h1>
  <div class="hop">{" ".join(hop)}</div>
</div></header>
<div class="wrap">
  <div class="tiles">{tiles}</div>
  <h2>Changes &middot; {view.authorized} applied, {view.deferred_count} deferred</h2>
  <p class="lead">{_LEAD}</p>
  {authorization}
  {applied}
  {deferred}
  {inventory}
  <footer>
    {" &middot; ".join(footer_bits)}<br>
    {_FOOTNOTE}
  </footer>
</div>
</body>
</html>
"""


def to_upgrade_report_html(
    report: UpgradeRunReport,
    *,
    rendered_at: str,
    report_uri: str,
    view: UpgradeReportView | None = None,
) -> str:
    """The page for one run. Given only the report, renders the floor view.

    `view` is what the report stage builds when it can read the evidence beside the document;
    when it is absent -- a test, an operator re-rendering from the document alone -- the page is
    rendered from the report and says where evidence was not captured, rather than failing.
    """
    return render_view(
        view or view_from_report(report, rendered_at=rendered_at),
        rendered_at=rendered_at,
        report_uri=report_uri,
    )
