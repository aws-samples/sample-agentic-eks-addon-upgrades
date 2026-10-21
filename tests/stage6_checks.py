"""Stage 6 checks: the two demonstrable outcomes first, then every hole individually.

Run with `python3 tests/stage6_checks.py` from the repository root, or via
`tests/run_checks.py`, which drives this and the other lanes and reports a per-lane exit
code. Needs only pydantic -- no model, no network, no AWS. That is a property of the
design rather than a convenience: `judge()` takes its evidence as an argument, so the
whole judging layer is testable offline.

Fixtures are **real bytes**, fetched once from the sources originally cited and committed
under `tests/fixtures/sources/`:

    keda-docs-2.20-cluster.md     19583 B  @fe5ba53d5692bc110e3b4f68802eb1924246a213
    metrics-server-README.md      11943 B  @4062beeed6ef996ba3ff7164967edafd3470e2e4

Using the real bytes matters. A hand-written fixture would let a check pass against text
shaped to fit the code, which is the mistake at the heart of the defect being fixed: an earlier
quote check compared the model's value against the model's quote and passed on a table
that existed nowhere.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eks_addon_readiness.knowledge import (  # noqa: E402
    ComponentKnowledge,
)
from eks_addon_readiness.models import Confidence  # noqa: E402
from eks_addon_readiness.research import (  # noqa: E402
    AcceptanceDecision,
    BoundKind,
    ChangeDirection,
    CompatibilityStatementInput,
    ExtractedClaimInput,
    FetchedPage,
    MarkKind,
    MarkLedger,
    PageSet,
    ReleaseSightingInput,
    ResearchExtraction,
    SourceAuthority,
    SourceRef,
    advisory_note,
    classify_source,
    decide,
    judge,
)
from eks_addon_readiness.research.bounds import derive_windows  # noqa: E402
from eks_addon_readiness.research.freshness import (  # noqa: E402
    StaticUpstreamResolver,
    retire_marks,
    revoke_mark,
    sweep,
)
from eks_addon_readiness.storage.facts import (  # noqa: E402
    DynamoDbFactWriter,
    FactRejected,
)
from eks_addon_readiness.storage.marks import MarkedFactStore  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "sources"

KEDA_URL = (
    "https://raw.githubusercontent.com/kedacore/keda-docs/"
    "fe5ba53d5692bc110e3b4f68802eb1924246a213/content/docs/2.20/operate/cluster.md"
)
KEDA_REV = "fe5ba53d5692bc110e3b4f68802eb1924246a213"
KEDA_REPO = "https://github.com/kedacore/keda-docs"

MS_URL = (
    "https://raw.githubusercontent.com/kubernetes-sigs/metrics-server/"
    "4062beeed6ef996ba3ff7164967edafd3470e2e4/README.md"
)
MS_REV = "4062beeed6ef996ba3ff7164967edafd3470e2e4"
MS_REPO = "https://github.com/kubernetes-sigs/metrics-server"

STAMP = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

_results: list[tuple[bool, str, str]] = []



# Upstream resolvers for the freshness lane. The synthetic facts below live on
# github.com/example/repo and are read between 2026-09-11 06:00 and 2026-09-12 09:00, so these
# two express "nothing has been released since either fact was read" and "a release landed
# after both of them were read" without any network call.
_SYNTHETIC_REPO = "example/repo"
_QUIET = StaticUpstreamResolver(
    {_SYNTHETIC_REPO: {"tag": "v1.0.0", "published_at": "2026-09-01T00:00:00Z"}}
)
_RELEASED = StaticUpstreamResolver(
    {_SYNTHETIC_REPO: {"tag": "v1.1.0", "published_at": "2026-09-12T11:00:00Z"}}
)

def _contradiction():
    """One synthetic Contradiction, for the routing checks that need any at all."""
    from eks_addon_readiness.models import Citation
    from eks_addon_readiness.research.receipt import Contradiction

    def cite(url: str) -> Citation:
        return Citation(
            url=url, title="t", source_revision="r", retrieved_at=STAMP
        )

    return Contradiction(
        field="stated_minimum[1.0.x]",
        left_value="1.25",
        right_value="1.27",
        left_citation=cite("https://a.test/x"),
        right_citation=cite("https://b.test/x"),
    )


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((bool(condition), name, detail))
    print(f"  {'ok  ' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))


def load(filename: str, url: str, revision: str) -> FetchedPage:
    body = (FIXTURES / filename).read_text(encoding="utf-8")
    return FetchedPage(url=url, revision=revision, body=body, status=200)


def keda_pages() -> PageSet:
    pages = PageSet()
    pages.record(load("keda-docs-2.20-cluster.md", KEDA_URL, KEDA_REV))
    return pages


def metrics_server_pages() -> PageSet:
    pages = PageSet()
    pages.record(load("metrics-server-README.md", MS_URL, MS_REV))
    return pages


def keda_row_quote(series_row: str) -> str:
    """The verbatim table row for a KEDA series, taken from the real fixture."""
    body = (FIXTURES / "keda-docs-2.20-cluster.md").read_text(encoding="utf-8")
    for line in body.splitlines():
        if line.strip().startswith(f"| {series_row} "):
            return line
    raise AssertionError(f"row {series_row} not in the KEDA fixture")


def ms_row_quote(series: str) -> str:
    body = (FIXTURES / "metrics-server-README.md").read_text(encoding="utf-8")
    for line in body.splitlines():
        if line.startswith(f"{series}  "):
            return line
    raise AssertionError(f"row {series} not in the metrics-server fixture")


def keda_qualifier() -> str:
    body = (FIXTURES / "keda-docs-2.20-cluster.md").read_text(encoding="utf-8")
    for line in body.splitlines():
        if "no guarantee" in line:
            return line
    raise AssertionError("the disclaimer sentence is not in the KEDA fixture")


# --------------------------------------------------------------------------------------
# The two demonstrable outcomes the work is judged on
# --------------------------------------------------------------------------------------


def keda_extraction(kind: BoundKind) -> ResearchExtraction:
    """KEDA's real table, transcribed faithfully, under a chosen statement kind.

    Parameterised on the kind so the pair of checks below differ in exactly one thing: the
    numbers are identical and real in both, and only what the source *called* them
    changes. That is the whole claim of the contract change, made falsifiable.
    """
    rows = {
        "v2.20": ("2.20.x", "1.33", "1.35"),
        "v2.19": ("2.19.x", "1.32", "1.34"),
        "v2.18": ("2.18.x", "1.31", "1.33"),
        "v2.17": ("2.17.x", "1.30", "1.32"),
    }
    statements = tuple(
        CompatibilityStatementInput(
            version_spec=spec,
            kind=kind,
            stated_minimum=low,
            stated_maximum_inclusive=high,
            quote=keda_row_quote(row),
            source=SourceRef(url=KEDA_URL, revision=KEDA_REV),
            qualifier_quote=keda_qualifier() if kind is BoundKind.TESTED_RANGE else None,
        )
        for row, (spec, low, high) in rows.items()
    )
    return ResearchExtraction(
        found=True,
        component_id="keda",
        version_series="2.17.x",
        canonical_repository=KEDA_REPO,
        official_documentation=(KEDA_REPO,),
        claims=(
            ExtractedClaimInput(
                field="kubernetes_range for 2.17.x",
                value="v1.30 - v1.32",
                source=SourceRef(url=KEDA_URL, revision=KEDA_REV),
                quote=keda_row_quote("v2.17"),
            ),
        ),
        statements=statements,
        releases=(
            ReleaseSightingInput(
                version="2.17.0",
                quote=keda_row_quote("v2.17"),
                source=SourceRef(url=KEDA_URL, revision=KEDA_REV),
            ),
        ),
    )


def lane_keda_no_supported_ceiling() -> None:
    print("\n===== DONE-CONDITION 1: a tested window derives, and the WORD travels =====")
    pages = keda_pages()

    # First: the quotes are real. If this failed, everything below would be meaningless.
    quote = keda_row_quote("v2.17")
    check(
        "the 2.17 row is present byte-for-byte in the fetched fixture",
        quote in pages.get(KEDA_URL, KEDA_REV).body,
        repr(quote.strip()),
    )
    check(
        "the disclaimer sentence is present byte-for-byte",
        "there is no guarantee" in pages.get(KEDA_URL, KEDA_REV).body,
    )

    candidate = judge(
        keda_extraction(BoundKind.TESTED_RANGE), pages, None, retrieved_at=STAMP
    )

    # THE DONE-CONDITION HERE WAS REVERSED from an older rule: a published tested matrix IS
    # the matrix a platform engineer acts on, so it
    # derives a window. What the old rule protected is preserved a different way -- the
    # WORD travels. These checks assert the reversal AND the preservation.
    check(
        "a fact IS produced from a tested window",
        candidate.knowledge is not None,
        f"decision={candidate.receipt.decision}",
    )
    check(
        "the candidate is PENDING_APPROVAL -- proposed to a person, never auto-stored",
        candidate.receipt.decision is AcceptanceDecision.PENDING_APPROVAL,
    )
    check(
        "the receipt still records the tested windows with their qualifiers",
        len(candidate.receipt.tested_windows) == 4,
        f"{candidate.receipt.tested_windows[:1]}",
    )
    check(
        "the basis names the claim as TESTED, not supported",
        any(
            "TESTED" in d.basis
            for d in derive_windows(keda_extraction(BoundKind.TESTED_RANGE).statements)
            if d.storable
        ),
        "the word travels with the window",
    )
    ceilings = sorted(
        r.kubernetes_versions.maximum_exclusive
        for r in candidate.knowledge.compatibility_rules
    )
    check(
        "the ceiling is the exclusive next-minor of the stated top",
        "1.33" in ceilings,
        f"ceilings={ceilings}",
    )

    # The same numbers, called supported, produce the same window -- the KIND no longer
    # gates derivation; it survives as the basis wording a reader sees.
    supported = judge(
        keda_extraction(BoundKind.SUPPORTED_RANGE), pages, None, retrieved_at=STAMP
    )
    check(
        "the same numbers stated as SUPPORTED produce the same bounded window",
        supported.knowledge is not None
        and any(
            r.component_version_series == "2.17.x"
            and r.kubernetes_versions.maximum_exclusive == "1.33"
            for r in supported.knowledge.compatibility_rules
        ),
        "same arithmetic; the difference is the recorded basis",
    )


def lane_assessment_rechecked() -> None:
    print("\n===== the assessment is re-checked, and can only LOWER confidence =====")
    from eks_addon_readiness.research.boundary import (
        AnchorBasis,
        Assessment,
        CitedSource,
        EvidenceTier,
        InstalledVersionStatus,
        PolicyDerivation,
        ProposedConfidence,
        RecommendationAction,
        UpgradeOrdering,
    )

    pages = keda_pages()
    base = keda_extraction(BoundKind.TESTED_RANGE)
    cited = (
        CitedSource(url=KEDA_URL, revision=KEDA_REV, used_for="compatibility matrix"),
    )

    def assessed(**overrides):
        fields = dict(
            installed_version="2.17.0",
            current_kubernetes="1.33",
            target_kubernetes="1.34",
            installed_version_status=InstalledVersionStatus.OUTSIDE_STATED_WINDOW,
            action=RecommendationAction.UPGRADE,
            recommended_version="2.17.0",
            recommended_series="2.19.x",
            upgrade_ordering=UpgradeOrdering.UNKNOWN,
            evidence_tier=EvidenceTier.MATRIX_IN_OFFICIAL_DOCS,
            proposed_confidence=ProposedConfidence.HIGH,
            confidence_rationale="the v2.19 row lists v1.32 - v1.34; target is 1.34",
            cited_sources=cited,
        )
        fields.update(overrides)
        return base.model_copy(update={"assessment": Assessment(**fields)})

    # An honest assessment: recommended release was seen, its series' window covers the
    # target. The fixture's single release is 2.17.0, so the recommendation names it
    # while pointing coverage at 2.19.x -- covered by the derived TESTED window.
    sound = judge(assessed(), pages, None, retrieved_at=STAMP)
    fired = {str(x.signal): x.passed for x in sound.receipt.signals}
    check(
        "the three assessment signals appear on the receipt",
        all(
            k in fired
            for k in (
                "RELEASES_PRESENT",
                "RECOMMENDATION_IS_A_SEEN_RELEASE",
                "TARGET_COVERED_BY_EVIDENCE",
            )
        ),
        f"signals={sorted(fired)}",
    )
    check(
        "recommendation and coverage both check out",
        fired["RECOMMENDATION_IS_A_SEEN_RELEASE"] and fired["TARGET_COVERED_BY_EVIDENCE"],
    )
    check(
        "the receipt shows the arithmetic: machine, proposed, tier",
        sound.receipt.machine_confidence is not None
        and sound.receipt.proposed_confidence is Confidence.HIGH
        and sound.receipt.evidence_tier == "MATRIX_IN_OFFICIAL_DOCS",
        f"machine={sound.receipt.machine_confidence} final={sound.receipt.confidence}",
    )
    check(
        "final confidence never exceeds what the machine computed",
        _rank(sound.receipt.confidence) <= _rank(sound.receipt.machine_confidence),
        "the model can lower, never raise",
    )

    # The model proposes LOW: the proposal caps the final level even when the machine
    # would have said more.
    humble = judge(
        assessed(proposed_confidence=ProposedConfidence.LOW), pages, None,
        retrieved_at=STAMP,
    )
    check(
        "a LOW proposal drags the final level to LOW",
        humble.receipt.confidence is Confidence.LOW,
        f"machine={humble.receipt.machine_confidence}",
    )

    # A recommendation naming a release the extraction never returned fails its check
    # and caps the final level at LOW -- the conclusion outran its own evidence.
    phantom = judge(
        assessed(recommended_version="9.9.9"), pages, None, retrieved_at=STAMP
    )
    fired = {str(x.signal): x.passed for x in phantom.receipt.signals}
    check(
        "a phantom recommendation fails RECOMMENDATION_IS_A_SEEN_RELEASE",
        not fired["RECOMMENDATION_IS_A_SEEN_RELEASE"],
    )
    check(
        "and the failed check caps the final confidence at LOW",
        _rank(phantom.receipt.confidence) <= _rank(Confidence.LOW),
        f"final={phantom.receipt.confidence}",
    )

    # A wrong N-2 derivation is recomputed and caught: anchor 1.34, window 3 gives
    # 1.32..1.34, not the 1.30 the model wrote.
    wrong_math = judge(
        assessed(
            evidence_tier=EvidenceTier.POLICY_DERIVED,
            derivation=PolicyDerivation(
                policy_statement_index=0,
                window_size=3,
                anchor_kubernetes_minor="1.34",
                anchor_basis=AnchorBasis.SOURCE_STATED,
                derived_minimum="1.30",
                derived_maximum_inclusive="1.34",
                applies_to_series="2.19.x",
            ),
        ),
        pages,
        None,
        retrieved_at=STAMP,
    )
    fired = {str(x.signal): x.passed for x in wrong_math.receipt.signals}
    check(
        "wrong policy arithmetic fails POLICY_DERIVATION_RECOMPUTES",
        not fired["POLICY_DERIVATION_RECOMPUTES"],
    )
    check(
        "and caps the final confidence at LOW",
        _rank(wrong_math.receipt.confidence) <= _rank(Confidence.LOW),
        f"final={wrong_math.receipt.confidence}",
    )


def _rank(level: Confidence) -> int:
    order = (
        Confidence.UNRESOLVED,
        Confidence.LOW,
        Confidence.MEDIUM,
        Confidence.HIGH,
    )
    return order.index(level)


def lane_failed_quote_never_stored() -> None:
    print(
        "\n===== DONE-CONDITION 2: a fact failing its quote check cannot reach the store ====="
    )
    pages = metrics_server_pages()

    # An earlier real splice: line-exact rows from two different commits of one README, composed
    # into a table that exists in neither. Reproduced verbatim from the diagnosis.
    spliced = (
        "Metrics Server | Metrics API group/version | Supported Kubernetes version\n"
        "---------------|---------------------------|-----------------------------\n"
        "0.9.x          | `metrics.k8s.io/v1beta1`  | 1.34+\n"
        "0.8.x          | `metrics.k8s.io/v1beta1`  | 1.31+\n"
        "0.7.x          | `metrics.k8s.io/v1beta1`  | 1.19+\n"
        "0.6.x          | `metrics.k8s.io/v1beta1`  | 1.19+"
    )
    check(
        "the spliced quote really is absent from the fetched bytes",
        spliced not in pages.get(MS_URL, MS_REV).body,
        "every line is real; the composite is not",
    )

    extraction = ResearchExtraction(
        found=True,
        component_id="metrics-server",
        version_series="0.7.x",
        canonical_repository=MS_REPO,
        official_documentation=(MS_REPO,),
        statements=(
            CompatibilityStatementInput(
                version_spec="0.7.x",
                kind=BoundKind.PUBLISHED_MINIMUM,
                stated_minimum="1.19",
                quote=spliced,
                source=SourceRef(url=MS_URL, revision=MS_REV),
            ),
        ),
        releases=(
            ReleaseSightingInput(
                version="0.7.0",
                quote=spliced,
                source=SourceRef(url=MS_URL, revision=MS_REV),
            ),
        ),
    )
    candidate = judge(extraction, pages, None, retrieved_at=STAMP)

    check(
        "the splice is caught: the quote is in no fetched page",
        any(
            not s.passed and str(s.signal) == "QUOTE_MATCHES_SOURCE"
            for s in candidate.receipt.signals
        ),
    )
    check(
        "confidence is UNRESOLVED, not LOW -- a composed quote is absent evidence",
        candidate.receipt.confidence is Confidence.UNRESOLVED,
        "the earlier implementation rated exactly this case LOW and stored it",
    )
    check(
        "the candidate is REJECTED, so it is not even proposed to a person",
        candidate.receipt.decision is AcceptanceDecision.REJECTED,
        f"decision={candidate.receipt.decision}",
    )

    # And the write path refuses independently of the gate, because one gate that a
    # one-line edit can disable is not a gate.
    writer = DynamoDbFactWriter("table-that-is-never-called")
    try:
        writer.write_approved(candidate)
        check("the fact writer refuses a non-approved candidate", False, "it wrote it")
    except FactRejected as exc:
        check(
            "the fact writer refuses a non-approved candidate independently",
            True,
            str(exc)[:70],
        )

    # The receipt contract refuses to represent an approval that names nobody.
    from eks_addon_readiness.research.receipt import (
        ResearchReceipt,
        SignalName,
        SignalResult,
    )

    try:
        ResearchReceipt(
            confidence=Confidence.HIGH,
            signals=(
                SignalResult(signal=SignalName.SHAPE_SANITY, passed=True, detail="ok"),
            ),
            change_direction=ChangeDirection.NARROWING,
            decision=AcceptanceDecision.APPROVED,
            summary="x",
            evidence_digest="sha256:0",
        )
        check("an APPROVED receipt with no approver cannot be built", False, "it validated")
    except ValueError as exc:
        check(
            "an APPROVED receipt with no ApprovalRecord cannot even be constructed",
            "must carry the ApprovalRecord" in str(exc),
            "so a fact cannot be approved by setting a status",
        )


# --------------------------------------------------------------------------------------
# Hole by hole
# --------------------------------------------------------------------------------------


def lane_acceptance_gate() -> None:
    print("\n===== HOLE 1: no fact is ever accepted automatically =====")
    # The enum itself is the invariant. A member that does not exist cannot be returned by
    # any code path, present or future -- which is stronger than any threshold.
    check(
        "AcceptanceDecision has NO AUTO_ACCEPT member at all",
        not hasattr(AcceptanceDecision, "AUTO_ACCEPT"),
        f"members: {[m.value for m in AcceptanceDecision]}",
    )
    check(
        "no confidence and direction combination auto-accepts",
        all(
            decide(c, (), d)
            in (AcceptanceDecision.PENDING_APPROVAL, AcceptanceDecision.REJECTED)
            for c in Confidence
            for d in ChangeDirection
        ),
        "every route is a proposal or nothing",
    )
    check(
        "HIGH + NARROWING -- the strongest case there is -- still only PROPOSES",
        decide(Confidence.HIGH, (), ChangeDirection.NARROWING)
        is AcceptanceDecision.PENDING_APPROVAL,
    )
    check(
        "a HIGH fact and a LOW fact take the identical route",
        decide(Confidence.HIGH, (), ChangeDirection.NEW)
        == decide(Confidence.LOW, (), ChangeDirection.NEW),
        "confidence is information for the approver, not a licence for the tool",
    )
    check(
        "UNRESOLVED with no contradiction is REJECTED -- nothing to propose",
        decide(Confidence.UNRESOLVED, (), ChangeDirection.NEW)
        is AcceptanceDecision.REJECTED,
    )
    check(
        "UNRESOLVED WITH a contradiction is proposed, because there is a judgement to make",
        decide(Confidence.UNRESOLVED, (_contradiction(),), ChangeDirection.NEW)
        is AcceptanceDecision.PENDING_APPROVAL,
    )
    # The asymmetry survives as advice, which is the thing the approver reads first.
    widening = advisory_note(Confidence.HIGH, (), ChangeDirection.WIDENING)
    narrowing = advisory_note(Confidence.HIGH, (), ChangeDirection.NARROWING)
    check(
        "the widen/narrow asymmetry is still stated, as a consequence not a rating",
        "WIDENS" in widening and "nothing supports" in widening,
        widening[:64],
    )
    check(
        "and the narrowing case names the cheaper failure",
        "NARROWS" in narrowing and "unnecessary" in narrowing,
        narrowing[:64],
    )
    check(
        "a LOW advisory tells the approver to read the quotes",
        "Read the quotes" in advisory_note(Confidence.LOW, (), ChangeDirection.NEW),
    )


def held_candidate():
    """A candidate that lands on the HELD path: MEDIUM confidence, widening.

    Prose-only first-party evidence caps at MEDIUM, and no prior fact plus a deliberately
    supplied prior makes the direction WIDENING.
    """
    pages = metrics_server_pages()
    row = ms_row_quote("0.6.x")
    extraction = ResearchExtraction(
        found=True,
        component_id="metrics-server",
        version_series="0.6.x",
        canonical_repository=MS_REPO,
        official_documentation=(MS_REPO,),
        statements=(
            CompatibilityStatementInput(
                version_spec="0.6.x",
                kind=BoundKind.SUPPORTED_RANGE,
                stated_minimum="1.25",
                stated_maximum_inclusive="1.30",
                quote=row,
                source=SourceRef(url=MS_URL, revision=MS_REV),
            ),
        ),
        releases=(
            ReleaseSightingInput(
                version="0.6.4",
                quote=row,
                source=SourceRef(url=MS_URL, revision=MS_REV),
            ),
        ),
        recommended_version="0.6.4",
    )
    return extraction, pages, row


def lane_held_path_works() -> None:
    print("\n===== HOLE 1b: the PENDING path works end to end (it was previously dead code) =====")
    extraction, pages, _ = held_candidate()
    # A prior with a narrower window makes the candidate WIDENING.
    prior_json = {
        "knowledge_base_revision": "prior",
        "canonical_id": "metrics-server",
        "canonical_repository": MS_REPO,
        "official_documentation": [MS_REPO],
        "compatibility_rules": [
            {
                "component_version_series": "0.6.x",
                "kubernetes_versions": {
                    "minimum_inclusive": "1.25",
                    "maximum_exclusive": "1.27",
                },
                "evidence": [
                    {
                        "url": MS_URL,
                        "title": "prior",
                        "source_revision": MS_REV,
                        "retrieved_at": "2026-09-01T00:00:00Z",
                    }
                ],
            }
        ],
        "releases": [
            {
                "version": "0.6.4",
                "released_at": "2020-01-01T00:00:00Z",
                "source_revision": MS_REV,
                "evidence": [
                    {
                        "url": MS_URL,
                        "title": "prior",
                        "source_revision": MS_REV,
                        "retrieved_at": "2026-09-01T00:00:00Z",
                    }
                ],
            }
        ],
        "verification_checks": [
            {
                "check_id": "prior-check",
                "description": "the prior entry's own check",
                "evidence": [
                    {
                        "url": MS_URL,
                        "title": "prior",
                        "source_revision": MS_REV,
                        "retrieved_at": "2026-09-01T00:00:00Z",
                    }
                ],
            }
        ],
        "default_upgrade_strategy": "SEQUENTIAL_MINOR",
        "refreshed_at": "2026-09-01T00:00:00Z",
        "evidence_confidence": "MEDIUM",
    }
    prior = ComponentKnowledge.model_validate(prior_json)
    candidate = judge(extraction, pages, prior, retrieved_at=STAMP)

    check(
        "the direction is computed as WIDENING against the prior",
        candidate.receipt.change_direction is ChangeDirection.WIDENING,
        f"{candidate.receipt.confidence} / {candidate.receipt.change_direction}",
    )
    check(
        "the candidate is PENDING_APPROVAL",
        candidate.receipt.decision is AcceptanceDecision.PENDING_APPROVAL,
        f"decision={candidate.receipt.decision}",
    )
    check(
        "a pending candidate still carries its knowledge, for a person to read",
        candidate.knowledge is not None,
    )
    check("the pending candidate reports itself as pending", candidate.pending)
    check("and does NOT report itself as approved", not candidate.approved)
    from eks_addon_readiness.storage.keys import candidate_key

    key = candidate_key(
        candidate.component_id,
        candidate.version_series,
        str(candidate.receipt.decision),
        candidate.digest(),
        STAMP.isoformat(),
    )
    check(
        "the pending candidate has a storage key naming the decision, so it is listable",
        key.startswith("facts-candidates/decision=PENDING_APPROVAL/"),
        key,
    )


def lane_bounds_contract() -> None:
    print("\n===== HOLE 2: published minima are bounded by their own table =====")
    pages = metrics_server_pages()
    rows = [
        ("0.9.x", "1.34"),
        ("0.8.x", "1.31"),
        ("0.7.x", "1.27"),
        ("0.6.x", "1.25"),
    ]
    extraction = ResearchExtraction(
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
                quote=ms_row_quote(spec),
                source=SourceRef(url=MS_URL, revision=MS_REV),
            )
            for spec, low in rows
        ),
        releases=(
            ReleaseSightingInput(
                version="0.6.4",
                quote=ms_row_quote("0.6.x"),
                source=SourceRef(url=MS_URL, revision=MS_REV),
            ),
        ),
    )
    candidate = judge(extraction, pages, None, retrieved_at=STAMP)
    check("the metrics-server extraction assembles a fact", candidate.knowledge is not None)
    if candidate.knowledge is None:
        return
    windows = {
        r.component_version_series: (
            r.kubernetes_versions.minimum_inclusive,
            r.kubernetes_versions.maximum_exclusive,
        )
        for r in candidate.knowledge.compatibility_rules
    }
    check(
        "0.6.x is bounded at the next floor up, not left open-ended",
        windows.get("0.6.x") == ("1.25", "1.27"),
        f"{windows.get('0.6.x')}  -- the earlier implementation stored ('1.25', None) and called 0.6.4 fit for 1.36",
    )
    check(
        "0.7.x is bounded by 0.8.x's floor",
        windows.get("0.7.x") == ("1.27", "1.31"),
        f"{windows.get('0.7.x')}",
    )
    check(
        "the topmost row keeps the project's own open-ended claim",
        windows.get("0.9.x") == ("1.34", None),
        f"{windows.get('0.9.x')}",
    )
    # The consequence, checked through the frozen Stage 4 rule that reads these windows.
    from eks_addon_readiness.decide.rules import specs_permitted_at

    at_136 = specs_permitted_at(candidate.knowledge, "1.36")
    check(
        "at Kubernetes 1.36 only 0.9.x is permitted, so 0.6.4 is an upgrade not a pass",
        at_136 == ("0.9.x",),
        f"permitted at 1.36: {at_136}",
    )
    at_126 = specs_permitted_at(candidate.knowledge, "1.26")
    check(
        "at 1.26 the 0.6.x window still answers, so the fix does not just narrow everything",
        "0.6.x" in at_126,
        f"permitted at 1.26: {at_126}",
    )

    print("\n----- enumerated minors: a discrete listing is bounded by what it lists -----")
    # An AWS-style default-version table: Kubernetes minor -> component version. Shape,
    # not source, decides -- so this is checked with a synthetic page, whose bytes the
    # quote genuinely comes from.
    doc_url = "https://example-docs.test/addon-versions.md"
    body = (
        "| Kubernetes version | Addon version |\n"
        "| --- | --- |\n"
        "| 1.36 | v1.14.3 |\n"
        "| 1.35 | v1.14.3 |\n"
        "| 1.34 | v1.13.2 |\n"
    )
    pages2 = PageSet()
    pages2.record(FetchedPage(doc_url, "d1", body, 200))
    enum_extraction = ResearchExtraction(
        found=True,
        component_id="example-addon",
        version_series="1.14.x",
        canonical_repository="https://github.com/example/addon",
        official_documentation=("https://example-docs.test",),
        statements=(
            CompatibilityStatementInput(
                version_spec="1.14.x",
                kind=BoundKind.ENUMERATED_MINORS,
                stated_minors=("1.35", "1.36"),
                quote="| 1.36 | v1.14.3 |\n| 1.35 | v1.14.3 |",
                source=SourceRef(url=doc_url, revision="d1"),
            ),
            CompatibilityStatementInput(
                version_spec="1.13.x",
                kind=BoundKind.ENUMERATED_MINORS,
                stated_minors=("1.34",),
                quote="| 1.34 | v1.13.2 |",
                source=SourceRef(url=doc_url, revision="d1"),
            ),
        ),
        releases=(
            ReleaseSightingInput(
                version="1.14.3",
                quote="| 1.36 | v1.14.3 |",
                source=SourceRef(url=doc_url, revision="d1"),
            ),
        ),
    )
    enum_candidate = judge(enum_extraction, pages2, None, retrieved_at=STAMP)
    enum_windows = (
        {
            r.component_version_series: (
                r.kubernetes_versions.minimum_inclusive,
                r.kubernetes_versions.maximum_exclusive,
            )
            for r in enum_candidate.knowledge.compatibility_rules
        }
        if enum_candidate.knowledge
        else {}
    )
    check(
        "a listing of 1.35 and 1.36 becomes [1.35, 1.37), matching the frozen catalogue rule",
        enum_windows.get("1.14.x") == ("1.35", "1.37"),
        f"{enum_windows.get('1.14.x')}",
    )
    check(
        "a listing of 1.34 alone becomes [1.34, 1.35), not >=1.34 forever",
        enum_windows.get("1.13.x") == ("1.34", "1.35"),
        f"{enum_windows.get('1.13.x')}  -- the earlier implementation stored ('1.34', None)",
    )


def lane_coverage() -> None:
    print("\n===== HOLE 3: every stored value is verified, and truths are not rejected =====")
    from eks_addon_readiness.research.verify import value_in_quote, verify_extraction

    # The exact earlier false negative: present in the real source, rejected.
    real_row = keda_row_quote("v2.20")
    check(
        "a range value with retyped whitespace is accepted (an earlier false negative)",
        value_in_quote("v1.33 - v1.35", real_row),
        f"value 'v1.33 - v1.35' vs quote {real_row.strip()!r}",
    )
    check(
        "a range value that genuinely differs is still rejected",
        not value_in_quote("v1.33 - v1.36", real_row),
    )

    # Coverage: a statement whose numbers are not in its quote is caught.
    pages = metrics_server_pages()
    extraction = ResearchExtraction(
        found=True,
        component_id="metrics-server",
        version_series="0.6.x",
        canonical_repository=MS_REPO,
        official_documentation=(MS_REPO,),
        statements=(
            CompatibilityStatementInput(
                version_spec="0.6.x",
                kind=BoundKind.SUPPORTED_RANGE,
                stated_minimum="1.25",
                stated_maximum_inclusive="1.30",  # NOT in the quoted row
                quote=ms_row_quote("0.6.x"),
                source=SourceRef(url=MS_URL, revision=MS_REV),
            ),
        ),
        releases=(
            ReleaseSightingInput(
                version="0.6.4",
                quote=ms_row_quote("0.6.x"),
                source=SourceRef(url=MS_URL, revision=MS_REV),
            ),
        ),
    )
    report = verify_extraction(extraction, pages)
    verified, total = report.coverage
    checked_fields = {v.field.split("[")[0] for v in report.value_checks}
    # The substantive claim is WHICH values are enumerated, not how many. The earlier implementation checked only
    # `extraction.claims`; this extraction carries no claims at all, so every value below
    # is one the earlier implementation would have stored unexamined.
    check(
        "verification reaches the statement's numbers and the releases, not only claims",
        {"stated_minimum", "stated_maximum_inclusive", "release"} <= checked_fields,
        f"{verified}/{total} values checked, fields={sorted(checked_fields)} "
        "(the earlier implementation checked 20 of 114 across four facts)",
    )
    check(
        "this extraction has no claims at all, so the earlier implementation would have checked nothing here",
        extraction.claims == () and total >= 3,
        f"total={total}",
    )
    check(
        "a stored maximum absent from its quote is reported unverified",
        any(
            v.field.startswith("stated_maximum_inclusive") and not v.verified
            for v in report.value_checks
        ),
    )
    candidate = judge(extraction, pages, None, retrieved_at=STAMP)
    check(
        "an unverified stored value drops the fact to LOW",
        candidate.receipt.confidence is Confidence.LOW,
        f"{candidate.receipt.confidence}",
    )
    check(
        "and a LOW fact is proposed for approval, never stored by the tool",
        candidate.receipt.decision is AcceptanceDecision.PENDING_APPROVAL,
    )
    check(
        "the receipt reports coverage as numbers a reader can check",
        candidate.receipt.values_total > 0
        and candidate.receipt.values_verified < candidate.receipt.values_total,
        f"{candidate.receipt.values_verified}/{candidate.receipt.values_total}",
    )


def lane_quote_vs_body() -> None:
    print("\n===== HOLE 4: quotes are compared against the page actually fetched =====")
    from eks_addon_readiness.research.verify import QuoteCheck, check_quote

    pages = metrics_server_pages()
    real = ms_row_quote("0.6.x")
    check(
        "a real quote from the cited page is PRESENT",
        check_quote(real, MS_URL, MS_REV, pages).verdict == QuoteCheck.PRESENT,
    )
    check(
        "a quote from a page nobody fetched is NOT_FETCHED, never a pass",
        check_quote(real, MS_URL, "some-other-commit", pages).verdict
        == QuoteCheck.NOT_FETCHED,
        "the revision is part of the key, so two commits are two pages",
    )
    check(
        "a composed quote is ABSENT",
        check_quote("0.6.x | 1.19+", MS_URL, MS_REV, pages).verdict
        == QuoteCheck.ABSENT,
    )
    # A quote real but attributed to the wrong page is its own verdict.
    two = metrics_server_pages()
    two.record(FetchedPage("https://other.test/x.md", "r2", real, 200))
    wrong = check_quote(real, "https://other.test/x.md", "r2", two)
    check(
        "the same text in two pages resolves to the page cited",
        wrong.verdict == QuoteCheck.PRESENT,
    )
    three = PageSet()
    three.record(FetchedPage(MS_URL, MS_REV, "nothing relevant here", 200))
    three.record(FetchedPage("https://other.test/x.md", "r2", real, 200))
    check(
        "a quote present only in an uncited page is WRONG_PAGE, not PRESENT",
        check_quote(real, MS_URL, MS_REV, three).verdict == QuoteCheck.WRONG_PAGE,
    )
    check(
        "the judging layer takes fetched bodies as an argument at all",
        "pages" in judge.__code__.co_varnames,
        "an earlier build_candidate(extraction, prior) could not receive one",
    )


def lane_authority() -> None:
    print("\n===== HOLE 5: source authority is derived by code, never asserted =====")
    pages = metrics_server_pages()
    row = ms_row_quote("0.6.x")
    check(
        "the component's own README table is FIRST_PARTY_TABULAR",
        classify_source(
            MS_URL,
            MS_REV,
            row,
            canonical_repository=MS_REPO,
            official_documentation=(MS_REPO,),
            pages=pages,
        )
        is SourceAuthority.FIRST_PARTY_TABULAR,
    )
    aws_url = "https://docs.aws.amazon.com/eks/latest/userguide/managing-coredns.md"
    aws_body = (
        "| Kubernetes version | CoreDNS version |\n| --- | --- |\n"
        "| 1.36 | v1.14.3-eksbuild.16 |\n"
    )
    aws_pages = PageSet()
    aws_pages.record(FetchedPage(aws_url, "r", aws_body, 200))
    aws_quote = "| 1.36 | v1.14.3-eksbuild.16 |"
    check(
        "a third-party table about the component is THIRD_PARTY even though it IS a table",
        classify_source(
            aws_url,
            "r",
            aws_quote,
            canonical_repository="https://github.com/coredns/coredns",
            official_documentation=(),
            pages=aws_pages,
        )
        is SourceAuthority.THIRD_PARTY,
        "the earlier implementation reached HIGH from exactly this page on a model-set boolean",
    )
    check(
        "a structured file in the component's own repo is FIRST_PARTY_STRUCTURED",
        classify_source(
            "https://raw.githubusercontent.com/kubernetes-sigs/metrics-server/x/Chart.yaml",
            "x",
            "kubeVersion: '>=1.25'",
            canonical_repository=MS_REPO,
            official_documentation=(),
            pages=_page_set(
                "https://raw.githubusercontent.com/kubernetes-sigs/metrics-server/x/Chart.yaml",
                "x",
                "kubeVersion: '>=1.25'",
            ),
        )
        is SourceAuthority.FIRST_PARTY_STRUCTURED,
    )
    check(
        "a lookalike host is not the component's own",
        classify_source(
            "https://raw.githubusercontent.com.evil.test/kubernetes-sigs/metrics-server/x/README.md",
            "x",
            "x",
            canonical_repository=MS_REPO,
            official_documentation=(),
            pages=_page_set(
                "https://raw.githubusercontent.com.evil.test/kubernetes-sigs/metrics-server/x/README.md",
                "x",
                "x",
            ),
        )
        is SourceAuthority.THIRD_PARTY,
    )
    # The boundary contract has nowhere to assert authority or confidence.
    fields = set(ResearchExtraction.model_fields) | set(
        CompatibilityStatementInput.model_fields
    ) | set(ExtractedClaimInput.model_fields)
    forbidden = {
        f
        for f in fields
        if any(
            token in f
            for token in (
                "confidence",
                "official_matrix",
                "authority",
                "verdict",
                "decision",
                "maximum_exclusive",
            )
        )
    }
    check(
        "no model-facing field can assert confidence, authority or an exclusive maximum",
        not forbidden,
        f"found: {sorted(forbidden)}" if forbidden else "",
    )


def _page_set(url: str, revision: str, body: str) -> PageSet:
    pages = PageSet()
    pages.record(FetchedPage(url, revision, body, 200))
    return pages


# --------------------------------------------------------------------------------------
# Freshness and revocation
# --------------------------------------------------------------------------------------


def _fact(
    component: str, refreshed: datetime, revision: str | None = None
) -> ComponentKnowledge:
    # ON GITHUB, and pinned to a BRANCH. Staleness is now decided by the newest release in the
    # component's GitHub repository against this fact's `refreshed_at`, so a synthetic fact on
    # example.test could only ever be NOT_COMPARABLE -- and a branch pin, which is what real
    # stored facts actually record, is now perfectly decidable.
    citation = {
        "url": f"https://raw.githubusercontent.com/{_SYNTHETIC_REPO}/main/x.md",
        "title": "t",
        "source_revision": "main",
        "retrieved_at": "2026-09-01T00:00:00Z",
    }
    return ComponentKnowledge.model_validate(
        {
            "knowledge_base_revision": revision or f"{component}-r1",
            "canonical_id": component,
            "canonical_repository": f"https://github.com/{_SYNTHETIC_REPO}",
            "official_documentation": [f"https://github.com/{_SYNTHETIC_REPO}"],
            "compatibility_rules": [
                {
                    "component_version_series": "1.0.x",
                    "kubernetes_versions": {
                        "minimum_inclusive": "1.30",
                        "maximum_exclusive": "1.40",
                    },
                    "evidence": [citation],
                }
            ],
            "releases": [
                {
                    "version": "1.0.0",
                    "released_at": "2020-01-01T00:00:00Z",
                    "source_revision": "main",
                    "evidence": [citation],
                }
            ],
            "verification_checks": [
                {"check_id": "synthetic-check", "description": "d", "evidence": [citation]}
            ],
            "default_upgrade_strategy": "IN_PLACE",
            "refreshed_at": refreshed.isoformat(),
            "evidence_confidence": "HIGH",
        }
    )


class _Store:
    """A minimal in-memory FactStore, for exercising the decorator."""

    def __init__(self, facts: tuple[ComponentKnowledge, ...]) -> None:
        self._facts = facts

    def find(self, identity: str) -> ComponentKnowledge | None:
        return next((f for f in self._facts if f.canonical_id == identity.strip()), None)

    def entries(self) -> tuple[ComponentKnowledge, ...]:
        return self._facts

    def digest(self) -> str:
        import hashlib

        h = hashlib.sha256()
        for f in sorted(self._facts, key=lambda f: f.canonical_id):
            h.update(f.canonical_json().encode())
        return "sha256:" + h.hexdigest()

    def __len__(self) -> int:
        return len(self._facts)


def lane_freshness() -> None:
    print("\n===== FRESHNESS: an upstream release, marked from outside the pipeline =====")
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    fresh = _fact("fresh-one", now - timedelta(hours=3))
    stale = _fact("stale-one", now - timedelta(hours=30))
    store = _Store((fresh, stale))

    result = sweep((fresh, stale), MarkLedger(), now, resolver=_QUIET)
    check(
        "a QUIET repository leaves both facts fresh, however old they are",
        result.expired == (),
        result.summary() + "  -- the 30h-old fact is NOT stale, because nothing was released",
    )
    drifted = sweep((fresh, stale), MarkLedger(), now, resolver=_RELEASED)
    check(
        "a RELEASE after the read marks both, however new they are",
        set(drifted.expired) == {"fresh-one", "stale-one"},
        drifted.summary(),
    )
    check(
        "the mark names the repository, the release and both instants, not an age",
        _SYNTHETIC_REPO in drifted.new_marks[0].reason
        and "v1.1.0" in drifted.new_marks[0].reason
        and "2026-09-12T11:00:00+00:00" in drifted.new_marks[0].reason
        and "hour" not in drifted.new_marks[0].reason.lower(),
        drifted.new_marks[0].reason[:88],
    )
    result = drifted
    check(
        "the mark names what wrote it, so an expiry is distinguishable from a revocation",
        result.new_marks[0].marked_by == "freshness-sweep",
    )
    check(
        "the sweep is idempotent -- running it again adds no mark",
        sweep((fresh, stale), result.ledger, now, resolver=_RELEASED).new_marks == (),
    )

    undecorated = MarkedFactStore(store, MarkLedger())
    decorated = MarkedFactStore(store, result.ledger)
    check(
        "with no marks the store digest is byte-identical to the undecorated store",
        undecorated.digest() == store.digest(),
        "so adding freshness to a deployment with no marks moves no digest",
    )
    check(
        "a superseded fact stops answering",
        decorated.find("stale-one") is None,
    )
    check(
        "the suppression is reportable, not silent",
        bool(decorated.suppressed),
        decorated.suppressed[0][1].reason[:60] if decorated.suppressed else "",
    )
    check(
        "a mark moves the store digest, so the ledger cannot cite the same store twice",
        decorated.digest() != store.digest(),
        "reproducibility is preserved by the digest the ledger already carries",
    )

    print("\n----- Stage 3 reports a stale fact as researchable, unchanged -----")
    from eks_addon_readiness.facts.gaps import _entry_for

    class _Component:
        workload_ref = "deploy/x"
        identity = "stale-one"
        version = "1.0.2"
        assessment_priority = 1

    entry = _entry_for(
        _Component(), insights={}, store=decorated, store_readable=True
    )
    check(
        "the gap entry is FACT_MISSING / NO_ENTRY, the existing researchable case",
        str(entry.status) == "FACT_MISSING" and str(entry.detail_code) == "NO_ENTRY",
        f"{entry.status} / {entry.detail_code}",
    )
    check(
        "Stage 3 needed no change: the frozen module produced this",
        "facts/gaps.py" in str(_entry_for.__code__.co_filename).replace("\\", "/"),
    )

    print("\n----- pre-flight and scheduled are the same mechanism -----")
    preflight = sweep((fresh, stale), MarkLedger(), now, resolver=_RELEASED, only=("fresh-one",))
    check(
        "a pre-flight sweep visits only the named components",
        preflight.visited == ("fresh-one",),
        preflight.summary(),
    )
    check(
        "so a run can be fresh at its moment with no stage doing I/O",
        preflight.expired == ("fresh-one",),
        "only the named component was visited, and its repository had released",
    )

    print("\n----- a mark stops applying once a fresh fact replaces the one it names -----")
    # THE LOOP. The mark above names revision `stale-one-r1`. A human approved a
    # re-research, so the store now holds `stale-one-r2` -- and the mark went on withholding
    # it, so the gap ledger reported the component researchable, the research ran, a candidate
    # was written and the same person was emailed to approve the same component again. Three
    # runs of it, and every run ended `marked 0, outcome_already_marked 1`.
    # Two replacements, one upstream answer: one read BEFORE the release that marked its
    # predecessor and one read after it, so the same resolver decides them differently and the
    # re-judgement is visibly on the fact's own merits rather than on the old mark.
    still_behind = _fact("stale-one", now - timedelta(hours=2), revision="stale-one-r2")
    caught_up = _fact("stale-one", now, revision="stale-one-r2")
    check(
        "a mark naming a replaced revision withholds nothing",
        result.ledger.suppresses("stale-one", fact_revision="stale-one-r2") is None,
        "no write and no permission needed: the comparison alone breaks the loop",
    )
    check(
        "the mark still withholds the revision it WAS raised against",
        result.ledger.suppresses("stale-one", fact_revision="stale-one-r1") is not None,
    )
    check(
        "so the fresh fact answers through the decorated store the stages already use",
        MarkedFactStore(_Store((caught_up,)), result.ledger).find("stale-one") is not None,
    )
    reswept = sweep((still_behind,), result.ledger, now, resolver=_RELEASED)
    check(
        "the sweep stops skipping it: upstream is re-read and the fact re-judged",
        reswept.expired == ("stale-one",)
        and reswept.new_marks[0].fact_revision == "stale-one-r2",
        f"re-marked on its own merits, recording {reswept.new_marks[0].fact_revision}",
    )
    check(
        "the re-mark REPLACES the spent one rather than piling up a second row",
        len([m for m in reswept.ledger.marks if m.component_id == "stale-one"]) == 1
        and reswept.retired == (),
        "one component, one staleness row -- `save` is a put and both share coordinates",
    )
    caught_up_sweep = sweep((caught_up,), result.ledger, now, resolver=_RELEASED)
    check(
        "a replacement the release is no longer newer than comes back UNMOVED and is usable",
        caught_up_sweep.expired == () and caught_up_sweep.retired == ("stale-one",),
        caught_up_sweep.summary(),
    )
    retired_ledger = retire_marks(
        result.ledger, "stale-one", (MarkKind.STALE,), now, reason="the fact was replaced"
    )
    stamped = next(m for m in retired_ledger.marks if m.component_id == "stale-one")
    check(
        "a retired mark is not in force",
        retired_ledger.suppresses("stale-one") is None,
    )
    check(
        "but its row is kept and stamped, because `save` deletes nothing",
        len(retired_ledger.marks) == len(result.ledger.marks)
        and stamped.retirement is not None,
        stamped.retirement.reason if stamped.retirement else "NOT STAMPED",
    )
    check(
        "the other component's mark is untouched by one component's retirement",
        retired_ledger.suppresses("fresh-one", fact_revision="fresh-one-r1") is not None,
    )
    check(
        "retiring a mark moves the store digest, like writing one",
        retired_ledger.digest() != result.ledger.digest(),
    )


def lane_revocation() -> None:
    print("\n===== REVOCATION: a bad fact stops answering, with an audit trail =====")
    now = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    bad = _fact("bad-one", now)
    store = _Store((bad,))
    mark = revoke_mark(
        "bad-one",
        (
            "produced by the pre-fix bounds logic, which read a tested window as a "
            "supported ceiling"
        ),
        now,
        actor="stage6-remediation",
        fact=bad,
        consumed_by=("s3://bucket/decisions/cluster=x/target=1.36/...json",),
    )
    ledger = MarkLedger(marks=(mark,))
    decorated = MarkedFactStore(store, ledger)
    check("a revoked fact stops answering", decorated.find("bad-one") is None)
    check(
        "the fact itself is not deleted -- the raw store still holds it",
        store.find("bad-one") is not None,
        "a deletion leaves no trace of what an old assessment rested on",
    )
    check(
        "the revocation names every document that consumed the fact",
        len(mark.consumed_by) == 1,
        mark.consumed_by[0],
    )
    check("the revocation names an accountable actor", mark.marked_by == "stage6-remediation")
    check(
        "a revocation outranks a staleness mark in the report",
        MarkLedger(
            marks=(
                mark,
                sweep((_fact("bad-one", now - timedelta(days=3)),), MarkLedger(), now, resolver=_RELEASED)
                .new_marks[0],
            )
        )
        .suppresses("bad-one")
        .kind
        is MarkKind.REVOKED,
    )
    check(
        "a re-research does not silently retire a revocation",
        retire_marks(
            ledger, "bad-one", (MarkKind.STALE,), now, reason="the fact was replaced"
        ).suppresses("bad-one")
        is not None,
        "the defect that produced the fact may still be present",
    )
    check(
        "nor does a FRESH REVISION: a revocation withholds every revision, staleness one",
        ledger.suppresses("bad-one", fact_revision="bad-one-r2") is not None,
        "the revision rule that breaks the staleness loop deliberately excludes revocations",
    )


def lane_frozen_untouched() -> None:
    print("\n===== INVARIANT: no digest-frozen ruleset MODULE was edited =====")
    # Stage 5's expected value is no longer its original baseline, and it moved as
    # `order/RULESET_MODULES` grew: a fifth module, `recommend`, so the plan can carry the optional
    # upgrade an AWS-decided managed add-on has earned, then a sixth, `dedupe`, so it stops carrying
    # a step that installs the version an earlier step for the same component already installs, then
    # a seventh, `floor`, so it stops carrying a step that installs a version AT OR BELOW the one
    # running -- which would move a cluster backwards -- then an eighth, `collapse`, so it stops
    # carrying a step that installs a series the plan ABANDONS before the cluster hops at all (a
    # component can otherwise be moved TWICE at one Kubernetes minor to land where one move would
    # have put it); then a ninth, `latest`, so the plan recommends from the APPROVED FACT by the one
    # policy the approval page and the report also use, and `recommend` gives an AWS-decided add-on
    # that is behind a required step instead of leaving it unsequenceable. Either way
    # a plan whose steps changed must not carry the rules digest
    # of the plan before them. All four CARRIED Stage 5 sources are byte-identical, so a mismatch
    # below still means one of them was edited -- which is what this invariant is about. Stage
    # 4 still equals its original baseline exactly; Stage 2 moved for the chart-version-in-app-label
    # fix, and Stages 3 and 5 moved for the comment-only publication cleanup.
    expected = {
        # Stage 2 moved for the chart-version-in-app-label fix: inventory/{resolve,confidence}.py
        # now take a Helm component's installed version from the image tag when the
        # `app.kubernetes.io/version` label is the chart version (kyverno). Deliberate change.
        "stage 2": "sha256:0c091b1beead7e7c21b3d0cb7f1e3f6dfc8910bc9900e534b331ab9fb49333a9",
        # Moved for a comment-only release cleanup (docstring edits in the carried modules); rules unchanged.
        "stage 3": "sha256:e21996fc26a3cc53c25b43034993d9a946a8fd2bff6074154ef7f96d4e59c32d",
        "stage 4": "sha256:d1583a353a9090e1493989ae8093cbe5529a46fd3b1a6a72298cee7c34d9c0bb",
        # Moved for a comment-only release cleanup (docstring edits in the carried modules); rules unchanged.
        "stage 5": "sha256:72f7aafd3acb72c319df02a3ec7ab6bd850a89bad91aa99a5eac53e244626d7a",
    }
    from eks_addon_readiness.decide.ruleset import ruleset_digest as d4
    from eks_addon_readiness.facts.ruleset import ruleset_digest as d3
    from eks_addon_readiness.order.ruleset import ruleset_digest as d5
    from eks_addon_readiness.report import ruleset_digest as d2

    for name, fn in (("stage 2", d2), ("stage 3", d3), ("stage 4", d4), ("stage 5", d5)):
        actual = fn()
        equals_v1 = name == "stage 4"
        suffix = "equals its original baseline" if equals_v1 else "matches its expected value (moved from baseline on purpose)"
        check(
            f"{name} ruleset digest still {suffix}",
            actual == expected[name],
            actual,
        )


def lane_generality() -> None:
    print("\n===== INVARIANT: no component name decides anything in the package =====")
    # The invariant is about BEHAVIOUR: per-component differences are data in the
    # knowledge store, so no component name may appear in a value the code acts on or in
    # a name the code defines. A component named in a docstring, a comment or a doctest is
    # documentation -- it explains why a rule exists and changes nothing at runtime -- so
    # the check walks the AST and skips docstrings rather than filtering text by line
    # shape, which cannot tell a docstring's second line from code.
    import ast

    names = (
        "keda",
        "cert-manager",
        "certmanager",
        "istio",
        "kyverno",
        "coredns",
        "metrics-server",
        "metricsserver",
        "aws-load-balancer",
        "kube-proxy",
        "vpc-cni",
    )
    offenders: dict[str, None] = {}
    scanned = 0
    for path in sorted((ROOT / "eks_addon_readiness").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        scanned += 1
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            haystacks: list[str] = []
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                haystacks.append(node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                haystacks.append(node.name)
            elif isinstance(node, ast.Name):
                haystacks.append(node.id)
            elif isinstance(node, ast.Attribute):
                haystacks.append(node.attr)
            for haystack in haystacks:
                lowered = haystack.casefold()
                for name in names:
                    if name in lowered:
                        offenders[
                            f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 0)} "
                            f"{name!r} in {haystack[:40]!r}"
                        ] = None
    # One documented exception, pre-existing and in a digest-frozen module: the
    # `_ADDON_WORKLOAD_ALIASES` table in `inventory/resolve.py`, which maps an EKS-managed
    # add-on's workload name to the add-on name because Kubernetes records no link between
    # them. Audited, and not editable without breaking
    # the carried-module digest equality. Allowed by exact path so that a NEW hit
    # anywhere -- including elsewhere in that same module -- still fails.
    KNOWN_EXCEPTION = "eks_addon_readiness/inventory/resolve.py"
    unexpected = {
        o: None
        for o in offenders
        if not o.startswith(KNOWN_EXCEPTION)
    }
    check(
        "no component name appears in a value or identifier the code acts on",
        not unexpected,
        "; ".join(list(unexpected)[:4])
        if unexpected
        else (
            f"{scanned} modules scanned; the only hits are the frozen alias table "
            f"({len(offenders)} occurrence(s) in {KNOWN_EXCEPTION})"
        ),
    )
    check(
        "the Stage 6 research package itself names no component at all",
        not any(o.startswith("eks_addon_readiness/research/") for o in offenders),
        "the new code adds no per-component branch",
    )


def main() -> int:
    print("STAGE 6 CHECKS")
    print(f"date: {datetime.now(UTC).isoformat()}")
    print(f"python: {sys.version.split()[0]}")
    print(f"fixtures: real bytes from {FIXTURES.relative_to(ROOT)}")
    for name in sorted(p.name for p in FIXTURES.iterdir() if p.is_file()):
        size = (FIXTURES / name).stat().st_size
        print(f"  {name}  {size} B")

    lane_keda_no_supported_ceiling()
    lane_assessment_rechecked()
    lane_failed_quote_never_stored()
    lane_acceptance_gate()
    lane_held_path_works()
    lane_bounds_contract()
    lane_coverage()
    lane_quote_vs_body()
    lane_authority()
    lane_freshness()
    lane_revocation()
    lane_frozen_untouched()
    lane_generality()

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\nSTAGE 6 CHECKS: {passed} passed, {failed} failed")
    if failed:
        print("FAILURES:")
        for ok, name, detail in _results:
            if not ok:
                print(f"  - {name}  {detail}")
    print(f"STAGE 6 CHECKS EXIT={1 if failed else 0}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
