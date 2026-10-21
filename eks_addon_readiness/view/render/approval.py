"""The approval page: only the facts awaiting a decision, and the control to finish.

WHY THIS IS A SECOND PAGE AND NOT A FLAG ON THE FIRST. The run report shows every
component in the cluster, grouped, with verdicts, a sequence, tiles and a digest chain --
and the proposals awaiting approval were rows scattered through it. Asked to approve three
facts, a person had to find them among thirty rows of things that need nothing from them.
Combining "here is the state of your cluster" with "here are three decisions only you can
make" made the second unreadable, which is what this page exists to fix.

WHAT IT RENDERS: one table, one row per pending proposal, and nothing else. The columns are
the ones the run report uses for the same rows -- installed, confidence, recommended,
sources, decision -- so a reader who has seen one page recognises the other.

WHAT IT DELIBERATELY OMITS, and each omission is a claim this page must not make:

  * **No verdict column.** This page renders BEFORE `Decide` runs on the proposals, so
    there is no verdict for these components yet. A verdict column would have to show
    UNRESOLVED for every row, which reads as a finding about the component when it is
    only a statement about the order the stages run in.
  * **No cluster inventory, no plan, no tiles.** They are the run report's job and it
    still does it, after this.
  * **No digest chain.** This page is not a document; it holds no digests of its own to
    cite. What it does carry per row is the candidate digest, which is what an approval
    is actually against.

WHAT IT CARRIES NO CAPABILITY FOR, same as the run report: no credential, no signature, no
presigned URL, and NOT THE TASK TOKEN. Every control is a command the person signs with
their own credentials. The token that resumes the paused execution lives in the token store
and is read there by the endpoint -- so this page, which anyone with S3 read access can
fetch, cannot resume the pipeline and cannot approve anything.
"""

from __future__ import annotations

from ..model import ApprovalControls, PendingApproval
from .command import decision_command, done_command
from .html import (
    _confidence_cell,
    _confidence_pill,
    _CONTROL_CSS,
    _CSS,
    _DECISION_SCRIPT,
    _e,
    _installed_cell,
    _recommended_cell,
    _sources_cell,
)

# The columns, and the share of the width each gets. `table-layout:fixed` on the run
# report exists because the page scrolled horizontally without it; the same applies here,
# and the shares differ because this table has no verdict column to fit.
_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Component", 17),
    ("Installed", 9),
    ("Confidence", 13),
    ("Recommended", 15),
    ("Sources", 27),
    ("Decision", 19),
)


def _row(pending: PendingApproval, controls: ApprovalControls, forms: dict | None = None) -> str:
    """One proposal, with its two commands in a full-width row beneath it.

    `row=None` on every cell helper: there is no inventory row here, so each cell falls
    through to the proposal's own value. That path is why those helpers take an optional
    row rather than this page synthesising an empty one -- a fake `ComponentRow` full of
    em-dashes would render dashes and look like missing data.
    """
    name = (
        f'<span class="name">{_e(pending.component)}</span>'
        f'<div class="sub">{_e(pending.version_series)}</div>'
    )
    return (
        '<tr class="arow row-await">'
        f"<td>{name}</td>"
        f"<td>{_installed_cell(None, pending)}</td>"
        f"<td>{_confidence_cell(None, pending)}</td>"
        f"<td>{_recommended_cell(None, pending)}</td>"
        f'<td class="src">{_sources_cell(None, pending)}</td>'
        f'<td class="dec">{_decision(pending, forms)}</td>'
        "</tr>"
    ) + ("" if forms else _commands(pending, controls))


def _form(form: dict, label: str, action: str) -> str:
    """A REAL BUTTON. One native HTML form, no JavaScript, no credential on the page.

    THIS IS THE CONTROL THAT ACTUALLY WORKS, and it took three attempts to get here. The
    hidden fields are a presigned S3 POST policy minted by the approval stage, so the browser
    submits an authenticated write without holding any credential of its own -- and S3 honours
    the policy's own expiry, unlike a Lambda function URL, which caps signature freshness at
    five minutes regardless of what you ask for.

    What the form may do is pinned by the policy to writing ONE object at ONE key. The key
    carries the run and the nonce, so it cannot be redirected at another candidate; the
    arriving object is a doorbell that triggers `decision_watcher`, which redeems the
    single-use grant and only then approves anything. The page still holds no power to decide.

    `file` is a hidden text input rather than a file picker: S3 takes that field's value as
    the object body, which is how a plain form writes a few bytes with no upload UI.
    """
    hidden = "".join(
        f'<input type="hidden" name="{_e(k)}" value="{_e(str(v))}">'
        for k, v in form["fields"].items()
    )
    return (
        f'<form class="act" action="{_e(form["url"])}" method="post" '
        'enctype="multipart/form-data">'
        f'{hidden}<input type="hidden" name="file" value="{_e(action)}">'
        f'<button class="btn btn-{action}" type="submit">{label}</button>'
        "</form>"
    )


def _proposal(pending: PendingApproval) -> str:
    """The short note under the pills: only what the columns cannot carry.

    MOVED HERE FROM `html.py`, because the run report no longer has a decision cell to put
    it in: approval happens before `Present` renders, so that page carries no controls and
    no proposal. This is the only page that still asks for a decision, so this is where the
    prose that supports one belongs.

    IT USED TO BE THE WHOLE ASSESSMENT, AND THAT WAS THE WRONG PLACE FOR IT. The table
    already has columns for INSTALLED, CONFIDENCE, RECOMMENDED and SOURCES, and they were
    rendering dashes while a paragraph of those same values sat in the DECISION cell --
    unreadable in a narrow column and unscannable across three components, which is the
    one thing a table is for. Those values fill their own columns.

    What stays here is what has no column: the sentence explaining the confidence, and the
    caveats and gaps.

    BOTH ARE COLLAPSED BY DEFAULT, which reverses the call before it (which reversed the one
    before that -- this has gone back and forth, so the reason is written down). Opened by
    default, three components' rationales and their caveat lists -- five to eight sentences
    each, once the research got good -- pushed the second and third rows below the fold and
    turned a three-row table into a page of prose the reader scrolls before finding the next
    Approve. The table IS the page: installed, confidence, recommended, sources and the
    decision are all in columns, and the row is what a reviewer decides on. The reasoning
    stays one click away under a summary that names it ("why this confidence", "5 caveat(s)"),
    so a reviewer knows it is there and how much of it there is before deciding to read it.
    The `<details>` stays; only the default moves.
    """
    blocks: list[str] = []

    if pending.confidence_rationale:
        blocks.append(
            '<details class="prop-why"><summary>why this confidence</summary>'
            f"<p>{_e(pending.confidence_rationale)}</p></details>"
        )

    notes = [(c, "prop-caveat") for c in pending.caveats]
    notes += [(f"not found: {g}", "prop-gap") for g in pending.not_found]
    if notes:
        items = "".join(f'<li class="{cls}">{_e(text)}</li>' for text, cls in notes)
        blocks.append(
            f'<details class="prop-notes"><summary>{len(notes)} caveat(s)</summary>'
            f"<ul>{items}</ul></details>"
        )

    if not blocks:
        return ""
    return f'<div class="prop">{"".join(blocks)}</div>'

def _decision(pending: PendingApproval, forms: dict | None = None) -> str:
    """The Approve / Reject pair for one proposal, plus what has no column.

    TWO SHAPES, and which one appears is decided by whether a presigned form was supplied.
    With forms, these are real submit buttons that record a decision on click. Without them
    -- a local render, or a deployment whose stage could not sign -- they fall back to the
    `<details>` summaries that reveal a signed command, which is what the run report does and
    what worked before there was any button at all. A control that silently fails would be
    worse than either.
    """
    if forms:
        pills = "".join(
            _form(forms[action], label, action)
            for action, label in (("approve", "Approve"), ("reject", "Reject"))
            if action in forms
        )
    else:
        pills = "".join(
            f'<details class="btn-wrap wrap-{action}">'
            f'<summary class="btn btn-{action}">{label}</summary></details>'
            for action, label in (("approve", "Approve"), ("reject", "Reject"))
        )
    return (
        f'<div class="decide">{_proposal(pending)}<div class="btns">{pills}</div>'
        f'<div class="decide-meta">{_confidence_pill(pending.confidence)} '
        f'<span class="sub">candidate '
        f'{_e(pending.candidate_digest.removeprefix("sha256:")[:12])}</span></div>'
        "</div>"
    )


def _commands(pending: PendingApproval, controls: ApprovalControls) -> str:
    """The two hidden rows holding this proposal's signed commands.

    Emitted even when there is no endpoint, in which case they say so instead of printing
    an unusable command: a page whose Approve pill opens onto nothing looks broken, and
    "this deployment has no approval endpoint" is a real answer.
    """
    out = []
    for action in ("approve", "reject"):
        if controls.endpoint:
            block = (
                f'<pre class="cmd cmd-{action}" tabindex="0">'
                + _e(
                    decision_command(
                        endpoint=controls.endpoint,
                        region=controls.region,
                        action=action,
                        component_id=pending.component,
                        version_series=pending.version_series,
                        candidate_uri=pending.candidate_uri,
                        candidate_digest=pending.candidate_digest,
                        report_digest=pending.report_digest,
                    )
                )
                + "</pre>"
            )
        else:
            block = (
                '<p class="btn-note">This deployment has no approval endpoint '
                "configured, so there is no command to run. Nothing can be approved "
                "from here.</p>"
            )
        verb = "Approving" if action == "approve" else "Rejecting"
        effect = (
            "stores the fact, and the run resumes with it available rather than as a gap"
            if action == "approve"
            else "stores no fact; the component stays an open question"
        )
        out.append(
            f'<tr class="cmdrow cmdrow-{action}">'
            f'<td colspan="{len(_COLUMNS)}">'
            f'<p class="btn-note"><strong>{verb} {_e(pending.component)} '
            f"{_e(pending.version_series)}</strong> {effect}. Signed by <em>your</em> "
            "credentials when you run it; this page holds none and cannot decide "
            "anything. Replace the placeholder before you run it.</p>"
            f"{block}</td></tr>"
        )
    return "".join(out)


def _done_block(
    controls: ApprovalControls, run_id: str, waiting: int, done_form: dict | None = None
) -> str:
    """The control that resumes the run, and the honest description of what it does.

    STATED AS "WHATEVER YOU HAVE DECIDED", not "all of them", because that is what it
    means. Done does not require every row to be settled -- a proposal left alone stays
    pending and is offered again on the next run, which is the same outcome as never
    having looked. Implying otherwise would make a person think they had refused
    something they merely skipped.
    """
    if done_form:
        # NO HEADING. "FINISH" was a section header over one sentence and one button -- a
        # heading for something that is not a section. The rule above it already separates it.
        return (
            '<div class="finish">'
            '<p class="note">Approving or rejecting a row records that decision. '
            "Resume the run when you have finished; anything left undecided is offered "
            "again on the next run.</p>"
            + _form(done_form, "Done", "done")
            + "</div>"
        )

    if not controls.endpoint:
        return (
            '<section class="block"><h2>Nothing to resume from here</h2>'
            '<p class="note">This deployment has no approval endpoint configured, so '
            "this page is a read-only view. The run is waiting and will fail on its own "
            "timeout.</p></section>"
        )
    command = done_command(
        endpoint=controls.endpoint, region=controls.region, run_id=run_id
    )
    return (
        '<section class="block block-done"><h2>When you have finished deciding</h2>'
        f'<p class="note">This run is <strong>paused</strong> on {waiting} proposal(s). '
        "It will not continue until you run the command below -- approving or rejecting "
        "a row records your decision but does not resume anything, because only you know "
        "whether you are finished. Anything you leave undecided stays pending and is "
        "offered again on the next run.</p>"
        '<details class="btn-wrap wrap-done" open>'
        '<summary class="btn btn-done">Done &mdash; resume the run</summary>'
        f'<pre class="cmd cmd-done" tabindex="0">{_e(command)}</pre>'
        "</details></section>"
    )


def _empty() -> str:
    return (
        '<section class="block"><h2>Nothing is awaiting approval</h2>'
        '<p class="note">Research produced no proposal a person can act on. That is not '
        "an error: a component whose sources state nothing usable yields no candidate, "
        "and it stays an honest gap.</p></section>"
    )


# Only the rules this page adds. Everything else -- pills, versions, buttons, command
# blocks, the cited-source list -- is the run report's stylesheet, imported rather than
# copied so the two pages cannot drift apart visually.
_EXTRA_CSS = """
/* ---- ONLY what this page adds. --------------------------------------------------
   The table chrome -- rules, header band, hover, the horizontal scroll container -- is
   the run report's, inherited from `_CSS`. An earlier version of this file invented its
   own borders and they FOUGHT the report's: two sets of rules on the same selectors, so
   the page looked neither like the report nor like anything designed. The report's table
   already fits, so this file overrides one thing only, the column widths, because its six
   columns are not the report's six. */
/* THE ACCENT RULE ENDS WHERE THE CONTENT ENDS. `_CSS` puts it on `header`, which spans the
   viewport while the content is a centred 78rem column -- so it read as a line with no end.
   Moved onto the inner column; the banner gradient still fills the width behind it. */
header { border-bottom:none; }
header .inner { border-bottom:2px solid var(--accent); }

/* ONE PANEL HOLDING EVERYTHING, which is what fixes the alignment rather than nudging each
   margin separately. `.note` carries a 1.05rem side margin because in the run report it always
   sits INSIDE a card; dropped straight onto the page it indented itself away from every other
   element. A card puts that indent back in a context where it means something, and every child
   then shares one left edge. */
.panel {
  background:var(--surface-1); border:1px solid var(--border); border-radius:12px;
  padding:1.4rem 1.5rem 1.5rem; margin:1.8rem 0 0;
}
.panel > .note { margin:0 0 1.1rem; font-size:.88rem; line-height:1.6; }
.panel > details.card { margin:0; }
.finish { margin:1.4rem 0 0; padding-top:1.2rem; border-top:1px solid var(--border); }
.finish > .note { margin:0 0 .9rem; font-size:.88rem; line-height:1.6; }

tbody td:first-child, thead th:first-child { width:13%; }
tbody td:nth-child(2), thead th:nth-child(2) { width:11%; }
tbody td:nth-child(3), thead th:nth-child(3) { width:12%; }
tbody td:nth-child(4), thead th:nth-child(4) { width:17%; }
tbody td:nth-child(5), thead th:nth-child(5) { width:25%; }
tbody td:nth-child(6), thead th:nth-child(6) { width:21%; }

/* The citation list, kept inside its column. `_clamp` shortens the label; this stops a
   long URL-ish token from widening the cell anyway. */
ul.cited { margin:0; padding-left:1rem; }
/* Sub-lines under a version were touching; a cell of four stacked values needs leading. */
tbody td .sub { margin-top:.28rem; line-height:1.45; }
tbody td { line-height:1.5; }
ul.cited li { margin:0 0 .3rem; overflow-wrap:anywhere; }

/* Buttons stack and fill the decision column: side by side they wrap unpredictably at
   this width, and a wrapped button loses its background in several renderers. */
form.act { display:block; margin:0 0 .35rem; }
form.act button.btn { justify-content:center; font:inherit; cursor:pointer; }
/* Full width only INSIDE the table, where the column is narrow enough that two buttons
   side by side wrap unpredictably. */
tbody form.act button.btn { width:100%; }

.block-done { border-color:var(--accent); }
/* THE SAME CONTROL AS THE MAIL'S: solid accent, dark ink, sized to its label. The outlined
   full-width version read as a disabled banner rather than the button that resumes the run. */
.block-done form.act { display:inline-block; margin:.2rem 0 0; }
.btn-done {
  background:var(--accent); border-color:var(--accent); color:#0f1115;
  font-weight:600; padding:.62rem 1.5rem; font-size:.95rem;
}
.cmd-done { --hue:var(--accent); }
.await-count {
  font-family:var(--mono); font-size:2rem; color:var(--accent);
  font-variant-numeric:tabular-nums;
}
"""


def to_approval_html(
    controls: ApprovalControls,
    *,
    run_id: str,
    cluster: str,
    target_kubernetes_version: str,
    rendered_at: str,
    forms: dict[str, dict[str, dict]] | None = None,
    done_form: dict | None = None,
) -> str:
    """The whole page. A pure function of its arguments -- no S3 read, no store, no clock.

    `rendered_at` is passed in rather than read from a clock for the reason every document
    in this project is clock-free: a renderer that reads the time cannot be re-run to
    check its own output.
    """
    pending = controls.pending
    if pending:
        cols = "".join(f'<col style="width:{share}%">' for _, share in _COLUMNS)
        head = "".join(f"<th>{_e(label)}</th>" for label, _ in _COLUMNS)
        rows = "".join(
            _row(p, controls, (forms or {}).get(p.candidate_digest)) for p in pending
        )
        # THE RUN REPORT'S OWN CONTAINER, deliberately: `details.card` + `div.scroll` +
        # `table` is what makes its tables sit properly, and reusing it means this page
        # inherits every fix that lands there instead of drifting from it.
        table = (
            # OPEN BY DEFAULT, and that is a correction of an earlier judgement rather than a
            # preference. It was closed on the reasoning that the reader arrives from an email
            # that already told them how many facts are waiting, so a wide table of
            # transcribed evidence is more than the first glance needs. That reasoning is
            # wrong about what this page is FOR: it exists only to be decided on, and the
            # proposal is what a reader decides. Landing on a page whose entire content is
            # behind one click reads as though nothing has arrived, and a reviewer who has to
            # click to see what they are approving is one who may approve without clicking.
            # The disclosure is kept -- the table still collapses on demand for a reader who
            # wants the shell back -- but the default is the content.
            #
            # No count pill either: a "2" beside a heading that already states the number is
            # a number with no second reading.
            '<details class="card" open><summary>'
            '<span class="sum-title">Proposed compatibility facts</span>'
            '<span class="sum-sub">approve or reject each row</span></summary>'
            f'<div class="scroll"><table><colgroup>{cols}</colgroup>'
            f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>"
            "</details>"
        )
    else:
        table = _empty()

    # THE RUN REPORT'S PAGE SHELL, class for class. `header > div.inner` then `div.wrap` is
    # what `_CSS` gives its padding, max-width and centring to. An earlier version invented
    # `<main>` and `class="head"` -- names the stylesheet has never heard of -- so the page
    # rendered flush against the left edge with the eyebrow clipped. Nothing was wrong with
    # the CSS; the markup was simply not asking for it.
    return f"""<!DOCTYPE html>
<html lang="en" data-page="approval">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Approvals waiting - {_e(cluster)} - Kubernetes {_e(target_kubernetes_version)}</title>
<style>{_CSS}{_EXTRA_CSS}{_CONTROL_CSS if controls.endpoint or done_form else ""}</style>
</head>
<body>
<header><div class="inner">
  <div class="eyebrow">Amazon EKS &middot; awaiting your decision</div>
  <h1>{_e(cluster)}</h1>
  <div class="hop">
    <span>Kubernetes</span>
    <span class="arrow">&rarr;</span>
    <span class="k">{_e(target_kubernetes_version)}</span>
    <span>&middot;</span>
    <span>{len(pending)} proposed fact(s) require approval</span>
  </div>
</div></header>

<div class="wrap">
  <section class="panel">
    <p class="note">
      The run is <strong>paused here</strong> and will not reach its report until you finish.
      Every value below is transcribed from a cited source; nothing was decided by a model.
    </p>
    {table}
    {_done_block(controls, run_id, len(pending), done_form)}
  </section>
  <footer>run {_e(run_id)} &middot; rendered {_e(rendered_at)}</footer>
</div>
{_DECISION_SCRIPT if done_form or controls.endpoint else ""}
</body>
</html>
"""
