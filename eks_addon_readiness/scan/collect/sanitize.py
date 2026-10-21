"""Sanitization by construction: only allowlisted fields are copied out.

Each function below takes a raw transport response and returns a snapshot
contract, copying an explicit set of fields and nothing else. The cluster
endpoint, the cluster role ARN, CA data, resource ARNs embedding the account id
and any Secret content therefore cannot reach a snapshot even though some of them
appear in the underlying responses.

That is the reason these are copy functions rather than `model_validate(raw)`:
validation would accept whatever the response happened to contain.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ...snapshot import (
    ClusterInsightSnapshot,
    EksAddonVersionCompatibility,
    EksManagedAddonSnapshot,
)

_MINOR_VERSION = re.compile(r"^(\d+)\.(\d+)")


def optional_str(value: Any) -> str | None:
    """The value if it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def normalize_kubernetes_version(raw: str) -> str | None:
    """Reduce a reported version to `major.minor`.

    The `ClusterSnapshot` contract stores `major.minor` because add-on
    compatibility is expressed per Kubernetes minor. Returns `None` rather than
    guessing when the input has no recognisable version prefix.

    >>> normalize_kubernetes_version("1.35")
    '1.35'
    >>> normalize_kubernetes_version("1.35.6-eks-bca9cf6")
    '1.35'
    >>> normalize_kubernetes_version("unknown") is None
    True
    """
    match = _MINOR_VERSION.match(raw.strip())
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}"


def addon_from_response(name: str, data: dict[str, Any]) -> EksManagedAddonSnapshot:
    """Copy only allowlisted add-on fields. Drops addonArn, tags and timestamps."""
    health = data.get("health") or {}
    issues = health.get("issues") if isinstance(health, dict) else None
    codes: tuple[str, ...] = ()
    if isinstance(issues, list):
        codes = tuple(
            sorted(
                str(issue.get("code"))
                for issue in issues
                if isinstance(issue, dict) and issue.get("code")
            )
        )
    namespace_config = data.get("namespaceConfig") or {}
    namespace = (
        namespace_config.get("namespace") if isinstance(namespace_config, dict) else None
    )
    return EksManagedAddonSnapshot(
        addon_name=str(data.get("addonName") or name),
        addon_version=optional_str(data.get("addonVersion")),
        status=optional_str(data.get("status")),
        namespace=optional_str(namespace),
        health_issue_codes=codes,
    )


def catalogue_from_versions(
    addon_name: str, versions: list[dict[str, Any]]
) -> list[EksAddonVersionCompatibility]:
    """Shape `describe-addon-versions` `addonVersions[]` into sanitized catalogue rows.

    One row per published add-on build, carrying its version and the sorted set of
    Kubernetes `clusterVersion`s AWS lists it as compatible with. A build with no
    parseable version or no compatibilities is skipped rather than stored empty. The
    response is a global catalogue with no ARNs or account IDs, so there is nothing to
    strip.
    """
    rows: list[EksAddonVersionCompatibility] = []
    for av in versions:
        if not isinstance(av, dict):
            continue
        version = optional_str(av.get("addonVersion"))
        if not version:
            continue
        compat = av.get("compatibilities")
        cluster_versions: set[str] = set()
        if isinstance(compat, list):
            for entry in compat:
                if isinstance(entry, dict):
                    cv = optional_str(entry.get("clusterVersion"))
                    if cv:
                        cluster_versions.add(cv)
        if not cluster_versions:
            continue
        rows.append(
            EksAddonVersionCompatibility(
                addon_name=addon_name,
                addon_version=version,
                compatible_kubernetes_versions=tuple(sorted(cluster_versions)),
            )
        )
    return rows


def insight_from_response(data: dict[str, Any]) -> ClusterInsightSnapshot:
    """Copy only allowlisted insight fields.

    Drops `resources`, which lists affected resources as ARNs embedding the AWS
    account ID. The add-on names those ARNs identify are already carried by
    `addon_compatible_versions`, so nothing is lost but an identifier the snapshot
    contract exists to exclude.
    """
    status_block = data.get("insight_status")
    status_block = status_block if isinstance(status_block, dict) else {}

    info_raw = data.get("additional_info")
    additional_info = (
        {k: v for k, v in info_raw.items() if isinstance(k, str) and isinstance(v, str)}
        if isinstance(info_raw, dict)
        else {}
    )

    summary = data.get("category_specific_summary")
    summary = summary if isinstance(summary, dict) else {}

    compatible: dict[str, tuple[str, ...]] = {}
    details = summary.get("addonCompatibilityDetails")
    if isinstance(details, list):
        for entry in details:
            if not isinstance(entry, dict):
                continue
            name = optional_str(entry.get("name"))
            versions = entry.get("compatibleVersions")
            if name and isinstance(versions, list):
                compatible[name] = tuple(v for v in versions if isinstance(v, str))

    deprecations = summary.get("deprecationDetails")
    deprecation_count = len(deprecations) if isinstance(deprecations, list) else 0

    # AWS reports this as a POSIX timestamp. Kept because it records when AWS
    # computed the opinion, which is not when we read it — the gap between the two
    # is exactly why this is evidence rather than a fact.
    computed_at: datetime | None = None
    raw_time = data.get("last_refresh_time")
    if isinstance(raw_time, int | float):
        try:
            computed_at = datetime.fromtimestamp(raw_time, tz=UTC)
        except (OverflowError, OSError, ValueError):
            computed_at = None

    return ClusterInsightSnapshot(
        insight_id=str(data.get("id") or "unknown"),
        name=str(data.get("name") or "unknown"),
        category=str(data.get("category") or "unknown"),
        kubernetes_version=optional_str(data.get("kubernetes_version")),
        status=optional_str(status_block.get("status")),
        reason=optional_str(status_block.get("reason")),
        recommendation=optional_str(data.get("recommendation")),
        additional_info=additional_info,
        computed_at=computed_at,
        addon_compatible_versions=compatible,
        deprecation_detail_count=deprecation_count,
    )
