"""The deterministic judging layer: extraction plus fetched bytes in, judged candidate out.

No model runs here. This is the half of Stage 6 that decides, and its signature carries the
key design choice:

    judge(extraction, pages, prior)

`pages` is the set of bodies actually fetched during the run. A judging layer without it
cannot check a quote against the page it cites: there is nowhere to get the page text if
`ResearchExtraction` carries no field for it and the only HTTP client lives inside the
model's own tool loop. With the bodies in hand every quote is checked against the bytes it
cites before anything else happens.

Order of operations, and each step's reason:

1. **Derive windows** from the statements (`bounds.py`). Before verification, because
   which kinds yield windows determines what values there are to verify.
2. **Verify** every quote against fetched bytes and every stored value against its quote
   (`verify.py`).
3. **Classify authority** of each source from its identity and the shape of its bytes
   (`authority.py`) -- never from a model-set flag.
4. **Assemble** the `ComponentKnowledge` contract.
5. **Score** signals, find contradictions, compute the machine's confidence (`signals.py`).
6. **Re-check the assessment**: the recommended version must be a release the model
   itself returned, the target-coverage claim is recomputed from the derived windows, an
   N-2 derivation is redone from its anchor, and the final confidence is
   min(machine, tier cap, model's proposal) -- the model can lower the level, never raise
   it.
7. **Route** through the acceptance gate (`acceptance.py`) and build the receipt.

Nothing raises for a research outcome, and nothing is hidden: a component whose sources
state no window returns a REJECTED candidate carrying the reason -- the WORD means "not
approvable", never "not shown". The stage handlers write the candidate, its receipt and
its report document in every case.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError

from ..knowledge import (
    CompatibilityRule,
    ComponentKnowledge,
    ReleaseRecord,
    UpgradeStrategy,
    VerificationCheck,
    VersionRange,
)
from ..models import Citation, Confidence
from .acceptance import classify_direction, decide, summarize
from .authority import SourceAuthority, classify_source
from .boundary import AnchorBasis, CompatibilityStatementInput, ResearchExtraction, SourceRef
from .bounds import DerivedWindow, derive_windows, recompute_policy_window, tested_windows
from .candidate import CandidateFact
from .pages import PageSet
from .receipt import AcceptanceDecision, ChangeDirection, ResearchReceipt
from .signals import (
    compute_confidence,
    corroboration,
    final_confidence,
    find_contradictions,
    first_party_source,
    policy_derivation_recomputes,
    quote_matches_source,
    recommendation_is_a_seen_release,
    releases_present,
    rule_containment_contradictions,
    shape_sanity,
    support_window_derived,
    target_covered_by_evidence,
    values_trace_to_quotes,
)
from .verify import VerificationReport, verify_extraction

# Release dates are not extracted, so a fixed sentinel is used. Downstream ordering is by
# version number, never by date, so the sentinel affects no decision. A clock reading here
# would make two judgements of the same evidence differ.
_RELEASE_DATE_SENTINEL = datetime(2020, 1, 1, tzinfo=UTC)


def _citation(source: SourceRef, title: str, retrieved_at: datetime) -> Citation:
    return Citation(
        url=source.url,
        title=title,
        source_revision=source.revision,
        retrieved_at=retrieved_at,
    )


OBSERVED_WINDOW_TITLE = (
    "no published compatibility statement; window is the Kubernetes minor this series is "
    "observed running on"
)


def _observed_window_applies(
    extraction: ResearchExtraction, report: VerificationReport, releases: list
) -> bool:
    """Whether the assembler may fall back to the observed window. All four must hold."""
    return bool(
        extraction.no_compatibility_found
        and extraction.assessment is not None
        and releases
        and all(q.verified for q in report.quote_checks)
    )


def _observed_rule(
    extraction: ResearchExtraction, retrieved_at: datetime, citations: list[Citation]
) -> CompatibilityRule:
    """One rule for the installed series: floor at the observed minor, no ceiling."""
    assert extraction.assessment is not None
    first = citations[0] if citations else None
    fallback = extraction.canonical_repository or f"https://github.com/{extraction.component_id}"
    citation = Citation(
        url=first.url if first else fallback,
        title=OBSERVED_WINDOW_TITLE,
        source_revision=first.source_revision if first else "observed",
        retrieved_at=retrieved_at,
    )
    return CompatibilityRule(
        component_version_series=extraction.version_series,
        kubernetes_versions=VersionRange(
            minimum_inclusive=extraction.assessment.current_kubernetes, maximum_exclusive=None
        ),
        evidence=(citation,),
    )


def _authority_title(authority: SourceAuthority) -> str:
    """What a citation calls itself, from the authority code derived for it.

    The title is what a report shows a reader, so it must not overstate. Titling a
    citation "official compatibility matrix" from a model-set flag can overstate; here the
    title is the derived classification and cannot disagree with it.
    """
    return {
        SourceAuthority.FIRST_PARTY_STRUCTURED: (
            "structured file in the component's own repository"
        ),
        SourceAuthority.FIRST_PARTY_TABULAR: ("compatibility table in the component's own source"),
        SourceAuthority.FIRST_PARTY_PROSE: "the component's own documentation (prose)",
        SourceAuthority.THIRD_PARTY: "third-party page about the component",
        SourceAuthority.UNVERIFIABLE: "cited source, not fetched in this run",
    }[authority]


class _Assembly:
    """Intermediate result of assembling the contract, or the reason it failed."""

    def __init__(
        self,
        knowledge: ComponentKnowledge | None,
        detail: str,
        authorities: tuple[SourceAuthority, ...],
    ) -> None:
        self.knowledge = knowledge
        self.detail = detail
        self.authorities = authorities


def _assemble(
    extraction: ResearchExtraction,
    pages: PageSet,
    derived,
    retrieved_at: datetime,
    report: VerificationReport,
) -> _Assembly:
    """Build a schema-valid `ComponentKnowledge`, or explain why none exists.

    Only *verified* releases become release records and only *storable* windows become
    rules, so an unverifiable value cannot reach the contract even if the gate above were
    somehow bypassed. Defence in depth, deliberately: a single gate can be removed by a
    one-line edit with nothing else in the system objecting.

    The chart repository follows the same rule through `report.chart_checks`: it reaches the
    fact only when its quote verified. The upgrade lane runs `helm repo add` on it.
    """
    canonical_repository = extraction.canonical_repository or None
    official_documentation = extraction.official_documentation
    # The chart repository is first-party for the CHART claims and nothing else: the
    # component's own `helm repo add` line names it, and the values quoted from it are
    # about the chart. Widening `official_documentation` for every claim would let any
    # page on that host vouch for compatibility, which is not what it publishes.
    chart_documentation = official_documentation + (
        (extraction.chart.repository_url,) if extraction.chart is not None else ()
    )

    def authority_of(
        source: SourceRef, quote: str, *, documentation: tuple[str, ...] | None = None
    ) -> SourceAuthority:
        return classify_source(
            source.url,
            source.revision,
            quote,
            canonical_repository=canonical_repository,
            official_documentation=(
                official_documentation if documentation is None else documentation
            ),
            pages=pages,
        )

    authorities: list[SourceAuthority] = []
    citations: list[Citation] = []
    seen_citations: set[tuple[str, str]] = set()

    def reattributed(source: SourceRef, quote: str) -> SourceRef:
        """The source the quote should be CITED to, moved to its sibling query-page if so.

        `verify.py` verifies a quote that was read off a sibling page of a paginated list
        (`?page=2` for a quote citing `?page=3`) and records where its bytes actually are.
        Resolving the source here is what makes that re-attribution land on the stored
        fact: both the citation written below and the authority classified for it then
        name the page whose bytes carry the quote, not the stale page number the model
        wrote. For every other quote this returns the source unchanged.
        """
        moved = report.reattribution_for(quote, source.url, source.revision)
        if moved is None:
            return source
        url, revision = moved
        # A fetched page may be unpinned (revision ""), but a SourceRef must carry a
        # non-empty revision. The sibling is the same list at the same pinning, so keep
        # the original revision when the sibling page records none -- only the URL's query
        # string is what actually moved.
        return SourceRef(url=url, revision=revision or source.revision)

    def cite(
        source: SourceRef, quote: str, *, documentation: tuple[str, ...] | None = None
    ) -> Citation:
        source = reattributed(source, quote)
        authority = authority_of(source, quote, documentation=documentation)
        authorities.append(authority)
        citation = _citation(source, _authority_title(authority), retrieved_at)
        key = (source.url, source.revision)
        if key not in seen_citations:
            seen_citations.add(key)
            citations.append(citation)
        return citation

    # Cite in a stable order: statements first (they are what a verdict rests on), then
    # claims, then releases. Strongest authority first within statements, so a report
    # names the best source at the top.
    rules: list[CompatibilityRule] = []
    for d in sorted(
        derived,
        key=lambda d: (
            0
            if authority_of(d.statement.source, d.statement.quote)
            in (
                SourceAuthority.FIRST_PARTY_STRUCTURED,
                SourceAuthority.FIRST_PARTY_TABULAR,
            )
            else 1
        ),
    ):
        citation = cite(d.statement.source, d.statement.quote)
        if not d.storable:
            continue
        try:
            rules.append(
                CompatibilityRule(
                    component_version_series=d.statement.version_spec,
                    kubernetes_versions=d.window,
                    served_api_versions=d.statement.served_api_versions,
                    evidence=(citation,),
                )
            )
        except ValidationError:
            # A malformed row is skipped rather than sinking a fact whose other rows are
            # sound. The skip is visible: the value shows as unverified in the coverage
            # count, because no rule carries it.
            continue

    for claim in extraction.claims:
        cite(claim.source, claim.quote)

    releases: list[ReleaseRecord] = []
    for sighting in extraction.releases:
        citation = cite(sighting.source, sighting.quote)
        try:
            releases.append(
                ReleaseRecord(
                    version=sighting.version,
                    released_at=_RELEASE_DATE_SENTINEL,
                    # The citation may have been re-attributed to a sibling query-page, so
                    # read the revision off it rather than the model's original source, to
                    # keep the record and its evidence pointing at the same page.
                    source_revision=citation.source_revision or sighting.source.revision,
                    evidence=(citation,),
                )
            )
        except ValidationError:
            continue

    # THE CHART REPOSITORY, FROM A VERIFIED QUOTE ONLY. `report.chart_checks` is the check
    # `verify.py` ran on it, kept outside the signals so a miscopied chart line cannot reject
    # a compatibility fact -- it only costs the component its lane, which the planner reports.
    # The chart NAME is not on the fact: it is the chart installed on the cluster, which the
    # inventory already knows, and the chart VERSION is asked of the repository at upgrade
    # time. `chart_name` stays None, which is also what keeps the frozen
    # `high_confidence_requires_chart_mapping` validator out of the way.
    chart_repository: str | None = None
    if extraction.chart is not None:
        chart = extraction.chart
        cite(chart.source, chart.quote, documentation=chart_documentation)
        if all(c.verified for c in report.chart_checks):
            chart_repository = chart.repository_url

    if not rules and _observed_window_applies(extraction, report, releases):
        # THE HONEST "NOTHING IS PUBLISHED" FACT. The model looked, quoted only what it read,
        # found releases and no compatibility statement of any kind, and said so. The prompt
        # has always promised this case an answer -- the newest release, LOW, with the reason
        # -- and the judge never delivered it, because a fact needs a rule and no source gave
        # one. Live: argo-rollouts, four times on 2026-09-27, rejected every time it was read
        # honestly and accepted only when a chart README's "Kubernetes 1.7+" happened to be
        # found. So the one thing that IS observed becomes the rule: the installed series is
        # running on this cluster's current Kubernetes minor, which is a fact about the
        # cluster, not about the project. Floor = that minor, no ceiling -- the ceiling is
        # what nobody published. The citation says so in its title, the receipt is LOW and the
        # planner (`order/latest.py`) treats a fact that is silent above the installed series
        # as "newest recorded release, unverified". Nothing here claims the target is
        # supported; `TARGET_COVERED_BY_EVIDENCE` still fails and still shows.
        rules.append(_observed_rule(extraction, retrieved_at, citations))
    if not rules:
        return _Assembly(
            None,
            "no compatibility rule could be assembled from what the sources state",
            tuple(authorities),
        )
    if not releases:
        return _Assembly(
            None,
            (
                "no release was quoted, so no concrete version can be recommended to "
                "install. A window with nothing to install is not an actionable fact."
            ),
            tuple(authorities),
        )
    if not citations:
        return _Assembly(None, "no citation could be formed for any value", tuple(authorities))

    try:
        knowledge = ComponentKnowledge(
            knowledge_base_revision=(
                f"{extraction.component_id}-researched-{retrieved_at.date().isoformat()}"
            ),
            canonical_id=extraction.component_id,
            canonical_repository=(
                canonical_repository or f"https://github.com/{extraction.component_id}"
            ),
            official_documentation=(
                official_documentation
                or (canonical_repository or f"https://github.com/{extraction.component_id}",)
            ),
            chart_repository=chart_repository,
            compatibility_rules=tuple(rules),
            releases=tuple(releases),
            verification_checks=(
                VerificationCheck(
                    check_id="component-serves-its-api",
                    description=("The component serves its documented API after the upgrade."),
                    required=True,
                    evidence=tuple(citations),
                ),
            ),
            default_upgrade_strategy=UpgradeStrategy.SEQUENTIAL_MINOR,
            refreshed_at=retrieved_at,
            # Placeholder. Replaced with the computed confidence below, once the signals
            # are scored. Leaving the placeholder in place would make every stored fact read
            # MEDIUM in the report regardless of its receipt.
            evidence_confidence=Confidence.MEDIUM,
        )
    except ValidationError as exc:
        return _Assembly(
            None,
            f"assembled knowledge failed validation: {exc.error_count()} error(s)",
            tuple(authorities),
        )

    return _Assembly(knowledge, "assembled knowledge is structurally valid", tuple(authorities))


SEQUENCING_FALLBACK_BASIS = (
    "the ceiling inference left no series covering both the current and the target "
    "Kubernetes minor, so the model's reading of its recommended series was used: the "
    "window starts at the minor the cluster runs now and keeps the stated ceiling"
)


def _widen_when_no_series_spans_the_path(
    derived: tuple,
    extraction: ResearchExtraction,
    report: VerificationReport,
) -> tuple[tuple, bool]:
    """Fall back to the model's reading ONLY where the judge's own inference cannot sequence.

    WHAT THE JUDGE GETS WRONG, AND FOR WHICH TABLES. `bounds._floor_table_ceilings` reads a
    table of rising floors as "each row stops where the next row starts". That is right for a
    table keyed by COMPONENT series (`0.6.x | 1.25+`, `0.7.x | 1.27+`: metrics-server), and it
    is what stopped 0.6.4 being declared fit for 1.36. It is wrong for a table keyed by
    KUBERNETES minor giving the minimum component version for each (`1.35 | >= 1.9`,
    `1.36 | >= 1.13`: karpenter). There, `>= 1.9` says nothing about where 1.9 stops, and
    1.13 runs on 1.35 too. Read the judge's way, no series spans 1.35 and 1.36, the planner
    calls the component unsequenceable, and it gets neither a step nor a brief. Live, 2026-09-27.

    THE TRIGGER IS THE FAILURE ITSELF, NOT A GUESS ABOUT TABLE SHAPE. If some derived series
    already covers every minor from the current to the target, the inference worked and
    nothing here runs -- today's behaviour, byte for byte. Only when NO series spans the path,
    AND the model's assessment says its recommendation covers the target, is ONE series
    widened: the highest derived series whose window already covers the target and whose row
    has a verified quote. Its floor becomes the current minor (the cluster it will be installed
    onto); its stated ceiling is kept. That is the inversion a minimum-per-minor table implies
    -- the version required at the target satisfies every earlier column too -- anchored on a
    row the checker verified rather than on a number the model wrote (karpenter's model named
    `1.14.x`, a series its own table never mentions). Nothing else moves. The basis on the
    window says what happened, the receipt shows it, and the confidence is capped at MEDIUM.

    Returns the derived windows (possibly with one replaced) and whether the fallback fired.
    """
    assessment = extraction.assessment
    if assessment is None or assessment.covers_target_kubernetes is not True:
        return derived, False
    current, target = assessment.current_kubernetes, assessment.target_kubernetes
    path = _minors_between(current, target)
    if not path:
        return derived, False
    if any(d.storable and all(_covers(d.window, m) for m in path) for d in derived):
        return derived, False
    verified_quotes = set(report.verified_quotes)
    candidates = [
        d
        for d in derived
        if d.storable and d.statement.quote in verified_quotes and _covers(d.window, target)
    ]
    if not candidates:
        return derived, False
    from .bounds import _spec_sort_key

    chosen = max(candidates, key=lambda d: _spec_sort_key(d.statement.version_spec))
    window = VersionRange(
        minimum_inclusive=current, maximum_exclusive=chosen.window.maximum_exclusive
    )
    out = tuple(
        DerivedWindow(d.statement, window, SEQUENCING_FALLBACK_BASIS) if d is chosen else d
        for d in derived
    )
    return out, True


def _minors_between(current: str, target: str) -> tuple[str, ...]:
    """Every `major.minor` from current to target inclusive, or () if unparsable/inverted."""
    try:
        cmaj, cmin = (int(p) for p in current.split("."))
        tmaj, tmin = (int(p) for p in target.split("."))
    except ValueError:
        return ()
    if cmaj != tmaj or tmin < cmin:
        return ()
    return tuple(f"{cmaj}.{m}" for m in range(cmin, tmin + 1))


def _covers(window, minor: str) -> bool:
    """Whether a half-open VersionRange contains a `major.minor`. Pure comparison."""

    def t(v: str) -> tuple[int, int] | None:
        parts = v.split(".")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        return int(parts[0]), int(parts[1])

    target = t(minor)
    floor = t(window.minimum_inclusive)
    if target is None or floor is None or target < floor:
        return False
    if window.maximum_exclusive is None:
        return True
    ceiling = t(window.maximum_exclusive)
    return ceiling is not None and target < ceiling


def _major_of(version: str) -> int | None:
    """The leading integer of a version or series spec, or None if it has none.

    Accepts a release (`3.3.9`), a series (`1.19.x`), or a bare major -- only the first
    dotted field is read, so `1.19.x` and `1.13.6` both yield their major. Returns None for
    anything whose first field is not an integer, so a malformed spec never silently reads
    as major 0.
    """
    head = version.strip().split(".", 1)[0]
    return int(head) if head.isdigit() else None


# The actionable reason the judge hands back when a Helm component was answered in a
# version scheme that does not match the INSTALLED version. research_harness.py re-asks ONCE
# on the same session with this text (see its `_REASK_AFTER_JUDGE` path), so it must say
# exactly what to change: the installed version is the application version the cluster runs,
# and the rules/releases are in a different numbering (typically the Helm chart's), so
# restate everything in the same numbering as the installed version.
_CHART_SCHEME_MISMATCH_SUMMARY = (
    "installed version {installed}, but every compatibility rule and release you returned "
    "uses a different numbering (major {app_major}, e.g. {example}) -- most likely the Helm "
    "chart version space rather than the application version space. {installed} is the "
    "application version the cluster actually runs, so a fact in a different numbering "
    "cannot be matched against it and this answer is not usable. Restate the compatibility "
    "rules, the releases and the recommended version in the SAME numbering as {installed} "
    "(the application version the cluster runs). If your sources publish only the other "
    "numbering, translate via the project's version mapping and keep the other numbering "
    "only in caveats."
)


def _chart_scheme_mismatch_reason(
    extraction: ResearchExtraction,
    *,
    installed_version: str | None,
    helm_release: str | None,
) -> str | None:
    """Reject reason when a Helm component was answered in the wrong version scheme, else None.

    THE RULE, STATED SCHEME-NEUTRALLY. The installed value for a Helm component is the
    APPLICATION version the cluster runs (after the chart-version-in-app-label fix in
    inventory, kyverno installs as 1.13.6, argo-rollouts as 1.6.6). Decide matches rules
    against that installed version, so an answer whose rules and releases are entirely in a
    DIFFERENT numbering -- typically the Helm chart's (kyverno chart 3.x, argo-rollouts chart
    2.x) -- can never describe the install: the component is scored INCOMPATIBLE and gets no
    brief. The guard rejects that answer so the harness re-asks for one in the installed
    version's numbering.

    The test is deliberately the narrowest thing that catches it and nothing legitimate:

      * it only runs for a Helm component (`helm_release` present) with a known
        `installed_version` -- a managed add-on or an unknown version is untouched;
      * it only fires when the installed MAJOR appears in NEITHER any compatibility rule's
        series NOR any release -- so a kyverno answer installed 1.13.6 against rules/releases
        in 3.x (the chart) is rejected, and so is an argo-rollouts answer installed 1.6.6
        against rules/releases in 2.x (the chart); while an answer whose rules and releases
        share the installed major (kyverno 1.13.6 against 1.x, argo-rollouts 1.6.6 against
        1.x, cert-manager 1.13.2 against 1.x, karpenter 1.0.5 against 1.x, keda 2.21.0
        against 2.x, metrics-server 0.9.0 against 0.x) passes unchanged;
      * if the model returned no statements and no releases at all there is nothing to
        disagree with, so the honest "nothing found" path keeps its own handling.

    Major-only on purpose: a chart and its app version routinely share a minor, and
    requiring an exact match would reject legitimate answers where the installed version
    simply is not one of the quoted releases. A whole major absent from everything is the
    unambiguous signal that the two value spaces are different -- the chart-vs-application
    split -- and not a gap in the matrix.
    """
    if not helm_release or not installed_version:
        return None
    installed_major = _major_of(installed_version)
    if installed_major is None:
        return None

    rule_majors = {
        m
        for s in extraction.statements
        if (m := _major_of(s.version_spec)) is not None
    }
    release_majors = {
        m
        for r in extraction.releases
        if (m := _major_of(r.version)) is not None
    }
    present = rule_majors | release_majors
    if not present:
        # Nothing to compare against -- the "nothing published" path owns this case.
        return None
    if installed_major in present:
        return None

    example = next(
        (r.version for r in extraction.releases),
        next((s.version_spec for s in extraction.statements), "1.x"),
    )
    return _CHART_SCHEME_MISMATCH_SUMMARY.format(
        installed=installed_version,
        app_major=min(present),
        example=example,
    )


def judge(
    extraction: ResearchExtraction,
    pages: PageSet,
    prior: ComponentKnowledge | None = None,
    *,
    retrieved_at: datetime | None = None,
    installed_version: str | None = None,
    helm_release: str | None = None,
) -> CandidateFact:
    """Judge one extraction against the bytes fetched for it. Deterministic; no model call.

    `retrieved_at` is injectable so a test can pin it. It defaults to now because a
    citation records when a source was read, which is a fact about the run rather than an
    input to any decision -- no rule here branches on it.

    `installed_version` and `helm_release` are the task inputs for the component. They are
    used only by the Helm version-scheme guard below: when a Helm component is answered
    entirely in a numbering whose major does not include the installed (application)
    version's major, the extraction is rejected with an actionable reason so the harness
    re-asks for an answer in the installed version's numbering. Both default to None, so
    every existing caller (the in-process path and the tests) behaves exactly as before.
    """
    stamp = retrieved_at or datetime.now(UTC)

    # HELM VERSION SCHEME. Checked before any window is derived, because the whole
    # extraction is in the wrong value space and no amount of verification makes a fact in
    # one numbering describe an install in another (a 3.x chart-version answer cannot
    # describe a 1.13.6 application install, nor a 2.x answer a 1.6.6 install). Rejected
    # with the reason the harness re-asks on; the component stays a researchable gap
    # meanwhile.
    scheme_reason = _chart_scheme_mismatch_reason(
        extraction, installed_version=installed_version, helm_release=helm_release
    )
    if scheme_reason is not None:
        receipt = ResearchReceipt(
            confidence=Confidence.UNRESOLVED,
            signals=(shape_sanity(False, scheme_reason),),
            contradictions=(),
            change_direction=ChangeDirection.NEW,
            decision=AcceptanceDecision.REJECTED,
            summary=(
                summarize(
                    Confidence.UNRESOLVED,
                    (),
                    ChangeDirection.NEW,
                    AcceptanceDecision.REJECTED,
                )
                + f" Reason: {scheme_reason}"
            ),
            evidence_digest=pages.digest(),
            values_verified=0,
            values_total=0,
            tested_windows=tested_windows(extraction.statements),
            machine_confidence=Confidence.UNRESOLVED,
            proposed_confidence=None,
            evidence_tier=None,
        )
        return CandidateFact(
            component_id=extraction.component_id,
            version_series=extraction.version_series,
            knowledge=None,
            receipt=receipt,
        )

    derived = derive_windows(extraction.statements)
    report = verify_extraction(extraction, pages)
    derived, sequencing_fallback = _widen_when_no_series_spans_the_path(derived, extraction, report)
    assembly = _assemble(extraction, pages, derived, stamp, report)

    def citation_for(statement: CompatibilityStatementInput) -> Citation:
        authority = classify_source(
            statement.source.url,
            statement.source.revision,
            statement.quote,
            canonical_repository=extraction.canonical_repository or None,
            official_documentation=extraction.official_documentation,
            pages=pages,
        )
        return _citation(statement.source, _authority_title(authority), stamp)

    signals = (
        quote_matches_source(report),
        values_trace_to_quotes(report),
        corroboration(extraction.statements, citation_for),
        first_party_source(assembly.authorities),
        support_window_derived(derived),
        shape_sanity(assembly.knowledge is not None, assembly.detail),
    )
    # TWO PASSES OVER TWO DIFFERENT THINGS, and the second is not a refinement of the first.
    #
    # `find_contradictions` reads the model's STATEMENTS: what sources said. It cannot see a
    # contradiction that only exists once `bounds.py` has derived windows from them -- and one
    # class of contradiction only exists there. A floor-only statement ("requires Kubernetes
    # 1.23+") is silent about its ceiling, which is not a contradiction; the derived rule turns
    # that silence into a window with NO maximum, which asserts coverage of every later
    # Kubernetes version. When the release that rule names belongs to a series whose own rule
    # DOES stop, the assembled fact contradicts itself while every statement in it was fine.
    #
    # Live consequence, on an approved keda fact: `2.20.x` bounded below 1.36 alongside
    # `2.20.2` unbounded. `order/moves.py` reads those two rules with two different functions
    # that disagree, and the sequencer planned two steps installing the same version.
    contradictions = find_contradictions(extraction.statements, citation_for)
    if assembly.knowledge is not None:
        contradictions += rule_containment_contradictions(assembly.knowledge.compatibility_rules)
    machine = compute_confidence(signals, contradictions)

    # The model reported that no compatibility statement exists anywhere. Believed. With an
    # observed-window fact assembled (see `_assemble`) that is a LOW fact: real releases, an
    # honest "nothing published", and a person to weigh it. Without one -- nothing found at
    # all, or a quote that failed -- it stays UNRESOLVED: a "not found" answer must not
    # acquire a rating from whatever incidental values the extraction also carried.
    if extraction.no_compatibility_found or not extraction.found:
        machine = Confidence.LOW if assembly.knowledge is not None else Confidence.UNRESOLVED

    # -- Re-check the assessment against the evidence, and fold the caps in. Every
    # check here compares something the model CONCLUDED against something the model
    # RETURNED AS EVIDENCE, so a conclusion that outruns its own evidence fails visibly
    # on the receipt and drags the final confidence to LOW.
    assessment = extraction.assessment
    proposed: Confidence | None = None
    evidence_tier: str | None = None
    anchor_from_task = False
    assessment_signals: tuple = ()

    if assessment is not None:
        evidence_tier = str(assessment.evidence_tier)
        proposed = Confidence(str(assessment.proposed_confidence))

        policy_window = None
        checks = [releases_present(len(extraction.releases))]
        seen = frozenset(r.version for r in extraction.releases)
        checks.append(recommendation_is_a_seen_release(assessment.recommended_version, seen))
        if assessment.derivation is not None:
            policy_window, policy_ok, policy_why = recompute_policy_window(assessment.derivation)
            anchor_from_task = (
                assessment.derivation.anchor_basis is AnchorBasis.TASK_LATEST_UPSTREAM
            )
            checks.append(policy_derivation_recomputes(policy_ok, policy_why))

        # Target coverage, recomputed from OUR windows -- never from the assessment's own
        # covers_target_kubernetes boolean, which is what this check exists to check.
        rec_series = assessment.recommended_series
        rec_specs = {v for v in (rec_series, assessment.recommended_version) if v}
        covering = [
            d
            for d in derived
            if d.storable
            and d.statement.version_spec in rec_specs
            and _covers(d.window, assessment.target_kubernetes)
        ]
        basis = covering[0].statement.kind if covering else None
        policy_covers = (
            policy_window is not None
            and assessment.derivation is not None
            and assessment.derivation.applies_to_series == rec_series
            and _covers(policy_window, assessment.target_kubernetes)
        )
        checks.append(
            target_covered_by_evidence(
                bool(covering) or policy_covers,
                rec_series,
                assessment.target_kubernetes,
                str(basis) if basis else ("POLICY" if policy_covers else "none"),
            )
        )
        assessment_signals = tuple(checks)
        signals = signals + assessment_signals

    confidence = final_confidence(
        machine,
        proposed=proposed,
        evidence_tier=evidence_tier,
        assessment_signals=assessment_signals,
        anchor_from_task=anchor_from_task,
    )
    if sequencing_fallback and confidence in (Confidence.HIGH,):
        # One window is the MODEL'S reading rather than the judge's inference. Real, quoted,
        # and visible on the receipt -- but a person should look, so never HIGH.
        confidence = Confidence.MEDIUM

    knowledge = assembly.knowledge
    if knowledge is not None and confidence is not Confidence.UNRESOLVED:
        knowledge = knowledge.model_copy(update={"evidence_confidence": confidence})
    verified, total = report.coverage

    if knowledge is None:
        # No usable fact. Rejected with the reason, so a report can explain the gap. The
        # direction is NEW because there is no window to compare against a prior.
        reason = (
            extraction.not_found_reason
            if (extraction.no_compatibility_found or not extraction.found)
            else assembly.detail
        )
        receipt = ResearchReceipt(
            confidence=Confidence.UNRESOLVED,
            signals=signals,
            contradictions=contradictions,
            change_direction=ChangeDirection.NEW,
            decision=AcceptanceDecision.REJECTED,
            summary=(
                summarize(
                    Confidence.UNRESOLVED,
                    contradictions,
                    ChangeDirection.NEW,
                    AcceptanceDecision.REJECTED,
                )
                + f" Reason: {reason or assembly.detail}"
            ),
            evidence_digest=pages.digest(),
            values_verified=verified,
            values_total=total,
            tested_windows=tested_windows(extraction.statements),
            machine_confidence=machine,
            proposed_confidence=proposed,
            evidence_tier=evidence_tier,
        )
        return CandidateFact(
            component_id=extraction.component_id,
            version_series=extraction.version_series,
            knowledge=None,
            receipt=receipt,
        )

    direction = classify_direction(extraction.version_series, knowledge, prior)
    decision = decide(confidence, contradictions, direction)

    if decision is AcceptanceDecision.REJECTED:
        knowledge = None

    receipt = ResearchReceipt(
        confidence=confidence,
        signals=signals,
        contradictions=contradictions,
        change_direction=direction,
        decision=decision,
        summary=summarize(confidence, contradictions, direction, decision),
        evidence_digest=pages.digest(),
        values_verified=verified,
        values_total=total,
        tested_windows=tested_windows(extraction.statements),
        machine_confidence=machine,
        proposed_confidence=proposed,
        evidence_tier=evidence_tier,
    )
    return CandidateFact(
        component_id=extraction.component_id,
        version_series=extraction.version_series,
        knowledge=knowledge,
        receipt=receipt,
    )
