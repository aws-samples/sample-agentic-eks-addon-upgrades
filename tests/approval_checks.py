"""Checks over the approval surface: the report, its HTML view, and the approval transition.

Three claims are under test here, and they are the ones the whole always-approve change
rests on.

**The report shows a person what they are actually deciding.** Not the raw fact -- the
verdict delta. So the checks assert that the "if approved" column is computed with the real
frozen Stage 4 rule function, and that the headline names the change.

**Every number is computed in the document, never in the template.** Asserted structurally:
the renderer is parsed and checked to contain no arithmetic, and the digest of the document
is shown to be independent of rendering.

**An approval cannot be forged.** Wrong candidate digest, wrong report digest, no signed
principal, already-approved, rejected -- each refused, each with its own reason.

The IAM half of the argument -- that no other role can invoke the approval function
-- is not testable offline, because it is a property of a deployed resource policy. It is
checked live in `tests/iam_deny_check.sh` against the real policy simulator.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eks_addon_readiness.knowledge import ComponentKnowledge  # noqa: E402
from eks_addon_readiness.models import Confidence, KubernetesHop  # noqa: E402
from eks_addon_readiness.research import (  # noqa: E402
    AcceptanceDecision,
    ApprovalRefused,
    BoundKind,
    CompatibilityStatementInput,
    FetchedPage,
    PageSet,
    ReleaseSightingInput,
    ResearchExtraction,
    SourceRef,
    advisory_note,
    approve,
    build_report,
    is_pending,
    judge,
    render_approval_html,
    verdict_delta,
)
from eks_addon_readiness.research.report import (  # noqa: E402
    bases_for,
    quoted_claims_for,
)

FIXTURES = ROOT / "tests" / "fixtures" / "sources"
MS_URL = (
    "https://raw.githubusercontent.com/kubernetes-sigs/metrics-server/"
    "4062beeed6ef996ba3ff7164967edafd3470e2e4/README.md"
)
MS_REV = "4062beeed6ef996ba3ff7164967edafd3470e2e4"
MS_REPO = "https://github.com/kubernetes-sigs/metrics-server"
STAMP = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

_results: list[tuple[bool, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((bool(condition), name, detail))
    print(
        f"  {'ok  ' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else "")
    )


def ms_row(series: str) -> str:
    body = (FIXTURES / "metrics-server-README.md").read_text(encoding="utf-8")
    for line in body.splitlines():
        if line.startswith(f"{series}  "):
            return line
    raise AssertionError(f"row {series} not in the fixture")


def pages() -> PageSet:
    ps = PageSet()
    ps.record(
        FetchedPage(
            MS_URL,
            MS_REV,
            (FIXTURES / "metrics-server-README.md").read_text(encoding="utf-8"),
            200,
        )
    )
    return ps


def extraction() -> ResearchExtraction:
    """metrics-server's real table, transcribed as published minimums."""
    rows = [("0.9.x", "1.34"), ("0.8.x", "1.31"), ("0.7.x", "1.27"), ("0.6.x", "1.25")]
    return ResearchExtraction(
        found=True,
        component_id="metrics-server",
        version_series="0.6.x",
        canonical_repository=MS_REPO,
        official_documentation=(MS_REPO,),
        statements=tuple(
            CompatibilityStatementInput(
                version_spec=spec,
                kind=BoundKind.PUBLISHED_MINIMUM,
                stated_minimum=low,
                served_api_versions=("metrics.k8s.io/v1beta1",),
                quote=ms_row(spec),
                source=SourceRef(url=MS_URL, revision=MS_REV),
            )
            for spec, low in rows
        ),
        releases=(
            ReleaseSightingInput(
                version="0.6.4",
                quote=ms_row("0.6.x"),
                source=SourceRef(url=MS_URL, revision=MS_REV),
            ),
            ReleaseSightingInput(
                version="0.9.0",
                quote=ms_row("0.9.x"),
                source=SourceRef(url=MS_URL, revision=MS_REV),
            ),
        ),
        recommended_version="0.9.0",
    )


def built():
    """One pending candidate and its approval report, over the real fixture bytes."""
    ps = pages()
    ext = extraction()
    candidate = judge(ext, ps, None, retrieved_at=STAMP)
    report = build_report(
        candidate,
        current=None,
        quoted_claims=quoted_claims_for(ext, ps),
        proposed_bases=bases_for(ext.statements),
        researched_at=STAMP,
        installed_version="0.6.4",
        cluster="my-eks-cluster",
        hops=(
            KubernetesHop(current_version="1.35", target_version="1.36"),
        ),
        advisory=advisory_note(
            candidate.receipt.confidence,
            candidate.receipt.contradictions,
            candidate.receipt.change_direction,
        ),
    )
    return candidate, report


def lane_report_content() -> None:
    print("\n===== the approval report shows what a person is deciding =====")
    candidate, report = built()

    check(
        "the candidate is pending, so it needs approval to matter at all",
        is_pending(candidate),
        f"{candidate.receipt.decision}",
    )
    check(
        "the report carries the verbatim quote for every proposed value",
        report.claims and all(c.quote.strip() for c in report.claims),
        f"{len(report.claims)} quoted values",
    )
    check(
        "each quote names its source URL and pinned commit",
        all(c.source_url and c.source_revision for c in report.claims),
        f"{report.claims[0].source_url[-40:]} @ {report.claims[0].source_revision[:12]}",
    )
    check(
        "each value carries its OWN quote verdict, so a reader sees which one failed",
        {c.quote_verdict for c in report.claims} <= {
            "PRESENT",
            "PRESENT_WHITESPACE",
            "WRONG_PAGE",
            "ABSENT",
            "NOT_FETCHED",
        },
        f"verdicts seen: {sorted({c.quote_verdict for c in report.claims})}",
    )
    check(
        "the report shows the computed confidence",
        report.confidence in tuple(Confidence),
        str(report.confidence),
    )
    check(
        "AND which signals fired, so confidence is not opaque",
        len(report.signals) >= 5
        and all(s.detail.strip() for s in report.signals),
        f"{sum(1 for s in report.signals if s.fired)} of {len(report.signals)} fired",
    )
    check(
        "the report shows coverage as numbers a reader can check",
        report.values_total > 0,
        f"{report.values_verified}/{report.values_total} values traced to verified quotes",
    )
    check(
        "the report shows what the store holds NOW, side by side with the proposal",
        report.current_windows == () and len(report.proposed_windows) == 4,
        "nothing held; 4 windows proposed",
    )
    check(
        "each proposed window carries the BASIS for its ceiling",
        all(row.basis for row in report.proposed_windows),
        report.proposed_windows[0].basis[:60],
    )
    check(
        "the source authority is shown, derived by code",
        all(c.source_authority for c in report.claims),
        sorted({c.source_authority for c in report.claims})[0],
    )
    check(
        "the advisory leads with the consequence of being wrong",
        "FIRST fact" in report.advisory or "WIDENS" in report.advisory
        or "NARROWS" in report.advisory,
        report.advisory[:56],
    )


def lane_verdict_delta() -> None:
    print("\n===== the verdict delta IS the decision, and it uses the frozen rules =====")
    candidate, report = built()

    check(
        "the report computes a delta per hop",
        len(report.verdict_deltas) == 1,
        f"{len(report.verdict_deltas)} hop(s)",
    )
    delta = report.verdict_deltas[0]
    check(
        "with no fact held, the verdict now is UNRESOLVED",
        delta.verdict_now == "UNRESOLVED",
        delta.reason_now[:60],
    )
    check(
        "approving changes it, and the change is what the headline names",
        delta.changes and delta.verdict_if_approved != "UNRESOLVED",
        f"{delta.verdict_now} -> {delta.verdict_if_approved}",
    )
    check(
        "installed 0.6.4 at Kubernetes 1.36 becomes REQUIRES_COMPONENT_UPGRADE",
        delta.verdict_if_approved == "REQUIRES_COMPONENT_UPGRADE",
        delta.reason_if_approved[:72],
    )
    check(
        "the headline states the consequence in one sentence",
        "UNRESOLVED -> REQUIRES_COMPONENT_UPGRADE" in report.headline(),
        report.headline()[:96],
    )
    # The frozen rule module is what produced it -- not a re-implementation.
    from eks_addon_readiness.decide import rules as frozen_rules

    check(
        "the delta is computed with the digest-frozen Stage 4 rule function",
        "decide/rules.py" in frozen_rules.verdict_for_hop.__code__.co_filename.replace(
            "\\", "/"
        ),
        "so the 'if approved' column is what the pipeline will actually say",
    )
    # A delta with a fact already held shows a real before-and-after.
    with_prior = verdict_delta(
        "metrics-server",
        "0.6.4",
        "0.6.x",
        candidate.knowledge,
        candidate.knowledge,
        (KubernetesHop(current_version="1.35", target_version="1.36"),),
    )
    check(
        "approving an identical fact changes no verdict, and the report can say so",
        not with_prior[0].changes,
        f"{with_prior[0].verdict_now} -> {with_prior[0].verdict_if_approved}",
    )


def lane_html_is_a_projection() -> None:
    print("\n===== the HTML computes nothing: one document, formats are views =====")
    _, report = built()
    html = render_approval_html(
        report,
        approve_endpoint="https://approve.example.test/",
        candidate_uri="s3://bucket/facts-candidates/decision=PENDING_APPROVAL/c.json",
        region="us-east-1",
    )

    check("the HTML renders", html.startswith("<!DOCTYPE html>") and len(html) > 4000, f"{len(html)} bytes")
    for needle, label in (
        (report.headline()[:40], "the headline"),
        ("PENDING_APPROVAL", "the decision"),
        ("QUOTE_MATCHES_SOURCE", "the signal names"),
        ("REQUIRES_COMPONENT_UPGRADE", "the verdict delta"),
        ("1.25+", "a verbatim quote"),
        (MS_REV[:12], "the pinned commit"),
        ("Approve &mdash; stores the fact", "the approve control"),
        ("Reject &mdash;", "the reject control"),
        ("REPLACE-WITH-WHAT-YOU-CHECKED", "the comment placeholder a human must replace"),
        (report.digest(), "the report digest"),
    ):
        check(f"the page shows {label}", needle in html, needle[:44])

    # WAS: `name="report_digest" value="..."` on a form. That form could never work --
    # a browser sends no SigV4 signature, so an AWS_IAM function URL refused it at the
    # authorizer with a bare 403 before the handler ran. The control is now a signed
    # command, so the digest travels in the command's BODY instead of a hidden input.
    # The command sits inside a <pre>, so its quotes are HTML-escaped. Asserted in the
    # escaped form deliberately: that is the byte sequence a reader copies, and an
    # assertion on the unescaped form would pass on markup no browser would show.
    check(
        "the signed command carries the report digest, so the audit trail records what was seen",
        f"&quot;report_digest&quot;:&quot;{report.digest()}&quot;" in html,
    )
    check(
        "the page contains NO form: a form posting to an AWS_IAM URL 403s unsigned",
        "<form" not in html and "<button" not in html,
    )
    check(
        "the command signs with the caller's own credentials; none is baked into the page",
        "--aws-sigv4 &#x27;aws:amz:us-east-1:lambda&#x27;" in html
        and "X-Amz-Signature" not in html,
    )
    check(
        "the page states that the agent cannot approve",
        "the endpoint accepts only the approver role" in html
        and "cannot act on it" in html,
    )
    # THIS CHECK USED TO PIN THE OVERSTATEMENT IT NOW FORBIDS. It asserted the literal
    # "denied permission to call the endpoint", so the page's claim of an EXPLICIT IAM
    # Deny on an agent's role was held in place by a passing test -- while no
    # such Deny exists on that role, or on any role but the four tool roles. The check's
    # name was always the real intent ("the page states that the agent cannot approve");
    # its assertion had drifted into pinning one particular, and untrue, reason.
    #
    # So the reason is now asserted in BOTH directions: the true one must be present, and
    # the overstated one must not come back. A page that asks a human to approve a fact is
    # the last place a security claim should be stronger than the policy behind it.
    check(
        "and does NOT overstate it as an explicit IAM Deny on the agent's role",
        "explicitly denied" not in html and "denied permission" not in html,
    )
    check(
        "with no endpoint the page is read-only and SAYS so, rather than a dead control",
        "read-only" in render_approval_html(report)
        and "aws-sigv4" not in render_approval_html(report),
    )

    # Structural: no arithmetic in the renderer.
    import ast

    src = Path(
        ROOT / "eks_addon_readiness" / "research" / "render_html.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(src)
    arithmetic = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div))
        and not isinstance(node.op, ast.Add)
    ]
    comparisons = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(o, (ast.Lt, ast.Gt, ast.LtE, ast.GtE)) for o in node.ops)
    ]
    calls_len = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("len", "sum", "sorted", "min", "max")
    ]
    check(
        "the renderer performs no arithmetic and no counting",
        not arithmetic and not comparisons and not calls_len,
        f"arith={arithmetic} cmp={comparisons} counting={calls_len}",
    )
    check(
        "the report digest does not depend on rendering",
        report.digest() == built()[1].digest(),
        "so the digest an approval records is reproducible",
    )


def lane_approval_transition() -> None:
    print("\n===== an approval cannot be forged, and it records what was shown =====")
    candidate, report = built()
    good = dict(
        approver="arn:aws:sts::111122223333:assumed-role/Admin/alice",
        comment="Checked the 0.6.x row against the README at that commit.",
        report_digest=report.digest(),
        submitted_candidate_digest=candidate.digest(),
        expected_report_digest=report.digest(),
        approved_at=STAMP + timedelta(minutes=5),
    )

    approved = approve(candidate, **good)
    check(
        "a valid approval moves the decision to APPROVED",
        approved.receipt.decision is AcceptanceDecision.APPROVED,
    )
    record = approved.receipt.approval
    check("it records the approver's principal ARN", record.approver.endswith("/alice"), record.approver)
    check("it records when", record.approved_at == good["approved_at"])
    check("it records their comment", "Checked the 0.6.x row" in record.comment)
    check(
        "it records the digest of the report they were shown",
        record.report_digest == report.digest(),
        "this is what proves WHAT was approved, not merely that something was",
    )
    check(
        "it records the candidate digest, so an approval cannot be replayed elsewhere",
        record.candidate_digest == candidate.digest(),
    )
    check(
        "the original pending candidate is unchanged",
        candidate.receipt.decision is AcceptanceDecision.PENDING_APPROVAL,
        "contracts are frozen; the proposal stays on record",
    )
    check("the approved candidate reports itself approved", approved.approved)

    for label, override, expect in (
        (
            "a wrong candidate digest",
            {"submitted_candidate_digest": "sha256:" + "0" * 64},
            "names a different candidate",
        ),
        (
            "a wrong report digest",
            {"report_digest": "sha256:" + "0" * 64},
            "does not match the report",
        ),
        ("an empty approver", {"approver": "   "}, "no signed principal"),
    ):
        try:
            approve(candidate, **{**good, **override})
            check(f"{label} is refused", False, "it was accepted")
        except ApprovalRefused as exc:
            check(f"{label} is refused", expect in str(exc), str(exc)[:64])

    try:
        approve(approved, **good)
        check("re-approving an APPROVED candidate is refused", False, "it was accepted")
    except ApprovalRefused as exc:
        check(
            "re-approving an APPROVED candidate is refused",
            "not PENDING_APPROVAL" in str(exc),
            "so an audit record cannot be overwritten",
        )

    # And the write path: only APPROVED, only with a record.
    from eks_addon_readiness.storage.facts import DynamoDbFactWriter, FactRejected

    writer = DynamoDbFactWriter("table-never-called")
    try:
        writer.write_approved(candidate)
        check("the writer refuses a PENDING candidate", False, "it wrote it")
    except FactRejected as exc:
        check("the writer refuses a PENDING candidate", "APPROVED" in str(exc), str(exc)[:58])


def lane_pending_does_not_block() -> None:
    print("\n===== a pending fact blocks nothing: the run completes and answers the rest =====")
    from eks_addon_readiness.decide.rules import verdict_for_hop
    from eks_addon_readiness.facts.gaps import _entry_for

    candidate, _ = built()

    class _Component:
        workload_ref = "deploy/x"
        identity = "metrics-server"
        version = "0.6.4"
        assessment_priority = 1

    class _EmptyStore:
        def find(self, identity):
            return None

        def digest(self):
            return "sha256:" + "0" * 64

    # A pending fact is not in the store, so Stage 3's normal missing-fact path runs.
    entry = _entry_for(
        _Component(), insights={}, store=_EmptyStore(), store_readable=True
    )
    check(
        "Stage 3 reports the component as a typed, researchable gap",
        str(entry.status) == "FACT_MISSING" and str(entry.detail_code) == "NO_ENTRY",
        f"{entry.status} / {entry.detail_code}",
    )
    verdict, _, _, reason = verdict_for_hop(
        entry,
        None,
        KubernetesHop(current_version="1.35", target_version="1.36"),
        is_first_hop=True,
    )
    check(
        "Stage 4 answers UNRESOLVED, honestly, rather than stalling",
        str(verdict) == "UNRESOLVED",
        reason[:64],
    )
    check(
        "and it does NOT say incompatible",
        "not evidence of incompatibility" in reason,
        "absence of a fact is not evidence of incompatibility",
    )
    check(
        "the pending candidate is discoverable by its key prefix",
        candidate.pending,
        "facts-candidates/decision=PENDING_APPROVAL/ is what the pending listing reads",
    )
    # The listing prefix the approval handler reads must match the key the stage writes.
    from eks_addon_readiness.stages.approve import PENDING_PREFIX
    from eks_addon_readiness.storage.keys import candidate_key

    key = candidate_key(
        candidate.component_id,
        candidate.version_series,
        str(candidate.receipt.decision),
        candidate.digest(),
        STAMP.isoformat(),
    )
    check(
        "the listing prefix matches the key the stage actually writes",
        key.startswith(PENDING_PREFIX),
        f"{PENDING_PREFIX} vs {key[:52]}",
    )


def lane_prior_fact_makes_a_real_delta() -> None:
    print("\n===== with a fact already held, the report shows a true before and after =====")
    citation = {
        "url": MS_URL,
        "title": "prior",
        "source_revision": MS_REV,
        "retrieved_at": "2026-09-01T00:00:00Z",
    }
    # A prior fact shaped like the earlier wrong one: 0.6.x from 1.25 with NO ceiling, which is
    # what declared 0.6.4 fit for 1.36.
    prior = ComponentKnowledge.model_validate(
        {
            "knowledge_base_revision": "metrics-server-researched-2026-09-09",
            "canonical_id": "metrics-server",
            "canonical_repository": MS_REPO,
            "official_documentation": [MS_REPO],
            "compatibility_rules": [
                {
                    "component_version_series": "0.6.x",
                    "kubernetes_versions": {"minimum_inclusive": "1.25"},
                    "evidence": [citation],
                }
            ],
            "releases": [
                {
                    "version": "0.6.4",
                    "released_at": "2020-01-01T00:00:00Z",
                    "source_revision": MS_REV,
                    "evidence": [citation],
                }
            ],
            "verification_checks": [
                {"check_id": "serves-its-api", "description": "d", "evidence": [citation]}
            ],
            "default_upgrade_strategy": "SEQUENTIAL_MINOR",
            "refreshed_at": "2026-09-09T00:00:00Z",
            "evidence_confidence": "HIGH",
        }
    )
    ps, ext = pages(), extraction()
    candidate = judge(ext, ps, prior, retrieved_at=STAMP)
    report = build_report(
        candidate,
        current=prior,
        quoted_claims=quoted_claims_for(ext, ps),
        proposed_bases=bases_for(ext.statements),
        researched_at=STAMP,
        installed_version="0.6.4",
        cluster="my-eks-cluster",
        hops=(KubernetesHop(current_version="1.35", target_version="1.36"),),
        advisory=advisory_note(
            candidate.receipt.confidence,
            candidate.receipt.contradictions,
            candidate.receipt.change_direction,
        ),
    )
    check(
        "the currently-held ceiling-less window is shown as it is",
        report.current_windows
        and "no recorded ceiling" in report.current_windows[0].window,
        report.current_windows[0].window if report.current_windows else "",
    )
    check(
        "the change is classified as NARROWING against it",
        str(report.change_direction) == "NARROWING",
        "the proposal bounds a window the held fact left open",
    )
    check(
        "the advisory names the cheap direction of being wrong",
        "NARROWS" in report.advisory,
        report.advisory[:56],
    )
    delta = report.verdict_deltas[0]
    check(
        "the held fact says COMPATIBLE at 1.36 -- the defect this guards against",
        delta.verdict_now == "COMPATIBLE",
        delta.reason_now[:70],
    )
    check(
        "and approving this proposal corrects it to REQUIRES_COMPONENT_UPGRADE",
        delta.verdict_if_approved == "REQUIRES_COMPONENT_UPGRADE",
        delta.reason_if_approved[:70],
    )
    Path("/tmp/approval-report-sample.html").write_text(
        render_approval_html(
            report,
            approve_endpoint="https://approve.example.test/",
            candidate_uri="s3://bucket/facts-candidates/decision=PENDING_APPROVAL/c.json",
            region="us-east-1",
        ),
        encoding="utf-8",
    )
    check(
        "a sample HTML report is written for visual review",
        Path("/tmp/approval-report-sample.html").stat().st_size > 6000,
        "/tmp/approval-report-sample.html",
    )


def main() -> int:
    print("APPROVAL CHECKS")
    print(f"date: {datetime.now(UTC).isoformat()}")
    print(f"python: {sys.version.split()[0]}")

    lane_report_content()
    lane_verdict_delta()
    lane_html_is_a_projection()
    lane_approval_transition()
    lane_pending_does_not_block()
    lane_prior_fact_makes_a_real_delta()

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\nAPPROVAL CHECKS: {passed} passed, {failed} failed")
    if failed:
        print("FAILURES:")
        for ok, name, detail in _results:
            if not ok:
                print(f"  - {name}  {detail}")
    print(f"APPROVAL CHECKS EXIT={1 if failed else 0}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
