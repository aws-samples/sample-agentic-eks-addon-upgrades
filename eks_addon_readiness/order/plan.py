"""The Stage 5 output document: an ordered, interleaved upgrade sequence.

The shape GAP-2 needed and `HopAnalysis` could not express — a component may appear more
than once, and component steps interleave with cluster steps in a single ordered list.

Clock-free and digest-citable, like every stage since Stage 2. Cites six digests, adding
the Stage 4 assessment digest to the five it inherits, so a reader can walk the chain
from the plan back to the cluster reading.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from ..knowledge import Reversibility
from ..models import ClusterReference, ContractModel


class StepKind(StrEnum):
    CLUSTER_UPGRADE = "CLUSTER_UPGRADE"
    # A move the component MUST make to stay supported across a hop. Not doing it strands
    # the component on an unsupported version.
    COMPONENT_MOVE = "COMPONENT_MOVE"
    # An ADVISORY upgrade: the component is already compatible across the whole path, but
    # a newer supported version exists and is recommended. Skipping it is safe — the
    # component still rides the upgrade — so it is deliberately a distinct kind from a
    # forced COMPONENT_MOVE, and carries no reversibility requirement.
    COMPONENT_UPGRADE_RECOMMENDED = "COMPONENT_UPGRADE_RECOMMENDED"


class PlanStep(ContractModel):
    """One ordered action: advance the cluster one minor, or move one component.

    `order` is the position in the sequence, from 1. Two steps never share an order;
    where the evidence does not force one before another, the interleaver still assigns
    a deterministic order but records in `unordered_with` that the choice was arbitrary,
    so a reader is not misled into thinking a real constraint exists.
    """

    order: int = Field(ge=1)
    kind: StepKind
    description: str = Field(min_length=1)

    # CLUSTER_UPGRADE
    from_kubernetes: str | None = Field(default=None, pattern=r"^\d+\.\d+$")
    to_kubernetes: str | None = Field(default=None, pattern=r"^\d+\.\d+$")

    # COMPONENT_MOVE
    component: str | None = None
    # Version *specs* — series (`0.6.x`) or exact release (`3.2.0`), widened in Stage 6.5.
    from_series: str | None = Field(
        default=None, pattern=r"^(?:\d+\.\d+\.x|\d+\.\d+\.\d+)$"
    )
    to_series: str | None = Field(
        default=None, pattern=r"^(?:\d+\.\d+\.x|\d+\.\d+\.\d+)$"
    )
    # The concrete version to install (Stage 6.5). Carried through from the move: the
    # latest recorded patch in `to_series` for a series target, the exact release for a
    # release target, or None when no release is recorded in the series. `to_series` is
    # what is *compatible*; this is what to *install*.
    recommended_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    at_kubernetes: str | None = Field(default=None, pattern=r"^\d+\.\d+$")
    reversibility: Reversibility | None = None
    reversibility_reason: str | None = None

    # Orders this step shares no real constraint with. Advisory: names where the
    # sequence is one valid choice among several, never a claim of dependency.
    unordered_with: tuple[int, ...] = ()

    @model_validator(mode="after")
    def fields_match_kind(self) -> PlanStep:
        if self.kind is StepKind.CLUSTER_UPGRADE:
            if not (self.from_kubernetes and self.to_kubernetes):
                raise ValueError("a CLUSTER_UPGRADE step needs from/to Kubernetes versions")
        elif self.kind is StepKind.COMPONENT_UPGRADE_RECOMMENDED:
            # Advisory: needs a component, a target and where it applies, but no
            # reversibility — it is optional, so the reversibility gate does not apply.
            if not (self.component and self.to_series and self.at_kubernetes):
                raise ValueError(
                    "a COMPONENT_UPGRADE_RECOMMENDED step needs a component, a "
                    "to_series and at_kubernetes"
                )
        else:
            if not (self.component and self.to_series and self.at_kubernetes):
                raise ValueError(
                    "a COMPONENT_MOVE step needs a component, a to_series and at_kubernetes"
                )
            if self.reversibility is None:
                raise ValueError("a COMPONENT_MOVE step must carry a reversibility")
        return self


class UnsequenceableComponent(ContractModel):
    """A component that could not be placed in the sequence, and why.

    The honest counterpart to the ordered steps. A component Stage 4 left UNRESOLVED, or
    one for which no feasible set of moves exists, appears here by name rather than being
    dropped — an omission would read as "nothing to do".
    """

    workload_ref: str = Field(min_length=1)
    identity: str | None = None
    verdict: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class UpgradePlan(ContractModel):
    """The Stage 5 document: the ordered path, and everything that could not be ordered."""

    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["5"] = "5"
    cluster: ClusterReference
    current_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    target_kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")

    # False when a component the user needs has no feasible move path. The steps that
    # are known are still reported, but the plan does not claim to reach the target.
    feasible: bool = True

    steps: tuple[PlanStep, ...] = ()
    unsequenceable: tuple[UnsequenceableComponent, ...] = ()

    snapshot_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    report_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    ledger_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    assessment_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    store_digest: str = Field(min_length=1)
    ruleset_digest: str = Field(min_length=1)

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    @property
    def component_moves(self) -> tuple[PlanStep, ...]:
        return tuple(s for s in self.steps if s.kind is StepKind.COMPONENT_MOVE)

    @property
    def recommended_upgrades(self) -> tuple[PlanStep, ...]:
        """Advisory upgrade steps — a newer supported version exists, but is optional."""
        return tuple(
            s for s in self.steps if s.kind is StepKind.COMPONENT_UPGRADE_RECOMMENDED
        )

    @property
    def one_way_steps(self) -> tuple[PlanStep, ...]:
        """Moves that a version rollback will not undo. These warrant firmer approval."""
        return tuple(
            s for s in self.component_moves if s.reversibility is Reversibility.ONE_WAY
        )

    def counts(self) -> dict[str, int]:
        return {
            "steps": len(self.steps),
            "cluster_upgrades": sum(
                1 for s in self.steps if s.kind is StepKind.CLUSTER_UPGRADE
            ),
            "component_moves": len(self.component_moves),
            "recommended_upgrades": len(self.recommended_upgrades),
            "one_way_moves": len(self.one_way_steps),
            "unsequenceable": len(self.unsequenceable),
        }
