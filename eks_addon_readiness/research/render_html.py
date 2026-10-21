"""An HTML view over `ApprovalReport`. A projection: it computes nothing.

The rule this module obeys, and the reason it is worth stating: **every number rendered here
is read off the document, never derived here.** No counting, no comparing, no formatting a
window from its parts. `report.py` did all of that once, and the digest the approver's record
stores covers those values -- so a template that recomputed anything could show a number the
digest does not cover, which is precisely the class of defect that makes a renderer report
three
different unresolved counts for one document.

Concretely: `report.headline()`, `report.changed_hops` and `signals_in_reading_order` are
called, and no arithmetic appears below. The one thing this module decides is colour, which
is presentation and covers nothing.

Self-contained: inline CSS, no external asset, no script at all. It has to render from a
file:// URL and from an S3 pre-signed link with no network of its own.
"""

from __future__ import annotations

from html import escape

from ..view.render.command import decision_command
from .report import ApprovalReport, signals_in_reading_order

_STYLE = """
:root {
  --ink: #16191f; --muted: #5f6b7a; --line: #d5dbdb; --bg: #ffffff;
  --panel: #f7f8fa; --ok: #037f51; --warn: #8a6116; --bad: #a8071a;
  --accent: #0972d3; --accent-bg: #f0f7ff;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.5rem 4rem; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
}
main { max-width: 62rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 2.25rem 0 .6rem; padding-bottom: .3rem;
     border-bottom: 1px solid var(--line); }
.sub { color: var(--muted); margin: 0 0 1.5rem; font-size: .92rem; }
.headline { background: var(--accent-bg); border-left: 4px solid var(--accent);
            padding: .9rem 1.1rem; border-radius: 4px; margin: 0 0 1rem; font-weight: 600; }
.advisory { background: var(--panel); border-left: 4px solid var(--warn);
            padding: .9rem 1.1rem; border-radius: 4px; margin: 0 0 1.5rem; }
table { border-collapse: collapse; width: 100%; margin: .4rem 0 1rem; font-size: .92rem; }
th, td { text-align: left; padding: .5rem .65rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { background: var(--panel); font-weight: 600; }
code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
pre.quote { background: var(--panel); border: 1px solid var(--line); border-radius: 4px;
            padding: .7rem .8rem; margin: .3rem 0 .1rem; white-space: pre-wrap;
            word-break: break-word; font-size: .86rem; }
.pill { display: inline-block; padding: .1rem .5rem; border-radius: 10px;
        font-size: .78rem; font-weight: 700; letter-spacing: .02em; }
.pill.ok { background: #e8f6f0; color: var(--ok); }
.pill.warn { background: #fdf6e3; color: var(--warn); }
.pill.bad { background: #fdeceb; color: var(--bad); }
.pill.mute { background: #eef1f3; color: var(--muted); }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 1.1rem; }
.cols section { border: 1px solid var(--line); border-radius: 4px; padding: .3rem .9rem 1rem; }
.cols h3 { font-size: .9rem; text-transform: uppercase; letter-spacing: .04em;
           color: var(--muted); margin: .9rem 0 .2rem; }
.changed { background: #fffdf5; }
.src { font-size: .82rem; color: var(--muted); word-break: break-all; }
/* The control is a command, not a form. `user-select:all` makes one click select the
   whole line, which is the closest thing to a copy button a page with no script can
   offer. The hue sits on the left edge only, never on the text, so neither choice
   reads as the recommended one. */
.cmd-l { font-size: .7rem; text-transform: uppercase; letter-spacing: .06em;
         font-weight: 700; color: var(--muted); margin: 1.1rem 0 .3rem; }
pre.cmd { user-select: all; -webkit-user-select: all; cursor: copy;
          background: var(--panel); border: 1px solid var(--line);
          border-left: 4px solid var(--muted); border-radius: 4px;
          padding: .65rem .8rem; margin: 0; font-size: .78rem; line-height: 1.5;
          white-space: pre-wrap; word-break: break-all; }
pre.cmd-approve { border-left-color: var(--ok); }
pre.cmd-reject { border-left-color: var(--bad); }
.note { color: var(--muted); font-size: .85rem; margin-top: .7rem; }
footer { margin-top: 3rem; color: var(--muted); font-size: .82rem;
         border-top: 1px solid var(--line); padding-top: .8rem; }
"""


def _pill(text: str, kind: str) -> str:
    return f'<span class="pill {kind}">{escape(text)}</span>'


def _confidence_pill(confidence: str) -> str:
    kind = {"HIGH": "ok", "MEDIUM": "warn", "LOW": "bad", "UNRESOLVED": "bad"}.get(
        confidence, "mute"
    )
    return _pill(confidence, kind)


def _verdict_pill(verdict: str) -> str:
    kind = {
        "COMPATIBLE": "ok",
        "REQUIRES_COMPONENT_UPGRADE": "warn",
        "INCOMPATIBLE": "bad",
        "UNRESOLVED": "mute",
    }.get(verdict, "mute")
    return _pill(verdict, kind)


def _quote_pill(verdict: str, verified: bool) -> str:
    return _pill(verdict, "ok" if verified else "bad")


def render_approval_html(
    report: ApprovalReport,
    *,
    approve_endpoint: str | None = None,
    candidate_uri: str | None = None,
    region: str = "us-east-1",
) -> str:
    """Render one approval report to a self-contained HTML page.

    THIS PAGE IS THE EVIDENCE RECEIPT, not the run report. It exists for a different
    reader and a different question: not "what does my cluster need?" but "on what
    grounds does this one proposed fact rest?" -- the verbatim quotes, the pinned
    source revisions, which signals fired, and where the sources disagree. The run
    report links here and does not inline any of it, because a run report carrying
    every quote behind every fact would bury the plan a reader opened it for.

    `approve_endpoint` is the IAM-authenticated Lambda function URL. When it is absent
    the page renders read-only and says so, which is the right behaviour for an
    archived copy: a control that silently does nothing is worse than none.

    `candidate_uri` is required for the control to work -- the endpoint identifies the
    candidate by URI -- and is not on `ApprovalReport`, which is a presentation
    document over the candidate rather than a pointer to where it was stored. Passed
    in by the stage that just wrote it.

    THE CONTROL USED TO BE A FORM, AND THE FORM COULD NOT WORK. It posted
    `application/x-www-form-urlencoded` straight at a function URL with `AWS_IAM`
    auth. A browser sends no SigV4 signature, so AWS refused it at the authorizer
    with a bare 403 before the handler ran, and the page gave its reader no way to
    know why. It is now the same signed command the run report shows, built by the
    same function, so the two pages cannot disagree about what the endpoint accepts.
    """
    e = escape
    parts: list[str] = [
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>Approve fact: {e(report.component_id)} {e(report.version_series)}</title>",
        f"<style>{_STYLE}</style></head><body><main>",
        f"<h1>Approve compatibility fact: <code>{e(report.component_id)}</code> "
        f"{e(report.version_series)}</h1>",
    ]

    installed = (
        f"installed {e(report.installed_version)}"
        if report.installed_version
        else "not installed on a cluster in this report"
    )
    cluster = f" on <code>{e(report.cluster)}</code>" if report.cluster else ""
    parts.append(
        f'<p class="sub">{installed}{cluster} &middot; researched '
        f"{e(report.researched_at.isoformat())} &middot; candidate "
        f"<span class=\"mono\">{e(report.candidate_digest[:19])}</span></p>"
    )

    # The consequence first. Computed in the document.
    parts.append(f'<p class="headline">{e(report.headline())}</p>')
    parts.append(f'<p class="advisory">{e(report.advisory)}</p>')

    # ---- the decision: what changes ----
    parts.append("<h2>What approving this changes</h2>")
    if report.verdict_deltas:
        parts.append(
            "<table><thead><tr><th>Hop</th><th>Verdict now</th>"
            "<th>Verdict if approved</th></tr></thead><tbody>"
        )
        for d in report.verdict_deltas:
            row_class = ' class="changed"' if d.changes else ""
            parts.append(
                f"<tr{row_class}><td class=\"mono\">{e(d.hop.current_version)} &rarr; "
                f"{e(d.hop.target_version)}</td>"
                f"<td>{_verdict_pill(d.verdict_now)}<div class=\"src\">{e(d.reason_now)}</div></td>"
                f"<td>{_verdict_pill(d.verdict_if_approved)}"
                f"<div class=\"src\">{e(d.reason_if_approved)}</div></td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append(
            "<p class=\"sub\">No cluster and target were supplied, so no verdict change "
            "was computed. Approving this records the fact; it will affect the next run.</p>"
        )

    # ---- side by side: held vs proposed ----
    parts.append("<h2>What the fact store holds now, and what this proposes</h2>")
    parts.append('<div class="cols">')
    for title, revision, rows, empty in (
        ("Currently held", report.current_revision, report.current_windows,
         "No fact is held for this component. Every verdict for it is UNRESOLVED today."),
        ("Proposed", report.proposed_revision, report.proposed_windows,
         "This candidate derived no storable window."),
    ):
        parts.append(f"<section><h3>{e(title)}</h3>")
        parts.append(
            f'<p class="src">{e(revision) if revision else "(no revision)"}</p>'
        )
        if rows:
            parts.append("<table><tbody>")
            for row in rows:
                basis = f'<div class="src">{e(row.basis)}</div>' if row.basis else ""
                parts.append(
                    f'<tr><td class="mono">{e(row.version_spec)}</td>'
                    f"<td>{e(row.window)}{basis}</td></tr>"
                )
            parts.append("</tbody></table>")
        else:
            parts.append(f'<p class="sub">{e(empty)}</p>')
        parts.append("</section>")
    parts.append("</div>")

    # ---- confidence AND the signals behind it ----
    parts.append("<h2>Computed confidence, and which signals fired</h2>")
    parts.append(
        f"<p>{_confidence_pill(str(report.confidence))} "
        f"{_pill(str(report.change_direction), 'mute')} "
        f"{_pill(str(report.decision), 'warn')} &middot; "
        f"{report.values_verified} of {report.values_total} stored values trace to a "
        "quote verified against fetched page bytes.</p>"
    )
    parts.append(
        "<table><thead><tr><th>Signal</th><th></th><th>Why</th></tr></thead><tbody>"
    )
    for s in signals_in_reading_order(report):
        parts.append(
            f'<tr><td class="mono">{e(s.name)}</td>'
            f"<td>{_pill('FIRED' if s.fired else 'did not fire', 'ok' if s.fired else 'bad')}</td>"
            f"<td>{e(s.detail)}</td></tr>"
        )
    parts.append("</tbody></table>")

    if report.contradictions:
        parts.append("<h2>Sources disagree</h2><table><tbody>")
        for c in report.contradictions:
            parts.append(f'<tr><td class="src">{e(c)}</td></tr>')
        parts.append("</tbody></table>")

    if report.tested_windows:
        parts.append("<h2>Tested windows, deliberately not treated as support ceilings</h2>")
        parts.append(
            "<p class=\"sub\">A window a source calls <em>tested</em> states no support "
            "ceiling and no open-ended support, so no compatibility window was derived from "
            "it. Recorded here as evidence.</p><table><tbody>"
        )
        for w in report.tested_windows:
            parts.append(f'<tr><td class="src">{e(w)}</td></tr>')
        parts.append("</tbody></table>")

    # ---- the evidence: claim, quote, source, commit ----
    parts.append("<h2>Every proposed value, its verbatim quote, and its pinned source</h2>")
    if report.claims:
        for c in report.claims:
            parts.append(
                f'<p><span class="mono"><strong>{e(c.field)}</strong> = '
                f"{e(c.value)}</span> {_quote_pill(c.quote_verdict, c.verified)} "
                f"{_pill(c.source_authority, 'mute')}</p>"
                f'<pre class="quote">{e(c.quote)}</pre>'
                f'<p class="src">{e(c.source_url)} @ {e(c.source_revision)}</p>'
            )
    else:
        parts.append('<p class="sub">This candidate carries no quoted values.</p>')

    # ---- the control ----
    parts.append("<h2>Approve or reject</h2>")
    if approve_endpoint:
        # THIS SENTENCE IS READ BY A HUMAN IMMEDIATELY BEFORE THEY APPROVE A FACT, so it
        # states only what the deployed policies actually say. It used to claim the front
        # door agent was "explicitly denied permission to call the endpoint". The endpoint's
        # resource policy holds one statement, an Allow naming the approver role, and no
        # Deny. Every other principal is refused
        # because it is not on a one-name allowlist, which is true and checkable, rather
        # than by an explicit Deny, which is neither. Overstating the control on the page
        # that asks for a decision is the worst place in this system to overstate anything.
        # See stages/approve.py's docstring.
        parts.append(
            '<p class="note">Each command below signs itself with <em>your</em> '
            "credentials when you run it, so the decision is recorded against your "
            "principal ARN together with the digest of this page. This page holds no "
            "credential and cannot decide anything; the endpoint accepts only the "
            "approver role, so anything that can show you this page still cannot "
            "act on it.</p>"
        )
        for action, label in (
            ("approve", "Approve &mdash; stores the fact"),
            ("reject", "Reject &mdash; stores no fact; it stays an open question"),
        ):
            command = decision_command(
                endpoint=approve_endpoint,
                region=region,
                action=action,
                component_id=report.component_id,
                version_series=report.version_series,
                candidate_uri=candidate_uri or "",
                candidate_digest=report.candidate_digest,
                report_digest=report.digest(),
            )
            parts.append(f'<div class="cmd-l">{label}</div>')
            parts.append(f'<pre class="cmd cmd-{action}">{e(command)}</pre>')
    else:
        parts.append(
            '<p class="sub">This is an archived copy with no approval endpoint '
            "configured, so it is read-only. An approval control that silently did nothing "
            "would be worse than none.</p>"
        )

    parts.append(
        "<footer>Report digest <span class=\"mono\">"
        f"{e(report.digest())}</span><br>Evidence digest <span class=\"mono\">"
        f"{e(report.evidence_digest)}</span><br>"
        "The report digest is recorded on the approval, so what you were shown is provable "
        "afterwards rather than merely asserted.</footer>"
    )
    parts.append("</main></body></html>")
    return "".join(parts)
