"""The upgrade approval: pause the run, show a person what changing this cluster means, wait.

    in    the Stage 5 plan, and the run's upgrade brief
    out   nothing, until a person decides

WHERE IT SITS.

    ... -> Plan -> Research -> Approval (waits) -> ... -> Brief -> UPGRADE APPROVAL (waits) -> Done

TWO WAITS IN ONE EXECUTION, AND THEY ASK DIFFERENT QUESTIONS. The first asks *"is this claim
about a component true?"* -- a fact, true of every cluster in the fleet, which is why one
approval there answers for all of them. This asks *"may we change THIS cluster?"*. The same
component may be approved here and refused next door, and neither answer says anything about
the other. That is why the decision this stage records is keyed on the cluster and why it is
not written into the fact store.

IT REPLACED THE REPORT STATE RATHER THAN BEING ADDED AFTER IT. `Present` rendered a
read-only report and was the machine's terminal state; this renders the same report WITH
controls, so a second rendering state would have produced two pages of the same run and left
a person deciding from whichever they happened to open. The report is not gone -- it is this
page, minus nothing.

WHAT THIS HANDLER DOES NOT DO. It does not decide, does not approve, and does not act on the
cluster. It renders, mints per-step capabilities, records where to resume, and stops. It
holds no `SendTaskSuccess` permission and could not resume its own run if it tried; that
permission belongs to the path a person's decision travels down.

WHAT HAPPENS IF NOBODY EVER DECIDES. The state carries a `TimeoutSeconds` and the execution
FAILS when it expires. Deliberate, for the fact stage's reason: a run that quietly gave up
and finished would be indistinguishable from one nobody was asked about. Every decision made
before the timeout is already durable in S3, so failing loses the resume and not the answers.

WHAT HAPPENS IF THERE IS NO BRIEF. The page renders anyway and says so per step. A brief
cannot fail a run and cannot gate an approval -- that is the property the whole brief stage
was built around -- so a missing one is a sentence on the page, never a reason not to ask.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from ..brief.document import UpgradeBrief
from ..decide import ReadinessAssessment
from ..facts import GapLedger
from ..notify import build_readiness_email, build_upgrade_email, send_approval_email
from ..notify.email import approver_address
from ..notify.presign import presign_s3_get
from ..order import UpgradePlan
from ..report import InventoryReport
from ..snapshot import ClusterSnapshot
from ..storage import (
    build_fact_store,
    build_uri,
    read_document,
    readiness_report_key,
    upgrade_approval_key,
    upgrade_recorded_key,
    write_text,
)
from ..storage.decisions import build_upgrade_click_form
from ..storage.step_tokens import (
    PausedUpgrade,
    StepGrant,
    build_step_token_store,
    new_nonce,
)
from ..upgrade_lane.applicable import catalogue_index, not_applicable_reason
from ..view import build_document
from ..view.advice import advice_from_brief
from ..view.model import UNSUPPORTED_AT_TARGET_KIND
from ..view.render.html import to_html
from .settings import from_event

# Step kinds a person is asked to authorise. A CLUSTER_UPGRADE is not among them: this
# system moves add-ons and does not advance a control plane, so that step appears in the
# plan as a boundary and on the page as "not authorisable".
#
# Listed rather than derived from "everything except CLUSTER_UPGRADE", so that adding a step
# kind to the plan does not silently make it something this stage asks about.
#
# `UNSUPPORTED_AT_TARGET_KIND` IS AUTHORISABLE THOUGH IT AUTHORISES NO PATH TO THE TARGET, and
# that is the whole reason it exists. It is not a plan step -- `view/build.py` mints it from an
# INCOMPATIBLE entry in the plan's `unsequenceable` array, where a component sits when no
# recorded version series of it reaches the target. Before it, such a component appeared on this
# page only inside the could-not-be-sequenced table, which carries no controls: the run asked a
# person about every move in the upgrade and gave them nothing to answer about the one component
# that will not survive it. The decision being asked for is per cluster -- "this cluster accepts
# running keda unsupported at 1.36" -- and this stage is where per-cluster decisions are
# recorded, so the grant, the form and the durable `StepDecision` are minted here on exactly the
# same terms as a move.
#
# IT MAY CARRY A REAL MOVE, AND IT IS STILL ITS OWN KIND. When the fact records a release newer
# than what is installed, approving the row means two things at once: take the component as far as
# its recorded releases go, AND accept that the target stays unsupported. Folding it into
# `COMPONENT_MOVE` would lose the second half everywhere it matters -- in this set, on the pill, in
# the row's wording, in the note above the table, and in the durable `StepDecision` a person reads
# six weeks later to find out what they agreed to.
#
# APPROVING ONE DOES NOT MAKE THE PLAN FEASIBLE and nothing in this stage may imply that it
# does. The plan document is untouched, `feasible` stays false, and the page's own wording is
# required to say so (`view/render/html.py::_unsupported_at_target_row`).
AUTHORISABLE_KINDS: frozenset[str] = frozenset(
    {"COMPONENT_MOVE", "COMPONENT_UPGRADE_RECOMMENDED", UNSUPPORTED_AT_TARGET_KIND}
)


def _withheld_until_the_cluster_moves(
    view: Any,
    catalogue: dict[tuple[str, str], tuple[str, ...]],
    no_chart: frozenset[str] = frozenset(),
) -> dict[int, str]:
    """Steps the cluster cannot take today, with the reason each is withheld.

    THE RULE IS `upgrade_lane.applicable.not_applicable_reason`, shared with the lane planner so
    the page and the lanes cannot disagree about which steps are available. Consider
    kube-proxy `v1.36.0-eksbuild.25` approved on a 1.35 cluster and refused by EKS; a first
    version of this check that withheld every managed add-on by placement would have
    blocked three builds AWS does list for 1.35. The catalogue decides now; see that module.

    `StepRow` carries no `at_kubernetes` or install method of its own: the minor is read out of
    the step's description with this module's `_step_parts`, the same parse the grants use, and
    `aws_managed` is the row's own property.
    """
    current = view.current_kubernetes_version
    withheld: dict[int, str] = {}
    for step in view.steps:
        if step.kind in AUTHORISABLE_KINDS and _step_parts(step)[0] in no_chart:
            # A HELM STEP WITH NO CHART REPOSITORY CANNOT RUN, so it gets no button. The lane
            # planner skips exactly these (`stages/upgrade_report.py`, "carries no chart
            # repository"); offering the control anyway is what let run8 approve a step that
            # was certain to be skipped. `no_chart` is computed by `_components_without_chart`
            # from the same provenance `release_record` hands the lane.
            withheld[step.order] = (
                "No chart repository is recorded in the approved fact, so the upgrade lane has "
                "nothing to `helm repo add` and would skip this step. Re-research the component "
                "to record one."
            )
            continue
        if step.kind == UNSUPPORTED_AT_TARGET_KIND:
            # ONLY THE DEAD-END SHAPE IS WITHHELD. A row that carries a move (the fact records a
            # release newer than what is installed) keeps its Approve button: approving it runs
            # a lane to that release, unsupported at the target. A row with nothing newer has no
            # change to release, so a button there could only produce an approval the release
            # step has no move for -- which is how run6 failed. The move shape is identified by
            # the sentence `view/build.py::_unsupported_rows` composes for it.
            if ". Move " not in step.description:
                withheld[step.order] = (
                    "Already on the newest recorded release, so there is nothing to upgrade. "
                    f"No recorded version supports Kubernetes {view.target_kubernetes_version}; "
                    "whether to upgrade the cluster anyway is your decision."
                )
            continue
        if step.kind not in AUTHORISABLE_KINDS:
            continue
        component, _installed, recommended, at_kubernetes = _step_parts(step)
        reason = not_applicable_reason(
            component=component,
            target_version=recommended,
            at_kubernetes=at_kubernetes,
            current_minor=current,
            managed_addon=step.aws_managed,
            catalogue=catalogue,
        )
        if reason:
            withheld[step.order] = reason
    return withheld


def _components_without_chart(inventory: Any, store: Any) -> frozenset[str]:
    """Helm components whose approved fact records no chart repository.

    THE SAME PROVENANCE THE LANE IS HANDED. `release_record.provenance` is what writes
    `chart_repository` onto every release step, and the lane planner skips a Helm step without
    one -- so reading it here means the page and the lane cannot disagree about which Helm steps
    can run. Keyed like a plan step's component (`identity or workload_ref`).
    """
    from .release_record import provenance

    methods = {
        (c.identity or c.workload_ref): c.install_method for c in inventory.components
    }
    return frozenset(
        component
        for component, found in provenance(inventory, store).items()
        if methods.get(component) == "HELM" and not found.chart_repository
    )


def _render_inputs(
    payload: dict[str, Any],
) -> tuple[Any, Any, Any, str, dict[int, str], tuple[int, ...]]:
    """Everything both pages are built from: the view, its provenance, the brief, what is withheld.

    SHARED BY THE APPROVAL AND THE READINESS REPORT so the two cannot disagree about a run. The
    approval adds controls, a token and a grant per step on top of this; the readiness report adds
    one sentence. Same documents, same view, same digest chain, same withheld set.
    """
    # THE HTML AND MARKDOWN `build_document` ALSO RETURNS ARE DISCARDED, deliberately. Those
    # are the read-only renders -- the page this stage needs carries the brief and the
    # controls, so it renders its own below. The document is used for its VIEW and its digest
    # chain, which is the part that is a pure function of the four stage documents.
    #
    # NOTHING PUBLISHES THAT DOCUMENT ANY MORE. `Present` used to write it to `reports/`, and
    # it is gone; the artifact of this run is the approval page. So there is no recorded
    # `html_digest` that could disagree with the bytes a person actually read -- which would
    # otherwise be a real inconsistency, because the document's digest covers the plain
    # render and not this one.
    document, _plain_html, _plain_markdown = build_document(
        inventory=(inventory := read_document(str(payload["inventory_uri"]), InventoryReport)),
        ledger=read_document(str(payload["ledger_uri"]), GapLedger),
        assessment=read_document(str(payload["assessment_uri"]), ReadinessAssessment),
        plan=read_document(str(payload["plan_uri"]), UpgradePlan),
        store=(store := build_fact_store()),
    )
    view = document.view
    provenance = document.provenance()

    # ABSENT IS A FIRST-CLASS ANSWER. A deployment with the brief switched off, an assemble
    # step that could not write, a payload that never carried the URI -- all three reach here
    # and all three mean "no advice", which the page states per step. Reading it must not be
    # able to fail the approval, so a broken document is treated as an absent one and said so
    # on the payload rather than raised.
    brief_uri = str(payload.get("brief_uri") or "").strip()
    advice, brief_error = None, ""
    if brief_uri:
        try:
            advice = advice_from_brief(read_document(brief_uri, UpgradeBrief))
        except Exception as exc:
            brief_error = f"{type(exc).__name__}: {exc}"
            print(f"upgrade brief could not be read: {brief_error}")

    # THE CATALOGUE, FROM THE SNAPSHOT THIS RUN SCANNED. Optional in the sense that a snapshot
    # that cannot be read leaves an empty index -- and an empty index makes the rule fall back
    # to placement for every row, which withholds MORE, never less. Failing closed here costs a
    # button; failing open would cost a lane.
    catalogue: dict[tuple[str, str], tuple[str, ...]] = {}
    snapshot_uri = str(payload.get("snapshot_uri") or "").strip()
    if snapshot_uri:
        try:
            catalogue = catalogue_index(
                read_document(snapshot_uri, ClusterSnapshot).eks_addon_catalogue
            )
        except Exception as exc:
            print(f"the snapshot's add-on catalogue could not be read: {type(exc).__name__}: {exc}")
    withheld = _withheld_until_the_cluster_moves(
        view, catalogue, _components_without_chart(inventory, store)
    )
    offered = tuple(
        s.order for s in view.steps if s.kind in AUTHORISABLE_KINDS and s.order not in withheld
    )
    return view, provenance, advice, brief_error, withheld, offered


def readiness_report(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Render the same page with no controls, mail it, and return -- for a deployment that
    executes no upgrade.

    THE DEPLOYMENT WITH `upgrade_stage_enabled = false` USED TO ASK FOR AN APPROVAL IT WOULD
    NEVER ACT ON, then reach `Done` without a word. A person approved changes to a cluster and
    heard nothing. So with the flag off the state machine calls this instead of the approval:
    the plan, each step's brief, the verdicts and the components as scanned, exactly as the
    approver would have seen them, with one sentence saying nothing here can be applied from this
    deployment -- and a mail that says the same and asks for nothing.

    NO TOKEN, NO GRANT, NO PAUSE. This is a plain invoke: the page is written, the mail is sent,
    and the run continues to `Done`. Nothing minted here can approve anything, because nothing is
    minted.

    The payload arrives NESTED under `readiness_report`, for `release_prep`'s reason: one Lambda
    function serves several jobs and an explicit key is how a reader of any of them can tell which
    invocation this is.
    """
    payload = event.get("readiness_report")
    if not isinstance(payload, dict):
        raise ValueError(
            "readiness_report expects the run under a 'readiness_report' key; "
            f"got keys {sorted(event)}"
        )
    settings = from_event(payload)
    run_id = _run_id(payload)
    view, provenance, advice, brief_error, withheld, offered = _render_inputs(payload)

    page = to_html(
        view,
        provenance,
        advice=advice,
        withheld=withheld,
        notice=(
            "Readiness report. This deployment does not execute upgrades "
            "(upgrade_stage_enabled is false), so nothing on this page can be approved or "
            "applied from here. Every change below is a recommendation, with its brief and the "
            "evidence it rests on."
        ),
    )
    page_digest = "sha256:" + hashlib.sha256(page.encode("utf-8")).hexdigest()
    page_uri = write_text(
        build_uri(settings.bucket, readiness_report_key(settings.cluster_name, run_id)),
        page,
        "text/html; charset=utf-8",
    )

    # A FAILURE HERE MUST NOT FAIL THE RUN, for the approval's reason: the page is written and
    # is the artifact; the mail is how somebody learns it exists.
    message_id, email_error = "", ""
    try:
        message_id = send_approval_email(
            build_readiness_email(
                run_id=run_id,
                cluster=settings.cluster_name,
                target_kubernetes_version=settings.target_kubernetes_version,
                page_url=presign_s3_get(page_uri),
                changes=len(offered),
                components=_components(view, offered),
            )
        )
    except Exception as exc:
        email_error = f"{type(exc).__name__}: {exc}"
        print(f"readiness report email could not be sent: {email_error}")

    return {
        "stage": "readiness-report",
        "run_id": run_id,
        "readiness_report_html_uri": page_uri,
        "readiness_report_page_digest": page_digest,
        "recommended_steps": list(offered),
        "withheld_steps": {str(order): reason for order, reason in withheld.items()},
        "brief_error": brief_error,
        "report_email_sent": bool(message_id),
        "report_email_error": email_error,
        "report_email_message_id": message_id,
        # Stated so a reader knows this deployment asked nobody anything.
        "execution_enabled": False,
    }


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Render the plan with its brief, mint the controls, store the token, and wait.

    Required in `event`: `task_token`, the run payload (`bucket`, `cluster_name`,
    `target_kubernetes_version`), and the four document URIs the report is built from. The
    brief URI is OPTIONAL -- see the module docstring.

    A RETURN VALUE FROM THIS HANDLER IS DISCARDED, because the state is
    `.waitForTaskToken`: the execution's next input is whatever the eventual
    `SendTaskSuccess` carries. The dict below is for the log and for a human invoking the
    function directly.
    """
    payload = _payload(event)
    settings = from_event(payload)
    task_token = str(event.get("task_token") or "").strip()
    run_id = _run_id(payload)

    view, provenance, advice, brief_error, withheld, offered = _render_inputs(payload)

    # MINTED BEFORE THE PAGE, because the page embeds forms built from these nonces, and the
    # token row must be written before any of them can be redeemed. Order: mint, build forms,
    # render, STORE, then send. A form that reaches a person before its grant is persisted is
    # a dead button whose failure reads as "already used" -- which sends someone looking for a
    # decision they never made.
    approver = approver_address()
    grants: dict[str, StepGrant] = {}
    nonces: dict[int, dict[str, str]] = {}
    by_order = {s.order: s for s in view.steps}
    for order in offered:
        step = by_order[order]
        component, installed, recommended, at_kubernetes = _step_parts(step)
        step_brief = advice.for_step(order) if advice is not None else None
        per_step: dict[str, str] = {}
        for action in ("approve", "reject"):
            nonce = new_nonce()
            grants[nonce] = StepGrant(
                action=action,
                approver=approver,
                step_order=order,
                step_kind=step.kind,
                component=component,
                installed=installed,
                recommended=recommended,
                at_kubernetes=at_kubernetes,
                brief_observed_at=(
                    step_brief.observed_at if step_brief is not None else ""
                ),
            )
            per_step[action] = nonce
        nonces[order] = per_step
    done_nonce = new_nonce()
    grants[done_nonce] = StepGrant(action="done", approver=approver)

    # WHERE A CLICK LANDS, AND WITHOUT IT A CLICK LOOKS LIKE NOTHING HAPPENED. A presigned S3
    # POST with no `success_action_redirect` answers 204 with an empty body, so the browser
    # showed a blank page and the only way to confirm a decision was recorded was to reload the
    # approval page and reason about it. S3 supports the redirect natively, which matters here:
    # this page carries NO JavaScript by design, so a button cannot disable itself or announce
    # anything client-side. The redirect is the only mechanism that reports back without
    # breaking that rule.
    #
    # Written BEFORE the forms are signed, because the policy embeds the redirect URL -- a form
    # signed against a page that does not exist yet would send somebody to a 404 at the moment
    # they most want reassurance.
    recorded_uri = write_text(
        build_uri(
            settings.bucket, upgrade_recorded_key(settings.cluster_name, run_id)
        ),
        _recorded_page(settings.cluster_name, run_id),
        "text/html; charset=utf-8",
    )
    recorded_url = presign_s3_get(recorded_uri)

    forms = {
        order: {
            action: {"url": form.url, "fields": form.fields}
            for action, nonce in per_action.items()
            if (
                form := build_upgrade_click_form(
                    settings.bucket, run_id, nonce, redirect_url=recorded_url
                )
            )
            is not None
        }
        for order, per_action in nonces.items()
    }
    forms = {order: actions for order, actions in forms.items() if actions}

    # THE FORM THAT LETS A PERSON FINISH, AND ITS ABSENCE WAS A DEAD END. The `done` grant was
    # minted from the first version of this stage and nothing ever built it a form, so the page
    # carried sixteen working controls and no way to hand the run back. Every click still
    # recorded a durable `StepDecision`, so the answers were never at risk -- but only the
    # watcher redeeming THIS grant reaches `SendTaskSuccess`, so the execution ran to its
    # timeout and failed. A missing control that presents as a timeout is the worst shape of
    # this bug: nothing in the run says a button was absent.
    done_form = build_upgrade_click_form(
        settings.bucket, run_id, done_nonce, redirect_url=recorded_url
    )
    done = (
        {"url": done_form.url, "fields": done_form.fields}
        if done_form is not None
        else None
    )

    page = to_html(
        view, provenance, advice=advice, forms=forms, done=done, withheld=withheld
    )
    # THE DIGEST OF THE BYTES WRITTEN, not of the document that produced them. Every decision
    # cites this so the trail says WHAT the person read; a digest of the value set would not
    # move when the renderer changed, and the renderer is what they actually saw.
    page_digest = "sha256:" + hashlib.sha256(page.encode("utf-8")).hexdigest()
    page_uri = write_text(
        build_uri(settings.bucket, upgrade_approval_key(settings.cluster_name, run_id)),
        page,
        "text/html; charset=utf-8",
    )

    # STORED EVEN WHEN NOTHING IS OFFERED. The execution is paused either way -- Step
    # Functions handed out a token the moment this state was entered -- so a run with no
    # authorisable step still needs something able to resume it. Resuming it here is tempting
    # and wrong: this function holds no SendTaskSuccess permission by design.
    store = build_step_token_store()
    store.pause_upgrade(
        PausedUpgrade(
            run_id=run_id,
            task_token=task_token,
            page_uri=page_uri,
            page_digest=page_digest,
            cluster=settings.cluster_name,
            target_kubernetes_version=settings.target_kubernetes_version,
            plan_digest=provenance.plan_digest,
            offered=offered,
            grants=grants,
        )
    )

    # A FAILURE HERE MUST NOT FAIL THE RUN. The page is written and the token is stored by the
    # time this runs, so the approval is already actionable; losing the notification is a
    # degradation, not a reason to fail an execution a person could still complete from the
    # console. The fact stage learned this by failing a run that held five valid grants and a
    # perfectly good page.
    message_id, email_error = "", ""
    briefed = len(advice.steps) if advice is not None else 0
    try:
        message_id = send_approval_email(
            build_upgrade_email(
                run_id=run_id,
                cluster=settings.cluster_name,
                target_kubernetes_version=settings.target_kubernetes_version,
                page_url=presign_s3_get(page_uri),
                waiting=len(offered),
                # WHAT `waiting` COUNTS IS STEPS, AND THE MAIL USED TO LEAVE THAT TO THE READER.
                # A component can hold more than one approvable step, and the AWS-managed
                # recommendations are steps too, so `len(offered)` exceeded the number of things
                # on the cluster: this run offered nine steps over seven components and mailed
                # "9 add-on change(s)" about a cluster a reader knows has seven add-ons. The
                # number was right and the sentence made it look wrong, which costs more than a
                # wrong number would -- it teaches a reader that the mail's arithmetic does not
                # reconcile, on the one mail they must open. So the second count goes with it and
                # `notify/upgrade_email._ask` states both whenever they differ.
                components=_components(view, offered),
                briefed=briefed,
                # AN AWS-MANAGED ADD-ON IS NOT AN UNBRIEFED ONE, and counting it as one made
                # the email say something false. `len(offered) - briefed` lumped both together,
                # so a run whose only missing briefs were coredns, kube-proxy, vpc-cni and
                # eks-pod-identity-agent mailed "4 of these have no advisory brief. Nothing was
                # read about what they break, so that is unknown rather than absent."
                #
                # Nothing was read because there is NOTHING TO READ: AWS publishes and
                # qualifies those versions against this Kubernetes minor itself, there is no
                # project release note and no Helm chart to reconfigure, and `brief_plan`
                # excludes them with that reason stated. The page has said so since the
                # EKS-managed rows were fixed; the email still claimed the opposite, which is
                # worse than silence -- it invites somebody to go looking for a brief that was
                # never owed, or to distrust a version AWS has already qualified.
                # AND AN UNSUPPORTED-AT-TARGET ROW WITH NOTHING TO MOVE TO IS NOT AN UNBRIEFED ONE
                # EITHER, for the same reason one step further on. A brief describes what breaks
                # when a component crosses from one version to another; a component already
                # running the newest release anybody recorded has no crossing. Counting it as
                # unbriefed would mail "nothing was read about what it breaks, so that is unknown
                # rather than absent" about a component nobody could have read anything useful
                # about -- and would send somebody hunting for a brief that was never owed, over
                # the one row on the page whose meaning is already the hardest.
                #
                # ONLY THE ONES THAT GOT NO BRIEF, WHICH IS WHY `advice` IS PASSED IN. These rows
                # ARE briefed now, on the move they can still make, and subtracting a row that is
                # already counted in `briefed` would take the same component off the total twice
                # -- mailing a smaller number than the truth about how much was read.
                unbriefed=max(
                    len(offered)
                    - briefed
                    - _aws_managed(view, offered)
                    - _unsupported_at_target(view, offered, advice),
                    0,
                ),
            )
        )
    except Exception as exc:
        email_error = f"{type(exc).__name__}: {exc}"
        print(f"upgrade approval email could not be sent: {email_error}")

    return {
        "stage": "upgrade-approval",
        "run_id": run_id,
        "upgrade_approval_html_uri": page_uri,
        "upgrade_approval_page_digest": page_digest,
        "awaiting_approval": len(offered),
        # WHICH STEPS, never the nonces that decide them. A nonce in a stage result is a nonce
        # in the execution history, readable by anything holding `states:GetExecutionHistory`
        # -- and that would hand any such reader a working
        # approval and defeat the reason the links go by email at all.
        "offered_steps": list(offered),
        # STEPS SHOWN WITHOUT A CONTROL, and why. Recorded on the payload so the report stage
        # can say "not applicable yet" about them rather than "left undecided".
        "withheld_steps": {str(order): reason for order, reason in withheld.items()},
        "briefed_steps": briefed,
        "brief_error": brief_error,
        "approval_email_sent": bool(message_id),
        "approval_email_error": email_error,
        "approval_email_message_id": message_id,
        # Stated so a reader of the log knows the run really is stopped here, rather than
        # inferring it from the absence of a later stage.
        "paused": True,
    }


def _components(view: Any, offered: tuple[int, ...]) -> int:
    """How many DISTINCT components the offered steps touch, so the mail can say both numbers.

    READ OFF `StepRow.component`, the same field the renderer puts in the row, so the mail's
    second number and the page's rows count the same things. Distinct, because the whole point is
    that a component can hold more than one step -- a genuine interleaved multi-hop is two steps
    for one component, and `order/collapse.py` keeps exactly that shape.

    A step with no component is not counted rather than counted as one: a `CLUSTER_UPGRADE` is
    not in `AUTHORISABLE_KINDS` so it never reaches here, but an unparsed row must not become a
    phantom add-on in the arithmetic.

    Returns 0 rather than raising, for `_aws_managed`'s reason stated in full there. 0 means "not
    counted" to `notify/upgrade_email._ask`, which then prints the single-number sentence instead
    of inventing a second one.
    """
    try:
        return len(
            {
                step.component
                for step in view.steps
                if step.order in offered and getattr(step, "component", None)
            }
        )
    except Exception:
        return 0


def _aws_managed(view: Any, offered: tuple[int, ...]) -> int:
    """How many offered steps are EKS-managed add-ons, so the mail does not miscount them.

    READ OFF `StepRow.aws_managed`, which `view/build.py` joins from the inventory's install
    method -- never inferred from `-eksbuild.` appearing in a version string. The renderer makes
    the same distinction for the same reason; this is the count behind the sentence.

    Returns 0 rather than raising if the view predates the field, because the email is the one
    thing in this stage that must never be able to fail an approval that is already actionable.
    """
    try:
        return sum(
            1 for step in view.steps
            if step.order in offered and getattr(step, "aws_managed", False)
        )
    except Exception:
        return 0


def _unsupported_at_target(view: Any, offered: tuple[int, ...], advice: Any = None) -> int:
    """How many offered rows are unreachable components that nothing was owed a brief for.

    READ OFF `StepRow.unsupported_at_target`, the same property the renderer decides the row's
    wording from, so the sentence in the mail and the sentence on the page rest on one field.

    ROWS THAT DID GET A BRIEF ARE NOT COUNTED, and that is what `advice` is for. Such a row is
    already inside the mail's `briefed` total, so counting it here as well would subtract one
    component twice from the same arithmetic and mail a number lower than the truth. The
    distinction is exactly the one the page draws in its Details column: a component that can
    still move gets a brief, one already on its newest recorded release has no crossing to brief.

    Returns 0 rather than raising, for `_aws_managed`'s reason stated in full there: the mail is
    the one thing in this stage that must never be able to fail an approval which is already
    actionable, and a miscounted sentence is a smaller loss than an unsent notification about a
    page holding live grants.
    """
    try:
        briefed = (
            {step.step_order for step in advice.steps} if advice is not None else set()
        )
        return sum(
            1 for step in view.steps
            if step.order in offered
            and getattr(step, "unsupported_at_target", False)
            and step.order not in briefed
        )
    except Exception:
        return 0


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    """The run payload, however the state handed it over.

    THE WHOLE PAYLOAD ARRIVES NESTED UNDER `run`, and that is a decision about Step Functions
    rather than a preference. A `.waitForTaskToken` state must name its Payload explicitly, so
    that it can inject `$$.Task.Token` -- and a `"brief_uri.$" = "$.brief_uri"` in that block
    FAILS THE STATE when the field is absent. It is absent on exactly one path: `Brief
    assemble`'s catch, which sets `$.brief_error` and leaves the payload without a brief. That
    is the degraded path the whole brief stage was built to survive, and naming the field would
    have turned it into the one thing a brief is forbidden to be -- a reason the run stops.

    So the state passes `"run.$" = "$"` and the token beside it. Top-level keys still win, so
    the handler is invocable directly with a flat dict for a local render.
    """
    nested = event.get("run")
    if not isinstance(nested, dict):
        return event
    return {**nested, **{k: v for k, v in event.items() if k != "run"}}


def _step_parts(step: Any) -> tuple[str, str, str, str]:
    """`(component, installed, recommended, at_kubernetes)` for one step.

    THE SAME PARSE THE RENDERER DOES, and the duplication is deliberate rather than shared.
    `view/render/html.py` may not be imported for its private helpers by a stage -- the
    render boundary runs the other way -- and a stage importing a renderer's internals to
    read a plan would be the inverse of the guard that keeps a renderer out of a stage
    document. Both read the sentence `view/build.py` composed; neither re-derives it.

    Falls back to the whole description rather than a guess. An unparsed grant records a
    component field that reads oddly; an invented one records a step nobody was shown.

    THE FIRST TWO PATTERNS ARE THE UNSUPPORTED-AT-TARGET ROW'S TWO SHAPES, AND NEITHER
    `recommended` REACHES THE TARGET. Both belong to a component no series of which supports the
    target Kubernetes minor -- keda, whose newest recorded series stops at 1.36 -- and both are
    carried into the grant because the durable `StepDecision` built from it is the record of what
    a person was shown when they accepted running that component unsupported.

    They differ in whether there is a move to record. The FIRST is the move shape: the fact
    records a release strictly newer than what is installed, so approving really does upgrade the
    component that far, and the grant has to say which release -- otherwise the decision trail for
    "we upgraded keda to 2.20.2 and accepted it unsupported at 1.36" loses the version. Its
    `installed` is the series the move starts from, matching every other move in the trail, and
    the exact installed release is in the sentence beside it. The SECOND is the dead-end shape:
    nothing newer is recorded, so `recommended` is a release with nothing to move to.

    MATCHED FIRST, for the reason `view/render/html.py` states about the same four patterns: an
    unsupported sentence is prose about an upgrade that does not reach the target, the
    optional-move pattern searches for the bare word "upgrade" anywhere in a description, and
    first match wins -- so the specific patterns are offered before the general ones rather than
    relying on the general ones never happening to match.

    `recommended` IS EMPTY, NOT NONE, when the fact records no release at all. The dead-end
    pattern's release clause is an alternation so that case parses rather than falling through,
    and `StepGrant` takes "" for a value it does not have -- the same shape the fallback below
    uses, so a reader of the stored grant cannot tell a missing value from a failed parse by
    its type and does not have to.
    """
    import re

    for pattern in (
        r"Unsupported at target: (?P<component>.+?) has no recorded version series "
        r"supporting Kubernetes (?P<at>[\d.]+)\. Move (?P<installed>\S+) → "
        r"(?P<recommended>[^,\s]+), the newest recorded release, which does NOT cover "
        r"Kubernetes [\d.]+ either\. Installed \S+\.",
        r"Unsupported at target: (?P<component>.+?) has no recorded version series "
        r"supporting Kubernetes (?P<at>[\d.]+)\. Installed (?P<installed>[^;]+); "
        r"(?:newest recorded release (?P<recommended>\S+) does NOT cover target"
        r"|no release is recorded)\.",
        r"Move (?P<component>[^:]+): (?P<installed>\S+) → \S+ "
        r"\(install (?P<recommended>[^)]+)\) at Kubernetes (?P<at>[\d.]+)",
        r"upgrade (?P<component>\S+) (?P<installed>\S+) → (?P<recommended>\S+) "
        r"at Kubernetes (?P<at>[\d.]+)",
    ):
        found = re.search(pattern, step.description)
        if found is not None:
            return (
                found["component"],
                found["installed"],
                found["recommended"] or "",
                found["at"],
            )
    return step.description, "", "", ""


def _run_id(event: dict[str, Any]) -> str:
    """This execution's identifier, as the resume path will look it up.

    Step Functions supplies it through the state's `Parameters` from `$$.Execution.Name`,
    which is unique per execution and is what an operator sees in the console -- so the id in
    the page's URL, the id in the token store and the id in the console are one value.

    Falls back to the payload's own `run_id` and then to the snapshot instant, so the handler
    is invocable outside a state machine for a local render. It never invents a random id: a
    resume path keyed on something the caller cannot name again would be unusable.
    """
    for key in ("execution_name", "run_id"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    return str(event.get("snapshot_captured_at") or "local").strip() or "local"


def now() -> datetime:
    """The clock, in one place so a test can hold it still."""
    return datetime.now(UTC)


def _recorded_page(cluster: str, run_id: str) -> str:
    """The page a browser lands on the instant a click is recorded.

    WHY A PAGE AND NOT A DISABLED BUTTON. This report carries no JavaScript by design, so a
    control cannot grey itself out or announce anything after it is pressed. Before this
    existed, a presigned POST answered 204 with an empty body: the tab went blank and the only
    way to confirm a decision had landed was to reload the approval page and reason about it.
    S3's own `success_action_redirect` reports back without a line of script.

    DELIBERATELY NOT A RECEIPT. It names no step, no version and no decision, because one page
    serves every control on the run -- and a page claiming "step 3 approved" would be wrong the
    moment it were reached from a different button. The durable record of WHAT was decided is
    the `StepDecision` in S3; this only says the click arrived.

    IT TELLS THE READER WHAT TO DO NEXT, which is the part a blank page could not: go back and
    decide the rest, and press Done when finished, because nothing resumes the run until then.
    """
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Recorded &mdash; {cluster}</title><style>"
        ":root{color-scheme:dark;--bg:#14161a;--fg:#e6e8eb;--mut:#9aa1a9;"
        "--edge:#2c3138;--ok:#5cc46c}"
        "body{margin:0;background:var(--bg);color:var(--fg);"
        "font:15px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif;"
        "display:grid;place-items:center;min-height:100vh;padding:1.5rem}"
        "main{max-width:34rem;border:1px solid var(--edge);border-radius:12px;padding:1.6rem}"
        "h1{margin:0 0 .6rem;font-size:1.15rem}"
        ".ok{color:var(--ok);font-weight:600}"
        "p{margin:.6rem 0;color:var(--mut)}"
        "code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85rem;"
        "color:var(--fg)}"
        "</style></head><body><main>"
        "<h1><span class=ok>&#10003;</span> Decision recorded</h1>"
        "<p>It is written down and will survive this tab, this browser and this run.</p>"
        "<p><b>Go back to the approval page to decide the rest.</b> Each control is "
        "single-use, so a decision you have already made cannot be changed by pressing it "
        "again &mdash; and anything you leave undecided is offered again on the next run "
        "rather than read as a refusal.</p>"
        "<p>Nothing changes on the cluster until you press <b>Done</b>, which hands the run "
        "back so it can carry on.</p>"
        f"<p>Cluster <code>{_e(cluster)}</code> &middot; run <code>{_e(run_id)}</code></p>"
        "</main></body></html>"
    )


def _e(value: str) -> str:
    """Escape a value that came from a cluster name or an execution name."""
    import html

    return html.escape(value or "")
