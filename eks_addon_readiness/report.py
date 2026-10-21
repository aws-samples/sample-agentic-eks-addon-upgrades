"""The Stage 2 output document: a self-describing, verifiable inventory report.

Stage 2 is a pure function of a snapshot, so a stored inventory is a derived
artifact. That raises the obvious objection: why store it at all, when it can be
recomputed?

Because recomputation only reproduces the original answer if **the rules have not
changed**, and they will change as later stages are built. A stored report that
does not say which rules produced it is not evidence of anything — months later
you could not tell whether a difference came from the cluster or from the code.

So a report carries two digests:

    snapshot_digest   the cluster state it was derived from
    ruleset_digest    the rules that derived it

With both, a stored report can be *verified* rather than trusted: re-run the same
rules over the same snapshot and the document must come out byte-identical. That
is what `eks-addon-readiness verify` does, and it is the difference between
claiming reproducibility and demonstrating it.

**The report deliberately carries no timestamp and no run identifier.** Its
content is a function of the snapshot and the rules, nothing else. Adding a clock
would make two identical assessments produce different documents and destroy the
property the digests exist to prove. When the cluster was observed is recorded —
as `snapshot_captured_at` — because that is the time that carries meaning.
"""

from __future__ import annotations

import hashlib
from functools import cache
from typing import Literal

from pydantic import Field

from .fingerprint import ruleset_digest as _rule_source_digest
from .inventory import ComponentRecord, resolve_inventory
from .models import ClusterReference, Confidence, ContractModel
from .snapshot import ClusterSnapshot

# The modules whose contents define the rules. Listed explicitly and in a fixed
# order so the digest does not depend on filesystem enumeration order, and so
# adding a module to the package is a deliberate act rather than a silent one.
RULESET_MODULES: tuple[str, ...] = (
    "versions",
    "identity",
    "confidence",
    "classify",
    "resolve",
)


@cache
def ruleset_digest() -> str:
    """Digest of the Stage 2 rule modules' source.

    Deliberately conservative: the raw source text is hashed, so a comment-only
    edit changes the digest. Erring this way is safe. A spurious "the rules
    changed" prompts a re-read; a missed one would let a changed rule masquerade
    as the original, which is the failure this digest exists to prevent.

    Raises `fingerprint.RulesetUnavailable` when a module's source cannot be read,
    rather than substituting an `UNAVAILABLE` placeholder. A document carrying
    a placeholder where its ruleset digest should be is a document that cannot
    prove anything, and it would pass every check downstream of here.
    """
    return _rule_source_digest(f"{__package__}.inventory", RULESET_MODULES)


class SignalReport(ContractModel):
    """One piece of reasoning behind a rating, and the ceiling it imposed."""

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    caps_at: Confidence
    detail: str = Field(min_length=1)


class ComponentReport(ContractModel):
    """One component, with the evidence behind every claim made about it.

    Every assertion is paired with its provenance: `identity_detail` for the
    identity, `version_source` for the version, `signals` for the confidence, and
    `classification_reason` for the bucket. A claim a reader cannot trace is a
    claim they cannot act on.
    """

    identity: str | None = None
    identity_source: str = Field(min_length=1)
    identity_detail: str = Field(min_length=1)
    classification: str = Field(min_length=1)
    classification_reason: str = Field(min_length=1)
    classification_provisional: bool = False
    # How strongly this should be checked. Never an exclusion: every component in
    # the report is assessed, and priority only orders the work.
    assessment_priority: str = Field(min_length=1)
    platform_evidence: tuple[str, ...] = ()
    version: str | None = None
    version_source: str = Field(min_length=1)
    confidence: Confidence
    signals: tuple[SignalReport, ...] = ()
    workload_ref: str = Field(min_length=1)
    namespace: str | None = None
    install_method: str = Field(min_length=1)
    replicas: int | None = None
    ready_replicas: int | None = None
    container_images: tuple[str, ...] = ()
    labels: dict[str, str] = Field(default_factory=dict)
    # Operational state, kept separate from `confidence`. Health answers "is this
    # running properly", confidence answers "how sure are we of the version".
    addon_status: str | None = None
    health_issue_codes: tuple[str, ...] = ()


class InventoryReport(ContractModel):
    """The Stage 2 output document.

    Contains no clock and no run identifier by design; see the module docstring.
    Two assessments of the same snapshot under the same rules produce byte-
    identical documents, so `digest()` is stable and can be used to deduplicate,
    to cite, and to verify.
    """

    schema_version: Literal["1.0"] = "1.0"
    stage: Literal["2"] = "2"
    cluster: ClusterReference
    kubernetes_version: str = Field(pattern=r"^\d+\.\d+$")
    # What this was derived from. Both are required to verify the document.
    snapshot_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    snapshot_captured_at: str = Field(min_length=1)
    ruleset_digest: str = Field(min_length=1)
    components: tuple[ComponentReport, ...] = ()

    def digest(self) -> str:
        """Content digest of the whole document."""
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    def at_priority(self, priority: str) -> tuple[ComponentReport, ...]:
        """Components at one assessment priority."""
        return tuple(c for c in self.components if c.assessment_priority == priority)

    @property
    def unresolved(self) -> tuple[ComponentReport, ...]:
        """Components with no usable version, excluding the low-priority tail.

        A workload with no platform signal and no version is usually an
        application that simply does not publish one, so counting it here would
        bury the cases that matter. It is still in `components` and still
        assessed — it just does not raise an alarm on its own.
        """
        return tuple(
            c
            for c in self.components
            if c.confidence is Confidence.UNRESOLVED and c.assessment_priority != "low"
        )

    @property
    def unhealthy(self) -> tuple[ComponentReport, ...]:
        """Components reporting a health issue.

        Reported separately from confidence: these versions are known, the
        components just are not running properly. That is an upgrade-readiness
        input for a later stage.
        """
        return tuple(c for c in self.components if c.health_issue_codes)

    @property
    def provisional(self) -> tuple[ComponentReport, ...]:
        """Components whose classification rests on incomplete evidence."""
        return tuple(c for c in self.components if c.classification_provisional)

    def counts(self) -> dict[str, int]:
        """Headline numbers, for a summary line or a dashboard."""
        by_confidence: dict[str, int] = {}
        for component in self.components:
            key = str(component.confidence)
            by_confidence[key] = by_confidence.get(key, 0) + 1
        return {
            "components": len(self.components),
            # Every component is assessed; these three only order the work.
            "priority_high": len(self.at_priority("high")),
            "priority_medium": len(self.at_priority("medium")),
            "priority_low": len(self.at_priority("low")),
            "unresolved": len(self.unresolved),
            "provisional": len(self.provisional),
            "unhealthy": len(self.unhealthy),
            **{f"confidence_{k.lower()}": v for k, v in sorted(by_confidence.items())},
        }


def _component(record: ComponentRecord) -> ComponentReport:
    return ComponentReport(
        identity=record.identity,
        identity_source=str(record.identity_source),
        identity_detail=record.identity_detail,
        classification=str(record.classification),
        classification_reason=record.classification_reason,
        classification_provisional=record.classification_provisional,
        assessment_priority=str(record.assessment_priority),
        platform_evidence=record.platform_evidence,
        version=record.version,
        version_source=str(record.version_source),
        confidence=record.confidence,
        signals=tuple(
            SignalReport(code=s.code, caps_at=s.caps_at, detail=s.detail)
            for s in record.signals
        ),
        workload_ref=record.workload_ref,
        namespace=record.namespace,
        install_method=record.install_method,
        replicas=record.replicas,
        ready_replicas=record.ready_replicas,
        container_images=record.container_images,
        labels=record.labels,
        addon_status=record.addon_status,
        health_issue_codes=record.health_issue_codes,
    )


def build_report(snapshot: ClusterSnapshot, snapshot_digest: str) -> InventoryReport:
    """Assess a snapshot and wrap the result in a verifiable document.

    `snapshot_digest` is passed in rather than recomputed so the report cites the
    digest the caller actually stored, which is what a later stage will look up.
    """
    return InventoryReport(
        cluster=snapshot.cluster,
        kubernetes_version=snapshot.kubernetes_version,
        snapshot_digest=snapshot_digest,
        snapshot_captured_at=snapshot.captured_at.isoformat(),
        ruleset_digest=ruleset_digest(),
        components=tuple(_component(r) for r in resolve_inventory(snapshot)),
    )
