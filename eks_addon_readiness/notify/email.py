"""The approval email: a short notice and one link. The page does the rest.

A pure builder plus a thin sender, split for the usual reason -- the builder is testable
without SES and the sender has nothing in it worth testing.

IT USED TO BE A SECOND COPY OF THE PAGE, AND THAT WAS THE MISTAKE. It rendered the same
columns -- installed, confidence, recommended, cited sources -- with a Decide button per row
that only ever navigated to the page. Two renderers of one dataset, guaranteed to drift, and
the mail could never carry a working control anyway: a link is a GET, recording a decision is
a write, and every writable presign needs a form, which needs a page.

So the division of labour is now clean. THE MAIL SAYS WHAT IS WAITING AND WHERE. The page
carries the table, the cited sources, the Approve and Reject buttons per row, and the Done
button at the end. Everything a reader has to look at before deciding is in one place, and it
is the same place the buttons are.

The per-component lines are a SUMMARY, not a table: enough to judge whether it is worth
opening now, and deliberately not enough to decide from.
"""

from __future__ import annotations

import html as html_escape
import os
from dataclasses import dataclass
from typing import Any

from ..view.model import PendingApproval

# Fallback subject prefix. Deliberately boring: a subject that looks like an alert trains
# people to ignore it, and this is the one mail in the pipeline that needs to be opened.
SUBJECT_PREFIX = "Approval needed"


@dataclass(frozen=True)
class ApprovalEmail:
    """A built message, ready to send. No SES type in the signature, so it is testable."""

    subject: str
    html_body: str
    text_body: str


def _e(value: str | None) -> str:
    return html_escape.escape(value or "")


def _label(value: str | None) -> str:
    return (value or "").replace("_", " ").lower()


# THE SAME COLUMNS AS THE RUN REPORT'S "CNCF add-ons: Helm-installed" TABLE, in the same
# order, and that is a requirement rather than a convenience. A person reads the report,
# then reads this mail about the same rows; two different column sets for the same data
# means re-learning the layout at the moment they are being asked to decide.
#
# Widths track the report's shares (16/7/9/12/12/26/18), adjusted only where email forces
# it: the decision column needs more room because a mail client cannot reveal a control on
# click, so the buttons are always visible.
#
# `Change` and `Covers target` are NOT columns here, for the same reason they are not
# columns there: they belong under the recommendation they describe, as sub-lines. Giving
# them columns of their own is what pushed Sources down to nothing on the first attempt.
#
# NO VERDICT COLUMN. The run report has one because it renders after Stage 4 has issued a
# verdict per component; this mail renders BEFORE that, on proposals Stage 4 has never seen.
# The only value it could show is UNRESOLVED on every row, which reads as a finding about
# the component when it is really a statement about the order the stages run in. The column
# it frees up goes to Sources, which is the one a person actually checks.
# FIVE COLUMNS, NOT SIX. There is no Decide column, because a link in a mail is a GET and
# recording a decision is a write -- every writable presign needs a form, and a form needs a
# page. So the mail shows what is waiting and the page carries the controls.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Component", "18%"),
    ("Installed", "12%"),
    ("Confidence", "16%"),
    ("Recommended", "18%"),
    ("Sources", "36%"),
)

# Shared cell chrome, inline on every cell because that is the only styling email clients
# can be relied on to honour: a <style> block is stripped by several of them, and Outlook
# ignores most selectors even when it keeps the block.
# Generous vertical padding and real leading. Mail clients honour inline style and nothing
# else, so the spacing has to be on every cell rather than in a rule -- and without it the
# stacked sub-lines under a version run together into one grey block.
_TD = (
    "padding:14px 10px;border-bottom:1px solid #2a2f3a;vertical-align:top;"
    "font-size:13px;color:#eef1f6;line-height:1.55"
)
_SUB = "font-size:11px;color:#8f97a5;margin-top:5px;line-height:1.5"
_ABSENT_CELL = '<span style="color:#8f97a5">&mdash;</span>'


def _cell(inner: str, *, extra: str = "") -> str:
    return f'<td style="{_TD};{extra}">{inner}</td>'


def _confidence_cell(pending: PendingApproval) -> str:
    """The confidence, and the two things that explain it.

    The model/evidence split is shown whenever they differ, because which of the two capped
    the rating is the most useful single thing a reader can know about it: the machine's
    ceiling being lower means the evidence was thin, and the model's own proposal being
    lower means the model was unsure of what it read.
    """
    hue = {"HIGH": "#34a853", "MEDIUM": "#ffd166", "LOW": "#ec835a"}.get(
        pending.confidence, "#8f97a5"
    )
    cell = f'<span style="color:{hue};font-weight:600">{_e(pending.confidence)}</span>'
    if (
        pending.proposed_confidence
        and pending.machine_confidence
        and pending.proposed_confidence != pending.machine_confidence
    ):
        cell += (
            f'<div style="{_SUB}">model {_e(pending.proposed_confidence)} / '
            f"evidence {_e(pending.machine_confidence)}</div>"
        )
    if pending.evidence_tier:
        cell += f'<div style="{_SUB}">{_e(_label(pending.evidence_tier))}</div>'
    return cell


def _sources_cell(pending: PendingApproval) -> str:
    """The documents the proposal was transcribed from, each linked to what it was used for.

    THE COLUMN THE WHOLE THING IS FOR. A version and a confidence rating are a claim; the
    cited source is the only part a reader can check for themselves, which is what makes
    approving this an informed act rather than a rubber stamp. It was missing from the first
    version of this mail, and a mail asking someone to approve a compatibility fact without
    showing them where it came from is asking them to trust the model -- exactly the thing
    this pipeline is built not to do.

    Linked on `used_for` rather than on the URL, because a raw documentation URL wraps to
    three lines in a narrow column and says less than "Kubernetes compatibility matrix".
    """
    if not pending.cited_sources:
        return f'{_ABSENT_CELL}<div style="{_SUB}">none cited</div>'
    items = "".join(
        f'<div style="margin-bottom:5px;line-height:1.4">'
        f'<a href="{_e(c.url)}" style="color:#f9ab00;font-size:12px">{_e(c.used_for)}</a>'
        f"</div>"
        for c in pending.cited_sources
    )
    return items


def build_approval_email(
    pending: tuple[PendingApproval, ...],
    *,
    run_id: str,
    cluster: str,
    target_kubernetes_version: str,
    page_url: str,
) -> ApprovalEmail:
    """Build the notice. `page_url` is a presigned S3 link to this run's approval page."""
    count = len(pending)
    subject = (
        f"{SUBJECT_PREFIX}: {count} compatibility fact(s) for {cluster} "
        f"-> Kubernetes {target_kubernetes_version}"
    )

    rows = []
    for p in pending:
        installed = _e(p.installed_version) or _ABSENT_CELL
        if p.installed_version_status:
            installed += f'<div style="{_SUB}">{_e(_label(p.installed_version_status))}</div>'
        if p.recommended_version:
            recommended = (
                '<span style="color:#f9ab00;font-weight:600">'
                f"{_e(p.recommended_version)}</span>"
            )
            bits = []
            if p.action:
                bits.append(_label(p.action))
            if p.covers_target_kubernetes is True:
                bits.append("covers target")
            elif p.covers_target_kubernetes is False:
                bits.append("does NOT cover target")
            else:
                bits.append("coverage not stated")
            recommended += f'<div style="{_SUB}">{_e(" - ".join(bits))}</div>'
            if p.upgrade_ordering:
                recommended += f'<div style="{_SUB}">{_e(_label(p.upgrade_ordering))}</div>'
        else:
            recommended = _ABSENT_CELL
        name = (
            f'<div style="font-weight:600;color:#eef1f6">{_e(p.component)}</div>'
            f'<div style="{_SUB}">{_e(p.version_series)}</div>'
        )
        rows.append(
            "<tr>"
            + _cell(name)
            + _cell(installed)
            + _cell(_confidence_cell(p))
            + _cell(recommended)
            + _cell(_sources_cell(p))
            + "</tr>"
        )

    if pending:
        head_cells = "".join(
            f'<th style="text-align:left;padding:0 10px 9px;font-size:10px;'
            f"letter-spacing:.07em;text-transform:uppercase;color:#8f97a5;"
            f'font-weight:600;border-bottom:1px solid #3b4250;width:{w}">{_e(label)}</th>'
            for label, w in _COLUMNS
        )
        table = (
            '<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
            'style="border-collapse:collapse;margin:20px 0;table-layout:fixed">'
            f"<thead><tr>{head_cells}</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        )
    else:
        table = (
            '<p style="color:#b9c0cc;font-size:14px">Research produced no proposal a person '
            "can act on. Open the page and press Done to let the run finish.</p>"
        )

    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:28px 24px;background:#0f1115;
font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#eef1f6">
<div style="max-width:820px;margin:0 auto">
  <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#f9ab00">
    Amazon EKS &middot; awaiting your decision
  </div>
  <h1 style="font-size:21px;margin:8px 0 6px;font-weight:600">{_e(cluster)}
    <span style="color:#8f97a5;font-weight:400">&rarr; Kubernetes
    {_e(target_kubernetes_version)}</span></h1>
  <p style="font-size:14px;color:#b9c0cc;margin:0;line-height:1.6">
    <strong>{count}</strong> proposed compatibility fact(s) require approval. The pipeline is
    paused and will not produce its report until each is approved or rejected.
  </p>
  {table}
  <a href="{_e(page_url)}" style="display:inline-block;padding:12px 22px;margin:4px 0 20px;
    background:#f9ab00;color:#0f1115;text-decoration:none;border-radius:6px;
    font-weight:600;font-size:15px">Open the approval page</a>
  <p style="font-size:11px;color:#8f97a5;border-top:1px solid #2a2f3a;padding-top:14px;
    line-height:1.6">
    Approve and reject are on the page, one pair per row, with a Done control that resumes
    the run. The link is presigned and time-limited; nothing in this message can approve
    anything on its own.
    <br>run {_e(run_id)}
  </p>
</div>
</body></html>
"""

    text_lines = [subject, "", f"{count} proposed compatibility fact(s) require approval.", ""]
    for p in pending:
        change = (
            f"{p.installed_version} -> {p.recommended_version}"
            if p.installed_version and p.recommended_version
            else (p.recommended_version or "no recommendation")
        )
        text_lines += [
            f"  {p.component} {p.version_series}",
            f"      {change}, confidence {p.confidence}",
        ]
        for c in p.cited_sources:
            text_lines.append(f"      - {c.used_for}: {c.url}")
        text_lines.append("")
    text_lines += [f"Approval page: {page_url}", "", f"run {run_id}"]

    return ApprovalEmail(subject=subject, html_body=html_body, text_body="\n".join(text_lines))


def send_approval_email(message: ApprovalEmail, *, client: Any = None) -> str:
    """Send it. Returns the SES message id, or "" when email is not configured.

    NOT CONFIGURED IS NOT AN ERROR, and that matters for the local and test paths: a
    deployment without SES still renders the page and still pauses, and the run can be
    resumed from the console. Raising here would make the absence of an email address fail
    a pipeline that is otherwise working.
    """
    sender = os.environ.get("APPROVAL_EMAIL_FROM", "").strip()
    recipients = [
        address.strip()
        for address in os.environ.get("APPROVAL_EMAIL_TO", "").split(",")
        if address.strip()
    ]
    if not sender or not recipients:
        return ""

    if client is None:
        import boto3

        client = boto3.client("ses")

    response = client.send_email(
        Source=sender,
        Destination={"ToAddresses": recipients},
        Message={
            "Subject": {"Data": message.subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": message.text_body, "Charset": "UTF-8"},
                "Html": {"Data": message.html_body, "Charset": "UTF-8"},
            },
        },
    )
    return str(response.get("MessageId", ""))


def approver_address() -> str:
    """Who the links are being sent to, which is who gets recorded as the approver.

    The FIRST recipient, not all of them. A grant records one approver, and if the
    deployment mails a list then the honest answer is "whoever holds this mailbox" rather
    than a name invented per click -- the nonce proves the mailbox was opened and nothing
    more. Stated here so the audit record's meaning is written down next to where it is
    decided.
    """
    recipients = [
        address.strip()
        for address in os.environ.get("APPROVAL_EMAIL_TO", "").split(",")
        if address.strip()
    ]
    return recipients[0] if recipients else "email-approver"
