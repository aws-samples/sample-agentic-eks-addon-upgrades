"""What one lane produced, and what the agent is allowed to say about it.

    in    one component's upgrade, validated
    out   one `LaneOutcome`, and one `UpgradeRunReport` per run

**A STATUS IS A CLAIM, SO THE ENUM IS SHORT AND EVERY MEMBER IS EARNED.** The temptation in a
stage like this is a status per shape of failure -- `HELM_FAILED`, `TIMED_OUT`,
`VALIDATION_FAILED` -- and the cost of that is a reader who has to learn the taxonomy before they
can tell "the component is running the new version" from "somebody must look at this". So there
are five, and the line between them is what a HUMAN has to do next.

**`UpgradeTriage` IS THE AGENT'S ONLY WAY TO INFLUENCE A MUTATION, AND IT IS DATA.** The agent
reads and reasons; what comes back is this document, validated by pydantic before anything acts
on it, and the only actions it can name are the two this package already knows how to perform.
A triage asking for anything else is refused at construction rather than interpreted -- which is
the difference between an agent that decides and an agent whose decision is checked.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from ..models import ClusterReference, ContractModel


class UpgradeStatus(StrEnum):
    """What happened to one component, in the vocabulary of what a person must do next."""

    # The new version is installed and validated: pods ready, no error lines, and for a managed
    # add-on an ACTIVE add-on with no health issues.
    UPGRADED = "UPGRADED"

    # The upgrade was applied, validation refused it, the diagnosis bundle was captured, and the
    # component was returned to the version it was on. NOT a success and not a disaster: the
    # cluster is where it started and there is evidence to read.
    ROLLED_BACK = "ROLLED_BACK"

    # Nothing can be concluded without a person. Reached when the rollback itself failed, when
    # the add-on update left the add-on in a failed state, or when the agent's triage says REFER.
    # A row with this status MUST name what was tried and what was observed -- see
    # `LaneOutcome.attempted` and `observed`.
    REFERRED = "REFERRED"

    # The fence or the pre-flight said no, and NOTHING WAS CHANGED. Its own status rather than a
    # kind of REFERRED, because "we did not touch the cluster" is the single most important thing
    # a reader can know, and burying it inside a general failure loses it.
    REFUSED_BEFORE_MUTATION = "REFUSED_BEFORE_MUTATION"

    # The just-in-time identity could not be opened, so the lane never began. Distinct from the
    # above because the cause is in AWS rather than in the component -- most often that the
    # component has no reviewed fence, which IAM refuses by condition.
    LANE_NOT_OPENED = "LANE_NOT_OPENED"


class TriageVerdict(StrEnum):
    """What the agent concluded after reading the cluster itself.

    FOUR VALUES, AND `SETTLING` IS WHAT MAKES THE DEADLINE WORK. A rollout takes time, so an agent
    asked 45 seconds in has to be able to say "not yet" -- without it the only answers are HEALTHY
    on a pod that is not Ready or UNHEALTHY on one that was about to come up, and both are wrong.
    It is withdrawn on the final call, which is the deadline's only enforcement.

    `FIXABLE` IS GONE. It meant "broken and a rollback fixes it", which conflated the observation
    with the remedy; `UNHEALTHY` plus `ROLLBACK` says both separately, and the remedy was always
    the code's to choose.
    """

    HEALTHY = "HEALTHY"
    SETTLING = "SETTLING"
    UNHEALTHY = "UNHEALTHY"
    REFER = "REFER"


class TriageAction(StrEnum):
    """The complete set of things the agent may ask for. Not a suggestion field.

    NO `PATCH`, NO `SCALE`, NO `DELETE_POD`, and the absence is the design. Each of those would
    be a mutation whose shape the agent chose, which is exactly the boundary this project draws:
    a model may say what it observed and which of the code's OWN actions fits, and nothing more.
    Adding a member here means adding deterministic code that performs it and a fence that bounds
    it, which is the conversation such a change should have to have.
    """

    NONE = "NONE"
    # Ask again at the next checkpoint. The only action that does not end the lane.
    WAIT = "WAIT"
    ROLLBACK = "ROLLBACK"
    REFER = "REFER"


class UpgradeTriage(ContractModel):
    """The agent's reading of one component after its upgrade. Validated before it is acted on."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["UpgradeTriage"] = "UpgradeTriage"

    component: str = Field(min_length=1)
    verdict: TriageVerdict
    proposed_action: TriageAction

    # What the agent actually saw, each line citing a file in the diagnosis bundle or a command.
    # Non-empty for anything but HEALTHY: a verdict with no observations is an opinion, and the
    # human's requirement is that a refusal names what was observed.
    observations: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    # Which checkpoint produced this, and how far into the window. Recorded because a HEALTHY at
    # 45s and a HEALTHY at 600s are different findings about the same component.
    checkpoint: int = Field(default=1, ge=1)
    elapsed_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def every_verdict_must_say_what_it_read(self) -> UpgradeTriage:
        """EVERY verdict needs observations now, HEALTHY included, and that is the B change.

        Under the old shape the agent judged a bundle somebody else captured, so a silent HEALTHY
        was merely unhelpful. It now READS THE CLUSTER, so a verdict with no observations is a
        verdict that read nothing -- and there is no honest reading of a cluster nobody looked at.
        The system prompt says so; this is where it is true.
        """
        if not self.observations:
            raise ValueError(
                f"triage for {self.component!r} is {self.verdict} with no observations. The agent "
                "reads the cluster itself, so an answer that cites nothing it read is an answer "
                "that read nothing."
            )
        expected = _ACTION_FOR_VERDICT[self.verdict]
        if self.proposed_action is not expected:
            raise ValueError(
                f"triage for {self.component!r} is {self.verdict} but asks for "
                f"{self.proposed_action}, not {expected}. The two fields are not independent: the "
                "verdict is what was observed and the action is the one branch that follows from "
                "it, so a document saying both cannot be acted on."
            )
        return self


# THE ONE BRANCH EACH VERDICT LICENSES. A table rather than four `if`s, so the pairing is a
# declaration a reader can check at a glance -- and so a fifth verdict cannot be added without
# stating which action it implies.
_ACTION_FOR_VERDICT: dict[TriageVerdict, TriageAction] = {
    TriageVerdict.HEALTHY: TriageAction.NONE,
    TriageVerdict.SETTLING: TriageAction.WAIT,
    TriageVerdict.UNHEALTHY: TriageAction.ROLLBACK,
    TriageVerdict.REFER: TriageAction.REFER,
}


class LaneOutcome(ContractModel):
    """One component's row: what was attempted, what was observed, and where the evidence is."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["UpgradeLaneOutcome"] = "UpgradeLaneOutcome"

    cluster: ClusterReference
    run_id: str = Field(min_length=1)
    step_order: int = Field(ge=1)
    component: str = Field(min_length=1)
    status: UpgradeStatus

    # Copied, not referenced, for `StepDecision.THE FINGERPRINT IS COPIED`'s reason: a reader six
    # weeks later must be able to see which move this row is about without resolving a plan.
    installed: str = ""
    applied: str = ""
    # Where the component ended up, which is NOT `applied` after a rollback.
    resulting: str = ""
    managed_addon: bool = False

    # WHAT WAS TRIED AND WHAT WAS SEEN. Required for a REFERRED row, per the human's requirement
    # that a refusal names both. Free text, one entry per attempt or observation, because the
    # shape of a failure is not knowable in advance and a typed field would flatten it.
    attempted: tuple[str, ...] = ()
    observed: tuple[str, ...] = ()

    # S3 URIs of the diagnosis bundles. `before` is captured on every run; `after` only when
    # validation refused. Cited as URIs and never inlined: a bundle is megabytes of logs and a
    # state payload has a 256 KiB ceiling.
    diagnosis_before_uri: str = ""
    diagnosis_after_uri: str = ""

    # EVERY CHECKPOINT, not only the decisive one. "HEALTHY at 45s" and "SETTLING four times then
    # HEALTHY at 420s" are different stories about the same component, and the report tells both.
    checkpoints: tuple[UpgradeTriage, ...] = ()
    # The one that ended the lane. A convenience over `checkpoints[-1]`, named so a reader is not
    # left inferring which answer counted.
    triage: UpgradeTriage | None = None
    # What the rollback did, when one fired. Empty when none did.
    rollback_command: str = ""
    rollback_succeeded: bool | None = None
    # The verdict after the rollback. A rolled-back state that is ALSO unhealthy is the worst
    # outcome this stage reaches, and this is where that is visible.
    revalidation: UpgradeTriage | None = None
    # Set when the access entry could not be deleted. A run that leaves this populated has a
    # possible STANDING cluster-write grant, which is the loudest thing this document can say.
    cleanup_error: str = ""
    finished_at: datetime

    @model_validator(mode="after")
    def a_referral_names_what_it_tried_and_what_it_saw(self) -> LaneOutcome:
        if self.status is UpgradeStatus.REFERRED and not (self.attempted and self.observed):
            raise ValueError(
                f"{self.component!r} is REFERRED with attempted={self.attempted!r} and "
                f"observed={self.observed!r}. What the stage cannot fix it refers to a human, "
                "and a referral must name what it tried AND what it observed -- not merely "
                "that it failed."
            )
        # ROLLED_BACK AND ONLY ROLLED_BACK REQUIRES THE `after` BUNDLE, and the asymmetry with
        # REFERRED is deliberate rather than an omission. A rollback is something THIS STAGE
        # chose to do, and the human's requirement is that the diagnosis is captured before it --
        # so a ROLLED_BACK row with no bundle means the capture was skipped, which is a defect in
        # the code and is refused here where it is legible.
        #
        # REFERRED is different: it is reachable from a build that died before it could capture
        # anything (a CodeBuild agent failure, a `.sync` timeout), and refusing to CONSTRUCT that
        # row would lose the whole report -- the one artifact the human is waiting for. So
        # REFERRED carries `attempted` and `observed` instead, which the check above requires,
        # and one of those lines is what says the bundle is missing and why.
        if self.status is UpgradeStatus.ROLLED_BACK and not self.diagnosis_after_uri:
            raise ValueError(
                f"{self.component!r} is ROLLED_BACK with no diagnosis bundle. The diagnosis is "
                "captured BEFORE the rollback for exactly this reason; a rollback that left "
                "nothing to read is a failure of this stage even though the component came back."
            )
        return self

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()


class UpgradeRunReport(ContractModel):
    """Every lane of one run, and the one sentence a person reads first."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["UpgradeRunReport"] = "UpgradeRunReport"

    cluster: ClusterReference
    run_id: str = Field(min_length=1)
    target_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    # The authorisation this run acted on. Named rather than assumed, so the report cannot be
    # read as evidence of a change nobody authorised.
    automation_execution_id: str = ""
    approver_arns: tuple[str, ...] = ()

    outcomes: tuple[LaneOutcome, ...] = ()
    finished_at: datetime

    @property
    def referred(self) -> tuple[str, ...]:
        """Components a person must look at. What `Anything referred?` branches on."""
        return tuple(
            sorted(
                o.component
                for o in self.outcomes
                if o.status
                in (
                    UpgradeStatus.REFERRED,
                    UpgradeStatus.REFUSED_BEFORE_MUTATION,
                    UpgradeStatus.LANE_NOT_OPENED,
                )
                or o.cleanup_error
            )
        )

    @property
    def upgraded(self) -> tuple[str, ...]:
        return tuple(
            sorted(o.component for o in self.outcomes if o.status is UpgradeStatus.UPGRADED)
        )

    @property
    def rolled_back(self) -> tuple[str, ...]:
        return tuple(
            sorted(o.component for o in self.outcomes if o.status is UpgradeStatus.ROLLED_BACK)
        )

    def summary(self) -> str:
        """One line for the mail subject and the top of the report."""
        return (
            f"{len(self.upgraded)} upgraded, {len(self.rolled_back)} rolled back, "
            f"{len(self.referred)} referred on {self.cluster.name}"
        )

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()
