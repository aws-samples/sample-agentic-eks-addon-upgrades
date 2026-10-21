"""The release record: one authorisation, written as JSON somebody can read without a parser.

Invoked by the `Release assembled` state, between `Release approved` and `Release recorded`. It
takes the automation's own outputs, the Stage 5 plan and the Stage 2 inventory, and returns the
document the state machine writes to `releases/cluster=<cluster>/run=<run>/release.json`. It reads
those two documents and writes nothing: the object is written by `aws-sdk:s3:putObject` from the
state machine's own role, so no Lambda in this deployment can manufacture an authorisation.

WHY THIS EXISTS AT ALL, WHICH IS A SHORT STORY WITH A DATE ON IT. The first real authorisation, on
a test run, stored 549 bytes
that took THREE parses to reach the principal who authorised it:

    json.loads(body)                          -> a str, because the Body was serialised twice
    json.loads(that)                          -> the object
    json.loads(obj["approver_decisions"][0])  -> the decision, because SSM emits JSON text

and its `approved_steps` was `[1, 2, 4, 7]`, which records that something was authorised without
recording what. The outer encoding was a one-line fix in ASL. The other two are not expressible
there at all, and that is the reason this module is code:

    ASL CAN COPY A STRING BUT CANNOT LOOK INSIDE ONE. Decoding the decisions needs a parse per
    element; ASL has no map-over-an-array intrinsic, `States.StringToJson` takes a single string,
    and a positional version would hardcode how many approvers there are -- which the resolver
    decides per cluster, so a three-admin cluster would silently lose the third. The alternatives
    were a `Map` state or this.

    TWO DOCUMENTS ARE IN S3. Turning `[1, 2]` into the moves those steps make means reading the
    Stage 5 plan, and saying whether each component is a Helm release or an EKS managed add-on
    means reading the Stage 2 inventory -- the plan does not carry that, and the executor calls a
    different API for each. A `Pass` state can read neither.

NO NEW LAMBDA FUNCTION, AND THE CONSTRAINT IS OLDER THAN THIS MODULE. It runs in the
upgrade-approval function, dispatched on an explicit `release_record` payload key -- see
`main.stage_upgrade_approval` -- the same arrangement `release_prep` uses and for the same reason:
that function's role is the only one that may already read `plans/`, and the gate adds no
principal that could write a release.

ONE DERIVATION FOR THE MAIL AND THE RECORD. `release_prep.approved_steps` builds the moves, and
the mail's `Changes` parameter is those same objects' `summary` lines. A reader who wants to know
whether the approver saw what was recorded can compare two fields, not two code paths.

IT REFUSES RATHER THAN RECORDING A GAP. An approved order the plan does not contain, a decision
naming no principal, an automation status that is not `Approved`, orders that disagree with the
moves described: every one raises. The `Release assembled` state has NO `Catch`, so the execution
FAILS -- which is the honest outcome once somebody has approved. Routing it to `Release rejected`
would state that nobody authorised the release, and by then somebody had.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..facts.store import FactStore
from ..order.plan import UpgradePlan
from ..report import ComponentReport, InventoryReport
from ..storage import read_document
from ..storage.facts import build_fact_store
from ..upgrade import Release
from . import release_prep
from .release_prep import StepProvenance

REGION = os.environ.get("AWS_REGION", "us-east-1").strip()


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    """Build the release document from the gate's payload and the plan.

    The payload arrives NESTED under `release_record`, for `release_prep`'s reason: one Lambda
    function serves three jobs, and an explicit key is how a reader of any of them can tell which
    invocation this is.
    """
    payload = event.get("release_record")
    if not isinstance(payload, dict):
        raise ValueError(
            "release_record expects the run under a 'release_record' key; "
            f"got keys {sorted(event)}"
        )

    gate = payload.get("release")
    if not isinstance(gate, dict):
        raise ValueError(
            "release_record was given no 'release' payload, which is the automation's own "
            "account of who approved and must not be reconstructed from anything else"
        )

    plan_uri = str(payload.get("plan_uri") or "").strip()
    if not plan_uri:
        raise ValueError(
            f"release_record was given no plan_uri for run {gate.get('run_id')!r}, so the "
            "approved steps could not be described"
        )

    inventory_uri = str(payload.get("inventory_uri") or "").strip()
    if not inventory_uri:
        raise ValueError(
            f"release_record was given no inventory_uri for run {gate.get('run_id')!r}. The "
            "plan says what moves; only the Stage 2 inventory says whether each component is a "
            "Helm release or an EKS managed add-on, and the executor calls a different API for "
            "each -- so a record without it is not one this pipeline may write."
        )

    inventory = _read_inventory(inventory_uri)
    plan = _read_plan(plan_uri)
    store = build_fact_store()
    return build(
        gate,
        plan=plan,
        install_methods=install_methods(inventory),
        provenance=provenance(inventory, store),
        region=REGION,
        # THE SAME DERIVATION `Release approvers` USED FOR THE MAIL, so an approved
        # unsupported-at-target move is recorded as the move the approver was shown.
        extra=release_prep.unsupported_moves(plan, inventory, store),
    )


def install_methods(inventory: InventoryReport) -> dict[str, str]:
    """component -> `HELM` / `EKS_MANAGED_ADDON` / `MANIFEST`, from the Stage 2 inventory.

    KEYED ON `identity or workload_ref`, WHICH IS NOT A CONVENTION BUT A QUOTATION.
    `order/interleave.py` names a plan step's component with exactly that expression
    (`decision.identity or decision.workload_ref`), so this is the same key on both sides of the
    join rather than a string match that happens to work. Verified against this cluster's real
    documents: all eight component steps of the 2026-09-20 plan resolve, and the ninth -- the
    Kubernetes hop -- has no component to resolve.
    """
    return {
        (component.identity or component.workload_ref): component.install_method
        for component in inventory.components
    }


def provenance(inventory: InventoryReport, store: FactStore) -> dict[str, StepProvenance]:
    """component -> what its lane needs, from the inventory and the approved fact.

    THE SAME KEY AS `install_methods`, for the same reason: `identity or workload_ref` is the
    expression the plan names a step's component with.

    THE FACT IS READ THROUGH `store.find`, WHICH IS THE APPROVED VIEW. A candidate nobody
    approved is invisible to it, so a chart the model quoted but no person accepted cannot reach
    the record -- the lane runs `helm` on approved values or not at all. `find` takes the identity
    because facts are keyed by canonical id and alias; a component with no identity has no fact
    to look up and gets the inventory half only.
    """
    return {
        (component.identity or component.workload_ref): _provenance_of(
            component, store.find(component.identity) if component.identity else None
        )
        for component in inventory.components
    }


def _provenance_of(component: ComponentReport, fact: Any) -> StepProvenance:
    """One component's provenance: the inventory's release, chart and labels; the fact's repo.

    THE CHART NAME IS THE INVENTORY'S, NOT THE FACT'S. `inventory/resolve.py` folds a Helm
    release into one record whose `identity` IS the chart name it installed, and an upgrade
    moves the chart that is installed -- so asking a model to transcribe the name would be
    asking it to copy what the cluster already states. Only where that chart is PUBLISHED needs
    a source, and that is the one chart value the fact carries.
    """
    kind, _, rest = component.workload_ref.partition("/")
    _, _, release = rest.partition("/")
    helm = kind == "HelmRelease"
    return StepProvenance(
        # `HelmRelease/<ns>/<release>` names the release; anything else -- a correlated add-on's
        # Deployment, an uncorrelated add-on -- is addressed by the component's own name, which is
        # what the lane's `describe-addon` and the capture's default selector expect.
        release_name=(release if kind == "HelmRelease" else None)
        or component.identity
        or component.workload_ref,
        namespace=component.namespace,
        pod_selector=_pod_selector(component),
        chart_repository=(
            None if fact is None or fact.chart_repository is None else str(fact.chart_repository)
        ),
        chart_name=(component.identity or None) if helm else None,
    )


# THE LABELS EKS PUTS ON A MANAGED ADD-ON'S PODS, in the order to try them. `k8s-app` is what
# coredns, kube-proxy and vpc-cni (`aws-node`) carry; eks-pod-identity-agent carries the
# recommended `app.kubernetes.io/name` instead. Read off the correlated workload's own labels
# rather than assumed per add-on, so a selector that matches nothing is a visible None on the
# record and not a silently empty capture.
_ADDON_SELECTOR_LABELS = ("k8s-app", "app.kubernetes.io/name")
MANAGED_ADDON = "EKS_MANAGED_ADDON"


def _pod_selector(component: ComponentReport) -> str | None:
    """The label selector that finds a managed add-on's pods, or None when there is no label.

    Only for a managed add-on: a Helm release's pods are found by the `app.kubernetes.io/instance`
    label Helm stamps on every rendered object, which the capture script already selects on.
    """
    if component.install_method != MANAGED_ADDON:
        return None
    for label in _ADDON_SELECTOR_LABELS:
        value = component.labels.get(label)
        if value:
            return f"{label}={value}"
    return None


def build(
    gate: dict[str, Any],
    *,
    plan: UpgradePlan,
    install_methods: dict[str, str],
    region: str,
    provenance: dict[str, StepProvenance] | None = None,
    extra: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """The stored document: JSON-ready, validated, and needing no second parse at any level.

    THROUGH THE MODEL RATHER THAN AROUND IT. `Release.from_gate_payload` already holds every
    refusal this record needs -- a status that is not `Approved`, a decision naming no principal,
    a payload key renamed in Terraform and not here -- and each of those was a real shape this
    deployment produced or was one edit from producing. Assembling the dict by hand here would be
    a second, weaker copy of that reader.

    `model_dump(mode="json")` RATHER THAN THE MODEL. What Step Functions receives has to be plain
    JSON, and this is the boundary where a `datetime` becomes the ISO instant with its offset and a
    `StrEnum` becomes its value. The alternative -- letting the Lambda runtime serialise a pydantic
    object -- fails at the wire rather than here.
    """
    steps = release_prep.approved_steps(
        plan,
        gate.get("approved_steps") or [],
        install_methods=install_methods,
        provenance=provenance,
        extra=extra,
    )
    release = Release.from_gate_payload(gate, region=region, steps=steps)
    # `json.loads(model_dump_json())` and not `model_dump(mode="json")`: the two agree on every
    # field this model has, and the JSON round trip is the one that proves the document survives
    # being JSON -- which is the entire subject of this module.
    document: dict[str, Any] = json.loads(release.model_dump_json())
    return document


def _read_plan(uri: str) -> UpgradePlan:
    """The Stage 5 document. A seam, so the tests exercise the record without S3."""
    return read_document(uri, UpgradePlan)


def _read_inventory(uri: str) -> InventoryReport:
    """The Stage 2 document, read for one field per component: how it is installed."""
    return read_document(uri, InventoryReport)
