"""The report mail: what changed, what state it is in, and one link. The page does the rest.

    in    one `UpgradeRunReport`, the `Release` that authorised it, and the run's own counts
    out   one mail, or a recorded absence

**THIS IS `notify/email.py`'S SHAPE, AND THAT IS THE POINT.** The fact-approval mail settled
how this project mails a person about a set of components: eyebrow, `cluster -> Kubernetes
<target>`, one paragraph of what is being said, ONE TABLE with one row per thing, the accent
button, a footer. The report is the same problem at the other end of the run, and a reader
who saw the approval mail twenty minutes ago should not have to learn a second layout.

**IT REPLACED A MAIL THAT CARRIED THE WHOLE RUN.** The previous builder rendered four fact
lists, a block per lane, the helm command, the CodeBuild id, the access entry and the digest
chain. All true, all a second copy of the report page, and `notify/email.py`'s docstring
already records where that ends: "the fact-approval mail learned this the hard way ... two
renderers of one dataset, guaranteed to drift". So the detail went back to the page.

**THE TABLE IS ONLY WHAT THIS RUN TOUCHED.** Four columns -- component, previous version,
upgraded to, status -- and one row per lane. A component nobody authorised did not change,
and a table of non-events buries the events. Everything not touched is a COUNT and one
sentence; the enumeration is the page's job.

**THE STATUS COLUMN IS THE HEALTH WORD, NOT THE ENUM.** `healthy`, `rolled back`,
`referred`. `UPGRADED` answers whether the command succeeded, which the two version columns
already show; a reader wants to know what state the component is in.

**A REFERRAL IS CARRIED BY THE STATUS COLUMN, NOT BY A BANNER.** There was a red callout
reading "<component> was upgraded and rolled back"; it is gone at the human's instruction.
The signal survives without it -- the subject line counts what needs review, the lead
repeats that count, and the row's status word is `rolled back` or `referred` -- so the
banner was a fourth statement of a fact already on the mail three times. Nothing about a
referral's STATE belongs here in any case: the lane's own `observed` list goes further
-- "the component is STILL not healthy, so the previous version is broken too" -- and on
the runs seen so far that is unsupported, because every health checkpoint returned no
verdict, so there was never a reading to be still anything and nothing looked at the
previous version. Unknown is not known-bad. Why a component was referred is the page's.

**THE VERSIONS COME OFF THE RELEASE, NOT OFF `LaneOutcome.applied`, AND THAT FIXES A BUG.**
`applied` holds the CHART version for a Helm lane -- `3.14.0` for metrics-server -- so the
previous renderer printed `0.6.x -> 3.14.0` and told a reader the component was on a version
that does not exist. `ReleaseStep.to_version` is the app version that was authorised
(`0.9.0`) and `from_series` is where it started. Both are already in `_report`'s scope
because it reads the release document to attribute the run. No new field, no second read.

**NO STEP ORDERS, NO LANE IDS, NO PHASE NAMES.** A plan step number is how the pipeline
addresses its own work. The page keeps them, because there a reader is auditing the run.

**NOT CONFIGURED IS NOT AN ERROR AND A FAILED SEND IS NOT EITHER**, unchanged from the
builder this replaces. The report is durable before this is called; a bounced mail must not
be able to discard it.
"""

from __future__ import annotations

import html as html_escape
import os
from dataclasses import dataclass, field
from typing import Any

from ..upgrade_lane.models import UpgradeRunReport, UpgradeStatus

# Deliberately boring, and the same idiom as the two approval mails: a subject that looks
# like an alert trains people to ignore it.
SUBJECT_PREFIX = "Final upgrade report"

# What each status says to a person, and the colour that goes with it. ROLLED_BACK and
# REFERRED are never collapsed: one means the cluster is back where it started, the other
# means nobody can say where it is.
_STATUS: dict[UpgradeStatus, tuple[str, str]] = {
    UpgradeStatus.UPGRADED: ("healthy", "#34a853"),
    UpgradeStatus.ROLLED_BACK: ("rolled back", "#ffd166"),
    UpgradeStatus.REFERRED: ("referred", "#ea4335"),
    UpgradeStatus.REFUSED_BEFORE_MUTATION: ("refused, unchanged", "#8f97a5"),
    UpgradeStatus.LANE_NOT_OPENED: ("never started", "#ea4335"),
}

# Ordering: what needs a person first, whatever order the lanes ran in. The same rule the
# builder this replaces followed, and the report page's too.
_ORDER: dict[UpgradeStatus, int] = {
    UpgradeStatus.REFERRED: 0,
    UpgradeStatus.LANE_NOT_OPENED: 1,
    UpgradeStatus.REFUSED_BEFORE_MUTATION: 2,
    UpgradeStatus.ROLLED_BACK: 3,
    UpgradeStatus.UPGRADED: 4,
}

_TD = ("padding:14px 10px;border-bottom:1px solid #2a2f3a;vertical-align:top;"
       "font-size:13px;color:#eef1f6;line-height:1.55")
_SUB = "font-size:11px;color:#8f97a5;margin-top:5px;line-height:1.5"


@dataclass(frozen=True)
class ReportEmail:
    subject: str
    html_body: str
    text_body: str


@dataclass(frozen=True)
class RunFacts:
    """The run facts the report DOCUMENT does not carry, passed in rather than re-derived.

    `UpgradeRunReport` is an account of the lanes and nothing else, so these four values are
    not on it. Every one of them is already on the state payload that `_report` receives, so
    passing them costs no read -- and each defaults to something that renders honestly when a
    caller cannot supply it, because the mail must never be the reason a report is lost.

    WHY NOT PUT THEM ON THE DOCUMENT. They belong there and the plan is to move them; until
    then, a contract change is a bigger change than this mail, and a mail that needed one
    could not ship. When they land, this dataclass is what gets deleted.
    """

    # The version the cluster is on NOW, which is not the target: this system upgrades
    # add-ons and never advances a control plane. Stating the target alone reads as a
    # control plane move that did not happen.
    current_kubernetes_version: str = ""
    # Authorisable steps a person left alone. A count, never a list: naming them would put
    # the page's content back in the mail.
    undecided_steps: int = 0
    # A presigned GET of the report page. Absent means the mail still sends and says so
    # rather than offering a dead control.
    page_url: str = ""
    # step order -> (from_series, to_version, install_method), built from the release the
    # stage already read. See the module docstring on why this is not `LaneOutcome.applied`.
    authorised: dict[int, tuple[str, str, str]] = field(default_factory=dict)


def _e(value: str | None) -> str:
    return html_escape.escape(value or "")


def _plural(n: int, one: str, many: str | None = None) -> str:
    """"1 add-on" / "2 add-ons". The count is in hand here, so the sentence can be correct."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _sorted(report: UpgradeRunReport) -> list[Any]:
    return sorted(report.outcomes,
                  key=lambda o: (_ORDER.get(o.status, 9), o.step_order))


def _versions(outcome: Any, facts: RunFacts) -> tuple[str, str, str]:
    """`(previous, upgraded_to, chart)` for one lane, preferring the authorisation.

    THE AUTHORISATION IS THE SOURCE FOR BOTH VERSIONS. `ReleaseStep.to_version` is the app
    version a named principal released; `LaneOutcome.applied` is what the lane handed the
    tool, which for Helm is the CHART version. Falling back to `applied` when the release
    carries nothing keeps a degraded report readable, and the chart is shown as a chart.
    """
    from_series, to_version, install = facts.authorised.get(outcome.step_order, ("", "", ""))
    previous = from_series or outcome.installed or "not recorded"
    upgraded = to_version or outcome.applied or "not recorded"
    chart = ""
    managed = outcome.managed_addon or install == "EKS_MANAGED_ADDON"
    if not managed and outcome.applied and outcome.applied != upgraded:
        chart = outcome.applied
    return previous, upgraded, chart


def _row(outcome: Any, facts: RunFacts) -> str:
    word, hue = _STATUS.get(outcome.status, (str(outcome.status).lower(), "#8f97a5"))
    moved = outcome.status is UpgradeStatus.UPGRADED
    previous, upgraded, chart = _versions(outcome, facts)

    name = (f'<div style="font-weight:600;color:#eef1f6">{_e(outcome.component)}</div>'
            f'<div style="{_SUB}">'
            f'{"EKS managed add-on" if outcome.managed_addon else "Helm release"}</div>')

    to_cell = (f'<span style="color:{"#34a853" if moved else "#8f97a5"};font-weight:600">'
               f"{_e(upgraded)}</span>")
    if chart:
        to_cell += f'<div style="{_SUB}">chart {_e(chart)}</div>'
    if not moved:
        to_cell += f'<div style="{_SUB}">attempted</div>'

    status = f'<span style="color:{hue};font-weight:600">{_e(word)}</span>'
    # THE HEALTH CHECKPOINT IS SHOWN ONLY IF THE DOCUMENT CARRIES ONE. `_outcome_from` does
    # not forward `checkpoints` today, so this is silent rather than wrong; when the
    # forwarding lands the sub-line appears with no change here.
    if moved and getattr(outcome, "checkpoints", ()):
        first = outcome.checkpoints[0]
        status += (f'<div style="{_SUB}">confirmed at '
                   f"{getattr(first, 'elapsed_seconds', 0)}s</div>")
    elif not moved and outcome.rollback_command:
        status += f'<div style="{_SUB}">rolled back to {_e(previous)}</div>'

    return ("<tr>"
            f'<td style="{_TD}">{name}</td>'
            f'<td style="{_TD}">{_e(previous)}</td>'
            f'<td style="{_TD}">{to_cell}</td>'
            f'<td style="{_TD}">{status}</td>'
            "</tr>")


def build_report_email(
    report: UpgradeRunReport, *, report_uri: str, facts: RunFacts | None = None
) -> ReportEmail:
    """Render one run's outcome. A pure builder, so it is testable with no SES."""
    facts = facts or RunFacts()
    outcomes = _sorted(report)
    applied = [o for o in outcomes if o.status is UpgradeStatus.UPGRADED]
    target = report.target_kubernetes_version
    current = facts.current_kubernetes_version

    # WHAT NEEDS A PERSON IS THE CONTRACT'S DEFINITION, NOT THE STATUS ALONE.
    # `UpgradeRunReport.referred` counts an outcome whose status is fine but whose
    # `cleanup_error` is set -- a lane that upgraded cleanly and then failed to delete its
    # temporary cluster-admin access entry. Partitioning on status alone put that lane in
    # `applied`, so a leaked standing write grant produced a subject line reading
    # "1 add-on upgraded" and nothing for a reader to react to. The table still describes
    # the component by what happened to it (it IS healthy); the subject and the lead follow
    # the contract.
    needs_person = tuple(report.referred)
    if needs_person:
        subject = (f"{SUBJECT_PREFIX}: {_plural(len(needs_person), 'add-on')} "
                   f"{'needs' if len(needs_person) == 1 else 'need'} review on "
                   f"{report.cluster.name} -> Kubernetes {target}")
    else:
        subject = (f"{SUBJECT_PREFIX}: {_plural(len(applied), 'add-on')} upgraded on "
                   f"{report.cluster.name} -> Kubernetes {target}")

    lead = f"<strong>{_plural(len(applied), 'add-on')} upgraded"
    if needs_person:
        lead += (f", {len(needs_person)} "
                 f"{'needs' if len(needs_person) == 1 else 'need'} review")
    lead += ".</strong> This run changed add-ons only"
    # SAID ONLY WHEN THE CALLER SUPPLIED IT. An invented current version would be the one
    # sentence on this mail a reader could disprove in ten seconds.
    if current:
        lead += f"; the control plane is still on {_e(current)}"
    lead += "."

    # THE ONE DETAIL THAT SURVIVED THE CUT, and it is not a detail. A temporary
    # cluster-write access entry that could not be deleted is a STANDING grant on the
    # cluster that nobody asked for, so the mail has to say so and say what to do about it.
    # Everything else about a lane went to the page on the grounds that a mail is a notice;
    # this stays because a notice is exactly what a leaked credential needs. It is now the
    # mail's ONLY banner, and conditional: silent when every entry came off, so a reader who
    # sees a red block on this mail is looking at a live cluster-write grant and nothing else.
    leaked = [o for o in outcomes if o.cleanup_error]
    warning = ""
    if leaked:
        warning = (
            '<p style="font-size:14px;color:#eef1f6;margin:14px 0 0;line-height:1.6;'
            'padding:12px 14px;border-radius:8px;border:2px solid #ea4335;'
            'background:rgba(234,67,53,.16)"><strong>WARNING</strong> the temporary '
            "cluster-write access entry for "
            f"{_e(', '.join(o.component for o in leaked))} could not be deleted "
            f"({_e('; '.join(o.cleanup_error for o in leaked))}). It is a standing grant on "
            "this cluster: check it and revoke it.</p>")

    columns = (("Component", "32%"), ("Previous version", "19%"),
               ("Upgraded to", "22%"), ("Status", "27%"))
    head = "".join(
        '<th style="text-align:left;padding:0 10px 9px;font-size:10px;letter-spacing:.07em;'
        "text-transform:uppercase;color:#8f97a5;font-weight:600;"
        f'border-bottom:1px solid #3b4250;width:{w}">{_e(label)}</th>'
        for label, w in columns)
    table = ('<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
             'style="border-collapse:collapse;margin:20px 0;table-layout:fixed">'
             f"<thead><tr>{head}</tr></thead>"
             f"<tbody>{''.join(_row(o, facts) for o in outcomes)}</tbody></table>")

    # THE REMAINDER AS A NUMBER AND ONE SENTENCE. The count says the run was partial; which
    # components, and what each still needs, is the page's.
    note = ""
    if facts.undecided_steps:
        note = ('<p style="font-size:13px;color:#b9c0cc;margin:0 0 4px;line-height:1.6">'
                f"<strong>{_plural(facts.undecided_steps, 'further add-on change')} "
                f"{'was' if facts.undecided_steps == 1 else 'were'} offered and not "
                "decided.</strong> Nothing on those add-ons changed, and they will be "
                "offered again on the next run.</p>")

    button = ""
    if facts.page_url:
        button = (f'<a href="{_e(facts.page_url)}" style="display:inline-block;'
                  "padding:12px 22px;margin:4px 0 20px;background:#f9ab00;color:#0f1115;"
                  'text-decoration:none;border-radius:6px;font-weight:600;font-size:15px">'
                  "Open the report</a>")

    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:28px 24px;background:#0f1115;
font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#eef1f6">
<div style="max-width:820px;margin:0 auto">
  <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#f9ab00">
    Amazon EKS &middot; final upgrade report
  </div>
  <h1 style="font-size:21px;margin:8px 0 6px;font-weight:600">{_e(report.cluster.name)}
    <span style="color:#8f97a5;font-weight:400">&rarr; Kubernetes {_e(target)}</span></h1>
  <p style="font-size:14px;color:#b9c0cc;margin:0;line-height:1.6">{lead}</p>
  {warning}
  {table}
  {note}
  {button}
  <p style="font-size:11px;color:#8f97a5;border-top:1px solid #2a2f3a;padding-top:14px;
    line-height:1.6">
    The report carries every add-on this run considered, the command that applied each
    change, what the health agent read, and the evidence bundles.
    <br>record <span style="word-break:break-all">{_e(report_uri)}</span>
  </p>
</div>
</body></html>
"""

    text = [subject, "",
            f"{_plural(len(applied), 'add-on')} upgraded"
            + (f", {len(needs_person)} "
               f"{'needs' if len(needs_person) == 1 else 'need'} review"
               if needs_person else "")
            + ". This run changed add-ons only"
            + (f"; the control plane is still on {current}" if current else "") + ".", ""]
    if leaked:
        text += [f"WARNING: the temporary cluster-write access entry for "
                 f"{', '.join(o.component for o in leaked)} could not be deleted "
                 f"({'; '.join(o.cleanup_error for o in leaked)}).",
                 "It is a standing grant on this cluster: check it and revoke it.", ""]
    text.append("  %-24s %-20s %-20s %s"
                % ("COMPONENT", "PREVIOUS VERSION", "UPGRADED TO", "STATUS"))
    for outcome in outcomes:
        word, _hue = _STATUS.get(outcome.status, (str(outcome.status).lower(), ""))
        previous, upgraded, _chart = _versions(outcome, facts)
        text.append("  %-24s %-20s %-20s %s"
                    % (outcome.component, previous,
                       upgraded + ("" if outcome.status is UpgradeStatus.UPGRADED
                                   else " (attempted)"),
                       word))
    if facts.undecided_steps:
        text += ["",
                 f"{_plural(facts.undecided_steps, 'further add-on change')} "
                 f"{'was' if facts.undecided_steps == 1 else 'were'} offered and not "
                 "decided. Nothing on those",
                 "add-ons changed, and they will be offered again on the next run."]
    text += ["", f"record    {report_uri}", "",
             "The report carries every add-on this run considered, the command that applied",
             "each change, what the health agent read, and the evidence bundles."]

    return ReportEmail(subject=subject, html_body=html_body, text_body="\n".join(text))


def send_report_email(message: ReportEmail, *, client: Any = None) -> str:
    """Send it. Returns the SES message id, or "" when mail is not configured.

    UNCHANGED FROM THE BUILDER THIS REPLACES, including the environment variables: one
    deployment, one place a person's address is configured. Two would mean a run whose
    approvals arrive and whose report does not, with nothing saying why.
    """
    sender = os.environ.get("APPROVAL_EMAIL_FROM", "").strip()
    recipients = [a.strip() for a in os.environ.get("APPROVAL_EMAIL_TO", "").split(",")
                  if a.strip()]
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
