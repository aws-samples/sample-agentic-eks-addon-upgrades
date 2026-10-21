"""Who may authorise this release, and what exactly they are being asked to authorise.

Invoked by the `Release approvers` state, immediately before `Release gate` starts the SSM
Automation. It answers two questions and writes nothing:

    approvers   the principals the CLUSTER already trusts to administer it, as a LIST
    changes     what moves, as ONE STRING -- `coredns 1.11.1 -> 1.12.0, vpc-cni 1.18.5 -> 1.19.2`

THE TWO SHAPES DIFFER BECAUSE THE DOCUMENT CONSUMES THEM DIFFERENTLY, and getting that wrong
cost an apply. `Approvers` is a `StringList` that `aws:approve` reads AS a list. `Changes` is
read inside two mail bodies, and SSM refuses a `StringList` in a substring position -- the apply
failed with `Parameter "Changes" is "STRING_LIST" type and can't be used as a substring
parameter`. So the joining happens here, in code a test can drive, rather than in the document.

THE APPROVER LIST IS DERIVED, NOT CONFIGURED, AND THAT IS THE WHOLE POINT. It used to be
`var.release_approver_arns` in `terraform.tfvars`: a hand-kept list of ARNs. Both ways
that drifts are silent. A principal who loses cluster admin keeps authorising releases until
somebody remembers the tfvars, and a new cluster admin is refused even though AWS already
considers them able to change the cluster by hand. Reading the cluster's own access entries
means the gate's authorisation boundary cannot disagree with the cluster's, because there is
only one boundary.

AN ALLOWLIST, NEVER A DENYLIST. `_ADMIN_ACCESS_POLICIES` names the two policies that count,
so a policy this module has not heard of is NOT admin -- including every policy AWS adds
after today, and including `AmazonEKSArgoCDClusterPolicy`, which is real, cluster-scoped and
held by a capability role on the cluster this runs against. A "not view, not edit" check
would hand that role the power to authorise a cluster change.

FAIL CLOSED, WITH NO FALLBACK AND NO SKIP. An empty resolved list raises
`ApproversUnresolved`, the state machine catches it, and the run fails at `Release rejected`.
There is deliberately no "nobody is configured, so allow" branch: `stages/approve.py` has
exactly that branch for FACT approvals and it is a defect this must not copy. An AWS error on
any of the three calls propagates for the same reason -- a list that could not be read is not
an empty list, and swallowing the error per entry would resolve a PARTIAL list, which is the
worst of the three outcomes because it looks like an answer.

`aws-auth` IS OUT OF SCOPE BY DECISION. A cluster admin mapped only in the `aws-auth`
ConfigMap has no access entry, so this does not see them and they cannot authorise a release.
No ConfigMap is read here and none should be: the remedy for such an admin is an access entry,
which is also how AWS itself now expects cluster access to be expressed.

IT LIVES IN THE UPGRADE APPROVAL FUNCTION, WHICH IS A DEPLOYMENT FACT WORTH STATING. No new
Lambda function is created for this; `main.stage_upgrade_approval` dispatches to it on an
explicit payload key. That function is the right host rather than a convenient one: it is the
only one whose role may already read `plans/`, and the two jobs are the same job seen twice --
collect intent on the page, then prepare the authorisation of that intent. It gains exactly
three read-only `eks:` actions and no write anywhere.

WHY THE CHANGE LINES COME FROM THE PLAN DOCUMENT. `Summary` carries counts -- "2 approved, 1
rejected" -- and counts are not checkable: an approver cannot verify a number against a
cluster. `coredns 1.11.1 -> 1.12.0` they can, in one command. The lines are built from the
Stage 5 plan's own fields rather than from the sentences the page rendered, so nothing here
re-parses prose, and rather than from the per-step decision objects, because those belong to
`upgrade_decision_watcher` and this role must not reach `upgrade-decisions/`.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

from ..facts.store import FactStore
from ..models import ContractModel
from ..order.plan import StepKind, UpgradePlan
from ..report import InventoryReport
from ..storage import read_document
from ..upgrade import ReleaseStep

REGION = os.environ.get("AWS_REGION", "us-east-1").strip()

# THE ALLOWLIST. Two policies, both AWS-managed, both meaning "may administer this cluster".
# `AmazonEKSClusterAdminPolicy` is full cluster admin; `AmazonEKSAdminPolicy` is admin over
# everything a namespace-scoped admin can reach, which at cluster scope is what changing an
# add-on takes. Nothing else qualifies, and adding a third is a deliberate edit with a test.
_ADMIN_ACCESS_POLICIES = frozenset(
    {
        "AmazonEKSClusterAdminPolicy",
        "AmazonEKSAdminPolicy",
    }
)

# Only a STANDARD entry names a principal a person can assume. `EC2_LINUX`, `EC2_WINDOWS`,
# `FARGATE_LINUX` and `HYBRID_LINUX` are node identities: a kubelet's, not a human's.
_STANDARD = "STANDARD"

# An access policy is authoritative for the whole cluster only at cluster scope. The same ARN
# scoped to a namespace is administration of that namespace, and an add-on upgrade is not.
_CLUSTER_SCOPE = "cluster"

# What SSM accepts for one `AutomationParameterValue`. Not a limit this pipeline expects to
# reach -- see `_joined`, which refuses rather than truncating if it ever is.
_PARAMETER_VALUE_LIMIT = 512


class ApproversUnresolved(RuntimeError):
    """No principal on this cluster may authorise a release, so the run must fail.

    A distinct type rather than a bare `RuntimeError` so the failure is legible in the state
    machine's error output and in a test's `pytest.raises`. It is raised for the empty list and
    for nothing else -- an AWS error propagates as itself, because "could not read" and "read,
    and there are none" are different problems even though both refuse the run.
    """


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Resolve the approvers and name the changes. Raises rather than returning a partial.

    The payload arrives NESTED under `release_prep` -- see `main.stage_upgrade_approval` on
    why: the same Lambda function serves the paused approval stage, and an explicit key is how
    a reader of either side can tell which job an invocation is.
    """
    payload = event.get("release_prep")
    if not isinstance(payload, dict):
        raise ValueError(
            "release_prep expects the run under a 'release_prep' key; "
            f"got keys {sorted(event)}"
        )

    cluster = str(payload.get("cluster") or "").strip()
    if not cluster:
        raise ValueError(
            "release_prep was given no cluster, and the approver list is a property of one"
        )

    plan_uri = str(payload.get("plan_uri") or "").strip()
    if not plan_uri:
        raise ValueError(f"release_prep was given no plan_uri for cluster {cluster}")

    approved = payload.get("approved_steps") or []
    plan = _read_plan(plan_uri)
    # THE INVENTORY IS OPTIONAL HERE, AND ITS ABSENCE FAILS CLOSED. Without it no installed
    # versions are known, so no unsupported-at-target move can be minted, and an approved row of
    # that kind is still refused below exactly as before.
    inventory_uri = str(payload.get("inventory_uri") or "").strip()
    extra = (
        unsupported_moves(plan, _read_inventory(inventory_uri), _fact_store())
        if inventory_uri
        else ()
    )
    changes = approved_changes(plan, approved, extra=extra)
    approvers = cluster_admins(cluster, client=_eks(REGION))

    return {
        "stage": "release-prep",
        "cluster": cluster,
        "run_id": str(payload.get("run_id") or ""),
        # A LIST, because `Approvers` is a `StringList` that `aws:approve` consumes AS a list,
        # and the SSM `Parameters` map is name -> list of strings. It is passed on with no
        # `States.Array` around it for exactly that reason.
        "approvers": list(approvers),
        # A STRING, JOINED HERE RATHER THAN A LIST PASSED ALONG, AND AWS IS WHY. `Changes` was
        # declared `StringList` and the apply failed: `Parameter "Changes" is "STRING_LIST" type
        # and can't be used as a substring parameter`. SSM substitutes a list only where a list
        # is consumed as one, and the mail bodies read this inside a sentence -- a substring
        # position by construction. So the joining happens on this side of the boundary, where
        # it is testable, rather than in an ASL intrinsic where it would not be.
        "changes": _joined(changes),
    }


def _joined(changes: tuple[str, ...]) -> str:
    """The change lines as one string, or a refusal if they could not be passed whole.

    `, ` RATHER THAN A NEWLINE OR A `<br>`, because ONE value feeds BOTH mail parts. A `<br>`
    would be markup in the text part, and a newline collapses to a space in the HTML one, so the
    separator has to read as prose in both: `coredns 1.11.1 -> 1.12.0, vpc-cni 1.18.5 -> 1.19.2`.
    The arrows carry the meaning; the commas only separate.

    TOO LONG IS A REFUSAL, NOT A TRUNCATION, and it is the one risk joining introduced that the
    list form did not have: an SSM automation parameter value is capped at 512 characters, and
    where each line used to have its own budget they now share one. A truncated list would show
    an approver FEWER changes than the release carries and then record that they authorised all
    of them -- the defect `approved_changes` raises for, arriving by a different road. A plan
    this pipeline produces runs to seven steps and about 250 characters, so this is a guard and
    not a limit anybody should meet.
    """
    joined = ", ".join(changes)
    if len(joined) > _PARAMETER_VALUE_LIMIT:
        raise ValueError(
            f"the approved changes render to {len(joined)} characters, over the "
            f"{_PARAMETER_VALUE_LIMIT}-character SSM parameter value limit. Refusing rather "
            "than truncating: a shortened list would have somebody authorise changes the mail "
            "never showed them."
        )
    return joined


def cluster_admins(cluster: str, *, client: Any) -> tuple[str, ...]:
    """Every principal the cluster's access entries make a cluster administrator.

    Sorted, because `ListAccessEntries` promises no order and an unsorted list would make the
    automation's recorded `Approvers` parameter differ between two runs of an unchanged
    cluster -- which reads as a change to whoever compares them.

    Three calls per entry at most, all read-only: the entry itself for its `type`, and its
    associated policies for the grant. Both listings are paginated, and both are followed --
    an unread second page is a missing approver, and a missing approver is a person who could
    authorise yesterday being refused today with nothing explaining why.
    """
    admins: list[str] = []
    for principal in _access_entries(client, cluster):
        entry = client.describe_access_entry(
            clusterName=cluster, principalArn=principal
        )
        if str((entry.get("accessEntry") or {}).get("type") or "") != _STANDARD:
            continue
        if _has_cluster_admin(client, cluster, principal):
            admins.append(principal)

    if not admins:
        raise ApproversUnresolved(
            f"no principal holds cluster admin on {cluster} via an access entry, so no "
            "release can be authorised. This is a refusal, not a default: an admin mapped "
            "only in the aws-auth ConfigMap is deliberately not read, and there is no "
            "fallback approver anywhere in this design."
        )
    return tuple(sorted(admins))


class StepProvenance(ContractModel):
    """What one component's lane needs beyond the move itself, gathered at approval time.

    From the Stage 2 inventory: the Helm release name, namespace and chart the objects were
    installed under, and for a managed add-on the label selector that finds its pods. From the
    APPROVED FACT: where that chart is published -- a value a person approved beside its quote.
    `release_record.py` builds one per component; `_step_record` copies it onto the `ReleaseStep`.

    EVERYTHING OPTIONAL, BECAUSE ABSENCE IS INFORMATION. A Helm component whose fact carries no
    chart repository is recorded without one, and the lane planner skips it with exactly that
    reason. Filling a default in here would turn "nobody approved a source" into "helm repo add
    with a guess".
    """

    release_name: str | None = None
    namespace: str | None = None
    pod_selector: str | None = None
    chart_repository: str | None = None
    chart_name: str | None = None


def approved_steps(
    plan: UpgradePlan,
    orders: Iterable[int],
    *,
    install_methods: Mapping[str, str],
    provenance: Mapping[str, StepProvenance] | None = None,
    extra: Iterable[Any] = (),
) -> tuple[ReleaseStep, ...]:
    """The approved steps as MOVES: order, component, versions, kind, install method, mail line.

    `provenance` carries what the lane acts on -- release name, namespace, chart -- keyed like
    `install_methods`. Optional because a record without it is still a true record of what was
    authorised; the planner then skips every Helm step for want of a chart, which it reports.

    `install_methods` IS REQUIRED, NOT OPTIONAL, AND THAT IS THE POINT OF THE FIELD. It maps a
    component to `HELM`, `EKS_MANAGED_ADDON` or `MANIFEST`, and the executor calls a different API
    for each -- `helm upgrade` against a release, `UpdateAddon` against an add-on. A record that
    left it out would be a record somebody has to guess an API from, so there is no way to build
    one without it: `ReleaseStep` refuses a component step with no method, and this signature
    refuses a caller who has not read the inventory.

    ONLY THE RECORD PATH CALLS THIS. The authorisation mail needs sentences rather than fields, and
    is built BEFORE the approval -- so `approved_changes` derives the lines from `_line` directly
    and `Release approvers` reads no second document. `_line` is the shared derivation, which is
    what keeps the mail and the record from disagreeing about what moves.

    A COMPONENT THE MAP DOES NOT NAME IS A REFUSAL, not a `None`. The plan and the inventory are
    products of the same run, so a component in one and not the other is an inconsistency -- and
    the wrong guess here means the wrong API called against a live cluster.

    IN THE PLAN'S ORDER, not the order the approvals arrived in. Sequence is a property of the
    plan, and a list that reads in a different order than the plan reads invites somebody to think
    the sequence changed.
    """
    steps = _ordered_steps(plan, orders, extra)
    unknown = sorted(
        {s.component for s in steps if s.component and s.component not in install_methods}
    )
    if unknown:
        raise ValueError(
            f"the inventory for {plan.cluster.name} does not name {unknown}, so how those "
            "components are installed is unknown. Refusing rather than recording a release the "
            "executor would have to guess an API for."
        )
    return tuple(_step_record(s, install_methods, provenance or {}) for s in steps)


def approved_changes(
    plan: UpgradePlan, orders: Iterable[int], *, extra: Iterable[Any] = ()
) -> tuple[str, ...]:
    """One line per approved step, for the mail: what moves, from what, to what.

    THE SAME `_line` THE RECORD'S `summary` USES, which is what makes drift impossible: the two
    readers share the derivation rather than agreeing by inspection. It does NOT go through
    `approved_steps`, because that needs the install methods and a sentence does not name an API.
    """
    return tuple(_line(step) for step in _ordered_steps(plan, orders, extra))


def unsupported_moves(
    plan: UpgradePlan, inventory: InventoryReport, store: FactStore
) -> tuple[Any, ...]:
    """The approvable unsupported-at-target rows, as steps the release can carry.

    A component no recorded series of which supports the target is not a plan step: the plan
    leaves it in `unsequenceable`, digest-pinned, and that stays true. But when the fact records
    a release newer than what is installed, the approval page offers "upgrade it as far as it
    goes, unsupported at the target", and an approval there must become a real lane rather than
    an unknown step number that rejects the whole release (run6).

    THE BRIEF STAGE'S FUNCTION, NOT A COPY. `upgrade_brief.unsupported_steps` numbers these rows
    through `view.build.unsupported_at_target`, the one place their orders are minted, so the
    order a person approved, the move the mail names and the move the lane makes are the same.
    A component already on the newest recorded release is absent: there is nothing to move to,
    and the approval page withholds its control.
    """
    from ..view.build import installed_from_inventory
    from .upgrade_brief import unsupported_steps

    return unsupported_steps(plan, installed_from_inventory(inventory), store)


def _ordered_steps(
    plan: UpgradePlan, orders: Iterable[int], extra: Iterable[Any] = ()
) -> tuple[Any, ...]:
    """The approved steps, in the plan's order, or a refusal.

    `extra` are the unsupported-at-target moves from `unsupported_moves`, numbered after every
    real plan step, so plan order is preserved and they run last.

    AN UNKNOWN ORDER IS A REFUSAL, AND IT IS THE SAME REFUSAL AT BOTH ENDS. A step number neither
    the plan nor `extra` contains means the plan and the decisions disagree about what this run
    is. Before the approval that means mailing a shorter list -- somebody authorising only what
    they could see -- so it raises and the run fails at `Release rejected`. After the approval it
    means a record that names a move it cannot describe, which is worse than no record: it raises
    there too, and the execution FAILS rather than storing a step with empty fields.
    """
    wanted = {int(o) for o in orders}
    by_order = {s.order: s for s in plan.steps}
    for step in extra:
        if step.order in by_order:
            raise ValueError(
                f"unsupported-at-target move {step.order} collides with a plan step for "
                f"{plan.cluster.name}"
            )
        by_order[step.order] = step
    missing = sorted(wanted - set(by_order))
    if missing:
        raise ValueError(
            f"approved steps {missing} are not in the plan for {plan.cluster.name}; "
            "the plan and the recorded decisions disagree about this run"
        )
    return tuple(by_order[o] for o in sorted(wanted))


def _step_record(
    step: Any,
    install_methods: Mapping[str, str],
    provenance: Mapping[str, StepProvenance],
) -> ReleaseStep:
    """One `PlanStep` as the `ReleaseStep` that outlives the plan.

    COPIED, NOT REFERENCED, for `StepDecision`'s reason: a record that says "step 3" means
    whatever step 3 means the next time somebody reads the plan.

    `to_version` IS `recommended_version` WHEN THERE IS ONE, because that is the concrete release
    Stage 6.5 says to install; `to_series` is what is COMPATIBLE and may be a series (`1.19.x`),
    which is not a thing anybody installs. Both are carried, so an executor never has to guess
    which of the two a single field meant.
    """
    return ReleaseStep(
        order=step.order,
        kind=step.kind,
        component=step.component,
        from_series=step.from_series,
        to_series=step.to_series,
        to_version=step.recommended_version,
        from_kubernetes=step.from_kubernetes,
        to_kubernetes=step.to_kubernetes,
        at_kubernetes=step.at_kubernetes,
        # Set only for a step that HAS a component, which is what `ReleaseStep`'s own validator
        # requires in both directions: the cluster hop installs nothing.
        install_method=install_methods[step.component] if step.component else None,
        summary=_line(step),
        **_provenance_fields(step, provenance),
    )


def _provenance_fields(step: Any, provenance: Mapping[str, StepProvenance]) -> dict[str, Any]:
    """The lane's fields for this step, or nothing for a step with no component."""
    if not step.component:
        return {}
    found = provenance.get(step.component)
    if found is None:
        return {}
    return {
        "release_name": found.release_name,
        "namespace": found.namespace,
        "pod_selector": found.pod_selector,
        "chart_repository": found.chart_repository,
        "chart_name": found.chart_name,
    }


def _line(step: Any) -> str:
    """`component from -> to`, or the cluster hop's own versions.

    THE TARGET IS `recommended_version` WHEN THERE IS ONE, because that is the concrete
    release Stage 6.5 says to install; `to_series` is what is COMPATIBLE and may be a series
    (`1.19.x`), which is not a thing anybody installs.

    A STEP WITH NO RECORDED INSTALLED VERSION SAYS NOTHING RATHER THAN `None`. An advisory step
    may carry no `from_series`, and `coredns None -> 1.12.0` in an authorisation mail is worse
    than `coredns -> 1.12.0`: the first looks like a value, the second like an absence.
    """
    if step.kind is StepKind.CLUSTER_UPGRADE:
        return f"Kubernetes {step.from_kubernetes} -> {step.to_kubernetes}"
    target = step.recommended_version or step.to_series or "?"
    if step.from_series:
        return f"{step.component} {step.from_series} -> {target}"
    return f"{step.component} -> {target}"


def _access_entries(client: Any, cluster: str) -> tuple[str, ...]:
    """Every access entry principal on the cluster, following `nextToken`."""
    found: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"clusterName": cluster}
        if token:
            kwargs["nextToken"] = token
        page = client.list_access_entries(**kwargs)
        found.extend(str(p) for p in page.get("accessEntries") or [])
        token = page.get("nextToken")
        if not token:
            return tuple(found)


def _has_cluster_admin(client: Any, cluster: str, principal: str) -> bool:
    """Whether this principal holds an admin access policy AT CLUSTER SCOPE."""
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"clusterName": cluster, "principalArn": principal}
        if token:
            kwargs["nextToken"] = token
        page = client.list_associated_access_policies(**kwargs)
        for associated in page.get("associatedAccessPolicies") or []:
            scope = (associated.get("accessScope") or {}).get("type") or ""
            if str(scope).lower() != _CLUSTER_SCOPE:
                continue
            if _is_admin_policy(str(associated.get("policyArn") or "")):
                return True
        token = page.get("nextToken")
        if not token:
            return False


def _is_admin_policy(arn: str) -> bool:
    """Whether the ARN is one of the two AWS-MANAGED admin cluster access policies.

    THE WHOLE ARN IS CHECKED, NOT ITS SUFFIX. Matching on the trailing name alone would accept
    `arn:aws:iam::<account>:policy/AmazonEKSClusterAdminPolicy` -- an account-owned policy that
    anybody holding `iam:CreatePolicy` can name whatever they like. So the shape is required:
    the `eks` service, the `aws` owner (which is what makes it AWS-managed rather than the
    account's), and the `cluster-access-policy` resource type.

    THE PARTITION IS NOT PINNED. `aws`, `aws-cn` and `aws-us-gov` all spell these policies the
    same way, and hardcoding `arn:aws:` would silently resolve an empty approver list in any
    other partition -- a refusal whose cause named nothing.
    """
    parts = arn.split(":")
    if len(parts) != 6 or parts[0] != "arn":
        return False
    _, _partition, service, _region, owner, resource = parts
    if service != "eks" or owner != "aws":
        return False
    kind, _, name = resource.partition("/")
    return kind == "cluster-access-policy" and name in _ADMIN_ACCESS_POLICIES


def _read_plan(uri: str) -> UpgradePlan:
    """The Stage 5 document. A seam, so the tests exercise the derivation without S3."""
    return read_document(uri, UpgradePlan)


def _read_inventory(uri: str) -> InventoryReport:
    """The Stage 2 document, for installed versions. A seam for the same reason."""
    return read_document(uri, InventoryReport)


def _fact_store() -> FactStore:
    """The approved fact store, for the newest recorded release. A seam for the same reason."""
    from ..storage.facts import build_fact_store

    return build_fact_store()


def _eks(region: str) -> Any:
    """The EKS client. A seam for the same reason, and the only place boto3 is touched."""
    import boto3

    return boto3.client("eks", region_name=region or None)
