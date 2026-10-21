"""What a person decided about one cluster's upgrade steps, recorded so the executor can read it.

    in    one step from a plan, and one click
    out   one `StepDecision` object, and one `RunDecisions` summary when they finish

**AN UPGRADE APPROVAL IS PER CLUSTER, AND THAT IS THE WHOLE DIFFERENCE FROM A FACT.** A
compatibility fact is fleet-wide on purpose: "metrics-server 0.9.x supports Kubernetes 1.36"
is true of every cluster, which is why `facts` is keyed on component and version series and
nothing else, and why one approval there answers the question for the whole fleet. An upgrade
approval is the opposite claim. It says *"change THIS cluster"*, and someone who accepted a
readiness-contract change on a demo cluster has said nothing whatsoever about production.

So the cluster is a PATH SEGMENT in the key and a required field on the document, not a
detail inside it. A reader listing what has been approved cannot accidentally answer for a
cluster they did not ask about, and there is no shape of query that returns a decision
without saying which cluster it belongs to. Keying this the way facts are keyed -- component
plus series -- would have made one team's demo approval authorise everybody's production, and
that is not a check to add later, it is a key to get right once.

**IT IS NOT AN `ExecutionGrant`, AND THE DISTINCTION IS DELIBERATE.** `execute/grant.py`
holds the contract the executor acts on: it carries step fingerprints, an expiry, an
acknowledgement of irreversibility, and the authority under which it was signed. That module
is not merged, and this stage must not invent a weaker copy of it under the same meaning --
a document that looked like a grant and skipped its checks is worse than no grant at all. So
this records THE DECISION and nothing about permission to act on it. When the executor lands
it reads these, applies its own gate, and mints its own grant; until then these are an audit
trail and a resume record, which is what the approval stage is actually able to promise.

**THE FINGERPRINT IS COPIED, NOT REFERENCED.** Every field describing the step is written
into the decision rather than looked up from the plan later. A plan digest proves the plan is
the one named; it does not prove which of eight steps a person read before they clicked. If
the plan is re-sequenced and step 3 becomes a different move, a decision that recorded only
"step 3" would silently authorise the new one. The same reasoning `execute/grant.py` gives
for `StepFingerprint`, applied at the point the decision is made rather than the point it is
used.

**THE AUTHORITY IS RECORDED AS THE WEAKER OF THE TWO IT COULD BE.** A click carries no
identity. What the nonce proves is that whoever held it opened the mailbox it was sent to, so
`approver` is the address the pipeline mailed and `approver_authority` says
`EMAIL_LINK_TOKEN` -- never `AWS_SIGV4`, which would claim AWS resolved a principal before
any handler ran. Describing your own guarantees as stronger than they are is a defect this
project has already paid for, and it is not repeated here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .models import ClusterReference, ContractModel
from .order.plan import StepKind


class Decision(StrEnum):
    """What a person said about one step.

    Two values and no third. An undecided step is ABSENT from the record rather than carrying
    a state, for `PausedRun`'s reason: a rejection is exactly as decided as an approval, and
    "nobody looked" is not a decision at all. `RunDecisions.undecided` names those by order,
    so a run that finished with steps untouched says so rather than implying a verdict.
    """

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Authority(StrEnum):
    """How the decider's identity was established. Not interchangeable with an AWS principal.

    TWO VALUES, AND THE SECOND ONE ARRIVED WITH A PATH THAT EARNS IT. This docstring used to
    say "only one value today, and it is the weaker one", with `AWS_SIGV4` deliberately absent
    rather than unused so no future writer could record the stronger authority for a click that
    never carried one -- and it added that a path which really does resolve a principal could
    add the member when it existed. The release gate is that path, so this is that member.

    `AWS_SIGV4` IS STILL ABSENT, because adding one honest member does not license the one that
    was withheld. Nothing here resolves a SigV4 principal for an approval: the presigned forms
    on the page are signed by the STAGE's role, not by the person clicking, so the signature
    attests to the pipeline and says nothing about who pressed the button. That is exactly the
    overclaim the omission guards, and it is unchanged.
    """

    EMAIL_LINK_TOKEN = "EMAIL_LINK_TOKEN"

    # THE ONLY MEMBER BACKED BY AWS HAVING AUTHENTICATED SOMEBODY. `aws:approve` refuses a
    # `SendAutomationSignal` from a principal outside its `Approvers` list, so a recorded
    # approval means AWS resolved a caller, matched it against a list an operator committed to
    # `terraform.tfvars`, and only then let the automation continue.
    #
    # SSM DOES THE MATCHING, WHICH IS WHY THIS VALUE CAN BE TRUSTED AT ALL. No code in this
    # project compares a principal against that list -- there is no string test here to get
    # subtly wrong, and `stages/approve.py` is the standing reminder of how easy that is to do
    # (its `endswith("/" + name)` check matches the SESSION segment of an assumed-role ARN).
    #
    # WHAT IT STILL DOES NOT MEAN: that the approver is permitted to change the cluster. It
    # records who authorised the release, not what their access entry allows. Reading
    # `Release.approver_arns` back and checking it against `eks list-associated-access-policies`
    # is the executor's job and is NOT done anywhere in this repository -- see the README's
    # "Release gate" section, which says so rather than leaving it to be assumed.
    AWS_IAM_SSM_APPROVE = "AWS_IAM_SSM_APPROVE"


class StepDecision(ContractModel):
    """One person's decision about one step of one cluster's upgrade.

    Written as each click lands rather than all at once when they finish. Two reasons, and the
    second is the one that matters: a run whose approval window expires still leaves every
    decision already made on disk, and the executor can see partial progress instead of
    nothing. Waiting for a Done that may never come would discard real decisions.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["UpgradeStepDecision"] = "UpgradeStepDecision"

    # THE SCOPE. Required, and a path segment in the key as well -- see the module docstring.
    cluster: ClusterReference
    target_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")

    # The plan the step came from. Its digest already covers the snapshot, the ledger, the
    # assessment, the fact store and the ordering ruleset, so naming it pins all of them.
    plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    # The digest of the page this person was actually shown. Recorded so the trail says WHAT
    # they read and not merely that they decided.
    presented_page_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    run_id: str = Field(min_length=1)

    # The step, copied rather than referenced. See the module docstring.
    step_order: int = Field(ge=1)
    step_kind: str = Field(min_length=1)
    component: str = Field(min_length=1)
    installed: str = ""
    recommended: str = ""
    at_kubernetes: str = ""

    decision: Decision
    approver: str = Field(min_length=1)
    approver_authority: Authority = Authority.EMAIL_LINK_TOKEN
    decided_at: datetime

    # Which brief this person had in front of them, or None if the step had none. Recorded
    # because "approved having read a brief" and "approved with no brief available" are
    # materially different decisions, and a reader six weeks later cannot tell them apart from
    # the plan alone. A brief is served again for up to a week, so the instant is the only
    # thing that identifies which reading they saw.
    brief_observed_at: str | None = None

    @model_validator(mode="after")
    def decided_at_carries_an_offset(self) -> StepDecision:
        """A naive instant is refused here rather than normalised downstream.

        `storage/facts.py` has to normalise a naive `refreshed_at` because the contract that
        holds it is digest-frozen and cannot be tightened; the cost was a `TypeError` inside a
        comparison, failing a whole execution the first time a writer omitted the offset.
        Nothing here is frozen and nothing has been stored yet, so the stronger fix is
        available: refuse at the boundary, where the error is legible.
        """
        moment = self.decided_at
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError(
                "decided_at must carry a UTC offset. A naive instant cannot be compared "
                "against an aware one, and every comparison this document is used in -- "
                "expiry, ordering, which decision came last -- is exactly that."
            )
        return self

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()


class RunDecisions(ContractModel):
    """Every decision one paused run collected, written when the person finishes.

    A SUMMARY AND NOT THE RECORD. Each `StepDecision` is already on disk under its own key by
    the time this is written, so this document adds one thing they cannot: what was left
    UNDECIDED. A reader of the per-step objects alone sees six approvals and cannot tell
    whether the seventh step was rejected or never looked at.

    `undecided` IS WHY THIS EXISTS. Same shape as the fact stage: a step nobody decided is
    offered again on the next run rather than being taken as a no. The executor must be able
    to tell "they said no" from "they never got to it", because the first is an answer and the
    second is a queue.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["UpgradeRunDecisions"] = "UpgradeRunDecisions"

    cluster: ClusterReference
    target_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    presented_page_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    run_id: str = Field(min_length=1)

    decisions: tuple[StepDecision, ...] = ()
    # Step orders that were offered and never decided. Named rather than derived from the
    # difference between two lists, because the offer is what this run showed and a later
    # reader has no way to reconstruct it.
    offered: tuple[int, ...] = ()
    finished_at: datetime

    @model_validator(mode="after")
    def decisions_belong_to_this_run(self) -> RunDecisions:
        """Every decision names this run, this cluster and this plan, and each step once.

        Refused at construction rather than checked by a reader: a summary that mixed two
        runs' decisions would be indistinguishable from one run that decided twice, and the
        executor would have no way to tell which.
        """
        for decision in self.decisions:
            if decision.run_id != self.run_id:
                raise ValueError(
                    f"decision for step {decision.step_order} names run "
                    f"{decision.run_id!r}, not {self.run_id!r}"
                )
            if decision.plan_digest != self.plan_digest:
                raise ValueError(
                    f"decision for step {decision.step_order} names a different plan. A "
                    "summary spanning two plans cannot say what was approved."
                )
            if decision.cluster.name != self.cluster.name:
                raise ValueError(
                    f"decision for step {decision.step_order} names cluster "
                    f"{decision.cluster.name!r}, not {self.cluster.name!r}. An upgrade "
                    "approval is per cluster; mixing them is the one error this document "
                    "must not be able to record."
                )
        orders = [d.step_order for d in self.decisions]
        duplicates = sorted({o for o in orders if orders.count(o) > 1})
        if duplicates:
            raise ValueError(
                f"steps {duplicates} were decided more than once. A single-use nonce per "
                "action makes that impossible in the deployed path, so a document carrying "
                "it is a bug rather than a history."
            )
        overlap = sorted(set(self.offered) & set(orders))
        if overlap:
            raise ValueError(
                f"steps {overlap} appear as both decided and undecided. `offered` is the "
                "steps still waiting, not everything that was shown."
            )
        return self

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    @property
    def approved(self) -> tuple[int, ...]:
        """Step orders this person approved, ascending. What the executor is looking for."""
        return tuple(
            sorted(d.step_order for d in self.decisions if d.decision is Decision.APPROVED)
        )

    @property
    def rejected(self) -> tuple[int, ...]:
        return tuple(
            sorted(d.step_order for d in self.decisions if d.decision is Decision.REJECTED)
        )

    def summary(self) -> str:
        """One line for the state payload and the top of a log."""
        return (
            f"{len(self.approved)} approved, {len(self.rejected)} rejected, "
            f"{len(self.offered)} left undecided on {self.cluster.name} "
            f"-> Kubernetes {self.target_kubernetes_version}"
        )


# ---------------------------------------------------------------------------
# Reading `$.release`. The payload the state machine builds is NOT this model's shape, and the
# gap is reconciled here rather than in ASL -- see `Release.from_gate_payload`.
# ---------------------------------------------------------------------------

# The word `aws:approve` uses for an approval. `Waiting` is the other value seen in a real
# `ApproverDecisions`: every LISTED approver appears in that output, answered or not, so the
# decision text is what separates a principal who authorised this release from one who merely
# could have. Compared as a literal because it is SSM's vocabulary, not this project's --
# `Decision` above is about a person's answer on the report and is a different enum on purpose.
_APPROVED = "Approve"

# Exactly the keys `Release approved` sets, and the reason this is written down: the method below
# assembles the model field by field, so `ContractModel`'s ban on extra fields never sees the
# payload. A key renamed in `workflow.tf` would otherwise arrive as an ignored extra while the
# field it was meant to fill took a default -- which is the same class of silent-green defect as
# the placeholder this gate already shipped once.
_GATE_PAYLOAD_KEYS = frozenset(
    {
        "run_id",
        "cluster",
        "approved_steps",
        "automation_execution_id",
        "approver_decisions",
        "approval_status",
    }
)


def _sole_output(value: object, *, field: str) -> str:
    """One string out of the one-element array an SSM step output always is.

    `ApprovalStatus` arrives as `["Approved"]` -- one word in an array -- because every value in
    a step's `Outputs` map is array-valued. `Release approved` unwraps the JSONPath filter's
    match and not this second level, so it is unwrapped here. A bare string is accepted too, so
    that an ASL change which flattens it fully does not break this reader for the better.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], str):
        return value[0]
    raise ValueError(
        f"{field} is {value!r}, which is neither a string nor a one-element list holding one. "
        "An SSM step output is array-valued; this reader unwraps exactly one level."
    )


def _approver_decisions(value: object) -> tuple[Mapping[str, object], ...]:
    """`ApproverDecisions` as decoded maps, one per LISTED approver.

    Each element is a JSON document as text, because SSM's step outputs are strings. Decoding
    them is the reason this adapter exists at all: ASL can copy a string but cannot look inside
    it, so filtering the list down to the principals who actually approved is not expressible in
    the `Pass` state without a `Map` and a function to invoke -- and this gate adds no Lambda.
    """
    if isinstance(value, str):
        raw: list[object] = [value]
    elif isinstance(value, list):
        raw = list(value)
    else:
        raise ValueError(
            f"approver_decisions is {value!r}, not a list of JSON strings. `aws:approve` emits "
            "one document per listed approver."
        )

    if any(isinstance(item, list) for item in raw):
        raise ValueError(
            "approver_decisions is nested one level too deep, so no ARN can be read out of it. "
            "A JSONPath filter returns one entry per match and each entry is itself SSM's "
            "array-valued output, which yields [[\"{...}\"]]. `Release approved` in "
            "infra/terraform/modules/platform/workflow.tf must wrap the read in "
            "States.ArrayGetItem(..., 0); "
            "the run of 2026-09-20 recorded exactly this shape before it did."
        )

    decoded: list[Mapping[str, object]] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(
                f"approver_decisions holds {item!r}, which is not a string. Each entry is a JSON "
                "document as text."
            )
        try:
            one = json.loads(item)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"approver_decisions holds {item!r}, which is not JSON: {exc}"
            ) from exc
        if not isinstance(one, dict):
            raise ValueError(
                f"approver_decisions holds {item!r}, which decodes to {type(one).__name__} "
                "rather than an object naming an Approver and a Decision."
            )
        decoded.append(one)
    return tuple(decoded)


def _approval_instant(raw: object) -> datetime:
    """`ApprovalDecisionTime` as an aware instant.

    **NO NORMALISATION, AND THAT IS A MEASURED DECISION RATHER THAN AN OVERSIGHT.** What SSM
    really sends is `2026-09-20T04:24:42.557527708Z` -- nine fractional digits, which is finer
    than `datetime` can hold, and a bare `Z` rather than a numeric offset. Both look like they
    need rewriting before `fromisoformat` will take them, and an earlier draft of this function
    did rewrite them. It was dead code: measured on CPython 3.12.13 and 3.14.6, `fromisoformat`
    accepts that exact string and truncates the fraction to microseconds itself, and
    `pyproject.toml` floors this project at 3.12. (Python 3.9 does refuse it, which is where the
    idea that it needed handling came from -- no interpreter in this project's range is that old.)
    Hand-rolled ISO parsing that no supported version needs is a liability, not a safety net.

    A timestamp with NO offset at all would parse to a naive instant here and be refused by
    `approved_at_carries_an_offset` with a message that names the field, which is the right place
    for it.
    """
    if not isinstance(raw, str):
        raise ValueError(
            f"ApprovalDecisionTime is {raw!r}, not a string. An approving decision must carry "
            "the instant SSM recorded it, because that is what a reader compares against the "
            "change it authorised."
        )
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"ApprovalDecisionTime {raw!r} is not an ISO-8601 instant: {exc}"
        ) from exc


class ReleaseStep(ContractModel):
    """One approved move, described well enough to act on without the plan beside it.

    **`[1, 2, 4, 7]` WAS THE FIRST SHAPE AND IT WAS NOT USABLE.** The first real release record
    listed step ORDERS and nothing else, so a reader -- the executor, or a person asking what was
    authorised last Tuesday -- had to fetch the Stage 5 plan of that run and join on the number.
    The plan is immutable and addressable, so that join is possible; it is also a second read, a
    second failure mode, and a silent wrong answer if anybody ever joins against a later plan.

    **`order` IS KEPT RATHER THAN REPLACED, AND THAT IS LOAD-BEARING.** The per-step decisions
    live at `upgrade-decisions/cluster=<cluster>/step=<order>.json`, so the order is the only
    thing linking this authorisation to the intent that preceded it. Naming the component instead
    would read better and break that link -- two steps can move the same component, as one
    test run did with cert-manager twice.

    **EVERY FIELD IS COPIED FROM THE PLAN, NEVER RE-DERIVED.** `StepDecision`'s docstring gives
    the argument in full and it holds here: a record that says "step 3" is a record that means
    whatever step 3 means today. `summary` is the very string the authorisation mail showed the
    approver, so what was read and what was recorded cannot disagree.

    **`install_method` IS THE ONE FIELD NOT FROM THE PLAN, AND THE EXECUTOR BRANCHES ON IT.**
    A Helm release is upgraded with Helm and an EKS managed add-on with `UpdateAddon`: two
    different APIs, two different failure modes, two different rollbacks. The plan does not carry
    the distinction -- it is `ComponentReport.install_method` on the Stage 2 inventory -- so
    `stages/release_record.py` reads that document and joins on `identity or workload_ref`, which
    is the very expression `order/interleave.py` used to name the component. An exact join, not a
    guess from a name.

    `kind` IS THE KIND OF MOVE AND NOT THE DELIVERY MECHANISM, and the two being separate fields
    is deliberate: `COMPONENT_MOVE` vs `COMPONENT_UPGRADE_RECOMMENDED` says whether skipping the
    step strands the component, which an executor also needs and which has nothing to do with how
    the component is installed.
    """

    order: int = Field(ge=1)
    # The plan's own classification: a forced move, an advisory upgrade, or the cluster hop
    # itself. An executor treats the three differently, and the plan is the only thing that
    # knows which is which.
    kind: StepKind
    # None for the cluster hop, which moves no component. Every other kind names one.
    component: str | None = None
    # SERIES AND CONCRETE VERSION BOTH, because they answer different questions. `to_series` is
    # what is COMPATIBLE (`1.19.x`); `to_version` is what to INSTALL (`1.19.6`). A record with
    # only the series would leave the executor choosing a patch nobody authorised.
    from_series: str | None = None
    to_series: str | None = None
    to_version: str | None = None
    # The cluster hop's own versions, for the one step kind that has no component.
    from_kubernetes: str | None = None
    to_kubernetes: str | None = None
    # HOW IT IS INSTALLED, WHICH IS WHICH API THE EXECUTOR CALLS: `HELM`, `EKS_MANAGED_ADDON` or
    # `MANIFEST`, copied verbatim from the Stage 2 inventory.
    #
    # A `str` AND NOT A `Literal`, for `approver_decisions`' reason: the value space is
    # `inventory/resolve.py`'s, not this module's, and a `Literal` here would REFUSE a release the
    # day that function learns a fourth method -- turning an inventory improvement into a broken
    # gate. `None` only where there is no component, which the validator below enforces.
    # THE CLUSTER MINOR THE MOVE IS PLACED AT, copied from `PlanStep.at_kubernetes`. Carried so the
    # lane planner can tell a move for TODAY'S minor from one the plan places after the control
    # plane has advanced, without opening the plan: a kube-proxy build
    # qualified for 1.36 must not be sent to EKS while the cluster is on 1.35, which happens
    # when nothing on the release says when
    # the step applied. Optional, so every release recorded before this field validates unchanged.
    at_kubernetes: str | None = Field(default=None, pattern=r"^\d+\.\d+$")
    install_method: str | None = None
    # WHAT THE LANE NEEDS TO ACT, COPIED ONTO THE RECORD AT APPROVAL TIME. These used to live in
    # a committed Terraform map (`upgrade_lane_catalogue`) that somebody maintained by hand per
    # component, so a component with an approved fact and no map entry was reported
    # LANE_NOT_OPENED for a reason nothing upstream could see coming. They come from the two
    # documents the release is already built from: the Stage 2 inventory (the Helm release name,
    # its namespace, the chart it installed, the add-on's pod labels) and the APPROVED FACT
    # (where that chart is published, a quote a person approved). The planner reads only this
    # record. NO CHART VERSION: which chart version ships `to_version` is asked of the
    # repository by the lane (`helm search repo`) at upgrade time, the way an add-on's
    # `-eksbuild.N` is asked of EKS. All optional, so every release recorded before validates
    # unchanged -- and a Helm step with no repository here is skipped with that reason.
    release_name: str | None = None
    namespace: str | None = None
    pod_selector: str | None = None
    chart_repository: str | None = None
    chart_name: str | None = None
    # The line the approver actually read in the mail, verbatim. Identical by construction: the
    # mail's `Changes` parameter is built from these same objects.
    summary: str = Field(min_length=1)

    @model_validator(mode="after")
    def a_component_step_says_how_it_is_installed(self) -> ReleaseStep:
        """A move with a component must name its install method; the cluster hop must not.

        ENFORCED ON THE MODEL RATHER THAN TRUSTED TO THE CALLER, because the caller that gets it
        wrong produces a record the executor cannot act on -- and it would look complete. A step
        whose component the inventory does not name is the interesting case: the join failed, so
        something is inconsistent between two documents of the same run, and an executor guessing
        Helm for a managed add-on would call the wrong API against a live cluster.
        """
        if self.component and not self.install_method:
            raise ValueError(
                f"step {self.order} moves {self.component} and does not say how it is "
                "installed. The executor chooses between Helm and UpdateAddon on this field, "
                "so an absent value is not a gap to fill in later."
            )
        if not self.component and self.install_method:
            raise ValueError(
                f"step {self.order} names no component and carries an install method "
                f"({self.install_method!r}). The cluster hop installs nothing."
            )
        return self


class Release(ContractModel):
    """One authorisation, by a principal AWS authenticated, of the steps a person approved.

    **THE FIRST DOCUMENT IN THIS PROJECT THAT NAMES A REAL PRINCIPAL, AND THAT IS ITS ONLY
    REASON TO EXIST.** Every approval before it -- a fact approval, an upgrade step decision --
    is answered by clicking a single-use presigned form, which proves that whoever held the
    capability opened the mailbox it was sent to. Not which human. `StepDecision` says so, in
    `approver_authority`, and it is right to. This document is the other half: the release gate
    puts the run in front of `aws:approve`, which refuses a signal from a principal outside its
    `Approvers` list, so `approver_arns` below is an identity AWS resolved rather than one this
    code inferred.

    **INTENT, THEN AUTHORISATION, AND THEY ARE DIFFERENT CLAIMS.** A `StepDecision` says
    somebody read step 3 and wanted it. A `Release` says somebody with a named ARN stood behind
    that want. Both are needed and neither substitutes for the other: an authorisation with no
    decisions behind it authorises nothing in particular, and decisions with no authorisation
    are a wish list.

    **IT IS STILL NOT AN `ExecutionGrant`.** `execute/grant.py` holds the contract the executor
    acts on -- step fingerprints, an expiry, an acknowledgement of irreversibility. That module
    is not merged, and this must not become a weaker copy of it wearing a stronger word.
    `RELEASE` HERE MEANS "THE GO-AHEAD WAS GIVEN", NOT "THE CHANGE MAY PROCEED": nothing in this
    document bounds what may be done, when it expires, or whether the approver's access entry
    permits it. The module docstring above makes this argument for `StepDecision` and every word
    of it applies here, with one addition -- this document is more likely to be misread as a
    grant precisely because it carries a principal.

    **WHAT IS DELIBERATELY NOT CHECKED, SO A READER DOES NOT ASSUME IT WAS.** That the approver
    holds cluster-admin. `approver_arns` is what makes the check POSSIBLE -- an executor can
    take an ARN from here and call `eks list-associated-access-policies` against the cluster's
    access entry -- and that check is not built. A prior investigation measured its cost: the
    read-only role is denied both `eks:ListAssociatedAccessPolicies` and
    `eks:ListAccessPolicies`, and `AmazonEKSMCPReadOnlyAccess` grants neither, so it needs a
    permission this pipeline does not currently hold anywhere.

    **NOTHING IN THE DEPLOYED PATH WRITES ONE OF THESE, AND THAT IS WORTH SAYING PLAINLY RATHER
    THAN LEAVING A READER TO WONDER WHERE THE WRITER IS.** The release gate adds no Lambda:
    `$.release` is assembled by the `Release approved` Pass state in
    `infra/terraform/modules/platform/workflow.tf`, straight from the automation's outputs, and no
    handler here
    serialises this document to storage. So this class is a READER'S CONTRACT -- the shape a
    future executor validates that payload against, and the place the field meanings are written
    down.

    **THE READER IS `from_gate_payload`, AND THE PAYLOAD IS NOT THIS SHAPE.** A contract nothing
    is checked against is a contract with one party, which this project has already paid for once
    -- every model test here passed while the deployed pipeline recorded a placeholder sentence as
    its approver. So the conversion from what the `Pass` state really produces into these fields
    is written down as code in one place, with the four differences between the two shapes named
    there, rather than left for each future reader to rediscover from a trace.
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["UpgradeRelease"] = "UpgradeRelease"

    run_id: str = Field(min_length=1)
    # THE SCOPE, for the module docstring's reason: an authorisation is per cluster, exactly as
    # the decisions it authorises are. Somebody who authorised a demo cluster's release has said
    # nothing about production.
    cluster: ClusterReference

    # NON-EMPTY, because an authorisation of nothing is worse than an absent one -- a reader
    # counts it. `Anything approved?` routes an empty approved list to `Done` without ever
    # starting the gate, so a `Release` naming no steps could only come from a defect in the
    # reshaping, and refusing it here is where that defect is legible.
    #
    # OBJECTS RATHER THAN ORDERS, WHICH IS A CHANGE THE FIRST REAL RECORD FORCED. It was
    # `tuple[int, ...]`, and one test run duly recorded `[1, 2, 4, 7]` -- a record of an
    # authorisation that could not say what had been authorised. See `ReleaseStep`.
    approved_steps: tuple[ReleaseStep, ...] = Field(min_length=1)

    # WHO AUTHORISED IT. Read from `aws:approve`'s `ApproverDecisions`, which is "a JSON map that
    # includes the approval decision of each approver" -- so this is AWS's account of the caller
    # it authenticated, not a name anybody typed.
    #
    # A TUPLE RATHER THAN ONE STRING even though `MinRequiredApprovals` is 1 today. Raising that
    # number is a one-line change to the runbook, and a field that had to change shape to record
    # the second approver would make the quorum change look larger than it is.
    approver_arns: tuple[str, ...] = Field(min_length=1)
    approver_authority: Authority = Authority.AWS_IAM_SSM_APPROVE
    approved_at: datetime

    # The automation this authorisation came out of. Recorded because it is the one handle that
    # leads back to the approver's own comment and the exact timing, in the SSM console, for as
    # long as the execution is retained -- and because a `Release` with no execution behind it is
    # not something a reader should have to take on trust.
    automation_execution_id: str = Field(min_length=1)

    # AWS'S OWN ACCOUNT, KEPT BESIDE THE ANSWER RATHER THAN INSTEAD OF IT. One entry per LISTED
    # approver, so a run with two resolved administrators records the one who answered AND the one
    # who did not -- `{"Approver": "...", "Decision": "Waiting"}`. `approver_arns` above is the
    # filtered answer; this is the evidence it was filtered from, which is what makes the filtering
    # checkable by somebody who does not trust it.
    #
    # OBJECTS, NOT THE JSON TEXT SSM EMITS. `ApproverDecisions` arrives as an array of strings, and
    # the first stored record kept them that way -- so reading the principal out of a document
    # calling itself `application/json` took a second parse. The decoding happens in
    # `stages/release_record.py` now, in code with tests, and what is stored needs no parse at all.
    #
    # PLAIN MAPPINGS RATHER THAN A MODEL, DELIBERATELY. These are AWS's fields, not this project's,
    # and `ContractModel` forbids extras -- so a model here would REFUSE a release the day SSM adds
    # a field to its own output. Keeping them as mappings is what lets that be a non-event; see the
    # test named for it.
    approver_decisions: tuple[Mapping[str, object], ...] = Field(min_length=1)

    # WHAT THE AUTOMATION SAID, AS ONE WORD, AND IT CAN ONLY EVER SAY THE ONE. A `Literal` rather
    # than a `str` is what keeps this honest: the old docstring argument against recording the
    # status at all was that a document carrying its own account of whether it was approved invites
    # a reader to trust that account. A field no other value validates cannot make a false claim --
    # a `Rejected` automation cannot produce a `Release` at all, here or in the state machine.
    #
    # A STRING, NOT `["Approved"]`. Every value in an SSM step's `Outputs` map is array-valued, and
    # the array reached the stored object once. Unwrapped where the parsing is, for the same reason
    # the decisions are.
    approval_status: Literal["Approved"] = "Approved"

    @model_validator(mode="after")
    def approved_at_carries_an_offset(self) -> Release:
        """A naive instant is refused here rather than normalised downstream.

        `StepDecision.decided_at_carries_an_offset` gives this argument in full and it holds
        identically: every comparison this document takes part in -- did the authorisation
        precede the change, which release came last, is this one stale -- is a comparison
        against an aware instant, and mixing the two raises a `TypeError` inside whatever is
        doing the comparing rather than here, where the error names the field.
        """
        moment = self.approved_at
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError(
                "approved_at must carry a UTC offset. An authorisation is compared against the "
                "decisions it authorises and against whatever acts on it, and a naive instant "
                "cannot be compared against an aware one."
            )
        return self

    @classmethod
    def from_gate_payload(
        cls,
        payload: Mapping[str, object],
        *,
        region: str,
        steps: tuple[ReleaseStep, ...],
    ) -> Release:
        """Validate the `$.release` payload the state machine builds, into this model.

        **`steps` IS REQUIRED AND COMES FROM THE PLAN, BECAUSE THE PAYLOAD CANNOT DESCRIBE ITSELF.**
        The gate payload carries step ORDERS -- `[1, 2, 4, 7]` -- and nothing about what they move;
        `stages/release_prep.py` derives the moves from the Stage 5 plan, and the same derivation
        builds the mail's `Changes` lines. Passing them in rather than deriving them here keeps
        this module free of storage and keeps one derivation behind both readers. The orders must
        match the payload's exactly, which is checked below.

        **WHY AN ADAPTER RATHER THAN A PASS STATE THAT PRODUCES THESE FIELDS DIRECTLY.** Four
        differences separate the two shapes, and three of them are not expressible in ASL:

        1. `approver_decisions` lists EVERY LISTED APPROVER, answered or not. A real payload from
           this deployment carries `"Decision":"Waiting"` for the approver role beside
           `"Decision":"Approve"` for the principal who answered, because `MinRequiredApprovals`
           is 1. `approver_arns` must hold only the ones who authorised, and separating them means
           parsing JSON inside a string -- which a `Pass` state cannot do, and which a `Map` state
           could only do by invoking something. This gate adds no Lambda, deliberately and at
           length; see `release_gate.tf`.
        2. `approved_at` IS NOT IN THE PAYLOAD. It is the latest `ApprovalDecisionTime` among the
           approvals, which is a comparison over decoded values for the same reason.
        3. `cluster` is a bare NAME -- `upgrade_decision_watcher` resumes the execution with
           `paused.cluster`, a string -- while this model holds a `ClusterReference`. The region
           is not a runbook parameter and never travels with the payload, so the caller supplies
           it; passing the wrong one is the one mistake this signature cannot prevent, which is
           why it is keyword-only rather than positional.
        4. `approval_status` is not a field here at all. It is CHECKED and dropped: the status
           lives in the automation, and a `Release` carrying its own account of whether it was
           approved would invite a reader to trust that account.

        **WHAT IT REFUSES IS THE POINT.** Each `raise` below is a shape this deployment has
        already recorded or is one edit away from recording: a promoted placeholder, a doubly
        nested filter result, a renamed key, a status the state machine should have routed
        elsewhere, an approver list where nobody answered. Every one of them would otherwise
        produce a `Release` that reads as an authorisation while naming no principal.

        Raises:
            ValueError: the payload is not an authorisation this model can honestly record.
            pydantic.ValidationError: it is, but a field fails the contract above.
        """
        keys = set(payload)
        if missing := sorted(_GATE_PAYLOAD_KEYS - keys):
            raise ValueError(
                f"the release payload is missing {missing}. `Release approved` sets every one of "
                "these, so a missing key means that Pass state was edited and this reader was "
                "not."
            )
        if unexpected := sorted(keys - _GATE_PAYLOAD_KEYS):
            raise ValueError(
                f"the release payload carries {unexpected}, which this reader does not know. "
                "Refused rather than ignored: this method assembles the model field by field, so "
                "a renamed key would otherwise leave the field it was meant to fill on its "
                "default, and the mismatch would surface as a missing approver rather than as an "
                "error."
            )

        status = _sole_output(payload["approval_status"], field="approval_status")
        if status != "Approved":
            raise ValueError(
                f"the release gate reported {status!r} rather than 'Approved'. `Release decided?` "
                "routes every non-success automation status to `Release rejected`, so a payload "
                "arriving here with another status means that routing and this reader disagree "
                "about what a refusal looks like -- and the refusal is the half that must win."
            )

        decisions = _approver_decisions(payload["approver_decisions"])
        approvals = [d for d in decisions if d.get("Decision") == _APPROVED]
        if not approvals:
            recorded = sorted({str(d.get("Decision")) for d in decisions})
            raise ValueError(
                f"no listed approver approved this release; the decisions recorded are {recorded}. "
                "The gate reported 'Approved', so this is the automation's status disagreeing with "
                "its own outputs rather than a rejection -- which is the state a promoted "
                "placeholder produced once, and the case where recording an authorisation with a "
                "plausible shape and no principal in it would do the most damage."
            )

        approver_arns = tuple(str(d["Approver"]) for d in approvals if d.get("Approver"))
        if len(approver_arns) != len(approvals):
            raise ValueError(
                "an approving decision names no Approver. `ApproverDecisions` is AWS's account of "
                "the caller it authenticated, so an entry without that field cannot be recorded "
                "as one."
            )

        # THE ORDERS MUST AGREE, AND A DISAGREEMENT IS A REFUSAL. The payload says which steps were
        # approved; `steps` says what those steps are. If they differ, one of the two is stale --
        # the plan was re-read, or the derivation was given a different run -- and a record built
        # from the pair would name moves nobody approved, or omit ones somebody did.
        wanted = tuple(int(o) for o in payload["approved_steps"])  # type: ignore[union-attr]
        described = tuple(step.order for step in steps)
        if described != wanted:
            raise ValueError(
                f"the release payload approved steps {list(wanted)} and the described moves are "
                f"for steps {list(described)}. A release record must describe exactly what was "
                "approved, so this is refused rather than reconciled."
            )

        return cls(
            run_id=str(payload["run_id"]),
            cluster=ClusterReference(name=str(payload["cluster"]), region=region),
            approved_steps=steps,
            approver_arns=approver_arns,
            approved_at=max(_approval_instant(d.get("ApprovalDecisionTime")) for d in approvals),
            automation_execution_id=str(payload["automation_execution_id"]),
            approver_decisions=tuple(decisions),
            approval_status="Approved",
        )

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()
