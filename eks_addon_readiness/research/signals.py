"""Confidence, computed from signals over evidence this process fetched itself.

Every function here is a pure function of already-fetched evidence -- no model call, no
network, no clock -- so the confidence a fact receives is reproducible from the receipt.

The design choices here, and why each is forced by a class of wrong fact:

**Confidence is derived from the source, not asserted by the model.** `FIRST_PARTY_SOURCE`
fires on `authority.py`'s derivation from the source's host, path and the shape of the
fetched bytes, rather than on a model-set boolean -- a boolean the model controls can buy
HIGH for a source that does not deserve it.

**Quote support is a hard gate.** A gate that produces false negatives -- rejecting a range
string that is verbatim in its source -- is tempting to demote to a soft signal, but the
answer to a check that is both too weak and too strict is a correct check, not a disabled
one. `verify.py` removes the false negatives, so the gate can carry weight.

**Coverage counts.** A fact is only as verified as its least verified stored value.
Computing confidence from `claims` alone would let `rule`, `additional_rules`,
`known_releases` and `recommended_version` reach the store unexamined.
"""

from __future__ import annotations

from typing import Any

from ..knowledge import is_exact_spec, is_series_spec
from ..models import Citation, Confidence
from .authority import SourceAuthority
from .boundary import BoundKind, CompatibilityStatementInput
from .receipt import Contradiction, SignalName, SignalResult

# Authorities strong enough that a single source reaches HIGH without corroboration. A
# machine-readable file or a table in the project's own bytes is a definite statement; a
# sentence in prose is where a tested window and a supported one look the same, so prose
# alone caps at MEDIUM.
STRONG_AUTHORITIES = frozenset(
    {
        SourceAuthority.FIRST_PARTY_STRUCTURED,
        SourceAuthority.FIRST_PARTY_TABULAR,
    }
)


def quote_matches_source(report) -> SignalResult:
    """Fire only if every quote appears in the bytes of the page it cites.

    All-or-nothing. One quote that exists in no fetched page taints the fact: a reader
    cannot tell which of the remaining quotes to trust once one is shown to be composed,
    and a composed quote is the mechanism by which a plausible table nobody published
    can pass a weaker check.
    """
    bad = report.fabricated_quotes + report.misattributed_quotes
    if bad:
        first = bad[0]
        return SignalResult(
            signal=SignalName.QUOTE_MATCHES_SOURCE,
            passed=False,
            detail=(
                f"{len(bad)} of {len(report.quote_checks)} quote(s) do not appear in the "
                f"bytes of the page they cite. First: {first.detail}"
            ),
        )
    return SignalResult(
        signal=SignalName.QUOTE_MATCHES_SOURCE,
        passed=True,
        detail=(
            f"all {len(report.quote_checks)} quote(s) appear in the fetched bytes of the "
            "page each cites"
        ),
        quotes=report.verified_quotes,
    )


def values_trace_to_quotes(report) -> SignalResult:
    """Fire only if every value destined for the store traces to a verified quote.

    A per-value coverage check, not just a per-quote one. Without it, stored values can
    appear in no quote at all, and a fact can reach HIGH on a single quote while most of
    what it stores is unsupported.
    """
    unverified = report.unverified_values
    _, total = report.coverage
    if unverified:
        first = unverified[0]
        return SignalResult(
            signal=SignalName.VALUES_TRACE_TO_QUOTES,
            passed=False,
            detail=(
                f"{len(unverified)} of {total} stored value(s) do not trace to a verified "
                f"quote. First: {first.field}={first.value!r} -- {first.detail}"
            ),
        )
    return SignalResult(
        signal=SignalName.VALUES_TRACE_TO_QUOTES,
        passed=True,
        detail=(
            f"every one of {total} stored value(s) traces to a quote verified against the "
            "fetched page it cites"
        ),
    )


def _independent(a: Citation, b: Citation) -> bool:
    """Whether two citations are independent sources rather than one page twice."""
    return str(a.url) != str(b.url)


def corroboration(
    statements: tuple[CompatibilityStatementInput, ...],
    citation_for,
) -> SignalResult:
    """Fire if some spec's window is stated by two independent sources.

    Independence is by URL: the same value quoted twice from one page is one source. Two
    quotes of the identical table row from the identical URL corroborate nothing.

    Statements of *different kinds* do not corroborate each other even when their numbers
    agree. One page saying "tested 1.30-1.32" and another saying "supported 1.30-1.32" are
    two different claims, and treating them as one would launder the weaker into the
    stronger -- the exact collapse this stage exists to prevent.
    """
    by_key: dict[tuple[str, str, str, str], list[Citation]] = {}
    for s in statements:
        if s.kind is BoundKind.UNSTATED:
            continue
        key = (
            s.version_spec,
            str(s.kind),
            s.stated_minimum or "",
            s.stated_maximum_inclusive or "",
        )
        by_key.setdefault(key, []).append(citation_for(s))

    for key, citations in sorted(by_key.items()):
        independent: list[Citation] = []
        for citation in citations:
            if all(_independent(citation, seen) for seen in independent):
                independent.append(citation)
        if len(independent) >= 2:
            return SignalResult(
                signal=SignalName.CORROBORATION,
                passed=True,
                detail=(
                    f"{key[0]} as {key[1]} is stated identically by "
                    f"{len(independent)} independent sources"
                ),
                citations=tuple(independent),
            )
    return SignalResult(
        signal=SignalName.CORROBORATION,
        passed=False,
        detail="no statement is made by two independent sources",
    )


def first_party_source(authorities: tuple[SourceAuthority, ...]) -> SignalResult:
    """Fire if any evidence is a table or structured file in the component's own source.

    Derived, never asserted. The signal reports every authority it saw, so a reader can
    see a third-party page was read and how it was weighted -- which is what makes an AWS
    page about a component legible as third-party rather than invisible.
    """
    strong = [a for a in authorities if a in STRONG_AUTHORITIES]
    if strong:
        return SignalResult(
            signal=SignalName.FIRST_PARTY_SOURCE,
            passed=True,
            detail=(
                f"{len(strong)} of {len(authorities)} source(s) are the component's own, "
                "read as a table or a structured file in the bytes fetched"
            ),
            authorities=authorities,
        )
    kinds = ", ".join(sorted({str(a) for a in authorities})) or "none"
    return SignalResult(
        signal=SignalName.FIRST_PARTY_SOURCE,
        passed=False,
        detail=(
            "no source is a table or structured file in the component's own repository "
            f"or documentation (saw: {kinds})"
        ),
        authorities=authorities,
    )


def support_window_derived(derived) -> SignalResult:
    """Fire if at least one storable support window came out of the statements.

    Fails when every statement was a bare policy rule or unstated -- a tested range DOES
    derive (basis TESTED, the word kept visible), so this failure now means the
    sources state no absolute numbers anywhere. Still the honest outcome the contract
    exists to make possible: read, transcribed correctly, nothing to derive, nothing
    invented.
    """
    storable = [d for d in derived if d.storable]
    if storable:
        return SignalResult(
            signal=SignalName.SUPPORT_WINDOW_DERIVED,
            passed=True,
            detail=(
                f"{len(storable)} of {len(derived)} statement(s) yield a support window; "
                f"first basis: {storable[0].basis}"
            ),
        )
    reasons = "; ".join(sorted({d.basis for d in derived})) or "no statements were made"
    return SignalResult(
        signal=SignalName.SUPPORT_WINDOW_DERIVED,
        passed=False,
        detail=f"no statement yields a support window. {reasons}",
    )


def shape_sanity(passed: bool, detail: str) -> SignalResult:
    """Record the structural check of the assembled contract as a signal.

    The check itself lives with the assembly, which is where the contract is built; this
    only records the result so it counts toward confidence uniformly with the others.
    """
    return SignalResult(
        signal=SignalName.SHAPE_SANITY, passed=passed, detail=detail
    )


# Fields whose disagreement is a genuine, verdict-affecting contradiction: the version
# bounds a compatibility verdict is computed from. Descriptive fields are excluded --
# the same API is legitimately written at different granularities across sources
# (`metrics.k8s.io/v1beta1` in a README, `v1beta1` in an APIService manifest whose group
# is a separate field), so a raw-string difference there is representational, not a
# conflict. Those fields can corroborate; they cannot block.
CONTRADICTION_FIELDS = frozenset(
    {"stated_minimum", "stated_maximum_inclusive"}
)


def find_contradictions(
    statements: tuple[CompatibilityStatementInput, ...],
    citation_for,
) -> tuple[Contradiction, ...]:
    """Independent sources stating incompatible bounds for the same spec AND kind.

    Kind participates in the key for the same reason it does in corroboration: a tested
    window and a supported window differing on a number are not two sources disagreeing
    about one fact, they are two different facts. Comparing across kinds would manufacture
    contradictions on every project that publishes both.
    """
    contradictions: list[Contradiction] = []
    by_key: dict[tuple[str, str], list[CompatibilityStatementInput]] = {}
    for s in statements:
        if s.kind is BoundKind.UNSTATED:
            continue
        by_key.setdefault((s.version_spec, str(s.kind)), []).append(s)

    for (spec, kind), group in sorted(by_key.items()):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left, right = group[i], group[j]
                left_citation, right_citation = citation_for(left), citation_for(right)
                if not _independent(left_citation, right_citation):
                    continue
                for field in sorted(CONTRADICTION_FIELDS):
                    left_value = getattr(left, field)
                    right_value = getattr(right, field)
                    if (
                        left_value is not None
                        and right_value is not None
                        and left_value != right_value
                    ):
                        contradictions.append(
                            Contradiction(
                                field=f"{field}[{spec}] as {kind}",
                                left_value=left_value,
                                right_value=right_value,
                                left_citation=left_citation,
                                right_citation=right_citation,
                            )
                        )
    contradictions.extend(_self_inconsistencies(by_key, citation_for))
    return tuple(contradictions)


def _minor(value: str) -> tuple[int, ...]:
    """`"1.36"` as `(1, 36)`, for comparing Kubernetes minors as numbers.

    STRING COMPARISON IS WRONG HERE and quietly so: `"1.9" > "1.36"` is true as text and
    false as versions, so a ceiling of 1.9 would read as ABOVE a ceiling of 1.36 and the
    containment check below would report the contradiction backwards -- or miss it. The
    field patterns guarantee `\\d+\\.\\d+`, so this cannot raise on validated input.
    """
    return tuple(int(part) for part in value.split("."))


def _series_of(spec: str) -> str:
    """The series a spec belongs to: `2.20.2` -> `2.20.x`, and `2.20.x` -> itself."""
    parts = spec.split(".")
    return f"{parts[0]}.{parts[1]}.x" if len(parts) == 3 else spec


def _self_inconsistencies(
    by_key: dict[tuple[str, str], list[CompatibilityStatementInput]],
    citation_for,
) -> list[Contradiction]:
    """One spec given two different windows BY THE SAME PAGE, which the pass above skips.

    `_independent` exists so the same table row quoted twice is not counted as two sources
    agreeing -- correct for corroboration, and correct for a cross-source DISPUTE. It is
    wrong here, and the difference is what the disagreement means:

        two pages disagree      -> the sources conflict, and independence is the question
        one page says both      -> the FACT contradicts itself, and no amount of
                                   independence analysis makes it consistent

    LIVE EXAMPLE, and the reason this exists. An approved keda fact carries `2.9.x` twice --
    `[1.23, 1.26)` and `[1.23, unbounded)` -- both cited to the same keda-docs page. Nothing
    flagged it, so it was approved, and `order/moves.py::_series_covers` returns on the FIRST
    rule naming a series while `_series_supported_at` scans them all. The two therefore
    disagree about that series and the sequencer can plan a move from a series to itself.

    Reported rather than raised: this routes through `receipt.contradictions`, which
    `acceptance.py` already uses to cap confidence and the approval page already renders. A
    person then sees the fact contradicting itself BEFORE approving it, which is the whole
    gap. Refusing outright would leave the component FACT_MISSING, researched again next run,
    contradicting itself again -- a loop rather than a fix.
    """
    found: list[Contradiction] = []
    for (spec, kind), group in sorted(by_key.items()):
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                left, right = group[i], group[j]
                left_citation, right_citation = citation_for(left), citation_for(right)
                # ONLY the pairs the pass above declined to look at. Anything independent
                # was already judged there, and reporting it twice would double-count one
                # disagreement into two contradictions.
                if _independent(left_citation, right_citation):
                    continue
                for field in sorted(CONTRADICTION_FIELDS):
                    left_value = getattr(left, field)
                    right_value = getattr(right, field)
                    if left_value == right_value:
                        continue
                    # A STATED BOUND AGAINST AN OMITTED ONE IS THE KEDA CASE, so `None` on
                    # one side is a real disagreement here -- unlike the cross-source pass,
                    # where a source that simply does not mention a ceiling is silent rather
                    # than claiming there is none. Within ONE page, listing a spec twice and
                    # bounding it only once is the page contradicting itself.
                    if left_value is None and right_value is None:
                        continue
                    found.append(
                        Contradiction(
                            field=f"{field}[{spec}] as {kind}, stated twice by one source",
                            left_value=str(left_value) if left_value else "not stated",
                            right_value=str(right_value) if right_value else "not stated",
                            left_citation=left_citation,
                            right_citation=right_citation,
                        )
                    )
    return found


def rule_containment_contradictions(rules: tuple[Any, ...]) -> tuple[Contradiction, ...]:
    """An exact release whose DERIVED window outlives the series it belongs to.

    THE GAP THAT PRODUCED A REAL DUPLICATE STEP, and it is invisible to every check above for
    two separate reasons -- which is why it is checked here, on the assembled rules, rather
    than on the statements.

        2.20.x  ->  [1.33, 1.36)      the series stops before 1.36
        2.20.2  ->  [1.23, unbounded) the release inside it works forever

    2.20.2 IS A MEMBER OF 2.20.x, so both cannot be true. The consequence is not cosmetic:
    `order/moves.py::_series_covers("2.20.x", "1.36")` is False while
    `_series_supported_at("1.36")` returns `2.20.2`, so the sequencer moves to the series at
    1.35 and then moves AGAIN at 1.36 -- to the same version. Two steps installing 2.20.2. The
    plan looked duplicated; the fact was contradictory.

    **WHY NOT ON THE STATEMENTS.** Checked there, this cannot fire. The unbounded window is
    derived from a floor-only statement (`PUBLISHED_MINIMUM`: "requires Kubernetes 1.23+"),
    whose `kind` differs from the series' `SUPPORTED_RANGE` -- and `find_contradictions`
    correctly refuses to compare across kinds, because a tested window and a supported window
    are different facts. A floor-only statement also does not CLAIM an unbounded ceiling; it is
    silent about the ceiling, and silence is not a contradiction.

    The unboundedness is manufactured later, when `bounds.py` turns that silence into a
    `VersionRange` with no maximum. Defensible in isolation -- an unknown ceiling cannot be
    invented -- but at that point the rule DOES assert coverage of every later Kubernetes
    version, and it asserts it for a release whose own series stops. So the contradiction
    exists in the derived rules and nowhere earlier, which is exactly where this reads it.

    A CEILING IS COMPARED, A FLOOR IS NOT. A release with a later floor than its series is
    ordinary -- a patch published after the series opened. A release outliving its series'
    ceiling is not: the series ceiling is a claim about every release in it.

    Takes `rules` structurally rather than importing `knowledge` for typing, so this module
    keeps its existing import surface.
    """
    ceilings: dict[str, Any] = {}
    for rule in rules:
        spec = rule.component_version_series
        if is_series_spec(spec) and rule.kubernetes_versions.maximum_exclusive:
            ceilings[spec] = rule

    found: list[Contradiction] = []
    for rule in rules:
        spec = rule.component_version_series
        if not is_exact_spec(spec):
            continue
        parent = ceilings.get(_series_of(spec))
        if parent is None:
            continue
        top = rule.kubernetes_versions.maximum_exclusive
        parent_top = parent.kubernetes_versions.maximum_exclusive
        # UNBOUNDED IS THE LOUDER HALF, and the half keda actually carried: the release states
        # no ceiling at all while its series states one, which claims the release outlives its
        # own series indefinitely.
        if top and _minor(top) <= _minor(parent_top):
            continue
        found.append(
            Contradiction(
                field=(
                    f"kubernetes_versions[{spec}] reaches past its own series "
                    f"{parent.component_version_series}"
                ),
                left_value=f"below {top}" if top else "no ceiling",
                right_value=f"below {parent_top}",
                left_citation=rule.evidence[0],
                right_citation=parent.evidence[0],
            )
        )
    return tuple(found)


def compute_confidence(
    signals: tuple[SignalResult, ...],
    contradictions: tuple[Contradiction, ...],
) -> Confidence:
    """Derive the confidence rating from which signals fired.

    Legible enough that a reader can reconstruct the rating from the receipt by hand:

    - no support window derived, or shape sanity fails -> UNRESOLVED. There is no usable
      fact here. Not a weak fact: no fact.
    - a quote does not match its source -> UNRESOLVED. A composed quote is not weak
      evidence, it is absent evidence, and grading it LOW would let it be promoted by a
      human who trusted the label. Grading this case LOW rather than UNRESOLVED is the
      mistake that stores it.
    - a stored value traces to no verified quote -> LOW. The quotes are real but do not
      cover everything being stored, which is a coverage problem a human can resolve by
      reading the source.
    - any contradiction -> LOW, with both sources shown.
    - covered and coherent, from first-party structured/tabular evidence, or corroborated
      -> HIGH.
    - covered and coherent from prose alone -> MEDIUM.
    """
    fired = {s.signal for s in signals if s.passed}

    if SignalName.SHAPE_SANITY not in fired:
        return Confidence.UNRESOLVED
    if SignalName.SUPPORT_WINDOW_DERIVED not in fired:
        return Confidence.UNRESOLVED
    # The hard gate. A quote that is not in the page it cites is not evidence at all.
    if SignalName.QUOTE_MATCHES_SOURCE not in fired:
        return Confidence.UNRESOLVED

    if SignalName.VALUES_TRACE_TO_QUOTES not in fired:
        return Confidence.LOW
    if contradictions:
        return Confidence.LOW

    if (
        SignalName.FIRST_PARTY_SOURCE in fired
        or SignalName.CORROBORATION in fired
    ):
        return Confidence.HIGH
    return Confidence.MEDIUM


# ---------------------------------------------------------------------------
# The assessment checks, and the cap arithmetic that folds them in.
# ---------------------------------------------------------------------------
#
# `compute_confidence` above stays the machine's own rating of the
# EVIDENCE, keyed only on the six evidence signals. The functions below rate the model's
# ASSESSMENT against that evidence, and `final_confidence` folds everything into one
# level a reader can recompute by hand:
#
#     final = min( machine, tier cap, model's proposal, LOW if any assessment check failed,
#                  MEDIUM if the derivation anchor came from the task rather than a source )
#
# The model can only ever LOWER the outcome. There is no path by which anything it wrote
# raises the machine's rating.

# What the best class of evidence behind a recommendation can justify. A matrix the
# project publishes (in its repository or its docs) can carry HIGH; so can an N-2
# derivation whose policy and anchor are both quoted — the arithmetic is recomputed. A
# chart kubeVersion constraint or a bare floor is a real but weaker claim, capped MEDIUM.
# No evidence at all is LOW: the recommendation is "latest, unverified" and says so.
EVIDENCE_TIER_CAPS: dict[str, Confidence] = {
    "MATRIX_IN_REPOSITORY": Confidence.HIGH,
    "MATRIX_IN_OFFICIAL_DOCS": Confidence.HIGH,
    "POLICY_DERIVED": Confidence.HIGH,
    "CHART_CONSTRAINT_ONLY": Confidence.MEDIUM,
    "FLOOR_ONLY": Confidence.MEDIUM,
    "NONE": Confidence.LOW,
}

_LEVEL_ORDER = (
    Confidence.UNRESOLVED,
    Confidence.LOW,
    Confidence.MEDIUM,
    Confidence.HIGH,
)


def _min_level(*levels: Confidence) -> Confidence:
    return min(levels, key=_LEVEL_ORDER.index)


def releases_present(release_count: int) -> SignalResult:
    """Concrete releases were named, so a version can be discussed at all.

    A compatibility table names SERIES; a human installs a RELEASE. An extraction with an
    empty releases list cannot answer "what do I install", however good its table is.
    """
    return SignalResult(
        signal=SignalName.RELEASES_PRESENT,
        passed=release_count > 0,
        detail=(
            f"{release_count} release(s) named, each with a quote"
            if release_count
            else "no releases returned, so no concrete installable version is known"
        ),
    )


def recommendation_is_a_seen_release(
    recommended: str | None, seen_versions: frozenset[str]
) -> SignalResult:
    """The recommended version must be a release the model itself returned with a quote.

    This is the check that makes the assessment's headline number citable: a version that
    appears nowhere in `releases` is a version nothing on the receipt evidences.
    """
    if recommended is None:
        return SignalResult(
            signal=SignalName.RECOMMENDATION_IS_A_SEEN_RELEASE,
            passed=False,
            detail="no version was recommended",
        )
    if recommended in seen_versions:
        return SignalResult(
            signal=SignalName.RECOMMENDATION_IS_A_SEEN_RELEASE,
            passed=True,
            detail=f"{recommended} appears in the returned releases with a quote",
        )
    return SignalResult(
        signal=SignalName.RECOMMENDATION_IS_A_SEEN_RELEASE,
        passed=False,
        detail=(
            f"{recommended} is not among the {len(seen_versions)} release(s) the "
            "extraction returned, so nothing on this receipt evidences it"
        ),
    )


def target_covered_by_evidence(
    covered: bool, recommended_series: str | None, target: str, basis: str
) -> SignalResult:
    """A derived window for the recommended series contains the target minor.

    Computed by `judge.py` from the windows `bounds.py` derived — never from the
    assessment's own `covers_target_kubernetes` boolean, which this signal exists to
    check.
    """
    if covered:
        return SignalResult(
            signal=SignalName.TARGET_COVERED_BY_EVIDENCE,
            passed=True,
            detail=(
                f"a derived window for {recommended_series} contains target {target} "
                f"(basis: {basis})"
            ),
        )
    return SignalResult(
        signal=SignalName.TARGET_COVERED_BY_EVIDENCE,
        passed=False,
        detail=(
            f"no derived window for {recommended_series or 'the recommended series'} "
            f"contains target {target}"
        ),
    )


def policy_derivation_recomputes(ok: bool, detail: str) -> SignalResult:
    """The N-2 arithmetic recomputed from the stated anchor matches what the model wrote.

    The recomputation itself is `bounds.recompute_policy_window`; this records the
    outcome so it counts on the receipt uniformly with the others.
    """
    return SignalResult(
        signal=SignalName.POLICY_DERIVATION_RECOMPUTES, passed=ok, detail=detail
    )


def final_confidence(
    machine: Confidence,
    *,
    proposed: Confidence | None,
    evidence_tier: str | None,
    assessment_signals: tuple[SignalResult, ...] = (),
    anchor_from_task: bool = False,
) -> Confidence:
    """Fold the machine's rating, the tier cap and the model's proposal into one level.

    Every input can only lower the result. With no assessment at all this is the machine's
    rating unchanged — the no-assessment path costs nothing.
    """
    level = machine
    if evidence_tier is not None:
        level = _min_level(level, EVIDENCE_TIER_CAPS.get(evidence_tier, Confidence.LOW))
    if proposed is not None:
        level = _min_level(level, proposed)
    if any(not s.passed for s in assessment_signals):
        # The model asserted something the evidence it returned does not hold up. The
        # data is still shown; the rating says a person must read it.
        level = _min_level(level, Confidence.LOW)
    if anchor_from_task:
        # The derivation counts down from a minor no source confirmed.
        level = _min_level(level, Confidence.MEDIUM)
    return level
