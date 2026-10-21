"""Delivering an approval request to a person, over a channel the agent cannot read.

WHY THIS PACKAGE EXISTS AT ALL, stated plainly because it is the whole security argument
for the clickable approval links.

A one-time link is a CAPABILITY: whoever holds the nonce can perform that one action. That
is fine -- it is how essentially every approval email on the internet works, and it is the
shape AWS's own "Human Approval" sample project for Step Functions uses. What makes it
either safe or useless is WHERE THE LINK IS WRITTEN DOWN.

This project's load-bearing claim is that anything able to read the report -- a person, a
script or an agent -- can SHOW a pending approval but cannot GRANT one. Such a reader sees
the report objects in S3, so a link rendered into the approval page would hand it a working
approval and destroy that
claim -- which is exactly why the page was reduced to a `curl` command a human signs, and
why that was miserable to use.

So the page keeps the EVIDENCE and the link goes down a different channel. The evidence is
what needs to be widely readable; the capability is what needs to be narrow. Email splits
them cleanly: the agent has no mailbox, holds no `ses:` permission, and cannot read the
approver's inbox. The S3 page therefore contains no nonce and no token, and stays as
readable as it ever was.

WHAT WOULD BREAK THIS, so it does not get broken later:

  * Rendering a nonce into any object the pipeline writes to S3.
  * Returning a nonce in a stage's result -- the Step Functions execution history is
    readable by anything with `states:GetExecutionHistory`.
  * Logging a nonce. A link in CloudWatch Logs is a link anything with `logs:FilterLogEvents`
    can redeem.

All three are the same mistake: putting the capability where the evidence lives.
"""

from .email import ApprovalEmail, build_approval_email, send_approval_email
from .upgrade_email import UpgradeEmail, build_readiness_email, build_upgrade_email

__all__ = [
    "ApprovalEmail",
    "UpgradeEmail",
    "build_approval_email",
    "build_readiness_email",
    "build_upgrade_email",
    "send_approval_email",
]
