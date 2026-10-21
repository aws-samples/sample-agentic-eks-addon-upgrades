"""The upgrade brief's two ends: plan the work from the plan, then assemble the run document.

    plan      the Stage 5 plan document  ->  one work item per component move
    assemble  the partials the Map wrote ->  ONE UpgradeBrief under briefs/

**THE TRIGGER IS THE PLAN, NOT THIS RUN'S APPROVALS.** What defines installed-to-recommended for
a component is the plan's `COMPONENT_MOVE` step, and a move can rest on a fact approved weeks ago
-- metrics-server's does. Keying off the facts approved during the current run would skip exactly
the components most likely to be moved, which is the failure this paragraph exists to prevent.

**IT IS THE WHOLE PLAN DOCUMENT AND NOT ONLY `plan.steps`.** A component no recorded version
series of which reaches the target sits in the plan's `unsequenceable` array with `feasible` false
and gets no step -- correctly, because no ordering of moves carries it to the target. It still
appears on the upgrade approval as a row somebody must decide (`view/build.py::_unsupported_rows`),
and it can still MOVE: keda cannot reach 1.36, but it can go from 2.17.2 to 2.20.2, and a person
who has decided to keep running it unsupported is exactly the person who needs to know what those
three minors change. Walking `plan.steps` alone meant that row was the only decision on the page
with no brief behind it. So this stage requests one for each of those components too, keyed on the
step order `view/build.py` will give the row -- read from the shared
`view.build.unsupported_at_target` rather than recomputed here, because
`AdviceView.for_step(order)` joins a brief to a row BY that number and two copies of the
arithmetic would attach the brief to the wrong row in silence.

**TWO MODES, ONE HANDLER**, the shape `stages/freshness.py` already uses for its revocation path.
They are the two ends of a Map: `plan` emits the items, the Map invokes `brief_harness` once per
item, and `assemble` reads the partials back and writes one document. Splitting them across two
Lambdas would have made the work list and the document two things that could disagree about which
steps exist.

**WHY A MAP AT ALL.** One component's brief takes minutes inside the Harness -- several
release-note pages, an issue search, reasoning over all of it. A single Lambda looping over a
dozen moves would exceed 900 seconds and lose every brief when it did. Fanning out means a slow
or unreachable project delays nothing else, which is the same reason Stage 6 is invoked once per
component.

**A REUSED BRIEF IS RE-ANCHORED TO THIS PLAN, NEVER APPENDED VERBATIM.** A stored brief is
addressed by component and version pair and by nothing else, which is exactly what makes it
reusable across plans that order their steps differently -- and exactly why it cannot be trusted
about which step it belongs to. It was written against a different plan and carries that plan's
step number, step kind and Kubernetes minor. `_reanchored` replaces those, along with the run's
reuse note; see it for the field-by-field split between what the reuse key pins and what it does
not. The freshly researched path needs none of this, because those briefs were built from this
plan's own steps.

**IT CANNOT FAIL A RUN.** A missing partial becomes an `unbriefed` entry naming the step; a
document that cannot be written is reported on the payload and the approval proceeds. The only
`raise` on this path is a malformed `plan_uri`, which is a programming error rather than a
condition of the world. That is deliberately the opposite of `stages/approval_gate.py`, where a
run legitimately fails because a signature did not arrive.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from ..brief import reuse
from ..brief.document import (
    BRIEFABLE_STEP_KINDS,
    ComponentBrief,
    ExcludedStep,
    SectionState,
    UnbriefedStep,
    UpgradeBrief,
    excluded_reason,
    unbriefable_reason,
)
from ..brief.ruleset import ruleset_digest
from ..facts.series import exact_version
from ..knowledge import Reversibility
from ..order.plan import PlanStep, StepKind, UpgradePlan
from ..report import InventoryReport
from ..storage import (
    brief_key,
    build_fact_store,
    build_uri,
    read_document,
    write_document,
)
from ..storage.keys import component_brief_prefix
from ..view.build import installed_from_inventory, unsupported_at_target


def _work_item(
    step: PlanStep, store: Any, event: dict[str, Any], plan: UpgradePlan
) -> dict[str, Any]:
    """One item for the Map: everything `brief_harness` needs for this component.

    THE FACT IS READ HERE, ONCE, rather than in each Map iteration. A component's repository, its
    documentation hosts, its chart name and its recorded releases all come from the same fact, and
    resolving them per iteration would be one DynamoDB read per component for data this state has
    already loaded -- and would give the Map's role a fact-store grant it does not otherwise need.

    THE MARKED STORE, not the raw one. A withheld fact is knowledge this system has decided not to
    answer from, and reading around that for the brief's convenience would make the brief the one
    place a revoked fact still speaks. A component with no answering fact gets no repository, and
    `repositories` on the payload is the explicit override for that case.
    """
    component = str(step.component)
    fact = store.find(component)
    override = {str(k): str(v) for k, v in (event.get("repositories") or {}).items()}
    installed = {
        str(k): str(v) for k, v in (event.get("installed_versions") or {}).items()
    }
    return {
        "step": step.model_dump(mode="json"),
        "component": component,
        "plan_digest": plan.digest(),
        "cluster_name": plan.cluster.name,
        "target_kubernetes_version": plan.target_kubernetes_version,
        # The exact installed release when the inventory observed one; otherwise the step's own
        # `from_series`, which may be a series spec -- `brief/walk.py::InstalledPrecision` records
        # that the walk then starts at that minor's `.0`. The minors the hop ENTERS are unaffected
        # either way, which is where the breaking changes are.
        "installed_version": installed.get(component) or step.from_series or "",
        "repository": override.get(component)
        or (str(fact.canonical_repository) if fact is not None else None),
        "documentation": [str(d) for d in (fact.official_documentation if fact else ())],
        "chart_name": (fact.chart_name if fact is not None else None),
        "known_releases": [r.version for r in (fact.releases if fact else ())],
        **({"bucket": event["bucket"]} if event.get("bucket") else {}),
    }


def _stored_for(
    bucket: str, component: str, from_version: str, to_version: str
) -> tuple[reuse.StoredBrief, ...]:
    """Every brief already stored for one upgrade, newest-first order not assumed.

    Lists one prefix. Returns empty on any failure, which means "research it" -- the safe
    direction: a failed listing that produced a reuse would serve a brief nobody could confirm
    exists, while a failed listing that researches costs a model call and produces a correct answer.
    """
    if not bucket:
        return ()
    import boto3

    prefix = component_brief_prefix(component, from_version, to_version, ruleset_digest())
    found: list[reuse.StoredBrief] = []
    try:
        client = boto3.client("s3")
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            for obj in page.get("Contents") or []:
                written = reuse.written_on(obj["Key"])
                if written is not None:
                    found.append(
                        reuse.StoredBrief(uri=build_uri(bucket, obj["Key"]), written_on=written)
                    )
            token = page.get("NextContinuationToken")
            if not token:
                break
    except Exception:
        return ()
    return tuple(found)


def _inventory(event: dict[str, Any]) -> InventoryReport | None:
    """The Stage 2 report, or None. Read ONCE, for two different fields.

    THE INVENTORY IS THE ONLY DOCUMENT IN REACH THAT CARRIES EITHER OF THEM. A `PlanStep` has the
    versions and the ordering, not the provenance and not what a component that has no step is
    running -- and this stage's role may read `plans/*` and `inventories/*` and nothing else
    (`infra/terraform/modules/brief/main.tf`), which is deliberate and is why the assessment is not
    opened here.

    NONE IS A REAL ANSWER on both counts and the two defaults point opposite ways, each toward the
    cheaper mistake: with no install method nothing is EXCLUDED (worst case, a model call on a
    component AWS would have qualified), and with no installed version no unreachable component
    gets a brief (worst case, a row that says nothing was read). The opposite defaults would skip
    briefing a Helm release whose provenance could not be read, and brief a move that does not
    exist.
    """
    uri = str(event.get("inventory_uri") or "").strip()
    if not uri:
        return None
    try:
        return read_document(uri, InventoryReport)
    except Exception:
        return None


def _install_methods(inventory: InventoryReport | None) -> dict[str, str]:
    """component identity -> how it was installed. Empty when the inventory is unavailable.

    Keyed on the IDENTITY rather than the workload reference, because that is what a `PlanStep`
    names its component with and what the exclusion below is looked up by. The installed-version
    map beside it is keyed on `workload_ref` instead, because an unsequenceable component is named
    by workload reference in the plan and need not have an identity at all.
    """
    return {
        c.identity.strip(): c.install_method
        for c in (inventory.components if inventory is not None else ())
        if c.identity and c.install_method
    }


def _unsupported_steps(
    plan: UpgradePlan, installed: dict[str, str | None], store: Any
) -> tuple[PlanStep, ...]:
    """One synthetic step per component that cannot reach the target but can still move.

    **THESE STEPS ARE NOT IN THE PLAN AND MUST NEVER BE WRITTEN INTO IT.** `order/plan.py` is
    right to leave these components in `unsequenceable` with `feasible` false: no ordering of
    moves carries them to the target, and a plan step would make a digest-cited document assert a
    move that reaches something it does not. What exists here is a work item -- the subject of a
    brief -- and it lives for the length of this invocation.

    NUMBERED BY `view.build.unsupported_at_target`, WHICH IS THE WHOLE POINT OF CALLING IT. The
    approval page numbers these rows after the last real plan step, and `AdviceView.for_step`
    joins a brief to a row by `step_order`. If this function computed `max(order) + 1` for itself,
    the two would agree until either changed and then diverge silently: the row would render
    `No brief`, the brief would attach to nothing, and no error would be raised anywhere.

    KIND `COMPONENT_MOVE`, AND THAT IS NOT THE ROW'S KIND. `StepKind` is a frozen three-member
    enum and a brief is defined over an installed-to-recommended jump, which is exactly what this
    is -- 2.17.2 to 2.20.2 is a real move, briefable on the same terms as any other. The distinct
    `COMPONENT_UNSUPPORTED_AT_TARGET` kind lives on the presentation row, where it has to, so the
    approval surface, the pill, the warning wording and the "does not reach the target" note can
    all tell this row from a move that DOES reach the target. Nothing downstream of here reads
    `ComponentBrief.step_kind` to decide anything.

    `at_kubernetes` IS THE CURRENT VERSION, NOT THE TARGET. The move happens on the cluster as it
    stands today; naming the target would claim the move applies at a minor no recorded series of
    this component supports, which is the one thing every surface of this row exists to deny.

    `from_series` IS THE EXACT INSTALLED RELEASE when there is one, not the series the row shows.
    The row shows `2.17.x` because that is how the plan words the FROM end of every move; the walk
    wants precision, because starting at 2.17.0 instead of 2.17.2 makes it read release notes for
    patches already installed (`brief/walk.py::InstalledPrecision`).

    Components with nothing newer recorded are absent, and deliberately: there is no jump, so
    there is nothing to read release notes across, and the page says exactly that on the row.

    `component` IS ALWAYS AN IDENTITY HERE, never a bare workload reference, and it has to be:
    `_work_item` looks the fact up by that string and `_install_methods` is keyed on it. Guaranteed
    rather than checked -- a component with no resolved identity has no fact, therefore no recorded
    release, therefore no move, therefore no entry in this list.
    """
    steps: list[PlanStep] = []
    for record in unsupported_at_target(plan, installed, store):
        if not record.is_move:
            continue
        steps.append(
            PlanStep(
                order=record.order,
                kind=StepKind.COMPONENT_MOVE,
                description=(
                    f"Brief only: {record.component} {record.installed_series} → "
                    f"{record.move_to}, the newest recorded release. NOT a plan step and not a "
                    f"path to Kubernetes {plan.target_kubernetes_version}: no recorded version "
                    "series of this component supports the target, so it stays unsupported "
                    "however far it moves."
                ),
                component=record.component,
                from_series=exact_version(record.installed_version)
                or record.installed_series,
                to_series=record.move_to,
                recommended_version=record.move_to,
                at_kubernetes=plan.current_kubernetes_version,
                # UNKNOWN AND NOT A GUESS. `PlanStep` requires a reversibility on a
                # COMPONENT_MOVE, and nothing has classified this jump -- Stage 5 never
                # considered it, because it never produced a step for it. `UNKNOWN` is the value
                # this project's knowledge base uses for exactly that, and it reaches no
                # approval surface: the row this brief attaches to carries its own kind and its
                # own wording.
                reversibility=Reversibility.UNKNOWN,
                reversibility_reason=(
                    "No move for this component was ever sequenced, so nothing classified this "
                    "jump. Read the brief's breaking changes before taking it."
                ),
            )
        )
    return tuple(steps)


# PUBLIC FOR THE RELEASE PATH. `stages/release_prep.py` turns an approved unsupported-at-target
# row into a release step with this same function, so the brief, the authorisation mail and the
# lane all describe one move -- same order, same from/to -- rather than three derivations.
unsupported_steps = _unsupported_steps


def _plan(event: dict[str, Any]) -> dict[str, Any]:
    """Emit one work item per briefable step, and name the steps that can never have one.

    A step of a kind this document does not cover is SKIPPED, not recorded as unbriefed: a
    CLUSTER_UPGRADE has no installed-to-recommended jump, so listing it as a step that failed to
    get a brief would fill the absence list with entries no reader can act on -- and `unbriefed`
    is read as "these moves have no advice", which would then be false. A briefable kind missing a
    field DOES get an entry, because that is a move a person will be asked to approve.

    THE WORK LIST IS THE PLAN'S STEPS PLUS THE COMPONENTS THAT CANNOT REACH THE TARGET. See
    `_unsupported_steps` for why the second group exists and why its numbers are read from the
    presentation rules rather than computed here. They join the same loop deliberately: one reuse
    lookup, one fact read, one work-item shape, so a brief for keda is produced, stored, served
    from storage and assembled by exactly the code that does it for every other move.
    """
    plan = read_document(str(event["plan_uri"]), UpgradePlan)
    store = build_fact_store()
    now = datetime.now(UTC)
    bucket = str(event.get("bucket") or os.environ.get("DOCUMENT_BUCKET", "")).strip()

    inventory = _inventory(event)
    installed_by = _install_methods(inventory)
    unsupported = _unsupported_steps(
        plan, installed_from_inventory(inventory), store
    )
    unsupported_orders = {step.order for step in unsupported}
    to_research: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    unbriefable: list[dict[str, str | int]] = []
    excluded: list[dict[str, str | int]] = []

    for step in (*plan.steps, *unsupported):
        if step.kind not in BRIEFABLE_STEP_KINDS:
            continue
        # EXCLUDED BEFORE ANYTHING IS SPENT ON IT -- before the reuse lookup, before the fact
        # read, and certainly before the agent. AWS decided and qualified these versions, so
        # there is nothing to research; see `brief/document.py::EXCLUDED_INSTALL_METHODS`.
        #
        # NOT APPLIED TO AN UNSUPPORTED-AT-TARGET COMPONENT, and the reason is the exclusion's own
        # justification read carefully. It rests on "AWS has already established this version
        # works on this Kubernetes minor", which is precisely what is FALSE here: this component is
        # in the plan's `unsequenceable` array because no recorded series of it reaches the target,
        # AWS-managed or not. Filing it under `excluded` would put that sentence in an advisory
        # document about a component nothing has qualified. Its move is researched on the same
        # terms as any other, which also costs at most one model call per such component.
        method = installed_by.get(str(step.component or ""))
        skip = None if step.order in unsupported_orders else excluded_reason(method)
        if skip is not None:
            excluded.append(
                {
                    "step_order": step.order,
                    "component": str(step.component),
                    "install_method": str(method),
                    "reason": skip,
                }
            )
            continue
        reason = unbriefable_reason(step)
        if reason is not None:
            unbriefable.append(
                {
                    "step_order": step.order,
                    "component": step.component or f"(step {step.order})",
                    "reason": reason,
                }
            )
            continue

        item = _work_item(step, store, event, plan)
        component = str(step.component)
        from_version = str(item["installed_version"] or "unknown")
        to_version = str(step.recommended_version or step.to_series)

        # THE REUSE LOOKUP. Keyed on the upgrade, so a brief written for this hop by any earlier
        # run -- for any cluster -- is served instead of paying for the agent again. See
        # `brief/reuse.py` for why it expires on a clock here when facts deliberately do not.
        stored = _stored_for(bucket, component, from_version, to_version)
        served = reuse.serve(stored, now)
        note = reuse.expiry_note(served, len(stored), now)

        if served is not None:
            reused.append(
                {
                    "step_order": step.order,
                    "component": component,
                    "uri": served.uri,
                    "written_on": served.written_on.isoformat(),
                    "age_days": served.age_days(now),
                    "reuse_note": note,
                    # THIS RUN'S STEP, CARRIED SO THE ASSEMBLER CAN RE-ANCHOR THE STORED DOCUMENT
                    # TO IT. A stored brief is addressed by component and version pair alone, so it
                    # keeps whatever step number, step kind and Kubernetes minor the plan it was
                    # WRITTEN for gave it -- and `_assemble` has no other way to learn what this
                    # plan says, because `_unsupported_steps` synthesises steps that are not in
                    # `plan.steps` at all. Carried rather than looked up for the same reason the
                    # uri is: the assembler follows what this stage actually decided instead of
                    # recomputing it from documents and reaching a different answer in silence.
                    "step": step.model_dump(mode="json"),
                }
            )
            continue
        to_research.append({**item, "reuse_note": note})

    return {
        **event,
        "stage": "brief-plan",
        "advisory": True,
        "blocking": False,
        "plan_digest": plan.digest(),
        "cluster_name": plan.cluster.name,
        "store_digest": store.digest(),
        "observed_at": now.isoformat(),
        # What the Map iterates. Named `brief_to_research` rather than `items` so a payload
        # carrying several fan-outs cannot have two states reading the same key -- and so the name
        # says that a component absent from it was NOT skipped, it was already answered.
        "brief_to_research": to_research,
        "brief_reused": reused,
        "brief_unbriefable": unbriefable,
        # Deliberately not briefed, kept apart from the ones that could not be. One is a decision,
        # the other is a gap, and a reader scanning for gaps must not find decisions among them.
        "brief_excluded": excluded,
        # What the Choice reads. A count rather than the list, because a Choice comparing a list
        # length is not something Step Functions expresses.
        "brief_research_count": len(to_research),
        "brief_reuse_window_days": reuse.MAX_REUSE_AGE_DAYS,
        # WHICH STEP NUMBERS BELONG TO A COMPONENT THAT CANNOT REACH THE TARGET. Reported so an
        # operator reading the payload can see that a brief was requested for a row the plan has
        # no step for -- and so a run where that number does not match the approval page's row can
        # be diagnosed from the log rather than from the absence of a Details button.
        "brief_unsupported_at_target": {
            step.order: str(step.component) for step in unsupported
        },
        "summary": (
            f"{len(to_research)} component move(s) to research, {len(reused)} served from "
            f"storage, {len(excluded)} excluded as AWS-managed, {len(unbriefable)} that cannot "
            f"be briefed; {len(unsupported)} of them for a component that cannot reach "
            "Kubernetes "
            f"{plan.target_kubernetes_version} at all and is being briefed on the move it can "
            "still make. Advisory only -- the upgrade approval does not wait on this."
        ),
    }


def _read_brief(uri: str) -> ComponentBrief | None:
    """One stored brief, or None if it is not there. Absence is expected and is not an error.

    Deliberately not distinguished by exception type. NoSuchKey, AccessDenied and a validation
    failure all mean the same thing to the assembler -- this step has no usable brief -- and the
    caller reports it as an unbriefed step naming the uri, which is more useful than a botocore
    type nobody reads.
    """
    try:
        return read_document(uri, ComponentBrief)
    except Exception:
        return None


# WHICH FIELDS OF A STORED BRIEF BELONG TO WHAT, stated as four lists rather than left implicit in
# `_reanchored`'s body. Every field of `ComponentBrief` appears in exactly one of them, and
# `tests/test_a_reused_brief_is_anchored_to_this_runs_step.py` fails if a field belongs to none --
# so adding one to the contract forces the same decision these lists record instead of defaulting
# to "carried over verbatim", which is how `step_order` and `reuse_note` came to be wrong.
#
# THE PLAN. A stored brief is addressed by component and by the from/to pair and by NOTHING ELSE
# (`storage/keys.py::component_brief_prefix`), which is precisely what makes it reusable across
# plans that order their steps differently -- so anything describing the plan it was written in has
# to be replaced by the plan in hand.
_ANCHORED_TO_THIS_PLAN: tuple[str, ...] = (
    "step_order",
    "step_kind",
    "from_series",
    "to_series",
    "recommended_version",
    "at_kubernetes",
)

# THIS RUN'S REUSE DECISION, which the stored document cannot know: it was written by a run that
# researched fresh and says so.
_ANCHORED_TO_THIS_RUN: tuple[str, ...] = ("reuse_note",)

# THE COMPONENT UPGRADE. What the reuse key pins, and the whole reason a brief is stored at all.
_THE_COMPONENT_UPGRADE: tuple[str, ...] = (
    "component",
    "chart_version",
    "coverage",
    "sequencing",
    "sections",
)

# THE EARLIER RESEARCH ACT, KEPT DELIBERATELY AND NOT AN OVERSIGHT. Both fields are load-bearing
# exactly because they describe the other run:
#
#   observed_at      `UpgradeBrief.reused` derives "served from storage" by comparing this against
#                    the run's own `observed_at`. Overwriting it with this run's clock would empty
#                    the `reused` list, zero `reused_from_storage`, and make a five-day-old reading
#                    of an issue tracker indistinguishable from a fresh one -- which the field's own
#                    contract calls the whole hazard of reuse.
#   evidence_session the prefix the bytes behind every quote were recorded under, BEFORE anything
#                    read them. It belongs to the same act as the sections it substantiates;
#                    dropping it would leave verified quotes with nothing to verify against and turn
#                    the document from evidence back into assertion.
_THE_EARLIER_RESEARCH: tuple[str, ...] = ("observed_at", "evidence_session")


def _this_runs_step(entry: dict[str, Any], plan: UpgradePlan) -> PlanStep | None:
    """The step THIS run has for a reused brief, or None if the payload cannot say.

    `brief-plan` puts the step on the entry, which is the only source that covers the synthetic
    steps `_unsupported_steps` mints for a component that cannot reach the target -- those are
    deliberately absent from `plan.steps`. The plan lookup behind it is for a payload written by an
    older `brief-plan` and assembled by this one, which a deploy landing mid-run produces: a real
    plan step is still recoverable by its number there, and recovering it re-anchors six fields
    instead of one.

    THE LOOKUP IS CONFIRMED BY COMPONENT AND NOT ONLY BY NUMBER. Both numbers come from the same
    plan document -- it is content-addressed and both modes read the same uri -- so a mismatch is
    not reachable today. Checked anyway because the cost of being wrong here is the entire defect
    being fixed, wearing a different hat: a step matched by number alone could re-anchor this
    brief's version fields to ANOTHER component's move, which is a worse document than the stale
    one. A step that fails the check re-anchors the join key and nothing else.
    """
    raw = entry.get("step")
    if isinstance(raw, dict):
        return PlanStep.model_validate(raw)
    order = int(entry["step_order"])
    component = str(entry["component"]).strip()
    return next(
        (s for s in plan.steps if s.order == order and str(s.component or "") == component),
        None,
    )


def _reanchored(
    brief: ComponentBrief, entry: dict[str, Any], plan: UpgradePlan
) -> ComponentBrief:
    """A stored brief, re-addressed to the step THIS plan gives it and this run's reuse decision.

    **THE DEFECT THIS EXISTS FOR, in the shape it was found live.** metrics-server's brief was
    stored on 2026-09-20, when metrics-server was step 4 of that day's plan. On 2026-09-26 the same
    move was step 3 and the cluster upgrade was step 4. The reuse loop appended the stored document
    verbatim, so `briefed_steps` read `[1, 2, 4]`, `AdviceView.for_step(3)` found nothing, and the
    person approving step 3 was shown a card with no brief on it while a perfectly good brief sat in
    the document attached to the control-plane upgrade. Nothing raised. cert-manager escaped only
    because its moves were steps 1 and 2 in both plans.

    **THE FIELDS SPLIT FOUR WAYS AND THE LISTS ABOVE ARE THE VERDICT.** Briefly, because the lists
    carry the reasoning:

        anchored to this plan   step_order, step_kind, from_series, to_series,
                                recommended_version, at_kubernetes
        anchored to this run    reuse_note
        the component upgrade   component, chart_version, coverage, sequencing, sections
        the earlier research    observed_at, evidence_session

    `at_kubernetes` IS THE ONE WORTH SPELLING OUT, because it is a silent version-correctness claim
    rather than a display bug. The reuse key says nothing about the cluster, so the same 0.6.x ->
    0.9.0 brief is served to a plan that sequences the move BEFORE the control-plane upgrade and to
    one that sequences it after -- 1.35 in the first case, 1.36 in the second. A carried-over 1.35
    asserts the move was assessed at a minor the cluster has already left.

    `from_series`, `to_series` and `recommended_version` are re-anchored even though today's key
    pins all three: the key is what makes the values agree, so the invariant currently holds by
    argument about `component_brief_prefix` rather than by construction. Re-anchoring is a no-op in
    every case the key permits, which is why it is safe -- and it is what keeps these fields correct
    if a later change ever widens the prefix.
    """
    step = _this_runs_step(entry, plan)
    anchored: dict[str, Any] = {
        # ALWAYS, AND FROM THE ENTRY. This is the join key the approval page renders by, so it is
        # re-anchored even on the degraded path where the step itself cannot be recovered.
        "step_order": int(entry["step_order"]),
        "reuse_note": str(entry["reuse_note"]),
    }
    if step is not None:
        anchored |= {
            "step_kind": step.kind.value,
            "from_series": step.from_series,
            "to_series": str(step.to_series),
            "recommended_version": step.recommended_version,
            "at_kubernetes": str(step.at_kubernetes),
        }
    return ComponentBrief.model_validate({**brief.model_dump(mode="json"), **anchored})


def _assemble(event: dict[str, Any]) -> dict[str, Any]:
    """Read every brief back -- reused and freshly researched alike -- and write the run document.

    IT READS URIs OFF THE PAYLOAD RATHER THAN RECONSTRUCTING KEYS. `brief-plan` records the uri of
    each reused brief and the Map records the uri each iteration wrote, so the assembler follows
    what actually happened instead of recomputing where things ought to be. Reconstructing a key
    means guessing a date and a content digest, and a guess that misses reads as "this step has no
    brief" -- an absence invented by arithmetic.

    EVERY EXPECTED STEP IS ACCOUNTED FOR, either as a brief or as an `unbriefed` entry naming the
    reason. A Map iteration that died leaves a named gap rather than a document that quietly covers
    four moves out of five, because an omission reads as "nothing to say about this move".

    **THE TWO PATHS ARE NOT SYMMETRICAL AND ONLY ONE NEEDS RE-ANCHORING.** A freshly researched
    brief was built by `brief_harness` from THIS run's work item -- `_work_item` hands it
    `step.model_dump()` and `_plan`'s own `reuse_note`, and `brief/document.py::component_brief`
    copies its header straight off that step -- so every field already describes the plan in hand
    and it is appended as it stands. A REUSED one was written against a different plan and knows
    nothing about this one; `_reanchored` is what makes it addressable here.
    """
    now = datetime.now(UTC)
    plan = read_document(str(event["plan_uri"]), UpgradePlan)
    bucket = str(event.get("bucket") or os.environ.get("DOCUMENT_BUCKET", "")).strip()

    briefs: list[ComponentBrief] = []
    unbriefed: list[UnbriefedStep] = []
    excluded_steps: list[ExcludedStep] = []

    for entry in event.get("brief_excluded") or []:
        excluded_steps.append(
            ExcludedStep(
                step_order=int(entry["step_order"]),
                component=str(entry["component"]),
                install_method=str(entry["install_method"]),
                reason=str(entry["reason"]),
            )
        )

    for entry in event.get("brief_unbriefable") or []:
        unbriefed.append(
            UnbriefedStep(
                step_order=int(entry["step_order"]),
                component=str(entry["component"]),
                reason=str(entry["reason"]),
            )
        )

    # THE REUSED ONES FIRST. Their uri came from the lookup, so a read failure here means the object
    # was listed and then could not be fetched -- worth reporting distinctly, because it points at a
    # permission or a lifecycle rule rather than at a component nobody briefed.
    for entry in event.get("brief_reused") or []:
        brief = _read_brief(str(entry["uri"]))
        if brief is None:
            unbriefed.append(
                UnbriefedStep(
                    step_order=int(entry["step_order"]),
                    component=str(entry["component"]),
                    reason=(
                        f"a stored brief was found at {entry['uri']} and then could not be read "
                        "back, so this move has no advice. That points at a permission or a "
                        "lifecycle rule rather than at a component nobody briefed."
                    ),
                )
            )
            continue
        # RE-ANCHORED, NEVER APPENDED VERBATIM. See `_reanchored`: the stored document carries the
        # step number, step kind and Kubernetes minor of the plan it was WRITTEN for, and appending
        # it as-is attached metrics-server's brief to the control-plane upgrade in two live runs.
        try:
            briefs.append(_reanchored(brief, entry, plan))
        except Exception as exc:
            # UNREACHABLE VIA `brief-plan`, which only reuses a step `unbriefable_reason` has
            # already passed, so every field this needs is present and well-formed. Caught anyway
            # because the alternative is worse in both directions: letting it propagate loses the
            # WHOLE document and every brief beside this one, and appending the un-anchored brief
            # reintroduces exactly the defect -- advice on the wrong approval card. A named absence
            # is this stage's own vocabulary for "we could not tell you", and it costs one step.
            unbriefed.append(
                UnbriefedStep(
                    step_order=int(entry["step_order"]),
                    component=str(entry["component"]),
                    reason=(
                        f"a stored brief was read back from {entry['uri']} and then could not be "
                        f"re-anchored to this run's step: {type(exc).__name__}: {exc}. It was "
                        "dropped rather than shown, because a brief still carrying the step number "
                        "of the plan it was written for renders beside the wrong move."
                    ),
                )
            )

    # THEN THE FRESHLY RESEARCHED ONES, from the Map's collected results. An iteration that returned
    # `briefed: false` said so itself and carries its own reason; one that returned nothing at all
    # is the caught-failure case and is named here.
    #
    # NOT RE-ANCHORED, AND THAT IS CORRECT RATHER THAN AN OMISSION. Each of these was written during
    # this run from this plan's step -- see `_assemble`'s docstring -- so its `step_order`,
    # `step_kind`, `at_kubernetes` and `reuse_note` are already this plan's and this run's. Putting
    # `_reanchored` on this path would replace six fields with the same six values and give the
    # impression the two paths carry the same hazard.
    for result in event.get("brief_results") or []:
        if not isinstance(result, dict):
            continue
        order = int(result.get("step_order") or 0)
        component = str(result.get("component") or f"(step {order})")
        uri = str(result.get("brief_uri") or "")
        brief = _read_brief(uri) if uri else None
        if brief is None:
            unbriefed.append(
                UnbriefedStep(
                    step_order=order or 1,
                    component=component,
                    reason=str(
                        result.get("reason")
                        or "no brief was produced for this move and no reason was recorded. Its "
                        "release notes, known issues and new features are UNKNOWN, not empty."
                    ),
                )
            )
            continue
        briefs.append(brief)

    document = UpgradeBrief(
        observed_at=str(event.get("observed_at") or now.isoformat()),
        cluster=plan.cluster,
        target_kubernetes_version=plan.target_kubernetes_version,
        plan_digest=plan.digest(),
        store_digest=str(event.get("store_digest") or build_fact_store().digest()),
        ruleset_digest=ruleset_digest(),
        briefs=tuple(sorted(briefs, key=lambda b: b.step_order)),
        unbriefed=tuple(sorted(unbriefed, key=lambda u: u.step_order)),
        excluded=tuple(sorted(excluded_steps, key=lambda e: e.step_order)),
    )
    uri, not_published = _publish(document, bucket)

    return {
        **event,
        "stage": "brief",
        "advisory": True,
        # Stated on the payload, not only in the document. Whatever reads this next must see
        # without opening anything that it may proceed regardless.
        "blocking": False,
        "observed_at": document.observed_at,
        "brief_uri": uri,
        "brief_digest": document.digest(),
        "brief_ruleset_digest": document.ruleset_digest,
        "brief_counts": document.counts(),
        "brief_summary": document.summary(),
        "briefed_steps": [b.step_order for b in document.briefs],
        "unbriefed_steps": {u.step_order: u.reason for u in document.unbriefed},
        "excluded_steps": {e.component: e.install_method for e in document.excluded},
        # Which briefs were served from storage and how old each is. On the payload so an operator
        # can see the run reused work without opening the document -- and so a reader is never
        # shown a week-old known-issues section believing it was read just now.
        "reused_briefs": {
            b.component: b.reuse_note for b in document.reused
        },
        # The two honesty lists, flattened onto the payload: sections that could not be produced,
        # and hops whose intermediate minors nobody read.
        "sections_not_looked": [
            {
                "step": b.step_order,
                "component": b.component,
                "section": str(section.section),
                "reason": section.reason,
            }
            for b in document.briefs
            for section in b.sections
            if section.state is SectionState.NOT_LOOKED
        ],
        "minors_unread": {
            b.component: list(b.coverage.minors_unread)
            for b in document.briefs
            if b.coverage is not None and b.coverage.minors_unread
        },
        **({} if uri else {"brief_not_published": not_published}),
    }


def _publish(document: UpgradeBrief, bucket: str) -> tuple[str | None, str | None]:
    """Store the run document. Returns `(uri, why_not)` -- exactly one of them is None.

    NON-FATAL, for `stages/freshness.py::_publish`'s reason applied to a document that is advisory
    from end to end. There, marks were already in force and only the report of them was at risk;
    here there is nothing but the report, and the design's rule is that a missing brief must not
    stop an approval. Raising would convert "the advisory document could not be stored" into "this
    cluster gets no upgrade approval", which is precisely backwards.
    """
    if not bucket:
        return None, (
            "no bucket was supplied on the payload and DOCUMENT_BUCKET is unset, so the brief "
            "was assembled and digested but not stored"
        )
    uri = build_uri(
        bucket,
        brief_key(
            document.cluster.name,
            document.target_kubernetes_version,
            document.plan_digest,
            document.ruleset_digest,
            document.digest(),
            document.observed_at,
        ),
    )
    try:
        return write_document(uri, document), None
    except Exception as exc:
        return None, (
            f"the brief could not be stored at {uri}: {type(exc).__name__}: {exc}. The upgrade "
            "approval proceeds without it; nothing downstream reads this document. If this is "
            "AccessDenied, the s3:PutObject grant on briefs/* has not been applied yet."
        )


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Plan the brief work, or assemble the run document. `mode` selects which.

    Required in `event`:

        plan_uri   the Stage 5 document. The plan is the trigger -- its component-move steps
                   define installed-to-recommended, whatever run approved the facts behind them.
        mode       `plan` (default) or `assemble`.

    Optional:

        bucket              where partials and the document live. Falls back to DOCUMENT_BUCKET.
        installed_versions  {component: version} -- the exact installed release, when the
                            inventory observed one.
        repositories        {component: url} -- a repository for a component with no answering
                            fact. Without it that component's sections read NOT_LOOKED.
        inventory_uri       the Stage 2 report, read for two fields: how each component was
                            installed, and what a component the plan has no step for is running.
                            Absent means brief everything and brief nothing extra -- see
                            `_inventory` for why those two defaults point opposite ways.

    Returns the payload it was given plus its own fields, because these are states BETWEEN the
    plan and the approval: a state returning only its own result would strip `plan_uri` off the
    payload and the approval would fail for want of its inputs.
    """
    mode = str(event.get("mode") or "plan").strip().casefold()
    if mode == "assemble":
        return _assemble(event)
    if mode != "plan":
        # Named rather than silently defaulted. A typo'd mode that quietly planned instead of
        # assembling would produce a run with no document and no error to explain it.
        raise ValueError(
            f"mode={mode!r} is not recognised; use 'plan' to emit the work list or 'assemble' "
            "to write the run document"
        )
    return _plan(event)
