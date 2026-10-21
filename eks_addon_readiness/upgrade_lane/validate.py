"""The floor: the handful of facts about a component that need no judgement to read.

    in    one `kubectl get pods -o json` for the release's own pods
    out   whether the cluster is already telling us, unambiguously, that this is broken

**THE AGENT JUDGES HEALTH. THIS DOES NOT.** That is a correction to how this stage was first built
and it is worth stating plainly, because the first version had it backwards. Deterministic code
used to decide HEALTHY or UNHEALTHY by pattern-matching log lines -- `^[EF]\\d{4}`, `level=error`,
a list of runtime strings -- and the agent only triaged what that check had already rejected.

That was the wrong division of labour. "Is this component working" is exactly the judgement a
model is good at and a regex is bad at: a component that logs `E0920 leader election lost` during
a normal rollout is fine, one that logs nothing at all may be wedged, and every chart has its own
dialect. A pattern list is guaranteed to false-positive on some components and miss others, and
the cost of the first is a working component rolled back.

**SO WHAT IS LEFT HERE IS A FLOOR, NOT A VERDICT.** Four conditions the API server states as
fact, which no reading can soften:

    CrashLoopBackOff / ImagePullBackOff / ErrImagePull / CreateContainerConfigError
    OOMKilled
    phase Failed
    the manifest declares a workload and no pod matched its selector

**WHAT IS DELIBERATELY NOT IN THE FLOOR, and each was in the old check.** "Not Ready" -- a pod
thirty seconds into a rollout is not Ready and is not broken. A raised restart count on its own --
a container that restarted once and settled is the normal shape of a version move. Error lines in
logs -- that is the judgement, and it is the agent's. An unreadable pod list -- that is not
brokenness, it is the absence of evidence, and it must reach a person as such rather than as a
rollback.

**WHY A FLOOR EXISTS AT ALL, GIVEN THE AGENT JUDGES.** Because the agent can be unreachable, and
a stage with no answer must still have one. The floor is what answers then. It is also the
tiebreak: if the agent says HEALTHY and the floor says CrashLoopBackOff, they disagree about
something not open to interpretation, and that disagreement is itself a referral -- a person
should see it rather than have either side win silently.

THE FLOOR CAN ONLY EVER SAY "BROKEN". It has no way to say "fine", which is the asymmetry that
keeps it from quietly becoming the verdict again.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from pydantic import Field

from ..models import ContractModel

# Container states that are brokenness by definition. Read off `containerStatuses[].state`, where
# the API server has already decided -- there is no interpretation left to do, which is the whole
# test for membership of this list.
BROKEN_WAITING_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "CreateContainerConfigError",
        "CreateContainerError",
        "InvalidImageName",
    }
)

# `Completed` is absent: a Job's container exiting cleanly is not a failure. `OOMKilled` and
# `Error` are, and `DeadlineExceeded` is the API server saying it gave up.
BROKEN_TERMINATED_REASONS = frozenset({"OOMKilled", "Error", "DeadlineExceeded"})

# `Failed` only. NOT `Pending` -- a pod waiting for a node during a rollout is Pending and fine --
# and NOT `Succeeded`, which for a long-running component is suspicious but is a judgement.
BROKEN_PHASES = frozenset({"Failed"})

# Log lines the AGENT is asked to read. Kept here, unused by any decision, because the buildspec
# excerpts logs for the prompt and a shared idea of "worth looking at" is useful for that. NOTHING
# BRANCHES ON THIS -- if something ever does, it has become a verdict again.
NOTABLE_LOG_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^[EF]\d{4}\s"),
    re.compile(r'\blevel[=":\s]+"?(error|fatal|panic)\b', re.IGNORECASE),
    re.compile(r"^panic:\s"),
    re.compile(r"^Traceback \(most recent call last\)"),
    re.compile(r'is forbidden: User ".*" cannot '),
)


class PodFacts(ContractModel):
    """One pod, reduced to what the floor reads and what the agent is shown."""

    name: str = Field(min_length=1)
    phase: str = ""
    ready: bool = False
    restarts: int = Field(default=0, ge=0)
    waiting_reasons: tuple[str, ...] = ()
    terminated_reasons: tuple[str, ...] = ()


class Floor(ContractModel):
    """What the cluster states as fact. `broken` is never a guess.

    `readable` IS SEPARATE FROM `broken`, and conflating them is the mistake this class exists to
    avoid. A pod list that could not be read is not a broken component -- it is an unvalidated
    one, and the two want different outcomes: brokenness wants a rollback, absent evidence wants a
    person.
    """

    readable: bool = True
    broken: bool = False
    # Non-empty whenever `broken`. Each entry names the pod and the fact, so the report and the
    # agent's prompt can both cite it.
    reasons: tuple[str, ...] = ()
    restarts_now: int = Field(default=0, ge=0)
    restarts_rose: bool = False
    pods_seen: int = Field(default=0, ge=0)
    pods_ready: int = Field(default=0, ge=0)


def pods_from_kubectl(payload: Mapping[str, object]) -> tuple[PodFacts, ...]:
    """Reduce a `kubectl get pods -o json` list to `PodFacts`.

    TOLERANT OF SHAPE AND INTOLERANT OF ABSENCE. Any field it cannot read becomes the
    broken-leaning default -- `ready` is False when the condition is missing rather than True --
    because a pod this function could not understand must not be counted as one that was fine.
    Note that `ready` feeds the agent's prompt and the counts, NOT the floor: not-Ready is not
    brokenness.
    """
    items = payload.get("items")
    if not isinstance(items, Sequence):
        return ()

    pods: list[PodFacts] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        metadata = item.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        name = metadata.get("name")
        status = item.get("status")
        status = status if isinstance(status, Mapping) else {}

        ready = False
        conditions = status.get("conditions")
        if isinstance(conditions, Sequence):
            for condition in conditions:
                if (
                    isinstance(condition, Mapping)
                    and condition.get("type") == "Ready"
                    and condition.get("status") == "True"
                ):
                    ready = True

        restarts = 0
        waiting: list[str] = []
        terminated: list[str] = []
        statuses = status.get("containerStatuses")
        if isinstance(statuses, Sequence):
            for container in statuses:
                if not isinstance(container, Mapping):
                    continue
                count = container.get("restartCount")
                if isinstance(count, int):
                    restarts += count
                state = container.get("state")
                if isinstance(state, Mapping):
                    for key, bucket in (("waiting", waiting), ("terminated", terminated)):
                        detail = state.get(key)
                        if isinstance(detail, Mapping):
                            reason = detail.get("reason")
                            if isinstance(reason, str) and reason:
                                bucket.append(reason)

        pods.append(
            PodFacts(
                name=str(name) if isinstance(name, str) else "<unnamed>",
                phase=str(status.get("phase") or ""),
                ready=ready,
                restarts=restarts,
                waiting_reasons=tuple(waiting),
                terminated_reasons=tuple(terminated),
            )
        )
    return tuple(pods)


def notable_log_lines(log_text: str, *, limit: int = 25) -> tuple[str, ...]:
    """Lines worth putting in front of the agent. NOT a verdict, and nothing branches on it.

    Capped because a crash-looping container produces thousands of identical lines and a prompt is
    finite. The full log is in the diagnosis bundle, which is where a person reads it.
    """
    found: list[str] = []
    for raw in log_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(pattern.search(line) for pattern in NOTABLE_LOG_PATTERNS):
            found.append(line[:400])
            if len(found) >= limit:
                break
    return tuple(found)


def floor_for(
    *,
    pods: Sequence[PodFacts],
    restarts_before: int = 0,
    pods_readable: bool = True,
    expect_pods: bool = True,
) -> Floor:
    """What the cluster states as fact about these pods. Never a judgement.

    `pods_readable=False` YIELDS `readable=False` AND `broken=False`, which is the distinction the
    old version of this module got wrong: it reported an unreadable pod list as UNHEALTHY, so a
    denied read rolled a component back. Absent evidence is not brokenness. The caller routes it to
    a referral, because a person has to establish what the code could not.

    `expect_pods=False` IS FOR A RELEASE THAT OWNS NO PODS. A chart of CRDs and RBAC has nothing to
    be ready, and counting "no pods" as broken would refuse a component that is fine. Passed from
    the rendered manifest rather than guessed from an empty list, so "this chart has no workload"
    and "the workload vanished" stay distinguishable.
    """
    if not pods_readable:
        return Floor(readable=False, broken=False, reasons=())

    reasons: list[str] = []

    if expect_pods and not pods:
        reasons.append(
            "the release's manifest declares a workload and no pod matched its selector, so "
            "either the upgrade removed them or the labels no longer match"
        )

    for pod in pods:
        if pod.phase in BROKEN_PHASES:
            reasons.append(f"pod {pod.name} is in phase {pod.phase}")
        for reason in pod.waiting_reasons:
            if reason in BROKEN_WAITING_REASONS:
                reasons.append(f"pod {pod.name} has a container waiting on {reason}")
        for reason in pod.terminated_reasons:
            if reason in BROKEN_TERMINATED_REASONS:
                reasons.append(f"pod {pod.name} has a container terminated with {reason}")

    restarts_now = sum(pod.restarts for pod in pods)
    return Floor(
        readable=True,
        broken=bool(reasons),
        reasons=tuple(reasons),
        restarts_now=restarts_now,
        # REPORTED AND NOT A REASON. A raised restart count is the commonest shape of a broken new
        # version AND the normal shape of a healthy one that restarted once and settled, so it goes
        # to the agent as evidence rather than to the floor as a fact.
        restarts_rose=restarts_now > restarts_before,
        pods_seen=len(pods),
        pods_ready=sum(1 for pod in pods if pod.ready),
    )
