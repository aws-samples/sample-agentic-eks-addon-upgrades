"""Lambda entry point for the upgrade agent's proxy.

A SEPARATE ENTRY MODULE rather than another handler in `stages.py`, for `research_harness.py`'s
reason: this one needs a package with `boto3` new enough for `InvokeHarness`, and `stages.py`'s
zip deliberately ships no boto3 at all -- the Lambda runtime provides it, and the runtime's copy is
older than the API. One zip for the upgrade agent keeps that vendoring off every other stage.

It holds no logic. The proxy is `eks_addon_readiness/stages/upgrade_triage.py`, and the instruction
it
sends is `eks_addon_readiness/upgrade_lane/prompt.py`.

TWO HANDLERS, AND ONLY ONE OF THEM RUNS A MODEL. Every mutation the upgrade stage
makes -- `helm upgrade`, `eks update-addon`, the rollback, the access-entry lifecycle -- is
performed
by deterministic code from a version-pinned artifact. This reads a sealed diagnosis bundle and
returns
a verdict that `UpgradeTriage` validated. It writes nothing, holds no cluster credential, and cannot
reach the cluster at all.
"""

from __future__ import annotations

from typing import Any

from eks_addon_readiness.stages import upgrade_report, upgrade_triage

Event = dict[str, Any]
Result = dict[str, Any]


def stage_upgrade_triage(event: Event, context: Any = None) -> Result:
    """Triage one upgraded component from its sealed diagnosis bundle. Never raises.

    Invoked by the lane's CodeBuild build, after validation has refused the component and the
    `after` bundle has been SEALED, and before the rollback. That order is the whole design:
    asking earlier would triage evidence that might not be on disk, and asking later would triage
    a component that had already been replaced.

    Returns `available: False` with a reason rather than failing, because the caller is about to
    decide whether to roll a component back and its deterministic default -- roll back -- must not
    depend on a model having answered.
    """
    return upgrade_triage.handler(event, context)


def stage_upgrade_report(event: Event, context: Any = None) -> Result:
    """Plan one lane per approved component, then report what the lanes did. `mode` says which.

    NO MUTATION HERE, and no model either -- which is the shape of the whole stage. Every change to
    the cluster happens in CodeBuild, from a version-pinned zip, or in a Step Functions AWS SDK
    call; this reads documents and writes one.

    Two modes on one function for `stage_brief_plan`/`stage_brief_assemble`'s reason inverted: those
    are two functions because their PERMISSIONS differ, and these two halves read and write the same
    prefixes in the same bucket, so splitting them would be two roles holding identical grants.

    IT NEVER RAISES FOR A CONDITION OF THE WORLD in report mode: an unreadable lane result becomes a
    REFERRED row naming the reason, because the report is the one artifact a person is waiting for
    after a cluster has been changed. In PLAN mode it DOES raise on a missing parameter or an
    unvalidatable release, because a plan that cannot establish what was authorised must not open a
    lane at all.
    """
    return upgrade_report.handler(event, context)
