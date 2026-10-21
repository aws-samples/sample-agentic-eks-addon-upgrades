"""The report a person is handed must lead with what needs them, and cite what it was rendered from.

WHAT THIS CATCHES. The report is the run's only account of a cluster that has already been changed,
and the failure mode is not that it is missing -- it is that it reads as fine. Four ways that
happens:

  A REFERRED COMPONENT IS BURIED. Sorted by step order, a component that needs a person sits under
  three that are fine, and the subject line says "upgrade report". So the ordering and the subject
  are asserted: what needs somebody comes first, and the subject says so before it is opened.

  A REFERRAL DOES NOT SAY WHY. The human's requirement is that what the stage cannot fix it refers,
  naming what it tried and what it observed. A row that said only "REFERRED" satisfies the letter of
  that and none of its point, so both lists have to reach the mail rather than staying in S3.

  AN UNDELETED ACCESS ENTRY READS AS A FOOTNOTE. It is a standing cluster-write grant nobody asked
  for. It gets the word WARNING and an instruction.

  THE MAIL BECOMES A SECOND SOURCE OF TRUTH. Every value comes off the `UpgradeRunReport` that is
  already in S3, and the mail cites that URI -- so a reader who distrusts the mail can fetch the
  thing it was rendered from, and there is no path where the two disagree.

A PURE BUILDER, so all of this is testable with no SES, no AWS and no credentials.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eks_addon_readiness.models import ClusterReference
from eks_addon_readiness.notify.upgrade_report_email import build_report_email
from eks_addon_readiness.upgrade_lane.models import (
    LaneOutcome,
    TriageAction,
    TriageVerdict,
    UpgradeRunReport,
    UpgradeStatus,
    UpgradeTriage,
)
from eks_addon_readiness.upgrade_lane.report_page import to_upgrade_report_html

CLUSTER = ClusterReference(name="my-eks-cluster", region="us-east-1")
REPORT_URI = "s3://bucket/upgrade-reports/cluster=my-eks-cluster/run=toast-1/report.json"
BUNDLE = "s3://bucket/upgrade-diagnosis/cluster=my-eks-cluster/run=toast-1/component=keda/after/"


def _outcome(
    *,
    component: str,
    step: int,
    status: UpgradeStatus,
    attempted: tuple[str, ...] = (),
    observed: tuple[str, ...] = (),
    cleanup_error: str = "",
    after_uri: str = "",
    triage: UpgradeTriage | None = None,
) -> LaneOutcome:
    return LaneOutcome(
        cluster=CLUSTER,
        run_id="toast-1",
        step_order=step,
        component=component,
        status=status,
        installed="1.0.0",
        applied="1.1.0",
        attempted=attempted,
        observed=observed,
        cleanup_error=cleanup_error,
        diagnosis_after_uri=after_uri,
        triage=triage,
        finished_at=datetime.now(UTC),
    )


def _page(report: UpgradeRunReport) -> str:
    """The report page the mail links to, rendered from the document alone, no evidence beside it.

    THE MAIL IS THE SUMMARY AND THE PAGE IS THE ACCOUNT. The mail used to carry every command,
    verdict and bundle URI itself; it now carries the counts, the warning, the table and one
    link, and the page carries the rest. So the "reaches the reader" assertions below are made
    against the page, and the mail is held only to citing the record it was rendered from.
    """
    return to_upgrade_report_html(
        report, rendered_at="2026-09-26T10:12:39Z", report_uri=REPORT_URI
    )


def _report(*outcomes: LaneOutcome) -> UpgradeRunReport:
    return UpgradeRunReport(
        cluster=CLUSTER,
        run_id="toast-1",
        target_kubernetes_version="1.36",
        automation_execution_id="auto-1",
        approver_arns=("arn:aws:sts::123456789012:assumed-role/Admin/approver-session",),
        outcomes=outcomes,
        finished_at=datetime.now(UTC),
    )


def test_the_subject_leads_with_what_needs_a_person() -> None:
    report = _report(
        _outcome(component="metrics-server", step=1, status=UpgradeStatus.UPGRADED),
        _outcome(
            component="keda",
            step=2,
            status=UpgradeStatus.REFERRED,
            attempted=("helm upgrade keda",),
            observed=("pods.json: CrashLoopBackOff",),
        ),
    )
    mail = build_report_email(report, report_uri=REPORT_URI)
    assert "1 add-on needs review" in mail.subject, (
        f"the subject is {mail.subject!r}. It is read at a glance and it is the only part that "
        "changes what a person does next, so it has to say whether anything needs them."
    )
    assert "my-eks-cluster" in mail.subject, "the subject does not name the cluster"


def test_a_clean_run_says_nothing_needs_you() -> None:
    """The positive case, so this file cannot be satisfied by a mail that always shouts."""
    mail = build_report_email(
        _report(_outcome(component="metrics-server", step=1, status=UpgradeStatus.UPGRADED)),
        report_uri=REPORT_URI,
    )
    assert "1 add-on upgraded" in mail.subject and "review" not in mail.subject, (
        f"the subject is {mail.subject!r}"
    )


def test_the_component_that_needs_a_person_is_listed_first() -> None:
    """Step order would bury it. What needs somebody comes first, whatever order it ran in."""
    report = _report(
        _outcome(component="metrics-server", step=1, status=UpgradeStatus.UPGRADED),
        _outcome(
            component="cert-manager",
            step=2,
            status=UpgradeStatus.ROLLED_BACK,
            after_uri=BUNDLE,
        ),
        _outcome(
            component="keda",
            step=3,
            status=UpgradeStatus.REFERRED,
            attempted=("helm upgrade keda",),
            observed=("pods.json: CrashLoopBackOff",),
        ),
    )
    mail = build_report_email(report, report_uri=REPORT_URI)
    positions = {
        name: mail.text_body.index(name) for name in ("keda", "cert-manager", "metrics-server")
    }
    assert positions["keda"] < positions["cert-manager"] < positions["metrics-server"], (
        f"the ordering is {sorted(positions, key=positions.get)}. A component that needs a person "
        "must not sit under the ones that are fine."
    )


def test_a_referral_carries_what_it_tried_and_what_it_saw() -> None:
    """Both lists, on the page the mail links to. The word REFERRED alone does not satisfy it."""
    report = _report(
        _outcome(
            component="keda",
            step=1,
            status=UpgradeStatus.REFERRED,
            attempted=("helm upgrade keda --version 2.18.0", "helm rollback keda 1"),
            observed=("the rollback itself failed", "helm history shows two pending revisions"),
        )
    )
    page = _page(report)
    for expected in (
        "helm upgrade keda --version 2.18.0",
        "helm rollback keda 1",
        "the rollback itself failed",
        "helm history shows two pending revisions",
    ):
        assert expected in page, (
            f"the report page does not carry {expected!r}. What the stage cannot fix it refers to "
            "a human, and a referral that leaves its reasons in S3 makes them open a console to "
            "learn whether their cluster is alright."
        )
    mail = build_report_email(report, report_uri=REPORT_URI)
    assert "keda" in mail.text_body and "review" in mail.subject, (
        "the mail must at least name the component and say it needs review"
    )


def test_an_undeleted_access_entry_is_a_warning_and_not_a_footnote() -> None:
    """A leaked access entry is a standing cluster-write grant nobody asked for."""
    report = _report(
        _outcome(
            component="metrics-server",
            step=1,
            status=UpgradeStatus.UPGRADED,
            cleanup_error="ThrottlingException",
        )
    )
    mail = build_report_email(report, report_uri=REPORT_URI)
    assert "WARNING" in mail.text_body, (
        "an access entry that could not be deleted does not produce a WARNING line. It is a "
        "standing write grant on the cluster; a person has to be told to revoke it."
    )
    assert "revoke" in mail.text_body.lower(), (
        "the warning does not say what to do about it. Naming a problem without the action is how "
        "it gets read and not acted on."
    )
    # And the report itself counts it as needing a person, so the subject says so too.
    assert "needs review" in mail.subject, (
        f"the subject is {mail.subject!r} even though a lane leaked its access entry."
    )


def test_the_mail_cites_the_document_it_was_rendered_from() -> None:
    """So the mail is never the only account, and the two cannot disagree unnoticed."""
    mail = build_report_email(
        _report(_outcome(component="metrics-server", step=1, status=UpgradeStatus.UPGRADED)),
        report_uri=REPORT_URI,
    )
    assert REPORT_URI in mail.text_body, "the text body does not cite the report document"
    assert REPORT_URI in mail.html_body, "the html body does not cite the report document"


def test_the_mail_names_who_authorised_the_change() -> None:
    """A report of a change with no authorisation behind it is the one thing never to assume."""
    page = _page(
        _report(_outcome(component="metrics-server", step=1, status=UpgradeStatus.UPGRADED))
    )
    assert "approver-session" in page, (
        "the report page does not name the principal that authorised the release. The release "
        "gate exists so that ARN is recorded; a report that drops it loses the point of the gate."
    )
    assert "auto-1" in page, "the report page does not name the automation execution"


def test_the_agents_verdict_is_reported_rather_than_paraphrased() -> None:
    """The agent's verdict is data the code validated, so it is reported as the two flat values.

    Not restated as prose: a reader has to be able to see that a model said FIXABLE and the code
    still rolled back, which is the normal case and the one that would look like a disagreement if
    the mail smoothed it over.
    """
    triage = UpgradeTriage(
        component="cert-manager",
        verdict=TriageVerdict.UNHEALTHY,
        proposed_action=TriageAction.ROLLBACK,
        observations=("list_k8s_resources Pod: 0/1 ready",),
    )
    page = _page(
        _report(
            _outcome(
                component="cert-manager",
                step=1,
                status=UpgradeStatus.ROLLED_BACK,
                after_uri=BUNDLE,
                triage=triage,
            )
        )
    )
    assert "UNHEALTHY" in page and "rollback initiated" in page, (
        "the report page does not report the agent's verdict and the branch that followed. Both "
        "are needed: the verdict is what it observed and the action is what the code then did, "
        "and a reader has to be able to see which."
    )
    assert "list_k8s_resources Pod: 0/1 ready" in page, (
        "the agent's own observation is not on the page; it is the evidence for the verdict"
    )


def test_every_bundle_uri_reaches_the_reader() -> None:
    """A diagnosis nobody is told the location of is a diagnosis nobody reads."""
    report = _report(
        _outcome(
            component="cert-manager",
            step=1,
            status=UpgradeStatus.ROLLED_BACK,
            after_uri=BUNDLE,
        )
    )
    assert BUNDLE in _page(report), "the report page does not cite the after bundle"
    mail = build_report_email(report, report_uri=REPORT_URI)
    assert REPORT_URI in mail.text_body, (
        "the mail must cite the record, which is where the bundle URI is recorded"
    )


# ---------------------------------------------------------------------------
# THE AUTHORISATION THE REPORT IS ABOUT, and the defect these catch.
#
# The upgrade stage hangs off `Release recorded`, so by the time it runs the authorisation exists as
# an OBJECT under `releases/cluster=.../run=.../release.json`. The lane planner reads that object.
#
# WHY NOT REBUILD IT FROM THE PAYLOAD. The gate payload carries step ORDERS and `approver_decisions`
# as JSON TEXT; turning that into a `Release` needs the Stage 5 plan and the Stage 2 inventory, and
# `release_prep.py` plus `release_record.py` already did exactly that. A second implementation of
# the same join would be free to disagree with the recorded one -- and the disagreement would be
# between two accounts of who authorised what.
# ---------------------------------------------------------------------------

RECORDED_RELEASE = {
    "schema_version": "1.0",
    "kind": "UpgradeRelease",
    "run_id": "toast-1",
    "cluster": {"name": "my-eks-cluster", "region": "us-east-1"},
    # OBJECTS, NOT ORDERS. Each carries the component, the target version and the install method,
    # so the lane planner needs neither the plan nor a catalogue flag to know what step 1 moves or
    # which API to call for it.
    "approved_steps": [
        {
            "order": 1,
            "kind": "COMPONENT_UPGRADE_RECOMMENDED",
            "component": "metrics-server",
            "from_series": "0.6.x",
            "to_series": "0.9.x",
            "to_version": "0.9.0",
            "install_method": "HELM",
            "summary": "metrics-server 0.6.x -> 0.9.x (0.9.0)",
        },
        {
            "order": 2,
            "kind": "COMPONENT_MOVE",
            "component": "coredns",
            "from_series": "1.11.x",
            "to_series": "1.12.x",
            "to_version": "v1.12.0-eksbuild.1",
            "install_method": "EKS_MANAGED_ADDON",
            "summary": "coredns 1.11.x -> 1.12.x (v1.12.0-eksbuild.1)",
        },
    ],
    "approver_arns": ["arn:aws:sts::123456789012:assumed-role/Admin/approver-session"],
    # EVERY LISTED APPROVER, ANSWERED OR NOT, which is what `aws:approve` emits. The dedicated
    # approver role is listed and answered `Waiting`; only the principal that answered `Approve`
    # reaches `approver_arns`. Recording both is what lets a reader see that the quorum was one.
    "approver_decisions": [
        {
            "Approver": "arn:aws:sts::123456789012:assumed-role/Admin/approver-session",
            "Decision": "Approve",
            "ApprovalDecisionTime": "2026-09-20T11:00:00Z",
        },
        {
            "Approver": "arn:aws:iam::123456789012:role/eks-addon-upgrades-release-approver",
            "Decision": "Waiting",
        },
    ],
    "approval_status": "Approved",
    "approver_authority": "AWS_IAM_SSM_APPROVE",
    "approved_at": "2026-09-20T11:00:00+00:00",
    "automation_execution_id": "auto-1",
}


def test_the_recorded_release_says_what_was_authorised_and_how_to_apply_it() -> None:
    """The document the lane planner reads, validated through the contract it must satisfy.

    Two properties the planner depends on, both of which used to require a second document:
    which component each authorised step moves, and which API to call for it.
    """
    from eks_addon_readiness.upgrade import Release

    release = Release.model_validate(RECORDED_RELEASE)
    assert [step.order for step in release.approved_steps] == [1, 2]
    assert [step.component for step in release.approved_steps] == ["metrics-server", "coredns"]
    assert [step.install_method for step in release.approved_steps] == [
        "HELM",
        "EKS_MANAGED_ADDON",
    ]
    assert [step.to_version for step in release.approved_steps] == [
        "0.9.0",
        "v1.12.0-eksbuild.1",
    ]
    # AND THE APPROVER IS ON IT, which is the whole reason the release gate exists.
    assert release.approver_arns == (
        "arn:aws:sts::123456789012:assumed-role/Admin/approver-session",
    )


def test_the_planner_reads_the_recorded_release_rather_than_rebuilding_it() -> None:
    """Asserted against the source, because a rebuild produces a plausible answer.

    A second derivation from the gate payload would work most of the time and disagree exactly when
    the plan and the payload had drifted -- which is the case the recorded document exists to pin.
    """
    source = (
        __import__("pathlib").Path(__file__).resolve().parent.parent
        / "eks_addon_readiness/stages/upgrade_report.py"
    ).read_text()
    assert "storage_keys.release_key" in source, (
        "`upgrade_report` does not read the recorded release object. The authorisation it acts on "
        "must be the one that was written, not one re-derived from the payload."
    )
    assert "from_gate_payload" not in source, (
        "`upgrade_report` rebuilds the release from the gate payload. That is a second "
        "implementation of `release_prep`'s join, free to disagree with the recorded "
        "authorisation about who authorised what."
    )
    assert "bucket=resolved.bucket" in source, (
        "the release read is not scoped to this run's bucket and cluster, so it could read "
        "another cluster's authorisation."
    )
