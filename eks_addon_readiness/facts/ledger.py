"""The Stage 3 output document: what is known, and what is merely unknown.

Stage 3 answers one question per component — *do we hold a compatibility fact about
this?* It deliberately does **not** answer "will this survive the upgrade". That is a
verdict, it needs a target Kubernetes version, and it belongs to Stage 4. Keeping the
line here means a missing fact is reported as a missing fact rather than quietly
becoming a pessimistic verdict, which is the failure mode this stage exists to prevent.

Consequently Stage 3 takes **no target version**. It is a pure function of the Stage 2
report, the Stage 1 snapshot's insights, and the fact store.

Like `InventoryReport`, this document carries no clock and no run identifier, so two
runs over the same inputs are byte-identical and `digest()` is citable.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from ..knowledge import VersionRange
from ..models import ClusterReference, ContractModel


class FactStatus(StrEnum):
    """Whether a compatibility fact exists for one installed component.

    Ordered by strength. When more than one source answers, the strongest is
    reported, and the per-source fields stay populated so nothing is hidden by the
    headline.
    """

    # AWS assessed this component against the next minor on this actual cluster.
    ANSWERED_BY_CLUSTER = "ANSWERED_BY_CLUSTER"
    # The fact store holds a rule covering the installed version's series.
    FACT_AVAILABLE = "FACT_AVAILABLE"
    # Identity and a usable version are known, and no source has a fact. This is the
    # researchable set: a name and a version is exactly what a research task needs.
    FACT_MISSING = "FACT_MISSING"
    # No usable version, or no identity. Research cannot help, because there is
    # nothing to look up. Distinguishing this from FACT_MISSING is the point:
    # conflating them would send a researcher after a question nobody can ask.
    NOT_ASKABLE = "NOT_ASKABLE"


class GapDetail(StrEnum):
    """Machine-readable reason behind a `FactStatus`.

    Free-text `reason` explains; this classifies. A reader filtering for "which
    components need a knowledge-base entry written" needs `NO_ENTRY`, not prose.
    """

    INSIGHT_MATCH = "INSIGHT_MATCH"
    RULE_MATCH = "RULE_MATCH"
    # An entry exists for this component but holds no rule for the installed series.
    # Distinct from NO_ENTRY because the work differs: extend an entry, versus
    # research and author one.
    NO_RULE_FOR_SERIES = "NO_RULE_FOR_SERIES"
    NO_ENTRY = "NO_ENTRY"
    # The store directory was absent, so every lookup is unknown rather than absent.
    STORE_UNREADABLE = "STORE_UNREADABLE"
    NO_USABLE_VERSION = "NO_USABLE_VERSION"
    NO_IDENTITY = "NO_IDENTITY"


class GapEntry(ContractModel):
    """One component, and the provenance of whatever is known about its compatibility.

    Both source blocks are populated independently. When AWS and the fact store both
    answer, `status` reports `ANSWERED_BY_CLUSTER` but `fact_*` stays filled, because
    the two sources have different reach: an insight covers only the next minor on
    this cluster, while a stored rule gives a range and so can serve multi-hop
    planning. Collapsing them into one field would discard that.
    """

    # Carried from the Stage 2 report so an entry is readable on its own.
    workload_ref: str = Field(min_length=1)
    identity: str | None = None
    version: str | None = None
    # Derived from `version`. Null whenever no series could be determined, which is
    # itself the reason for NOT_ASKABLE.
    version_series: str | None = Field(default=None, pattern=r"^\d+\.\d+\.x$")
    # Preserved so the researchable set can be worked in the order Stage 2 assigned.
    assessment_priority: str = Field(min_length=1)

    status: FactStatus
    detail_code: GapDetail
    reason: str = Field(min_length=1)

    # What AWS said, when it said anything.
    insight_id: str | None = None
    insight_kubernetes_version: str | None = None
    insight_compatible_versions: tuple[str, ...] = ()
    # Whether the installed version appears in the list above, compared on normalized
    # forms so a `v` prefix or eksbuild suffix cannot cause a false negative.
    #
    # A set-membership fact about captured evidence, **not** a compatibility verdict.
    # It says "AWS's list for the next minor does or does not contain the version we
    # found"; it says nothing about whether an upgrade is safe, which needs every
    # component, every hop and their dependencies.
    #
    # Computed here because leaving it out does not prevent the comparison — it only
    # moves it into model prose, where nothing can check it. Observed on 2026-09-07:
    # asked what was needed to reach 1.36, the agent rendered this as a bare
    # "coredns 1.14.3-eksbuild.16 — compatible", a string comparison it performed
    # itself and no digest covers. Anything a rule can decide belongs in a rule.
    insight_lists_installed_version: bool | None = None

    # What the store held, when it held anything.
    fact_knowledge_base_revision: str | None = None
    fact_series: str | None = None
    fact_kubernetes_versions: VersionRange | None = None

    @model_validator(mode="after")
    def answered_statuses_cite_a_source(self) -> GapEntry:
        """An answer without provenance is an assertion, so refuse to record one."""
        if self.status is FactStatus.ANSWERED_BY_CLUSTER and not self.insight_id:
            raise ValueError("ANSWERED_BY_CLUSTER requires an insight_id")
        if self.status is FactStatus.FACT_AVAILABLE and not (
            self.fact_knowledge_base_revision and self.fact_kubernetes_versions
        ):
            raise ValueError(
                "FACT_AVAILABLE requires a knowledge base revision and a version range"
            )
        return self

    @property
    def researchable(self) -> bool:
        """Whether a research task could close this gap.

        Derived rather than stored: a stored copy of a rule this simple only creates
        somewhere for the two to disagree.
        """
        return self.status is FactStatus.FACT_MISSING


class GapLedger(ContractModel):
    """The Stage 3 document: one entry per component, with the full evidence chain.

    Cites four digests, because four independent things can change the result and a
    reader must be able to tell which did:

    - `report_digest`   the Stage 2 inventory this was derived from
    - `snapshot_digest` the Stage 1 evidence, carried through so the chain is walkable
    - `store_digest`    the state of the fact store
    - `ruleset_digest`  the Stage 3 rules

    Without `store_digest` in particular, gaining a fact and losing a component would
    be indistinguishable from each other.
    """

    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["3"] = "3"
    cluster: ClusterReference
    kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")

    report_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    snapshot_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    store_digest: str = Field(min_length=1)
    ruleset_digest: str = Field(min_length=1)

    entries: tuple[GapEntry, ...] = ()

    # Entries the store could not parse. Surfaced at the top level because a
    # malformed file makes components silently look like FACT_MISSING; a reader has
    # to be able to see that the store was degraded rather than merely incomplete.
    store_errors: tuple[str, ...] = ()

    def digest(self) -> str:
        """Content digest of the whole document."""
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    def with_status(self, status: FactStatus) -> tuple[GapEntry, ...]:
        return tuple(e for e in self.entries if e.status is status)

    @property
    def researchable(self) -> tuple[GapEntry, ...]:
        """The gaps a research stage can actually close, highest priority first."""
        order = {"high": 0, "medium": 1, "low": 2}
        return tuple(
            sorted(
                (e for e in self.entries if e.researchable),
                key=lambda e: (order.get(e.assessment_priority, 3), e.workload_ref),
            )
        )

    @property
    def answered(self) -> tuple[GapEntry, ...]:
        """Entries where some source supplied a compatibility statement."""
        return tuple(
            e
            for e in self.entries
            if e.status
            in (FactStatus.ANSWERED_BY_CLUSTER, FactStatus.FACT_AVAILABLE)
        )

    def counts(self) -> dict[str, int]:
        """Headline numbers, for a summary line or a dashboard."""
        by_status: dict[str, int] = {}
        for entry in self.entries:
            key = str(entry.status)
            by_status[key] = by_status.get(key, 0) + 1
        return {
            "components": len(self.entries),
            "answered": len(self.answered),
            "researchable": len(self.researchable),
            # Coverage is reported as a ratio of counts rather than a percentage:
            # 4/12 is honest, and 33% invites being quoted without its denominator.
            **{f"status_{k.lower()}": v for k, v in sorted(by_status.items())},
        }
