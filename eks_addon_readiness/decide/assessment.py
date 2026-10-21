"""The Stage 4 output document.

Deliberately **not** `models.AnalysisReport`, which was drafted before the
reproducibility discipline that Stages 2 and 3 settled on. `AnalysisReport` carries
`run_id: UUID` and `created_at: datetime`, either of which would change the digest on
every run and end the replay guarantee at exactly the stage where a verdict is formed.
It also holds a single `inventory: ComponentInventory`, so it describes one component
rather than a cluster.

The inner types it was built from — `KubernetesHop`, `CompatibilityVerdict`, `Finding`,
`Severity` — are reused unchanged. It is the envelope that did not fit.

Like `InventoryReport` and `GapLedger`: no clock, no run identifier, every upstream
digest cited, so two runs over the same inputs are byte-identical.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import Field, model_validator

from ..models import (
    ClusterReference,
    CompatibilityVerdict,
    ContractModel,
    Finding,
    KubernetesHop,
)


class HopDecision(ContractModel):
    """One component measured against one single-minor hop."""

    hop: KubernetesHop
    verdict: CompatibilityVerdict
    # Component series permitted at this hop's target. For a verdict sourced from an EKS
    # Insight these are AWS's version strings rather than series, because that is what
    # AWS published — the distinction is recorded in `decided_from`.
    valid_targets: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    reason: str = Field(min_length=1)
    # `stored-fact`, `eks-insight`, or `none`. Names the evidence class behind the
    # verdict so a reader can weigh a single-hop AWS answer against a stored range.
    decided_from: str = Field(min_length=1)

    @model_validator(mode="after")
    def unresolved_offers_no_targets(self) -> HopDecision:
        """An unresolved verdict must not smuggle in a recommendation.

        If we cannot decide whether the component may stay, we certainly cannot say what
        to move it to, and a populated list would read as advice.
        """
        if self.verdict is CompatibilityVerdict.UNRESOLVED and self.valid_targets:
            raise ValueError("an UNRESOLVED verdict must not carry valid_targets")
        return self


class ComponentDecision(ContractModel):
    """One component across every hop to the target."""

    workload_ref: str = Field(min_length=1)
    identity: str | None = None
    installed_version: str | None = None
    installed_series: str | None = Field(default=None, pattern=r"^\d+\.\d+\.x$")
    assessment_priority: str = Field(min_length=1)
    # Carried from Stage 3 so a reader need not hold both documents open.
    fact_status: str = Field(min_length=1)
    knowledge_base_revision: str | None = None
    hops: tuple[HopDecision, ...] = Field(min_length=1)

    @property
    def verdict(self) -> CompatibilityVerdict:
        """The governing verdict across all hops: the worst one encountered.

        A component that is fine at 1.36 and stranded at 1.37 is not fine. Reporting the
        best hop, or only the final one, would hide the hop that actually blocks.

        `UNRESOLVED` deliberately does **not** outrank `INCOMPATIBLE`. A known blocker on
        one hop is more actionable than an unknown on another, and burying it under
        "unresolved" would lose the one thing we do know.
        """
        order = {
            CompatibilityVerdict.INCOMPATIBLE: 0,
            CompatibilityVerdict.UNRESOLVED: 1,
            CompatibilityVerdict.REQUIRES_COMPONENT_UPGRADE: 2,
            CompatibilityVerdict.COMPATIBLE: 3,
        }
        return min((h.verdict for h in self.hops), key=lambda v: order[v])

    @property
    def blocking_hop(self) -> HopDecision | None:
        """The first hop carrying the governing verdict, or None when compatible."""
        if self.verdict is CompatibilityVerdict.COMPATIBLE:
            return None
        return next(h for h in self.hops if h.verdict is self.verdict)


class ReadinessAssessment(ContractModel):
    """The Stage 4 document: what may stay, what must move, what we cannot say.

    Cites five digests. Four are inherited so the chain back to the cluster reading stays
    walkable; the fifth is this stage's own rules. A reader who finds two differing
    assessments can tell which input moved.
    """

    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["4"] = "4"
    cluster: ClusterReference
    current_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    target_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    # The expanded single-minor sequence actually evaluated. Present explicitly because
    # "1.35 to 1.37" hides that two hops were checked, and a reader must see the
    # intermediate version a component has to survive.
    hops: tuple[KubernetesHop, ...] = Field(min_length=1)

    snapshot_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    report_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ledger_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    store_digest: str = Field(min_length=1)
    ruleset_digest: str = Field(min_length=1)

    decisions: tuple[ComponentDecision, ...] = ()

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    def with_verdict(self, verdict: CompatibilityVerdict) -> tuple[ComponentDecision, ...]:
        return tuple(d for d in self.decisions if d.verdict is verdict)

    @property
    def blockers(self) -> tuple[ComponentDecision, ...]:
        """Components that cannot reach the target by upgrading, highest priority first."""
        order = {"high": 0, "medium": 1, "low": 2}
        return tuple(
            sorted(
                self.with_verdict(CompatibilityVerdict.INCOMPATIBLE),
                key=lambda d: (order.get(d.assessment_priority, 3), d.workload_ref),
            )
        )

    @property
    def undecided(self) -> tuple[ComponentDecision, ...]:
        """Components no source could decide. Reported separately from blockers.

        Merging the two would let a gap in our knowledge masquerade as a finding about
        the cluster, which is the failure this whole pipeline is arranged to prevent.
        """
        return self.with_verdict(CompatibilityVerdict.UNRESOLVED)

    def counts(self) -> dict[str, int]:
        by_verdict: dict[str, int] = {}
        for decision in self.decisions:
            key = str(decision.verdict)
            by_verdict[key] = by_verdict.get(key, 0) + 1
        return {
            "components": len(self.decisions),
            "hops": len(self.hops),
            "blockers": len(self.blockers),
            "undecided": len(self.undecided),
            "findings": sum(len(h.findings) for d in self.decisions for h in d.hops),
            **{f"verdict_{k.lower()}": v for k, v in sorted(by_verdict.items())},
        }
