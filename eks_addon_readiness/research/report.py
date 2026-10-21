"""The approval report: one digested document, and the verdict delta that is the real decision.

**One document, formats are views over it.** Every number a reader sees is computed here,
once, in a `ContractModel` with a digest -- never in a template. That is not style. A
renderer that computes the same count three ways can produce three different answers for
one document, so a reader cannot tell which is true. A template that computes nothing
cannot disagree with the document it renders.

The digest matters for a second reason here: the `ApprovalRecord` stores the digest of the
report the approver was shown. That is what makes "they approved this" a checkable claim
rather than a timestamp, so the document has to be hashable independently of how it is
rendered.

**What this document is for.** The thing a person is actually deciding is not the raw fact.
It is *what changes if they approve it*. "This proposes 0.6.x supports Kubernetes 1.25 to
1.27" is a fact; "approving this turns metrics-server from COMPATIBLE to
REQUIRES_COMPONENT_UPGRADE at 1.36" is the decision. So `verdict_delta` runs the affected
component through the frozen Stage 4 rules twice -- once against what the store holds now,
once against the store as it would be with this fact -- and shows both. Computed with the
real rule module rather than re-implemented, because a delta computed by a second
implementation of the rules is a delta between two guesses.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any

from pydantic import Field

from ..decide.rules import verdict_for_hop
from ..facts.ledger import FactStatus, GapDetail, GapEntry
from ..facts.series import spec_matches_version
from ..knowledge import ComponentKnowledge
from ..models import Confidence, ContractModel, KubernetesHop
from .candidate import CandidateFact
from .receipt import AcceptanceDecision, ChangeDirection, SignalName

# A concrete `major.minor.patch`; the only shape `recommended_version` may carry.
_EXACT_RELEASE = re.compile(r"^\d+\.\d+\.\d+$")


class QuotedClaim(ContractModel):
    """One value being proposed, the verbatim quote for it, and where that quote lives.

    Flat and fully resolved on purpose: a template rendering this needs no lookups, so it
    cannot look something up differently from how the document did.
    """

    field: str = Field(min_length=1)
    value: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_revision: str = Field(min_length=1)
    # How the quote checked out against the fetched page bytes: PRESENT, PRESENT_WHITESPACE,
    # WRONG_PAGE, ABSENT, NOT_FETCHED. Shown per claim, because "the fact is LOW" does not
    # tell an approver WHICH value failed.
    quote_verdict: str = Field(min_length=1)
    verified: bool
    # How the source was classified by code -- never by the model.
    source_authority: str = Field(min_length=1)


class FiredSignal(ContractModel):
    """One confidence signal, whether it fired, and the sentence explaining why.

    Confidence alone is opaque. An approver told "HIGH" learns nothing they can check; an
    approver shown that quote-matches-source passed over 9 quotes, values-trace-to-quotes
    passed over 43 values, and corroboration did NOT fire, can form their own view.
    """

    name: str = Field(min_length=1)
    fired: bool
    detail: str = Field(min_length=1)


class WindowRow(ContractModel):
    """One compatibility window, rendered as the strings a reader compares.

    `basis` is the sentence from `bounds.py` explaining where the ceiling came from -- or
    why there is none. That sentence is the single most useful thing on this report for the
    class of defect this stage was rebuilt to fix.
    """

    version_spec: str = Field(min_length=1)
    window: str = Field(min_length=1)
    basis: str = ""


class HopVerdictDelta(ContractModel):
    """What one Kubernetes hop's verdict is now, and what it becomes if this is approved."""

    hop: KubernetesHop
    verdict_now: str = Field(min_length=1)
    reason_now: str = Field(min_length=1)
    verdict_if_approved: str = Field(min_length=1)
    reason_if_approved: str = Field(min_length=1)

    @property
    def changes(self) -> bool:
        return self.verdict_now != self.verdict_if_approved


def _as_text(value) -> str | None:
    """Enum or None -> plain string or None, so the report carries JSON, not enums."""
    return None if value is None else str(value)


class CitedSourceRow(ContractModel):
    """One document the assessment relied on, and what it was used for.

    Shown as a list an approver can open one by one. Only pages that were fetched
    reach this list, so every row has recorded bytes behind it.
    """

    url: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    used_for: str = Field(min_length=1)


def chart_summary(knowledge) -> dict[str, Any]:
    """The chart repository off an assembled fact, in the shape the wire and the page share.

    One function for the two Stage 6 handlers and the report so the three cannot disagree
    about what "the chart" means: it is what the FACT carries, which is only what verified.
    A key that is always present, never absent, so a consumer can read it without `get`.
    """
    if knowledge is None or knowledge.chart_repository is None:
        return {"chart_repository": None}
    return {"chart_repository": str(knowledge.chart_repository)}


# THE PREFIX OF THE NOTE A HELM PROPOSAL CARRIES WHEN NO CHART REPOSITORY VERIFIED. Shared by
# the judge stage, which adds the note, and the fact-approval renderer, which shows it in the
# Recommended column rather than inside the collapsed caveats: without a repository the upgrade
# lane skips the component, and an approver must see that before approving, not two stages later.
CHART_MISSING_PREFIX = "chart repository:"


def chart_missing_note(helm_release: str | None, knowledge, not_found: list[str]) -> list[str]:
    """`not_found` with a chart-repository entry added for a Helm release that has none.

    Nothing for a managed add-on or a manifest (no `helm_release`), nothing when a repository
    verified, and nothing when the model already wrote its own `chart repository:` entry --
    its list of pages tried is more useful than this generic one.
    """
    if not helm_release or chart_summary(knowledge)["chart_repository"] is not None:
        return not_found
    if any(str(g).strip().lower().startswith(CHART_MISSING_PREFIX) for g in not_found):
        return not_found
    return [
        *not_found,
        f"{CHART_MISSING_PREFIX} none verified for Helm release {helm_release}, so the upgrade "
        "lane cannot install this component. Re-research it before approving an upgrade.",
    ]


def cited_sources_from_claims(claims: tuple[QuotedClaim, ...]) -> tuple[CitedSourceRow, ...]:
    """The pages the extraction rests on, derived from its own quotes.

    THE FALLBACK FOR A MODEL THAT LEFT `cited_sources` EMPTY, which it did twice in one evening
    for a KEDA extraction whose 18 statements and 5 releases all verified. The list is display
    -- nothing downstream acts on it -- but it is the list an approver reads, and an empty one
    under a HIGH fact reads as "no sources". Every quoted statement, release and chart claim
    already names the (url, revision) it was read from, so the list is computed from those:
    it cannot be empty while there are quotes, and it cannot name a page nothing was quoted
    from. `used_for` is built from the fields quoted off each page, in the vocabulary the
    prompt asks the model for.

    Deterministic and ordered by first appearance, so the two report builders (Stage 6 and
    the approve-time rebuild) produce the same rows and the report digest still matches.
    """
    by_page: dict[tuple[str, str], list[str]] = {}
    for claim in claims:
        by_page.setdefault((claim.source_url, claim.source_revision), []).append(claim.field)
    rows: list[CitedSourceRow] = []
    for (url, revision), fields in by_page.items():
        uses: list[str] = []
        specs = []
        for field in fields:
            # `quoted_claims_for` names a statement's values `minimum for 2.4.x (KIND)`,
            # `stated maximum for 2.4.x (KIND)` and `listed minor for 2.4.x`.
            found = re.search(r"\bfor (\S+?)(?: \(|$)", field)
            if found and found.group(1) not in specs:
                specs.append(found.group(1))
        if specs:
            uses.append("compatibility rows for " + ", ".join(specs))
        if any(f == "release" for f in fields):
            uses.append("release list")
        if any(f == "chart_repository" for f in fields):
            uses.append("chart repository")
        if not uses:
            uses.append("quoted values")
        rows.append(
            CitedSourceRow(url=url, revision=revision or "unpinned", used_for="; ".join(uses))
        )
    return tuple(rows)


class ApprovalReport(ContractModel):
    """Everything a person needs in order to approve or refuse one proposed fact.

    Every field is a computed value, so a renderer is a pure projection. `digest()` is what
    the `ApprovalRecord` stores, which is how an approval becomes evidence of what was seen.
    """

    schema_version: str = "1.0"
    component_id: str = Field(min_length=1)
    version_series: str = Field(min_length=1)
    installed_version: str | None = None
    cluster: str | None = None
    target_kubernetes_version: str | None = None

    candidate_digest: str = Field(min_length=1)
    evidence_digest: str = Field(min_length=1)
    researched_at: datetime

    # The headline. Advice, not a gate -- see `acceptance.advisory_note`.
    advisory: str = Field(min_length=1)
    decision: AcceptanceDecision
    confidence: Confidence
    change_direction: ChangeDirection
    values_verified: int = 0
    values_total: int = 0

    signals: tuple[FiredSignal, ...] = Field(min_length=1)
    claims: tuple[QuotedClaim, ...] = ()
    contradictions: tuple[str, ...] = ()
    # Windows the source called TESTED, deliberately not turned into support ceilings.
    tested_windows: tuple[str, ...] = ()

    # The side-by-side: what the store holds now, and what this proposes.
    current_windows: tuple[WindowRow, ...] = ()
    proposed_windows: tuple[WindowRow, ...] = ()
    current_revision: str | None = None
    proposed_revision: str | None = None

    # The actual decision.
    verdict_deltas: tuple[HopVerdictDelta, ...] = ()

    # ---------------------------------------------------------------------
    # THE ASSESSMENT, as the approver reads it.
    #
    # Without these the report showed a window and its quotes and nothing about
    # what to DO -- no version to install, no ordering, no list of documents the
    # conclusion rested on. An approver was being asked to sign off a
    # compatibility rule rather than a recommendation, which is not the decision
    # they are actually making.
    #
    # Every field is copied from `Assessment` after `judge.py` re-checked it, and
    # the two confidence fields are shown TOGETHER on purpose: seeing that the
    # model proposed HIGH while the machine allowed LOW is the single most useful
    # thing on the page, and it is invisible if only the final level is printed.
    #
    # All optional: an extraction from the older transcribe-only prompt carries no
    # assessment, and a report for one must still render.
    # ---------------------------------------------------------------------
    recommended_version: str | None = None
    recommended_series: str | None = None
    recommended_chart_version: str | None = None
    # THE CHART REPOSITORY, AS THE FACT WILL CARRY IT: only if its quote verified, read off
    # the assembled knowledge rather than the extraction. This is what the upgrade lane will
    # `helm repo add`, so it is what the approver is approving.
    chart_repository: str | None = None
    action: str | None = None
    installed_version_status: str | None = None
    upgrade_ordering: str | None = None
    covers_current_kubernetes: bool | None = None
    covers_target_kubernetes: bool | None = None
    evidence_tier: str | None = None
    # What the model proposed, and what the signals alone allowed. `confidence`
    # above is the minimum of the two, so a reader can check the arithmetic.
    proposed_confidence: str | None = None
    machine_confidence: str | None = None
    confidence_rationale: str | None = None
    # The pinpoint list: url, revision, and what each document was used for.
    cited_sources: tuple[CitedSourceRow, ...] = ()
    caveats: tuple[str, ...] = ()
    not_found: tuple[str, ...] = ()

    def digest(self) -> str:
        """Content digest of the report. Stored on the approval, so it proves what was shown."""
        return "sha256:" + hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def changed_hops(self) -> tuple[HopVerdictDelta, ...]:
        return tuple(d for d in self.verdict_deltas if d.changes)

    def headline(self) -> str:
        """One sentence naming what approval would change. Computed once, here.

        A reader who reads nothing else should still learn the consequence.
        """
        changed = self.changed_hops
        if not changed:
            if not self.verdict_deltas:
                return (
                    f"Approving this stores a compatibility fact for {self.component_id}; "
                    "no cluster was supplied, so no verdict change was computed."
                )
            return (
                f"Approving this changes no verdict for {self.component_id} across the "
                f"{len(self.verdict_deltas)} hop(s) checked. It records the evidence."
            )
        parts = [
            f"Kubernetes {d.hop.target_version}: {d.verdict_now} -> {d.verdict_if_approved}"
            for d in changed
        ]
        return (
            f"Approving this changes {len(changed)} of {len(self.verdict_deltas)} verdict(s) "
            f"for {self.component_id}. " + "; ".join(parts) + "."
        )


def _window_text(rule) -> str:
    maximum = rule.kubernetes_versions.maximum_exclusive
    floor = rule.kubernetes_versions.minimum_inclusive
    if maximum is None:
        return f"Kubernetes {floor} or later (no recorded ceiling)"
    return f"Kubernetes {floor} up to but excluding {maximum}"


def _windows(knowledge: ComponentKnowledge | None, bases: dict[str, str]) -> tuple[WindowRow, ...]:
    if knowledge is None:
        return ()
    return tuple(
        WindowRow(
            version_spec=rule.component_version_series,
            window=_window_text(rule),
            basis=bases.get(rule.component_version_series, ""),
        )
        for rule in sorted(knowledge.compatibility_rules, key=lambda r: r.component_version_series)
    )


def _gap_for(
    component_id: str,
    installed_version: str | None,
    version_series: str,
    knowledge: ComponentKnowledge | None,
) -> GapEntry:
    """A minimal `GapEntry` for the verdict computation.

    Built rather than read from a ledger because the delta is about ONE component and asking
    for a whole ledger would make the report unbuildable without a full pipeline run. The
    fields Stage 4's rules actually read are identity, version, version_series and status,
    and those are exactly what is set here.
    """
    rule = None
    if knowledge is not None and installed_version:
        rule = next(
            (
                r
                for r in sorted(
                    knowledge.compatibility_rules,
                    key=lambda r: r.component_version_series,
                )
                if spec_matches_version(r.component_version_series, installed_version)
            ),
            None,
        )

    # `GapEntry` requires the fact provenance whenever the status claims a fact is
    # available, and it is right to: a status without the revision and window it came from
    # is a claim with no source. So the entry is FACT_AVAILABLE only when a rule actually
    # matches the installed version, and carries that rule's provenance when it does.
    if knowledge is not None and rule is not None:
        return GapEntry(
            workload_ref=f"proposed/{component_id}",
            identity=component_id,
            version=installed_version,
            version_series=version_series if installed_version else None,
            assessment_priority="1",
            status=FactStatus.FACT_AVAILABLE,
            detail_code=GapDetail.RULE_MATCH,
            reason=(
                f"Fact for {rule.component_version_series} under consideration for this "
                "verdict comparison."
            ),
            fact_knowledge_base_revision=knowledge.knowledge_base_revision,
            fact_series=rule.component_version_series,
            fact_kubernetes_versions=rule.kubernetes_versions,
        )

    if knowledge is not None:
        # An entry exists but no rule covers the installed version. That is
        # NO_RULE_FOR_SERIES, and Stage 4 still reads the knowledge, so the delta is real.
        known = ", ".join(sorted(r.component_version_series for r in knowledge.compatibility_rules))
        return GapEntry(
            workload_ref=f"proposed/{component_id}",
            identity=component_id,
            version=installed_version,
            version_series=version_series if installed_version else None,
            assessment_priority="1",
            status=FactStatus.FACT_MISSING,
            detail_code=GapDetail.NO_RULE_FOR_SERIES,
            reason=(
                f"The entry covers {known} but not the installed version."
                if known
                else "The entry covers no version spec."
            ),
        )

    return GapEntry(
        workload_ref=f"proposed/{component_id}",
        identity=component_id,
        version=installed_version,
        version_series=version_series if installed_version else None,
        assessment_priority="1",
        status=FactStatus.FACT_MISSING,
        detail_code=GapDetail.NO_ENTRY,
        reason="No fact is held for this component while the proposal is pending.",
    )


def verdict_delta(
    component_id: str,
    installed_version: str | None,
    version_series: str,
    current: ComponentKnowledge | None,
    proposed: ComponentKnowledge | None,
    hops: tuple[KubernetesHop, ...],
) -> tuple[HopVerdictDelta, ...]:
    """Run one component through the frozen Stage 4 rules twice: without, then with.

    The real Stage 4 rule function, not a re-implementation. A delta computed by a second
    implementation of the rules is a delta between two guesses, and the whole value of this
    section is that the "if approved" column is what the pipeline will actually say.
    """
    deltas: list[HopVerdictDelta] = []
    for index, hop in enumerate(hops):
        before_verdict, _, _, before_reason = verdict_for_hop(
            _gap_for(component_id, installed_version, version_series, current),
            current,
            hop,
            is_first_hop=index == 0,
        )
        after_verdict, _, _, after_reason = verdict_for_hop(
            _gap_for(component_id, installed_version, version_series, proposed),
            proposed,
            hop,
            is_first_hop=index == 0,
        )
        deltas.append(
            HopVerdictDelta(
                hop=hop,
                verdict_now=str(before_verdict),
                reason_now=before_reason,
                verdict_if_approved=str(after_verdict),
                reason_if_approved=after_reason,
            )
        )
    return tuple(deltas)


def _observed_window_caveat(knowledge: ComponentKnowledge | None) -> tuple[str, ...]:
    """The first caveat on a fact whose only window is observed, not published.

    `judge._observed_rule` titles that rule's citation so a reader of the fact can tell; the
    approver reads the page, not the fact, so the page says it first and in plain words.
    """
    from .judge import OBSERVED_WINDOW_TITLE

    if knowledge is None:
        return ()
    observed = [
        r.component_version_series
        for r in knowledge.compatibility_rules
        if any(c.title == OBSERVED_WINDOW_TITLE for c in r.evidence)
    ]
    if not observed:
        return ()
    return (
        f"No first-party compatibility statement was found for {', '.join(observed)}. The "
        "window recorded is the Kubernetes minor this series is observed running on, with no "
        "ceiling; nothing states that any newer Kubernetes version is supported. The "
        "recommendation is the newest recorded release, unverified.",
    )


def build_report(
    candidate: CandidateFact,
    *,
    current: ComponentKnowledge | None,
    quoted_claims: tuple[QuotedClaim, ...],
    proposed_bases: dict[str, str],
    researched_at: datetime,
    installed_version: str | None = None,
    cluster: str | None = None,
    hops: tuple[KubernetesHop, ...] = (),
    advisory: str,
    assessment=None,
) -> ApprovalReport:
    """Assemble the approval report. Pure: no store read, no clock, no network.

    Everything it needs is passed in, so the same candidate and the same store state always
    produce the same report -- and therefore the same digest, which is what makes the digest
    on an `ApprovalRecord` meaningful.

    `assessment` is the re-checked `Assessment` off the extraction, or None for an
    extraction from the older transcribe-only prompt. It is passed rather than read off the
    candidate because `CandidateFact` deliberately does not carry it: the candidate is what
    an approval is keyed to, and widening it would move every candidate digest.
    """
    proposed = candidate.knowledge

    def a(field: str, *, stringify: bool = False):
        """One assessment field, or None when there is no assessment."""
        value = getattr(assessment, field, None)
        if value is None:
            return None
        return str(value) if stringify else value

    # THE RECOMMENDATION IS THE PLAN'S, NOT THE MODEL'S. This page used to show the assessment's
    # `recommended_version`, and the plan recommended from the fact's rules -- two policies, and
    # on argo-rollouts they disagreed (page: 1.10.0; plan: stay on 1.6.6). What an approver
    # approves must be what gets planned, so the number here comes from `order/latest.py`, the
    # one function the plan and the readiness report also call, over the knowledge being
    # approved. The model's own number stays in the extraction beside the page for the reader
    # who wants to compare.
    from ..order.latest import Basis, fact_recommendation

    hop_versions: tuple[str, ...] = ()
    for hop in hops:
        for version in (hop.current_version, hop.target_version):
            if version not in hop_versions:
                hop_versions = (*hop_versions, version)
    # A REJECTED CANDIDATE CARRIES NO KNOWLEDGE, and the page for it still has to render: the
    # approver reads why it was refused. Nothing will be planned from it, so there is nothing
    # for the policy to say; the assessment's own fields stand, as they did before the policy
    # existed. (If the judge crashes on a component, it must appear as rejected rather than
    # vanish from the approval page.)
    if proposed is None:
        recommended_version = a("recommended_version")
        recommended_series = a("recommended_series")
        action = a("action", stringify=True)
        covers_target = a("covers_target_kubernetes")
    else:
        recommendation = fact_recommendation(proposed, installed_version, hop_versions)
        action = {
            Basis.COVERED: "UPGRADE",
            Basis.LATEST_UNVERIFIED: "LATEST_UNVERIFIED",
            Basis.KEEP: "KEEP",
        }[recommendation.basis]
        covers_target = {
            Basis.COVERED: True,
            Basis.LATEST_UNVERIFIED: None,
            Basis.KEEP: a("covers_target_kubernetes"),
        }[recommendation.basis]
        exact = recommendation.version and _EXACT_RELEASE.match(recommendation.version)
        recommended_version = recommendation.version if exact else None
        recommended_series = (
            recommendation.version
            if recommendation.version and not exact
            else a("recommended_series")
        )

    return ApprovalReport(
        component_id=candidate.component_id,
        version_series=candidate.version_series,
        installed_version=installed_version,
        cluster=cluster,
        target_kubernetes_version=hops[-1].target_version if hops else None,
        candidate_digest=candidate.digest(),
        evidence_digest=candidate.receipt.evidence_digest,
        # The assessment, flattened for display. Shown beside the windows so an approver
        # reads the recommendation and the evidence for it on one page.
        recommended_version=recommended_version,
        recommended_series=recommended_series,
        recommended_chart_version=a("recommended_chart_version"),
        chart_repository=chart_summary(proposed)["chart_repository"],
        action=action,
        installed_version_status=a("installed_version_status", stringify=True),
        upgrade_ordering=a("upgrade_ordering", stringify=True),
        covers_current_kubernetes=a("covers_current_kubernetes"),
        covers_target_kubernetes=covers_target,
        evidence_tier=(a("evidence_tier", stringify=True) or candidate.receipt.evidence_tier),
        proposed_confidence=(
            a("proposed_confidence", stringify=True)
            or _as_text(candidate.receipt.proposed_confidence)
        ),
        machine_confidence=_as_text(candidate.receipt.machine_confidence),
        confidence_rationale=a("confidence_rationale"),
        # The model's own list when it gave one -- its `used_for` is the richer text -- and
        # the list derived from the quotes when it did not. Never empty while there are quotes.
        cited_sources=tuple(
            CitedSourceRow(url=c.url, revision=c.revision, used_for=c.used_for)
            for c in (a("cited_sources") or ())
        )
        or cited_sources_from_claims(quoted_claims),
        caveats=_observed_window_caveat(proposed) + tuple(a("caveats") or ()),
        not_found=tuple(a("not_found") or ()),
        researched_at=researched_at,
        advisory=advisory,
        decision=candidate.receipt.decision,
        confidence=candidate.receipt.confidence,
        change_direction=candidate.receipt.change_direction,
        values_verified=candidate.receipt.values_verified,
        values_total=candidate.receipt.values_total,
        signals=tuple(
            FiredSignal(name=str(s.signal), fired=s.passed, detail=s.detail)
            for s in candidate.receipt.signals
        ),
        claims=quoted_claims,
        contradictions=tuple(
            f"{c.field}: {c.left_value!r} ({c.left_citation.url}) vs "
            f"{c.right_value!r} ({c.right_citation.url})"
            for c in candidate.receipt.contradictions
        ),
        tested_windows=candidate.receipt.tested_windows,
        current_windows=_windows(current, {}),
        proposed_windows=_windows(proposed, proposed_bases),
        current_revision=current.knowledge_base_revision if current else None,
        proposed_revision=proposed.knowledge_base_revision if proposed else None,
        verdict_deltas=verdict_delta(
            candidate.component_id,
            installed_version,
            candidate.version_series,
            current,
            proposed,
            hops,
        ),
    )


def quoted_claims_for(extraction, pages) -> tuple[QuotedClaim, ...]:
    """Every proposed value, with its quote, its pinned source, and how both checked out.

    Derived here rather than returned from `judge()` so the judging signature stays about
    judging. It re-runs the same `verify.py` functions over the same inputs, which is safe
    because they are pure -- and it is per-claim on purpose: an approver told "this fact is
    LOW" cannot tell WHICH value failed, and that is the thing they need in order to decide.
    """
    from .authority import classify_source
    from .boundary import BoundKind
    from .verify import chart_values, check_quote, literal_in_quote, value_in_quote

    rows: list[QuotedClaim] = []
    # The chart repository vouches for the chart claims and nothing else -- the same
    # widening `judge._assemble` applies, so the authority shown here is the one recorded.
    chart_documentation = extraction.official_documentation + (
        (extraction.chart.repository_url,) if extraction.chart is not None else ()
    )

    def add(
        field: str,
        value: str,
        quote: str,
        source,
        *,
        literal: bool = False,
        documentation: tuple[str, ...] | None = None,
    ) -> None:
        qc = check_quote(quote, source.url, source.revision, pages)
        authority = classify_source(
            source.url,
            source.revision,
            quote,
            canonical_repository=extraction.canonical_repository or None,
            official_documentation=(
                extraction.official_documentation if documentation is None else documentation
            ),
            pages=pages,
        )
        in_quote = literal_in_quote(value, quote) if literal else value_in_quote(value, quote)
        rows.append(
            QuotedClaim(
                field=field,
                value=value,
                quote=quote,
                source_url=source.url,
                source_revision=source.revision,
                quote_verdict=qc.verdict,
                verified=qc.verified and in_quote,
                source_authority=str(authority),
            )
        )

    for statement in extraction.statements:
        if statement.kind is BoundKind.UNSTATED:
            continue
        if statement.stated_minimum is not None:
            add(
                f"minimum for {statement.version_spec} ({statement.kind})",
                statement.stated_minimum,
                statement.quote,
                statement.source,
            )
        if statement.stated_maximum_inclusive is not None:
            add(
                f"stated maximum for {statement.version_spec} ({statement.kind})",
                statement.stated_maximum_inclusive,
                statement.quote,
                statement.source,
            )
        for minor in statement.stated_minors:
            add(
                f"listed minor for {statement.version_spec}",
                minor,
                statement.quote,
                statement.source,
            )

    for claim in extraction.claims:
        add(claim.field, claim.value, claim.quote, claim.source)

    for release in extraction.releases:
        add("release", release.version, release.quote, release.source)

    # The chart repository the upgrade lane would `helm repo add`, shown with the same
    # verdict as every other value so an approver can see whether it verified.
    for field, value, carrier in chart_values(extraction):
        add(
            field,
            value,
            carrier.quote,
            carrier.source,
            literal=True,
            documentation=chart_documentation,
        )

    return tuple(rows)


def bases_for(statements) -> dict[str, str]:
    """Spec -> the sentence explaining where its ceiling came from, or why there is none.

    Straight from `bounds.derive_windows`, so the report shows the same reasoning the code
    used rather than a paraphrase of it.
    """
    from .bounds import derive_windows

    return {d.spec: d.basis for d in derive_windows(tuple(statements)) if d.storable}


# Signal names in the order an approver should read them: the two that can disqualify a
# fact first, then the ones that grade it.
SIGNAL_READING_ORDER = (
    SignalName.QUOTE_MATCHES_SOURCE,
    SignalName.VALUES_TRACE_TO_QUOTES,
    SignalName.SUPPORT_WINDOW_DERIVED,
    SignalName.SHAPE_SANITY,
    SignalName.FIRST_PARTY_SOURCE,
    SignalName.CORROBORATION,
)


def signals_in_reading_order(
    report: ApprovalReport,
) -> tuple[FiredSignal, ...]:
    """Signals ordered for a reader, computed here rather than sorted in a template."""
    order = {str(name): i for i, name in enumerate(SIGNAL_READING_ORDER)}
    return tuple(sorted(report.signals, key=lambda s: order.get(s.name, len(order))))
