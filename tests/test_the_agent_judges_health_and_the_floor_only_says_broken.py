"""The agent judges health. Deterministic code keeps a floor of facts, and it can only say "broken".

WHY THIS FILE EXISTS. The stage was first built the other way round: `validate.py` decided HEALTHY
or UNHEALTHY by pattern-matching log lines -- `^[EF]\\d{4}`, `level=error`, a list of runtime
strings -- and the agent only triaged what that check had already rejected. That is the wrong
division of labour, and the human said so. "Is this component working" is exactly what a model is
good at and a regex is bad at: a component that logs `E0920 leader election lost` during a normal
rollout is fine, one that logs nothing at all may be wedged, and every chart has its own dialect.
A pattern list is guaranteed to false-positive on some components and miss others -- and the cost
of the first is a working component rolled back.

SO THE FLOOR IS ALL THAT IS LEFT OF THE OLD CHECK, and this file holds it to three properties:

  IT ONLY EVER SAYS "BROKEN". It has no way to say "fine", which is the asymmetry that keeps it
  from quietly becoming the verdict again.

  IT ONLY READS FACTS. A container in CrashLoopBackOff is a fact. A pod that is not Ready thirty
  seconds into a rollout is not, a restart count that rose by one is not, and a log line
  containing the word error is certainly not. Each of those was in the old check.

  AN UNREADABLE POD LIST IS NOT BROKENNESS. It is the absence of evidence, and the two want
  different outcomes: brokenness wants a rollback, absent evidence wants a person.

AND THE DECISION TABLE IS ASSERTED AGAINST THE BUILDSPEC, because the floor and the verdict only
mean something in how they combine -- most of all in the two cases that are easy to get wrong:
the agent contradicting a stated fact, and the agent not answering at all.

Deliberately no AWS call.
"""

from __future__ import annotations

from pathlib import Path

from eks_addon_readiness.upgrade_lane.validate import (
    PodFacts,
    floor_for,
    notable_log_lines,
)

BUILDSPEC = (
    Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/upgrade_buildspec.yml"
)


def _pod(
    name: str = "metrics-server-abc-def",
    *,
    phase: str = "Running",
    ready: bool = True,
    restarts: int = 0,
    waiting: tuple[str, ...] = (),
    terminated: tuple[str, ...] = (),
) -> PodFacts:
    return PodFacts(
        name=name,
        phase=phase,
        ready=ready,
        restarts=restarts,
        waiting_reasons=waiting,
        terminated_reasons=terminated,
    )


# ---------------------------------------------------------------------------
# What the floor must NOT call broken. Every one of these was UNHEALTHY under the old check.
# ---------------------------------------------------------------------------


def test_a_pod_that_is_not_ready_yet_is_not_broken() -> None:
    """Thirty seconds into a rollout a pod is not Ready and is perfectly fine.

    The old check rolled that back. Readiness is evidence the agent is shown, not a fact the floor
    acts on.
    """
    floor = floor_for(pods=[_pod(ready=False)])
    assert not floor.broken, f"a not-Ready pod was called broken: {floor.reasons}"
    assert floor.pods_ready == 0 and floor.pods_seen == 1, (
        "the counts are still reported, because the agent needs them as evidence"
    )


def test_a_raised_restart_count_alone_is_not_broken() -> None:
    """A container that restarted once and settled is the normal shape of a version move.

    It is also the commonest shape of a broken one, which is exactly why it is a judgement and not
    a fact. Reported as `restarts_rose` for the agent to weigh.
    """
    floor = floor_for(pods=[_pod(restarts=3)], restarts_before=1)
    assert not floor.broken, f"a restart delta was called broken: {floor.reasons}"
    assert floor.restarts_rose, "the delta is not reported, so the agent cannot weigh it"


def test_error_lines_in_logs_are_not_broken() -> None:
    """The judgement the whole rewrite is about. Nothing here may branch on a log line.

    `notable_log_lines` still exists -- the buildspec uses it to choose what to put in front of the
    agent -- but it feeds a prompt, not a decision.
    """
    noisy = "E0920 12:00:00.000 leader election lost\nlevel=error msg=\"retrying\"\npanic: boom"
    assert notable_log_lines(noisy), "the notable-lines helper found nothing to show the agent"
    # And the floor is computed without ever seeing the text: its signature takes no log argument.
    floor = floor_for(pods=[_pod()])
    assert not floor.broken
    import inspect

    assert "log" not in inspect.signature(floor_for).parameters, (
        "`floor_for` takes a log argument again. Log text is the agent's to judge; the moment the "
        "floor reads it, the pattern list is the verdict once more."
    )


def test_an_unreadable_pod_list_is_unknown_and_not_broken() -> None:
    """Absent evidence is not brokenness, and the old check conflated them.

    A denied read used to mean UNHEALTHY, so a missing RBAC verb rolled a component back. It now
    means `readable=False` with nothing broken, and the buildspec routes that to a person.
    """
    floor = floor_for(pods=[], pods_readable=False)
    assert not floor.broken, "an unreadable pod list was called broken"
    assert not floor.readable, "the unreadability is not reported, so nothing can act on it"


def test_a_release_that_owns_no_pods_is_not_broken() -> None:
    """A chart of CRDs and RBAC has nothing to be ready."""
    assert not floor_for(pods=[], expect_pods=False).broken


# ---------------------------------------------------------------------------
# What the floor MUST call broken: things the API server has already decided.
# ---------------------------------------------------------------------------


def test_crashloopbackoff_is_a_fact() -> None:
    floor = floor_for(pods=[_pod(ready=False, waiting=("CrashLoopBackOff",))])
    assert floor.broken and any("CrashLoopBackOff" in r for r in floor.reasons), floor.reasons


def test_an_image_that_will_not_pull_is_a_fact() -> None:
    for reason in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
        floor = floor_for(pods=[_pod(ready=False, waiting=(reason,))])
        assert floor.broken, f"{reason} was not treated as a fact"


def test_an_oomkill_is_a_fact() -> None:
    assert floor_for(pods=[_pod(terminated=("OOMKilled",))]).broken


def test_a_completed_container_is_not_a_fact() -> None:
    """A Job's container exiting cleanly is not a failure, however it reads."""
    assert not floor_for(pods=[_pod(terminated=("Completed",))]).broken


def test_phase_failed_is_a_fact_and_pending_is_not() -> None:
    assert floor_for(pods=[_pod(phase="Failed")]).broken
    assert not floor_for(pods=[_pod(phase="Pending", ready=False)]).broken, (
        "a Pending pod was called broken. During a rollout a pod waiting for a node is Pending "
        "and fine; only the API server's `Failed` is a fact."
    )


def test_a_vanished_workload_is_a_fact() -> None:
    floor = floor_for(pods=[], expect_pods=True)
    assert floor.broken and any("no pod matched" in r for r in floor.reasons), floor.reasons


def test_broken_always_comes_with_reasons() -> None:
    """A floor that says broken and cannot say why cannot be reported or argued with."""
    for pods in (
        [_pod(phase="Failed")],
        [_pod(waiting=("CrashLoopBackOff",))],
        [_pod(terminated=("OOMKilled",))],
    ):
        floor = floor_for(pods=pods)
        assert floor.broken and floor.reasons, f"broken with no reasons for {pods}"


# ---------------------------------------------------------------------------
# The decision table, where the floor and the verdict actually combine.
# ---------------------------------------------------------------------------


def _commands() -> str:
    """The buildspec with comment lines removed, so the checks read the shell and not the prose."""
    return "\n".join(
        line
        for line in BUILDSPEC.read_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def test_the_agent_is_asked_on_every_upgrade_and_not_only_on_a_failure() -> None:
    """It is the judge, so it is asked when things look fine too.

    Under the old shape the agent was invoked inside the failure branch, so a broken component that
    the deterministic check happened to pass would never have been looked at. The checkpoint loop
    runs on every successful mutation, before anything is decided.

    NOTE WHAT IS NO LONGER ASSERTED HERE: that the bundle is sealed before the agent is asked.
    That mattered when the agent judged the bundle. It now reads the CLUSTER, so the seal's only
    job is to license the rollback -- which is what the next assertion checks.
    """
    text = _commands()
    loop = text.index("for OFFSET in ${HEALTH_CHECKPOINTS}")
    ask = text.index('--function-name "${TRIAGE_FUNCTION}"')
    # ANCHORED ON A SHELL LINE, not on the `# 4. ACT.` heading: `_commands()` strips comments, so a
    # heading is not in the text these indices are measured in.
    act = text.index('if [ "${DECISION}" = "healthy" ]')
    assert loop < act and ask < act, (
        "the checkpoint loop or the ask happens after the decision is acted on, so the agent's "
        "verdict cannot be the verdict."
    )
    # THE SEAL PRECEDES THE ROLLBACK, which is the ordering `plan_rollback` enforces by refusing an
    # unsealed bundle. A rollback destroys the evidence, so the evidence goes to disk first.
    assert text.index("--phase after --require-complete") < text.index("--kind helm-rollback"), (
        "the rollback command is requested before the bundle is sealed, so `plan_rollback` would "
        "refuse it and the component would be reported REFERRED when it could have been returned."
    )


def test_a_healthy_verdict_with_a_quiet_floor_is_the_only_route_to_upgraded() -> None:
    text = _commands()
    # THE LOOP SETS `DECISION=healthy` ONLY on an explicit HEALTHY verdict, and the floor can still
    # veto it below. Two gates, and both have to be present.
    assert "HEALTHY)   DECISION=healthy" in text, (
        "the checkpoint loop does not route an explicit HEALTHY verdict to `healthy`. Anything "
        "looser lets an empty verdict -- the unavailable case -- report a component as working."
    )
    assert '[ "${DECISION}" = "healthy" ] && [ "${FLOOR_BROKEN}" != "false" ]' in text, (
        "the floor cannot veto a HEALTHY verdict, so an agent that misread a stated "
        "CrashLoopBackOff would close the lane on a broken component."
    )
    assert text.count("STATUS=UPGRADED") == 1, (
        "there is more than one route to UPGRADED. A second one is a way for a component to be "
        "reported as working without anything having judged it."
    )


def test_the_agent_contradicting_a_stated_fact_goes_to_a_person() -> None:
    """Neither side wins silently.

    If the agent says HEALTHY over a stated CrashLoopBackOff, one of them is wrong about something
    not open to interpretation -- and a person settles that far more cheaply than either a silent
    pass or a silent rollback.
    """
    text = _commands()
    assert '[ "${DECISION}" = "healthy" ] && [ "${FLOOR_BROKEN}" != "false" ]' in text, (
        "there is no branch for the agent disagreeing with a stated fact"
    )
    assert "neither side wins silently" in BUILDSPEC.read_text(), (
        "the disagreement branch does not explain itself in the observed text, so the report would "
        "say REFERRED without saying that the two accounts differ."
    )


def test_an_unavailable_agent_with_a_quiet_floor_refers_rather_than_passing() -> None:
    """NOTHING JUDGED THIS. Not a pass, and not a rollback of a probably-healthy component.

    This is the case the whole rewrite turns on. With the agent as judge, a Bedrock outage means
    there is no verdict -- and the two obvious answers are both dishonest: passing reports a
    component as working when nothing checked it, and rolling back undoes a version move that was
    probably fine. Referring says what actually happened.
    """
    text = _commands()
    # THE WINDOW EXPIRING WITHOUT A HEALTHY VERDICT IS THE AUTOMATIC-ROLLBACK CONDITION, and that is
    # what an unavailable agent over a quiet floor reaches: the loop keeps asking, nothing answers,
    # the deadline passes and the component is returned. It is NOT silently passed.
    assert 'if [ -z "${DECISION}" ]; then' in text, (
        "there is no branch for the window expiring with no decision, so an upgrade nothing "
        "judged falls through to whatever the default happens to be."
    )
    assert "window expired without a HEALTHY verdict" in BUILDSPEC.read_text(), (
        "the expired-window case does not say so in what the report will carry. A row whose reason "
        "is not 'nothing established this as healthy' sends somebody looking for a failure."
    )
    # AND EXPIRY MEANS ROLLBACK, not a pass. The requirement in one line: still
    # unhealthy after the deadline, so return it to the version it was on, automatically.
    expiry = text.index('if [ -z "${DECISION}" ]; then')
    branch = text[expiry : expiry + 400]
    assert "DECISION=rollback" in branch, (
        f"the expired-window branch does not set a rollback: {branch[:200]!r}. An upgrade nothing "
        "established as healthy must be returned, not reported as working."
    )


def test_an_unavailable_agent_with_a_broken_floor_still_rolls_back() -> None:
    """The floor is what answers when the agent cannot, which is why it exists at all."""
    text = _commands()
    # THE FLOOR IS WHAT ANSWERS WHEN THE AGENT CANNOT, which is why it exists at all. In the loop's
    # fallthrough case a stated fact breaks out of the window immediately rather than waiting for a
    # model that may never come back.
    assert 'if [ "${FLOOR_BROKEN}" = "true" ]; then DECISION=rollback; break; fi' in text, (
        "an unanswered checkpoint over a BROKEN floor does not roll back. The floor is the only "
        "thing that can decide when the agent is unreachable, and a stated CrashLoopBackOff should "
        "not have to wait out the whole window."
    )


def test_an_unsealed_bundle_never_rolls_back() -> None:
    """A rollback destroys the evidence, so with no evidence captured it does not run.

    The library refuses it too -- `plan_rollback` requires a sealed manifest -- but the buildspec
    must not even try, because the outcome should be a referral naming the capture failure rather
    than a refusal deep in a command it asked for.
    """
    text = _commands()
    assert 'if [ "${SEALED}" != "yes" ]; then' in text, (
        "the decision does not branch on whether the bundle sealed, so a capture failure would "
        "fall through to a rollback that destroys the only evidence of what happened."
    )
    assert "destroyed the only evidence" in BUILDSPEC.read_text()
