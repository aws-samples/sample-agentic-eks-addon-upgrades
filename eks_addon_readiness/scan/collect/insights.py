"""AWS's own upgrade-readiness insights, frozen into the snapshot.

Captured at scan time rather than queried at verdict time. An insight is a
computed opinion AWS recalculates, so re-fetching it during assessment would make
a verdict irreproducible.

Two calls per insight, because the list response omits
`category_specific_summary` and only the by-id form returns it. A failed detail
read degrades that one insight to its summary rather than discarding it — the
status and reason are still worth having without the compatible-version list.
"""

from __future__ import annotations

from typing import NamedTuple

from ...models import UnresolvedFinding
from ...snapshot import ClusterInsightSnapshot
from ..ports import ClusterReader
from .findings import gap
from .sanitize import insight_from_response, optional_str


class Insights(NamedTuple):
    """The insight slice of a snapshot, with the gaps collecting it left."""

    insights: tuple[ClusterInsightSnapshot, ...]
    gaps: tuple[UnresolvedFinding, ...]


def collect_insights(reader: ClusterReader) -> Insights:
    """List insights, then read each one in full."""
    gaps: list[UnresolvedFinding] = []
    listed, error = reader.list_insights()
    if error:
        gaps.append(
            gap(
                "CLUSTER_INSIGHTS_UNAVAILABLE",
                "AWS upgrade-readiness insights could not be listed.",
                error,
                ("eks:ListInsights via get_eks_insights",),
                (
                    "Confirm the identity holds AmazonEKSMCPReadOnlyAccess and that "
                    "get_eks_insights is in the reader's tool allowlist.",
                    "Without insights, add-on compatibility for EKS-managed add-ons "
                    "has no authoritative source and must not be assumed.",
                ),
            )
        )
        return Insights(insights=(), gaps=tuple(gaps))

    records: list[ClusterInsightSnapshot] = []
    for summary in listed:
        insight_id = optional_str(summary.get("id"))
        detail: dict | None = None
        if insight_id:
            detail, detail_error = reader.read_insight(insight_id)
            if detail_error:
                gaps.append(
                    gap(
                        "CLUSTER_INSIGHT_DETAIL_UNAVAILABLE",
                        f"Detail for insight {summary.get('name') or insight_id!r} "
                        "could not be read.",
                        detail_error,
                        (f"get_eks_insights detail for {insight_id}",),
                        (
                            "The insight's status is retained; its per-add-on "
                            "compatible-version list is not.",
                            "Do not treat an empty addon_compatible_versions as "
                            "meaning no versions are compatible.",
                        ),
                    )
                )
        record = insight_from_response(detail or summary)
        records.append(record)

        # Never silently drop data whose shape we have not modelled. See
        # `ClusterInsightSnapshot.deprecation_detail_count`.
        if record.deprecation_detail_count:
            gaps.append(
                gap(
                    "DEPRECATION_DETAILS_NOT_MODELLED",
                    f"Insight {record.name!r} reports "
                    f"{record.deprecation_detail_count} deprecation detail(s), which "
                    "this build records only as a count.",
                    "`deprecationDetails` was empty on every cluster available when "
                    "this contract was written, so its populated shape has never been "
                    "observed. Modelling it from an empty array would have been a "
                    "guess; storing it unvalidated would have broken the "
                    "no-arbitrary-payload rule.",
                    ("the structure of category_specific_summary.deprecationDetails",),
                    (
                        f"Inspect it directly: get_eks_insights with insight_id="
                        f"{record.insight_id}",
                        "Then extend ClusterInsightSnapshot with the observed shape.",
                    ),
                )
            )

    return Insights(
        insights=tuple(
            sorted(records, key=lambda r: (r.category, r.name, r.insight_id))
        ),
        gaps=tuple(gaps),
    )
