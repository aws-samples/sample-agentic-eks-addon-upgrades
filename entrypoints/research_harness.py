"""Lambda entry point for Stage 6 via the Harness.

A SEPARATE ENTRY MODULE rather than another handler in `stages.py`, for one reason: this one
needs a package with `boto3` new enough for `InvokeHarness`, and `stages.py`'s zip
deliberately ships no boto3 at all -- the Lambda runtime provides it, and the runtime's copy
is older than the API. Giving this handler its own zip keeps that vendoring off every other
stage.

It holds no logic. The proxy is `eks_addon_readiness/stages/research_harness.py`, and what it
proxies to is the Harness (which reads) and the judge Lambda (which decides). Nothing here
runs a model and nothing here writes a fact.
"""

from __future__ import annotations

from typing import Any

from eks_addon_readiness.stages import research_harness

Event = dict[str, Any]
Result = dict[str, Any]


def stage_research_harness(event: Event, context: Any = None) -> Result:
    """Research one component through the Harness, then judge the extraction it returned.

    Invoked by the pipeline's `Research` Map, once per queued component. Returns the judge's
    verdict, or a recorded failure -- never raises, so one unreadable component does not
    discard the research that succeeded for the rest.
    """
    return research_harness.handler(event, context)
