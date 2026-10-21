"""Assemble the Stage 4 assessment from a gap ledger, a fact store and a target.

Stage 4 is the first stage that accepts a target Kubernetes version, and therefore the
first permitted to turn a fact into a verdict. It reads the Stage 3 ledger for *what is
known* and the fact store for *what the knowledge says*, and it touches no cluster.
"""

from __future__ import annotations

from ..facts import FactStore, GapLedger
from ..models import CompatibilityVerdict
from .assessment import ComponentDecision, HopDecision, ReadinessAssessment
from .kubernetes import expand_hops
from .rules import verdict_for_hop
from .ruleset import ruleset_digest


class LedgerMismatch(ValueError):
    """Raised when the ledger was not built from the store now being used.

    The ledger records the store digest it saw. Deciding against a different store would
    produce a document whose five digests all look valid while two of them describe
    different knowledge.
    """


def _decide_component(gap, store: FactStore, hops) -> ComponentDecision:
    knowledge = store.find(gap.identity) if gap.identity else None
    decided_from = (
        "stored-fact"
        if knowledge is not None
        else ("eks-insight" if gap.insight_id else "none")
    )

    decisions: list[HopDecision] = []
    for index, hop in enumerate(hops):
        verdict, targets, findings, reason = verdict_for_hop(
            gap, knowledge, hop, is_first_hop=index == 0
        )
        # An insight that does not reach this hop decided nothing, so the source is
        # recorded as none rather than crediting AWS with an answer it did not give.
        source = decided_from
        if (
            decided_from == "eks-insight"
            and verdict is CompatibilityVerdict.UNRESOLVED
        ):
            source = "none"
        decisions.append(
            HopDecision(
                hop=hop,
                verdict=verdict,
                valid_targets=targets,
                findings=findings,
                reason=reason,
                decided_from=source,
            )
        )

    return ComponentDecision(
        workload_ref=gap.workload_ref,
        identity=gap.identity,
        installed_version=gap.version,
        installed_series=gap.version_series,
        assessment_priority=gap.assessment_priority,
        fact_status=str(gap.status),
        knowledge_base_revision=(
            knowledge.knowledge_base_revision if knowledge is not None else None
        ),
        hops=tuple(decisions),
    )


def build_assessment(
    ledger: GapLedger,
    store: FactStore,
    target_kubernetes_version: str,
) -> ReadinessAssessment:
    """Decide every component in the ledger against every hop to the target.

    The store is required to be the one the ledger was built from. Checked rather than
    trusted, for the same reason Stage 3 checks the report against the snapshot.
    """
    if store.digest() != ledger.store_digest:
        raise LedgerMismatch(
            "the ledger was built against a different fact store. Ledger cites "
            f"{ledger.store_digest}, store is {store.digest()}. Re-run gap detection "
            "before deciding."
        )

    hops = expand_hops(ledger.kubernetes_version, target_kubernetes_version)

    return ReadinessAssessment(
        cluster=ledger.cluster,
        current_kubernetes_version=ledger.kubernetes_version,
        target_kubernetes_version=target_kubernetes_version,
        hops=hops,
        snapshot_digest=ledger.snapshot_digest,
        report_digest=ledger.report_digest,
        ledger_digest=ledger.digest(),
        store_digest=ledger.store_digest,
        ruleset_digest=ruleset_digest(),
        decisions=tuple(
            _decide_component(gap, store, hops) for gap in ledger.entries
        ),
    )
