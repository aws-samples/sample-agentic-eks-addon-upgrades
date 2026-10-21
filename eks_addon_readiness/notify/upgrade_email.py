"""The upgrade approval notice: the ask, the count, and one link. The page does the rest.

A SEPARATE BUILDER FROM `email.py`, and not because the two messages look different. They
ask for different things. `email.py` asks *"is this claim about a component true?"* -- a
question about the world, answered once for the whole fleet. This asks *"may we change this
cluster?"* -- a question about one cluster, answered per cluster, where the same component
may be approved here and refused next door. Sharing one builder would mean one subject line
and one body serving both, and the first thing to go would be the distinction.

**IT CARRIES ALMOST NOTHING, DELIBERATELY.** No versions, no component names, no counts of
breaking changes, no digests. The fact-approval mail learned this the hard way: it rendered
five columns of proposal detail, which was a second copy of the page guaranteed to drift,
and a reader deciding from a preview pane is a reader deciding badly. A mail client is the
worst surface this content can land on -- no reveal-on-click, styling stripped by half the
clients -- so everything a person reads before deciding lives on the page, and the mail's
job is to get them there.

**WHAT IT DOES CARRY IS THE SHAPE OF THE ASK.** The cluster, the target version, and how
many changes are waiting. That is enough to judge whether to open it now or after lunch, and
deliberately not enough to decide from.

**AND IT NAMES WHAT THE NUMBER COUNTS, because it used to be read as something else.** `waiting`
counts APPROVABLE PLAN STEPS, and a step is not a component: one component can hold more than one
step, and the four AWS-managed recommendations are steps too. On one cluster that
made the mail say "9 add-on change(s)" about a cluster with seven components, and "add-on
change(s)" is read as "add-ons" -- so the count looked wrong and the mail looked untrustworthy
over an arithmetic it had never explained. `components` is passed in so the sentence can state
both numbers whenever they differ. It is a count and not a list, for the reason the rest of this
docstring gives: naming which components would put the page's content back in the mail.

**NO NONCE, EVER, AND THAT IS THIS PACKAGE'S WHOLE POINT.** The link is a presigned S3 GET
of the page. The per-step capabilities are the presigned POST forms ON that page, minted by
the stage and reachable only by someone who can already read it. See `notify/__init__.py`:
putting a capability where the evidence lives is the one mistake this split exists to
prevent.
"""

from __future__ import annotations

import html as html_escape
from dataclasses import dataclass

# Deliberately boring, and the same prefix the fact mail uses. A subject that looks like an
# alert trains people to ignore it, and this is a mail that must be opened. It names the
# CLUSTER rather than the components, because the cluster is what the decision is about --
# see the module docstring.
SUBJECT_PREFIX = "Approval needed"


@dataclass(frozen=True)
class UpgradeEmail:
    """A built message, ready to send. No SES type in the signature, so it is testable."""

    subject: str
    html_body: str
    text_body: str


def _e(value: str | None) -> str:
    return html_escape.escape(value or "")


def _ask(waiting: int, components: int) -> str:
    """How many changes are waiting, and across how many add-ons -- when those differ.

    `waiting` counts APPROVABLE PLAN STEPS. A component can hold more than one, so the two
    numbers are not the same measurement and the reader has no way to know that from one of them.
    Stating both is what stops "9 add-on change(s)" being read as "9 add-ons" on a cluster the
    reader knows has seven components.

    BOTH NUMBERS ONLY WHEN THEY DIFFER. With one step per component there is nothing to misread,
    and "7 change(s) to 7 add-on(s)" reads like a bug rather than like arithmetic. `components` of
    0 means the count could not be taken (see `stages/upgrade_approval.py::_components`), and an
    absent number is never printed as a real one.
    """
    if components and components != waiting:
        return f"{waiting} change(s) to {components} add-on(s)"
    return f"{waiting} add-on change(s)"


def build_upgrade_email(
    *,
    run_id: str,
    cluster: str,
    target_kubernetes_version: str,
    page_url: str,
    waiting: int,
    components: int = 0,
    briefed: int = 0,
    unbriefed: int = 0,
) -> UpgradeEmail:
    """Build the notice. `page_url` is a presigned S3 link to this run's approval page.

    `waiting` is a count of APPROVABLE PLAN STEPS and `components` of the add-ons those steps
    touch; `_ask` states both whenever they differ, because a step is not a component and the
    reader cannot tell that from one number. `components` defaults to 0, which means "not
    counted" and prints nothing -- so a caller that cannot take the count still sends a mail
    that is true.

    `briefed` and `unbriefed` produce ONE extra sentence and only when something is missing.
    A reader needs to know before they open the page that some of these changes come with no
    account of what they break, because that changes whether this is a five-minute job or an
    afternoon. It is a count and not a list: naming which components lack a brief would put
    the page's content back in the mail.
    """
    ask = _ask(waiting, components)
    subject = (
        f"{SUBJECT_PREFIX}: {ask} on {cluster} "
        f"-> Kubernetes {target_kubernetes_version}"
    )

    # STATED ONLY WHEN SOMETHING IS MISSING. An "all briefed" sentence on every mail is a
    # sentence people stop reading, and then the one that says otherwise is skipped too.
    gap = ""
    if unbriefed:
        gap = (
            '<p style="font-size:14px;color:#eef1f6;margin:14px 0 0;line-height:1.6;'
            'padding:12px 14px;border-radius:8px;border:2px dashed #ec835a;'
            'background:rgba(236,131,90,.10)">'
            f"<strong>{unbriefed} of those changes "
            f"{'has' if unbriefed == 1 else 'have'} no "
            "advisory brief.</strong> Nothing was read about what "
            f"{'it' if unbriefed == 1 else 'they'} break, so that is unknown rather than "
            "absent. The page says which, and what was attempted.</p>"
        )

    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:28px 24px;background:#0f1115;
font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#eef1f6">
<div style="max-width:820px;margin:0 auto">
  <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#f9ab00">
    Amazon EKS &middot; authorisation needed to change a cluster
  </div>
  <h1 style="font-size:21px;margin:8px 0 6px;font-weight:600">{_e(cluster)}
    <span style="color:#8f97a5;font-weight:400">&rarr; Kubernetes
    {_e(target_kubernetes_version)}</span></h1>
  <p style="font-size:14px;color:#b9c0cc;margin:0 0 4px;line-height:1.6">
    <strong>{_e(ask)}</strong> need your approval before this cluster can
    move to Kubernetes {_e(target_kubernetes_version)}. Each change is a separate decision,
    and each one changes a running cluster. <strong style="color:#eef1f6">Nothing has been
    changed yet.</strong>
  </p>
  {gap}
  <a href="{_e(page_url)}" style="display:inline-block;padding:12px 22px;margin:20px 0;
    background:#f9ab00;color:#0f1115;text-decoration:none;border-radius:6px;
    font-weight:600;font-size:15px">Review and approve</a>
  <p style="font-size:11px;color:#8f97a5;border-top:1px solid #2a2f3a;padding-top:14px;
    line-height:1.6">
    Approve and reject are on the page, one pair per change, with what each change breaks
    and why it is needed. The link is presigned and time-limited; nothing in this message
    can approve anything on its own. An approval applies to this cluster only.
    <br>run {_e(run_id)}
  </p>
</div>
</body></html>
"""

    text_lines = [
        subject,
        "",
        f"{ask} need your approval before {cluster} can move to "
        f"Kubernetes {target_kubernetes_version}.",
        "Each change is a separate decision and each changes a running cluster. Nothing has "
        "been changed yet.",
    ]
    if unbriefed:
        text_lines += [
            "",
            f"{unbriefed} of those changes have no advisory brief: what they break is "
            "unknown rather than absent.",
        ]
    text_lines += [
        "",
        f"Review and approve: {page_url}",
        "",
        "An approval applies to this cluster only.",
        f"run {run_id}",
    ]

    return UpgradeEmail(
        subject=subject, html_body=html_body, text_body="\n".join(text_lines)
    )


def build_readiness_email(
    *,
    run_id: str,
    cluster: str,
    target_kubernetes_version: str,
    page_url: str,
    changes: int,
    components: int = 0,
) -> UpgradeEmail:
    """The notice for a deployment that EXECUTES NOTHING: here is the readiness, nothing to decide.

    `build_upgrade_email`'s sibling for `upgrade_stage_enabled = false`. Until this existed that
    deployment asked a person to approve changes it would never apply, then finished without a
    word. The report is the same page the approver would have seen -- the plan, each step's
    brief, the compatibility verdicts, the components as scanned -- rendered without a control,
    and this mail says so plainly rather than asking for anything.
    """
    what = _ask(changes, components)
    # NOT `SUBJECT_PREFIX`: that reads "Approval needed", and this mail needs nothing.
    subject = (
        f"EKS readiness: {cluster} -> Kubernetes {target_kubernetes_version} "
        f"({what} recommended, execution disabled)"
    )
    html_body = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:28px 24px;background:#0f1115;
font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#eef1f6">
<div style="max-width:820px;margin:0 auto">
  <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#f9ab00">
    Amazon EKS &middot; add-on upgrade readiness
  </div>
  <h1 style="font-size:21px;margin:8px 0 6px;font-weight:600">{_e(cluster)}
    <span style="color:#8f97a5;font-weight:400">&rarr; Kubernetes
    {_e(target_kubernetes_version)}</span></h1>
  <p style="font-size:14px;color:#b9c0cc;margin:0 0 4px;line-height:1.6">
    <strong>{_e(what)}</strong> {'is' if changes == 1 else 'are'} recommended before this
    cluster moves to Kubernetes {_e(target_kubernetes_version)}. The report has each change
    in order, what it breaks and why it is needed, and every component as scanned.
    <strong style="color:#eef1f6">This deployment does not execute upgrades, so nothing is
    asked of you and nothing has been changed.</strong>
  </p>
  <a href="{_e(page_url)}" style="display:inline-block;padding:12px 22px;margin:20px 0;
    background:#f9ab00;color:#0f1115;text-decoration:none;border-radius:6px;
    font-weight:600;font-size:15px">Open the readiness report</a>
  <p style="font-size:11px;color:#8f97a5;border-top:1px solid #2a2f3a;padding-top:14px;
    line-height:1.6">
    The link is presigned and time-limited. The page carries no controls; to have this
    pipeline apply these changes, enable the upgrade stage in its deployment.
    <br>run {_e(run_id)}
  </p>
</div>
</body></html>
"""
    text_body = "\n".join(
        [
            subject,
            "",
            f"{what} {'is' if changes == 1 else 'are'} recommended before {cluster} moves to "
            f"Kubernetes {target_kubernetes_version}.",
            "This deployment does not execute upgrades, so nothing is asked of you and nothing "
            "has been changed.",
            "",
            f"Readiness report: {page_url}",
            "",
            f"run {run_id}",
        ]
    )
    return UpgradeEmail(subject=subject, html_body=html_body, text_body=text_body)
