"""Deterministic compatibility facts for EKS-managed add-ons, from the AWS catalogue.

An EKS-managed add-on's compatibility is not something to research: AWS publishes it, in
full, via `describe-addon-versions`. Stage 1 captures that catalogue into the snapshot
(`ClusterSnapshot.eks_addon_catalogue`); this module turns it into the same
`ComponentKnowledge` shape a researched fact has, so a managed add-on flows through the
*identical* decide path (`store.find(identity)`) and its verdict is driven by AWS's own
data across every Kubernetes version AWS publishes — no model, no web fetch, no research.

Two design choices worth stating:

- **Built fresh from the snapshot, never persisted.** The catalogue is account/region/
  time-specific and AWS revises it; a copy written to the fact store would go stale. So
  `CatalogueFactStore` wraps the real store and answers `find()` for managed add-ons from
  the snapshot in memory, delegating every other component to the wrapped store. Replay
  stays honest because the catalogue lives in the snapshot, which is the frozen input.

- **A version AWS does not publish yields no rule for that hop.** The catalogue tops out
  at the newest Kubernetes minor AWS offers. A hop into an unreleased minor (e.g. 1.37
  before AWS ships it) is simply not covered by any rule, so the decide path returns
  UNRESOLVED for that hop — the honest answer, since no source on earth has that data yet.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..knowledge import (
    CompatibilityRule,
    ComponentKnowledge,
    ReleaseRecord,
    UpgradeStrategy,
    VerificationCheck,
    VersionRange,
)
from ..models import Citation, Confidence
from ..snapshot import ClusterSnapshot, EksAddonVersionCompatibility
from .series import exact_version, version_series
from .store import FactStore

# The one documentation URL cited for a catalogue-derived fact. It is the AWS page that
# describes the very API this fact is built from, so a reader can confirm the source.
_CATALOGUE_DOC_URL = (
    "https://docs.aws.amazon.com/eks/latest/userguide/addon-compat.html"
)
_CATALOGUE_TITLE = "EKS describe-addon-versions (AWS control-plane catalogue)"


def _minor_tuple(minor: str) -> tuple[int, int] | None:
    parts = minor.split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def _next_minor(minor: str) -> str | None:
    """`1.36` -> `1.37`. The exclusive ceiling for a range that ends at `minor`."""
    t = _minor_tuple(minor)
    if t is None:
        return None
    return f"{t[0]}.{t[1] + 1}"


def _contiguous_ranges(minors: set[str]) -> list[VersionRange]:
    """Collapse a set of supported Kubernetes minors into contiguous half-open ranges.

    `{1.35, 1.36}` -> one range `[1.35, 1.37)`. A gap (say support for 1.33 and 1.35 but
    not 1.34) yields two ranges rather than one that falsely spans the hole. Malformed
    minors are dropped. The ceiling is always the exclusive next-minor of the block's top,
    because the catalogue lists discrete minors, not open-ended support — AWS has not
    stated the add-on works on a version it does not list.
    """
    parsed = sorted(t for m in minors if (t := _minor_tuple(m)) is not None)
    if not parsed:
        return []
    ranges: list[VersionRange] = []
    block_start = parsed[0]
    prev = parsed[0]
    for cur in parsed[1:]:
        # Contiguous only within the same major and consecutive minor.
        if cur[0] == prev[0] and cur[1] == prev[1] + 1:
            prev = cur
            continue
        ranges.append(
            VersionRange(
                minimum_inclusive=f"{block_start[0]}.{block_start[1]}",
                maximum_exclusive=f"{prev[0]}.{prev[1] + 1}",
            )
        )
        block_start = cur
        prev = cur
    ranges.append(
        VersionRange(
            minimum_inclusive=f"{block_start[0]}.{block_start[1]}",
            maximum_exclusive=f"{prev[0]}.{prev[1] + 1}",
        )
    )
    return ranges


def _citation() -> Citation:
    return Citation(
        url=_CATALOGUE_DOC_URL,
        title=_CATALOGUE_TITLE,
        source_revision="describe-addon-versions",
        retrieved_at=datetime.now(UTC),
    )


def build_addon_knowledge(
    addon_name: str, entries: list[EksAddonVersionCompatibility]
) -> ComponentKnowledge | None:
    """One `ComponentKnowledge` for a managed add-on, from its catalogue entries.

    Aggregates every published build of the add-on by version *series* (`1.14.x`), unions
    the Kubernetes minors each build supports, and emits a `CompatibilityRule` per
    contiguous block. Every concrete build version becomes a `ReleaseRecord`, so Stage 5
    can name a concrete patch to install. Confidence is HIGH: this is AWS's authoritative
    control-plane catalogue, a single but definitive source. Returns None when no usable
    rule can be formed (no entries, or none reduce to a series with supported minors).
    """
    if not entries:
        return None

    citation = _citation()
    series_minors: dict[str, set[str]] = {}
    releases: dict[str, None] = {}
    for e in entries:
        series = version_series(e.addon_version)
        if series is None:
            continue
        series_minors.setdefault(series, set()).update(
            e.compatible_kubernetes_versions
        )
        exact = exact_version(e.addon_version)
        if exact:
            releases.setdefault(exact, None)

    rules: list[CompatibilityRule] = []
    for series, minors in sorted(series_minors.items()):
        for rng in _contiguous_ranges(minors):
            rules.append(
                CompatibilityRule(
                    component_version_series=series,
                    kubernetes_versions=rng,
                    served_api_versions=(),
                    evidence=(citation,),
                )
            )
    if not rules:
        return None

    release_records = tuple(
        ReleaseRecord(
            version=v,
            released_at=datetime(2020, 1, 1, tzinfo=UTC),
            source_revision="describe-addon-versions",
            evidence=(citation,),
        )
        for v in sorted(releases)
    ) or (
        ReleaseRecord(
            version="0.0.0",
            released_at=datetime(2020, 1, 1, tzinfo=UTC),
            source_revision="describe-addon-versions",
            evidence=(citation,),
        ),
    )

    return ComponentKnowledge(
        knowledge_base_revision=f"{addon_name}-eks-catalogue-"
        f"{datetime.now(UTC).date().isoformat()}",
        canonical_id=addon_name,
        canonical_repository="https://github.com/aws/eks-charts",
        official_documentation=(_CATALOGUE_DOC_URL,),
        compatibility_rules=tuple(rules),
        releases=release_records,
        verification_checks=(
            VerificationCheck(
                check_id="addon-active-after-upgrade",
                description="The EKS-managed add-on reports ACTIVE after the upgrade.",
                required=True,
                evidence=(citation,),
            ),
        ),
        default_upgrade_strategy=UpgradeStrategy.SEQUENTIAL_MINOR,
        refreshed_at=datetime.now(UTC),
        evidence_confidence=Confidence.HIGH,
    )


def catalogue_knowledge(
    snapshot: ClusterSnapshot,
) -> dict[str, ComponentKnowledge]:
    """Every managed add-on's catalogue-derived knowledge, keyed by add-on name.

    Grouped from `snapshot.eks_addon_catalogue`. An add-on with no usable catalogue data
    is simply absent, so it falls back to whatever the wrapped store / insight provides.
    """
    grouped: dict[str, list[EksAddonVersionCompatibility]] = {}
    for entry in snapshot.eks_addon_catalogue:
        grouped.setdefault(entry.addon_name, []).append(entry)
    out: dict[str, ComponentKnowledge] = {}
    for addon_name, entries in grouped.items():
        knowledge = build_addon_knowledge(addon_name, entries)
        if knowledge is not None:
            out[addon_name] = knowledge
    return out


class CatalogueFactStore:
    """A `FactStore` that answers managed add-ons from the snapshot catalogue.

    Wraps the real store: `find(identity)` returns the catalogue-derived knowledge when
    the identity is a managed add-on present in the snapshot catalogue, and delegates to
    the wrapped store for every other component. `digest()` folds in the catalogue so the
    ledger/assessment digests change when the catalogue does — the same store-digest
    contract every consumer already checks. `write` is intentionally not exposed even if
    the wrapped store has it: a catalogue fact is derived, never persisted.
    """

    def __init__(self, wrapped: FactStore, snapshot: ClusterSnapshot) -> None:
        self._wrapped = wrapped
        self._catalogue = catalogue_knowledge(snapshot)

    def find(self, identity: str) -> ComponentKnowledge | None:
        key = (identity or "").strip()
        if key in self._catalogue:
            return self._catalogue[key]
        return self._wrapped.find(identity)

    def digest(self) -> str:
        import hashlib

        base = self._wrapped.digest()
        if not self._catalogue:
            return base
        h = hashlib.sha256()
        h.update(base.encode())
        for name in sorted(self._catalogue):
            h.update(name.encode())
            h.update(self._catalogue[name].canonical_json().encode())
        return "sha256:" + h.hexdigest()
