"""Lambda entry points for the upgrade brief: the agent proxy, the tool, and the two ends.

A SEPARATE ENTRY MODULE rather than more handlers in `stages.py`, for `research_harness.py`'s
reason: the proxy needs a package with `boto3` new enough for `InvokeHarness`, and `stages.py`'s
zip deliberately ships no boto3 at all -- the Lambda runtime provides it, and the runtime's copy is
older than the API. One zip for the brief keeps that vendoring off every other stage.

FOUR HANDLERS IN ONE FILE, AND THEY ARE FOUR DIFFERENT ROLES. They share this module because they
share a deployment package, not because they share permissions: each gets its own Lambda, its own
role and its own grants in `infra/terraform/modules/brief`. In particular the tool has no Bedrock
and the
orchestrator has no GitHub egress.

    stage_brief_plan       reads the plan, emits one work item per component move
    stage_brief_component  ONE component: invoke the brief Harness, verify, write a partial
    stage_brief_assemble   reads the partials, writes the one UpgradeBrief document
    tool_search_issues     the `search_issues` Gateway tool

NOTHING HERE HOLDS LOGIC. The modules do: `eks_addon_readiness/stages/upgrade_brief.py`,
`brief_harness.py` and `fetch_issues.py`.

EVERY ONE OF THESE IS ADVISORY. None of them raises for a condition of the world -- an unreachable
project, a rate limit, a model that would not answer -- because the run must continue to the report
with the absence recorded. See `brief/__init__.py`.
"""

from __future__ import annotations

from typing import Any

from eks_addon_readiness.stages import brief_harness, fetch_issues, upgrade_brief

Event = dict[str, Any]
Result = dict[str, Any]


def stage_brief_plan(event: Event, context: Any = None) -> Result:
    """Emit one work item per component move in the plan, for the Map to fan out over.

    The trigger is the PLAN, not this run's approvals: a move can rest on a fact approved weeks
    ago, and keying off the current run's approvals would skip exactly those components.
    """
    return upgrade_brief.handler({**event, "mode": "plan"}, context)


def stage_brief_component(event: Event, context: Any = None) -> Result:
    """Brief ONE component through the brief Harness and write its partial.

    Invoked by the Map once per component move. Returns a recorded outcome rather than raising, so
    one project whose sources are unreadable does not discard the briefs that succeeded beside it.
    """
    return brief_harness.handler(event, context)


def stage_brief_assemble(event: Event, context: Any = None) -> Result:
    """Assemble the run's one advisory document from the partials the Map wrote.

    A step whose partial is missing is named in `unbriefed` with the reason rather than omitted:
    an omission reads as "nothing to say about this move".
    """
    return upgrade_brief.handler({**event, "mode": "assemble"}, context)


def tool_search_issues(event: Event, context: Any = None) -> Result:
    """The `search_issues` tool, called by the brief agent through its Gateway.

    Records one object per issue so a quote can be verified, plus a receipt naming the lookup's
    own outcome so a rate-limited search cannot later be reported as "no known issues".
    """
    return fetch_issues.handler(event, context)
