"""`Release` is the first document in this project that names an AWS principal. Hold it to that.

WHAT THIS FILE IS ABOUT. Every approval document before this one records `EMAIL_LINK_TOKEN` as
its authority, because a click on a presigned link proves somebody opened a mailbox and not
which human it was. `Release` is different in kind: `aws:approve` refuses a signal from a
principal outside its `Approvers` list, so AWS authenticated whoever answered before anything
in this repository ran.

THE RISK THAT COMES WITH THAT IS OVERCLAIMING, WHICH THIS PROJECT HAS ALREADY PAID FOR ONCE.
`Authority`'s own docstring says `AWS_SIGV4` was left out deliberately rather than unused, so
that no future writer could record the stronger authority for a click that never carried one.
Adding a stronger member is exactly the moment that guard gets tested. So two things are
asserted together here: the new member EXISTS, and `StepDecision` still defaults to the weaker
one. Backfilling the stronger authority onto the page clicks would be the overclaim, and it
would be a one-line edit.

NOTHING HERE WRITES A `Release` TO STORAGE, because nothing in this project does: the release gate
adds no Lambda, `$.release` is assembled by a `Pass` state in the state machine, and this model is
the contract a future executor validates that payload against. What the last group of tests below
exercises is that validation -- `Release.from_gate_payload`, fed the exact payload shapes this
deployment has really produced, including the two broken ones.

THAT GAP COST A DEPLOYED DEFECT, so the last two tests close it. Every model test below passed
while the deployed pipeline recorded the string "No output available yet because the step is not
successfully executed" as its approver -- because the model was the only thing under test, and the
ARN is put there by Terraform. A model held to a contract nothing is checked against is a
contract with one party. Those two tests read `workflow.tf` and `release_gate.tf` directly; they
are not a substitute for running it against a real deployment.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eks_addon_readiness.knowledge import Reversibility
from eks_addon_readiness.models import ClusterReference
from eks_addon_readiness.order.plan import PlanStep, StepKind, UpgradePlan
from eks_addon_readiness.upgrade import Authority, Release, ReleaseStep, StepDecision

PLATFORM = Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform"


def _steps(*orders: int) -> tuple[ReleaseStep, ...]:
    """The described moves for `_gate_payload`'s approved orders.

    TWO STEPS MOVING THE SAME COMPONENT, deliberately: a test run approved cert-manager twice
    in sequence, which is why `order` cannot be replaced by a component name.
    """
    moves = {
        1: ("cert-manager", "1.13.x", "1.19.x", "1.19.6"),
        2: ("cert-manager", "1.19.x", "1.21.x", "1.21.2"),
    }
    # HELM, because that is what this cluster's real inventory says cert-manager is. coredns and
    # vpc-cni come back EKS_MANAGED_ADDON from the same document.
    return tuple(
        ReleaseStep(
            order=order,
            kind=StepKind.COMPONENT_MOVE,
            component=moves[order][0],
            from_series=moves[order][1],
            to_series=moves[order][2],
            to_version=moves[order][3],
            install_method="HELM",
            summary=f"{moves[order][0]} {moves[order][1]} -> {moves[order][3]}",
        )
        for order in (orders or (1, 2))
    )


def _release(**overrides: object) -> Release:
    fields: dict[str, object] = {
        "run_id": "keda-row-183348",
        "cluster": ClusterReference(name="my-eks-cluster", region="us-east-1"),
        "approved_steps": _steps(),
        "approver_decisions": (
            {"Approver": "arn:aws:iam::123456789012:role/Admin", "Decision": "Approve"},
        ),
        "approver_arns": ("arn:aws:iam::123456789012:role/Admin",),
        "approved_at": datetime.now(UTC),
        "automation_execution_id": "832a6fba-f4f0-4b2a-ab85-587ad1234567",
    }
    fields.update(overrides)
    return Release(**fields)  # type: ignore[arg-type]


def _decision(
    role: str, decision: str, at: str | None = "2026-09-20T04:24:42.557527708Z"
) -> str:
    """One entry of `ApproverDecisions`, as the JSON TEXT SSM puts in a step output.

    A string rather than a dict because that is what arrives: step outputs are arrays of strings,
    which is the whole reason a reader has to decode them. The default instant is the real one
    from the acceptance run, nine fractional digits and a bare `Z` included.
    """
    entry = {"Approver": f"arn:aws:iam::123456789012:{role}", "Decision": decision}
    if at is not None and decision == "Approve":
        entry["ApprovalDecisionTime"] = at
    return json.dumps(entry)


def _gate_payload(**overrides: object) -> dict[str, object]:
    """`$.release` as the `Release approved` Pass state really builds it.

    Copied from the execution history of a test run -- recorded in
    a real execution -- rather than written to suit the model: `cluster` is a
    bare name, `approval_status` is a one-element array, `approver_decisions` holds one JSON
    string per LISTED approver including the one still `Waiting`, and there is no `approved_at`
    anywhere.
    """
    payload: dict[str, object] = {
        "run_id": "keda-row-183348",
        "cluster": "my-eks-cluster",
        "approved_steps": [1, 2],
        "automation_execution_id": "00000000-0000-4000-8000-000000000007",
        "approval_status": ["Approved"],
        "approver_decisions": [
            _decision("role/eks-addon-upgrades-release-approver", "Waiting"),
            _decision("role/Admin", "Approve"),
        ],
    }
    payload.update(overrides)
    return payload


def test_a_release_records_who_authorised_it() -> None:
    release = _release()
    assert release.approver_arns == ("arn:aws:iam::123456789012:role/Admin",)
    assert release.approver_authority is Authority.AWS_IAM_SSM_APPROVE


def test_the_new_authority_exists_and_names_the_mechanism() -> None:
    """The member's VALUE is asserted, not just its presence.

    It is written into every document this model produces, so renaming it later is a schema
    change disguised as a rename -- a reader of an old object would find an authority the enum
    no longer has.
    """
    assert Authority.AWS_IAM_SSM_APPROVE.value == "AWS_IAM_SSM_APPROVE"


def test_the_page_clicks_still_record_the_weaker_authority() -> None:
    """The guard `Authority`'s docstring asks for, asserted at the moment it could be lost.

    A `StepDecision` is written when somebody clicks a link in an email. Nothing about that
    click resolved an AWS principal, and the release gate happening LATER does not change what
    the click proved.
    """
    assert StepDecision.model_fields["approver_authority"].default is (
        Authority.EMAIL_LINK_TOKEN
    ), (
        "StepDecision no longer defaults to EMAIL_LINK_TOKEN. A click on a presigned link "
        "carries no identity; recording AWS_IAM_SSM_APPROVE against it would claim AWS "
        "resolved a principal before any handler ran, which is the overclaim Authority's "
        "docstring exists to prevent."
    )
    # AND `AWS_SIGV4` IS STILL ABSENT. Adding one honest member does not license the one that
    # was deliberately withheld: no path in this project resolves a SigV4 principal for an
    # approval, so the member would exist only to be recorded wrongly.
    assert not hasattr(Authority, "AWS_SIGV4"), (
        "Authority gained AWS_SIGV4. Its docstring says that member is absent deliberately "
        "rather than unused -- a path that really does resolve a SigV4 principal may add it, "
        "and the release gate is not that path: it resolves an IAM principal through SSM."
    )


def test_a_naive_approval_instant_is_refused() -> None:
    """Refused at the boundary, matching `StepDecision.decided_at_carries_an_offset`.

    Same reasoning that model gives: every comparison this document takes part in -- did the
    authorisation precede the change, which release came last -- is a comparison against an
    aware instant, and mixing the two raises a TypeError deep inside whatever is comparing them
    rather than here where the error is legible.
    """
    with pytest.raises(ValidationError, match="UTC offset"):
        _release(approved_at=datetime(2026, 9, 16, 12, 0, 0))


def test_a_release_that_authorises_nothing_is_refused() -> None:
    """An empty `approved_steps` must not be recordable.

    `Anything approved?` routes an empty list to `Done` without ever starting the gate, so a
    `Release` naming no steps could only arrive from a defect -- and a document asserting that
    a principal authorised nothing is worse than an absent one, because a reader counts it.
    """
    with pytest.raises(ValidationError):
        _release(approved_steps=())


def test_a_release_with_no_approver_is_refused() -> None:
    """The approver list is the entire point of this document.

    `aws:approve` cannot succeed without at least one approval, so an empty list means the
    payload was reshaped wrongly -- and that is precisely the case where a permissive contract
    would let the executor read "authorised" off a document nobody signed.
    """
    with pytest.raises(ValidationError):
        _release(approver_arns=())


def test_a_release_refuses_an_undeclared_field() -> None:
    """`ContractModel` forbids extras; asserted because this payload is built by a Pass state.

    A `Parameters` block in Terraform is not type-checked against this model, so a renamed key
    would otherwise arrive as a silently ignored extra and the field it was meant to fill would
    take its default.
    """
    with pytest.raises(ValidationError):
        _release(approver_arn="arn:aws:iam::123456789012:role/Admin")


def test_the_digest_covers_the_approver() -> None:
    """Two releases differing only in who signed them must not share a digest."""
    one = _release()
    other = _release(approver_arns=("arn:aws:iam::123456789012:role/Other",))
    assert one.digest() != other.digest()


# ---------------------------------------------------------------------------
# Where the ARN is actually read from. The defect this file missed once.
# ---------------------------------------------------------------------------


def test_the_pass_state_reads_the_approve_step_and_not_a_promoted_output() -> None:
    """The regression guard for a gate that went green recording a placeholder.

    In the version this guards against, `Release approved` read
    `AutomationExecution.Outputs['approve.ApproverDecisions']`. `aws:approve` does not populate
    document-level outputs: with the step in status `Success` and its own `Outputs` holding the
    ARN, that path returned the literal sentence "No output available yet because the step is not
    successfully executed", which the Pass state then copied into `$.release.approver_decisions`.
    The execution SUCCEEDED. Nothing failed, nothing was retried, and no principal was recorded.

    ASSERTED AS TEXT AGAINST THE TERRAFORM, which is weaker than a run and the strongest thing
    available with no credentials -- the sibling `test_the_release_gate_cannot_pass_silently.py`
    makes the same trade for the same reason.
    """
    text = (PLATFORM / "workflow.tf").read_text()
    match = re.search(r'"approver_decisions\.\$"\s*=\s*"([^"]+)"', text)
    assert match, (
        "`approver_decisions.$` is gone from workflow.tf. If the key was renamed, fix this test "
        "rather than deleting it -- recording the principal is the only thing this gate produces "
        "that nothing upstream of it could."
    )
    path = match.group(1)
    assert "StepExecutions" in path, (
        f"`approver_decisions.$` reads {path!r}. An `aws:approve` output is only readable from "
        "that step's own entry in `StepExecutions[]`; the execution's `Outputs` map yields the "
        "string \"No output available yet because the step is not successfully executed\", and a "
        "run carrying that as its approver SUCCEEDS."
    )
    assert "Outputs['approve." not in path, (
        f"`approver_decisions.$` reads {path!r}, a dotted document-level output name. That is the "
        "exact path that shipped broken."
    )
    assert "StepName=='approve'" in path.replace(" ", ""), (
        f"`approver_decisions.$` reads {path!r}, which does not select the step by name. A "
        "positional `StepExecutions[1]` resolves the same value today and silently reads some "
        "other step's outputs the day one is inserted before `approve`."
    )


def test_a_release_can_be_read_out_of_the_payload_the_gate_really_produced() -> None:
    """The recorded payload of a test run, byte for byte, through the reader.

    Every other model test here builds a `Release` from keyword arguments that already have this
    model's shape. That is what let a placeholder reach production: the payload the state machine
    actually emits differs in four ways, and none of them were under test. This one starts from
    what the execution history held.
    """
    release = Release.from_gate_payload(_gate_payload(), region="us-east-1", steps=_steps())

    assert release.approver_arns == ("arn:aws:iam::123456789012:role/Admin",), (
        "the approver role's entry was recorded as an approver. It is in `ApproverDecisions` "
        "because it is LISTED, carrying \"Decision\":\"Waiting\" -- and a release naming a "
        "principal who never answered is precisely the overclaim this gate exists to prevent."
    )
    assert release.approver_authority is Authority.AWS_IAM_SSM_APPROVE
    assert release.cluster == ClusterReference(
        name="my-eks-cluster", region="us-east-1"
    )
    # THE ORDERS, and then what they move. `[1, 2]` was the whole record once; the second
    # assertion is the part that made it usable.
    assert [step.order for step in release.approved_steps] == [1, 2]
    assert [step.summary for step in release.approved_steps] == [
        "cert-manager 1.13.x -> 1.19.6",
        "cert-manager 1.19.x -> 1.21.2",
    ]
    assert release.automation_execution_id == "00000000-0000-4000-8000-000000000007"
    # The nanosecond fraction SSM sends, truncated rather than refused or rounded up.
    assert release.approved_at == datetime(2026, 9, 20, 4, 24, 42, 557527, tzinfo=UTC)


def test_the_approval_instant_is_the_latest_one_when_several_principals_approve() -> None:
    """`MinRequiredApprovals` is 1 today, and raising it is a one-line change to the runbook.

    `approved_at` is the moment the release BECAME authorised, so with two approvals it is the
    later of them -- taking the first would date the authorisation before it existed.
    """
    release = Release.from_gate_payload(
        _gate_payload(
            approver_decisions=[
                _decision("role/Admin", "Approve", "2026-09-20T04:24:42.557527708Z"),
                _decision("role/Other", "Approve", "2026-09-20T05:01:03.000000001Z"),
            ]
        ),
        region="us-east-1",
        steps=_steps(),
    )
    assert release.approved_at == datetime(2026, 9, 20, 5, 1, 3, tzinfo=UTC)
    assert release.approver_arns == (
        "arn:aws:iam::123456789012:role/Admin",
        "arn:aws:iam::123456789012:role/Other",
    )


def test_the_placeholder_that_shipped_cannot_become_a_release() -> None:
    """The defect itself, as a payload. This is the test that was missing when it shipped.

    The document used to promote `approve.ApproverDecisions`, and `GetAutomationExecution`
    returned the sentence below in its place. The `Pass` state copied it into
    `$.release.approver_decisions` and the execution SUCCEEDED. Terraform no longer produces this
    payload, and the reader must refuse it anyway: the promotion is one tidy-looking edit away
    from coming back, and a reader that accepted the sentence would record it as an approver.
    """
    with pytest.raises(ValueError, match="not JSON"):
        Release.from_gate_payload(
            _gate_payload(
                approver_decisions=[
                    "No output available yet because the step is not successfully executed"
                ]
            ),
            region="us-east-1",
            steps=_steps(),
        )


def test_the_doubly_nested_decisions_are_named_rather_than_read_wrongly() -> None:
    """The shape run `ceadda10` recorded, before `States.ArrayGetItem` was added.

    A JSONPath filter returns one entry per match and each entry is itself SSM's array-valued
    output, so the bare filter yields `[["{...}", "{...}"]]`. The error has to name the fix,
    because the symptom -- a release with no approver -- points at neither the filter nor the
    intrinsic.
    """
    with pytest.raises(ValueError, match="ArrayGetItem"):
        Release.from_gate_payload(
            _gate_payload(approver_decisions=[[_decision("role/Admin", "Approve")]]),
            region="us-east-1",
            steps=_steps(),
        )


def test_a_release_where_nobody_answered_is_refused() -> None:
    """Status `Approved` with every decision still `Waiting` is a contradiction, not an approval.

    It should not be reachable -- `aws:approve` reports Approved only once
    `MinRequiredApprovals` is met -- which is exactly why it is worth refusing here: if the two
    halves of SSM's own output ever disagree, the half that names a principal wins, and this
    document is not written at all.
    """
    with pytest.raises(ValueError, match="no listed approver approved"):
        Release.from_gate_payload(
            _gate_payload(
                approver_decisions=[
                    _decision("role/eks-addon-upgrades-release-approver", "Waiting"),
                    _decision("role/Admin", "Waiting"),
                ]
            ),
            region="us-east-1",
            steps=_steps(),
        )


def test_a_status_that_is_not_approved_never_becomes_a_release() -> None:
    """`Release decided?` routes a rejection to `Release rejected`, and this is the second lock.

    A payload carrying `Rejected` into the reader means the state machine's routing and this
    model disagree about what a refusal is. The refusal wins.
    """
    with pytest.raises(ValueError, match="'Rejected'"):
        Release.from_gate_payload(
            _gate_payload(approval_status=["Rejected"]),
            region="us-east-1",
            steps=_steps(),
        )


def test_a_renamed_payload_key_is_refused_rather_than_ignored() -> None:
    """`ContractModel` forbids extras, and it cannot see this payload.

    The adapter assembles the model field by field, so a key renamed in `workflow.tf` would
    otherwise be dropped silently and the field it filled would take its default -- surfacing as
    an absent approver rather than as an error, which is the failure mode this whole file is
    about.
    """
    renamed = _gate_payload()
    renamed["approver_decision"] = renamed.pop("approver_decisions")
    with pytest.raises(ValueError, match="approver_decisions"):
        Release.from_gate_payload(renamed, region="us-east-1", steps=_steps())

    # AND A KEY MERELY ADDED, which the rename above never reaches -- a rename trips the
    # missing-key check first. A new `Parameters` entry in the Pass state is the likelier edit of
    # the two, and it must not be silently dropped either: whatever it carries, this reader was
    # not written to record it.
    added = _gate_payload()
    added["approver_authority"] = "AWS_SIGV4"
    with pytest.raises(ValueError, match="does not know"):
        Release.from_gate_payload(added, region="us-east-1", steps=_steps())


def test_a_field_ssm_adds_later_does_not_break_the_read() -> None:
    """Unknown keys INSIDE a decision are tolerated, unlike unknown keys in the payload.

    The asymmetry is deliberate. The payload is built by this repository, so a key there that
    this reader does not know is a mistake in our own Terraform. The decision documents are
    AWS's, and a service adding a field to its output is not a defect in this pipeline -- failing
    on it would turn an SSM release note into a broken gate.
    """
    decision = json.loads(_decision("role/Admin", "Approve"))
    decision["SomeFieldSsmAddsIn2027"] = "whatever it turns out to be"
    release = Release.from_gate_payload(
        _gate_payload(approver_decisions=[json.dumps(decision)]),
        region="us-east-1",
        steps=_steps(),
    )
    assert release.approver_arns == ("arn:aws:iam::123456789012:role/Admin",)


def test_the_document_promotes_no_approve_outputs() -> None:
    """The half of the fix that is an absence, and would therefore be easy to undo.

    A future reader who finds `Release approved` reaching into `StepExecutions[]` may well decide
    the tidy thing is a document-level `outputs` block. It is not tidy; it is the placeholder
    again. The declaration creating successfully is what makes it dangerous.
    """
    text = (PLATFORM / "release_gate.tf").read_text()
    declarations = [
        line
        for line in text.splitlines()
        if not line.lstrip().startswith("#") and '"approve.' in line
    ]
    assert not declarations, (
        "release_gate.tf promotes an `approve.*` output to the document level: "
        f"{declarations}. `aws:approve` does not populate those, so the promotion yields "
        '"No output available yet because the step is not successfully executed" and the gate '
        "records that sentence as its approver."
    )



# ---------------------------------------------------------------------------
# The object on the way out: what `Release recorded` really writes to S3
# ---------------------------------------------------------------------------
#
# WHY THIS SECTION EXISTS. `release.json` is written by an SDK integration, so no test here had
# ever looked at the BYTES. The first real authorisation -- run
# `00000000-0000-4000-8000-000000000003`, read back from S3 -- stored 549 bytes that need THREE
# parses to reach a principal:
#
#     json.loads(body)                     -> a str, because Body was serialised twice
#     json.loads(that)                     -> the object at last
#     json.loads(o["approver_decisions"][0]) -> the decision, because SSM emits JSON text
#
# and `approved_steps` was `[1, 2, 4, 7]`, which names nothing a reader can act on without the
# plan beside it. The upgrade agent is the intended consumer, so the shape is a contract:
#
# THE ENCODING IS MODELLED, NOT CALLED. Step Functions cannot run locally, so `_what_s3_receives`
# states one AWS behaviour -- the `s3:putObject` integration serialises the `Body` VALUE -- and is
# anchored to the real object above. It refuses any expression it was not written for rather than
# guessing, so an unmodelled `Body` fails these tests instead of passing them.

REAL_GATE_DECISIONS = (
    '{"Approver":"arn:aws:iam::123456789012:role/Admin","Decision":"Approve",'
    '"ApprovalDecisionTime":"2026-09-20T09:00:53.311143040Z"}',
    '{"Approver":"arn:aws:iam::123456789012:role/my-eks-cluster-terraform",'
    '"Decision":"Waiting"}',
)


# `component -> install_method` exactly as the Stage 2 inventory for this cluster holds it.
# THE JOIN KEY IS `identity or workload_ref`, which is the expression
# `order/interleave.py` uses to name a plan step's component -- so the two sides of the join are
# the same expression rather than two strings that happen to agree.
REAL_INSTALL_METHODS = {
    "cert-manager": "HELM",
    "keda": "HELM",
    "metrics-server": "HELM",
    "coredns": "EKS_MANAGED_ADDON",
    "kube-proxy": "EKS_MANAGED_ADDON",
    "vpc-cni": "EKS_MANAGED_ADDON",
    "eks-pod-identity-agent": "EKS_MANAGED_ADDON",
}


def _real_gate_payload() -> dict[str, object]:
    """`$.release` exactly as a test run produced it, decisions and all."""
    return {
        "run_id": "00000000-0000-4000-8000-000000000003",
        "cluster": "my-eks-cluster",
        "approved_steps": [1, 2],
        "automation_execution_id": "00000000-0000-4000-8000-000000000008",
        "approval_status": ["Approved"],
        "approver_decisions": list(REAL_GATE_DECISIONS),
    }


def _release_recorded_body_expression() -> str:
    """The `Body.$` expression on the `Release recorded` state, read from the HCL."""
    text = (PLATFORM / "workflow.tf").read_text()
    state = re.search(r'"Release recorded" = \{(.*?)\n    \}\n', text, re.S)
    assert state, 'no "Release recorded" state in workflow.tf'
    body = re.search(r'^\s*"Body\.\$"\s*=\s*"(.*)"\s*$', state.group(1), re.M)
    assert body, "the Release recorded state passes no Body to PutObject"
    return body.group(1)


def _what_s3_receives(expression: str, document: dict[str, object]) -> str:
    """The bytes S3 stores, given the `Body.$` expression and the document it points at.

    ONE MODELLED FACT: the integration JSON-serialises whatever `Body` evaluates to. A bare
    payload reference therefore stores the object once; an intrinsic that stringifies it first
    stores a JSON string -- which is what a test run did.
    """
    if expression.startswith("States.JsonToString("):
        return json.dumps(json.dumps(document))
    if re.fullmatch(r"\$(\.[A-Za-z_][A-Za-z0-9_]*)*", expression):
        return json.dumps(document)
    raise AssertionError(
        f"this test does not model Body.$ = {expression!r}. Model it here before changing it "
        "there: the encoding of the release record is the one thing no handler in this "
        "repository controls."
    )


def _stored_release() -> dict[str, object]:
    """The release document as it lands in S3, built the way the deployed pipeline builds it.

    Through the real handler rather than by hand, so this crosses every boundary the deployed
    system does: the gate payload, the plan, the record handler, the `Body` expression, and
    `json.loads` at the far end.
    """
    from eks_addon_readiness.stages import release_record

    document = release_record.build(
        _real_gate_payload(),
        plan=_plan_for_the_real_run(),
        install_methods=REAL_INSTALL_METHODS,
        region="us-east-1",
    )
    body = _what_s3_receives(_release_recorded_body_expression(), document)
    return json.loads(body)


def _plan_for_the_real_run() -> UpgradePlan:
    """A Stage 5 plan holding the two steps a test run had approved.

    The versions are that run's real ones, taken from the `Changes` parameter the automation was
    started with: `cert-manager 1.13.x -> 1.19.6, cert-manager 1.19.x -> 1.21.2`.
    """
    digest = "sha256:" + "a" * 64
    return UpgradePlan(
        cluster=ClusterReference(name="my-eks-cluster", region="us-east-1"),
        current_kubernetes_version="1.32",
        target_kubernetes_version="1.33",
        steps=(
            PlanStep(
                order=1,
                kind=StepKind.COMPONENT_MOVE,
                description="Move cert-manager: 1.13.x -> 1.19.x (install 1.19.6)",
                component="cert-manager",
                from_series="1.13.x",
                to_series="1.19.x",
                recommended_version="1.19.6",
                at_kubernetes="1.33",
                reversibility=Reversibility.REVERSIBLE,
            ),
            PlanStep(
                order=2,
                kind=StepKind.COMPONENT_MOVE,
                description="Move cert-manager: 1.19.x -> 1.21.x (install 1.21.2)",
                component="cert-manager",
                from_series="1.19.x",
                to_series="1.21.x",
                recommended_version="1.21.2",
                at_kubernetes="1.33",
                reversibility=Reversibility.REVERSIBLE,
            ),
        ),
        snapshot_digest=digest,
        report_digest=digest,
        ledger_digest=digest,
        assessment_digest=digest,
        store_digest="sha256:" + "c" * 64,
        ruleset_digest="sha256:" + "b" * 64,
    )


def test_the_stored_release_needs_no_second_parse_at_any_level() -> None:
    """One `json.loads` and every value is a value. The whole point of the shape.

    THREE LEVELS, ALL ASSERTED TOGETHER, because the real object was wrong at two of them and a
    fix for either alone would still leave a reader parsing strings out of a document that
    claims `application/json`.
    """
    stored = _stored_release()
    assert isinstance(stored, dict), (
        "release.json does not parse to an object. The Body is being serialised twice -- pass "
        '`"Body.$" = "$..."` and let the s3:putObject integration serialise it once.'
    )
    decisions = stored["approver_decisions"]
    assert isinstance(decisions, list) and all(isinstance(d, dict) for d in decisions), (
        f"approver_decisions holds {[type(d).__name__ for d in decisions]}, not objects. "
        "aws:approve emits JSON text, so the parse has to happen on our side -- in the record "
        "handler, where a test can drive it."
    )
    steps = stored["approved_steps"]
    assert isinstance(steps, list) and all(isinstance(s, dict) for s in steps), (
        f"approved_steps holds {[type(s).__name__ for s in steps]}, not objects."
    )
    assert isinstance(stored["approval_status"], str), (
        "approval_status is still the one-element array an SSM step output arrives as."
    )


def test_the_stored_release_says_what_was_approved_and_keeps_the_order() -> None:
    """`[1, 2]` told a reader nothing. Each step now names the move, with `order` kept.

    `order` IS NOT DECORATION: the per-step decisions live at
    `upgrade-decisions/cluster=.../step=N.json`, so dropping it would cut the link between this
    record and the decision that authorised each step.
    """
    steps = _stored_release()["approved_steps"]
    assert [s["order"] for s in steps] == [1, 2]
    first = steps[0]
    assert first["component"] == "cert-manager"
    assert first["from_series"] == "1.13.x"
    assert first["to_version"] == "1.19.6"
    assert first["kind"] == "COMPONENT_MOVE"
    assert first["summary"] == "cert-manager 1.13.x -> 1.19.6"


def test_the_stored_release_still_names_the_principal_and_the_instant() -> None:
    """The derived fields survive the reshaping: who approved, when, and under what authority.

    The raw decisions are kept BESIDE these rather than instead of them. A reader that wants
    AWS's own account has it; a reader that wants the answer does not have to filter `Waiting`
    entries to find it.
    """
    stored = _stored_release()
    assert stored["approver_arns"] == ["arn:aws:iam::123456789012:role/Admin"]
    assert stored["approved_at"].startswith("2026-09-20T09:00:53")
    assert stored["approver_authority"] == "AWS_IAM_SSM_APPROVE"
    assert stored["automation_execution_id"] == "00000000-0000-4000-8000-000000000008"
    assert stored["cluster"]["name"] == "my-eks-cluster"
    assert stored["cluster"]["region"] == "us-east-1"


def test_an_approved_step_the_plan_does_not_contain_fails_the_record() -> None:
    """A release record that names a move it cannot describe is worse than one that refuses.

    The plan and the recorded decisions disagreeing about what this run is means one of them is
    stale. Emitting a step with empty fields would hand the executor a move with no versions in
    it; failing sends the run to a FAILED execution naming the step.
    """
    from eks_addon_readiness.stages import release_record

    payload = _real_gate_payload()
    payload["approved_steps"] = [1, 9]
    with pytest.raises(ValueError, match="9"):
        release_record.build(
            payload,
            plan=_plan_for_the_real_run(),
            install_methods=REAL_INSTALL_METHODS,
            region="us-east-1",
        )


def test_the_mail_line_and_the_step_record_come_from_one_derivation() -> None:
    """`Changes` in the mail and `summary` in the record cannot drift, by construction.

    Two derivations of the same sentence is how an approver authorises one thing and the record
    names another. So the mail's line IS the step's `summary`, and this asserts the identity
    rather than the equality of two hand-kept strings.
    """
    from eks_addon_readiness.stages import release_prep

    plan = _plan_for_the_real_run()
    steps = release_prep.approved_steps(plan, [1, 2], install_methods=REAL_INSTALL_METHODS)
    changes = release_prep.approved_changes(plan, [1, 2])
    assert changes == tuple(step.summary for step in steps)
    assert changes == (
        "cert-manager 1.13.x -> 1.19.6",
        "cert-manager 1.19.x -> 1.21.2",
    )


def test_the_body_is_the_document_itself_and_not_an_intrinsic() -> None:
    """The one-line half of the repair, pinned where a future edit would undo it.

    `States.JsonToString` is the obvious thing to reach for -- and it IS correct on
    `ApprovedSteps` two parameters away, where the value really is embedded in a mail. The
    difference is that the integration serialises THIS one, so pre-serialising doubles it.
    """
    expression = _release_recorded_body_expression()
    assert not expression.startswith("States."), (
        f"Release recorded passes Body.$ = {expression!r}. It must pass the document itself: "
        "the s3:putObject integration serialises the Body value, so any intrinsic that "
        "stringifies it first lands a JSON string where the object should be."
    )


def test_the_record_says_helm_or_managed_addon_for_every_move() -> None:
    """The field the executor branches on, and the reason the inventory is read at all.

    A Helm release is upgraded with `helm upgrade`; an EKS managed add-on with `UpdateAddon`. Two
    APIs, two failure modes, two rollbacks. The plan carries neither -- it says what moves, not how
    the thing was installed -- so the record joins the Stage 2 inventory on the component.
    """
    steps = _stored_release()["approved_steps"]
    assert [s["install_method"] for s in steps] == ["HELM", "HELM"], (
        "the stored moves do not say how each component is installed, so an executor reading "
        "this record has to guess between helm upgrade and UpdateAddon."
    )


def test_a_component_the_inventory_does_not_name_is_refused() -> None:
    """Two documents of one run disagreeing is a refusal, not a null.

    Recording `install_method: null` would hand the executor a move it must guess an API for, and
    guessing Helm for a managed add-on is a wrong call against a live cluster. The plan and the
    inventory are both products of the same run, so a component in one and not the other is an
    inconsistency worth failing on.
    """
    from eks_addon_readiness.stages import release_record

    with pytest.raises(ValueError, match="cert-manager"):
        release_record.build(
            _real_gate_payload(),
            plan=_plan_for_the_real_run(),
            install_methods={"keda": "HELM"},
            region="us-east-1",
        )


def test_the_cluster_hop_carries_no_install_method() -> None:
    """The one step kind that installs nothing, asserted from both sides.

    `ReleaseStep` refuses a component step with no method AND a method with no component. The
    second half matters because the cluster hop moves Kubernetes itself: a delivery mechanism on it
    would be a claim about an API nobody calls for it.
    """
    hop = ReleaseStep(
        order=5,
        kind=StepKind.CLUSTER_UPGRADE,
        from_kubernetes="1.35",
        to_kubernetes="1.36",
        summary="Kubernetes 1.35 -> 1.36",
    )
    assert hop.install_method is None

    with pytest.raises(ValidationError, match="installs nothing"):
        ReleaseStep(
            order=5,
            kind=StepKind.CLUSTER_UPGRADE,
            from_kubernetes="1.35",
            to_kubernetes="1.36",
            install_method="HELM",
            summary="Kubernetes 1.35 -> 1.36",
        )


def test_a_move_with_no_install_method_is_refused_by_the_model() -> None:
    """The requirement lives on the model, so no caller can produce a record without it."""
    with pytest.raises(ValidationError, match="how it is installed"):
        ReleaseStep(
            order=1,
            kind=StepKind.COMPONENT_MOVE,
            component="cert-manager",
            from_series="1.13.x",
            to_series="1.19.x",
            to_version="1.19.6",
            summary="cert-manager 1.13.x -> 1.19.6",
        )
