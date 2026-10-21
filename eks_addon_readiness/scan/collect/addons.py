"""EKS-managed add-ons and their published version catalogue.

One of two inventory sources that are always consulted, because neither is
complete alone: EKS-managed add-ons exist only in the EKS API and never appear in
Helm state, while Helm-installed add-ons exist only in the cluster and never
appear in the EKS API.
"""

from __future__ import annotations

from typing import NamedTuple

from ...models import UnresolvedFinding
from ...snapshot import EksAddonVersionCompatibility, EksManagedAddonSnapshot
from ..ports import ClusterReader
from .findings import gap
from .sanitize import addon_from_response, catalogue_from_versions


class ManagedAddons(NamedTuple):
    """The managed add-on slice of a snapshot, with the gaps collecting it left."""

    addons: tuple[EksManagedAddonSnapshot, ...]
    catalogue: tuple[EksAddonVersionCompatibility, ...]
    gaps: tuple[UnresolvedFinding, ...]


def collect_managed_addons(reader: ClusterReader) -> ManagedAddons:
    """Read every EKS-managed add-on, then its version catalogue.

    The catalogue is the authoritative, multi-hop compatibility source: it lets a
    managed add-on be decided deterministically across every Kubernetes version AWS
    publishes, without research. It is read per installed add-on, and a failure for
    one add-on is a typed gap rather than fatal — the others are still worth having.
    """
    addons: list[EksManagedAddonSnapshot] = []
    catalogue: list[EksAddonVersionCompatibility] = []
    gaps: list[UnresolvedFinding] = []

    addon_names, error = reader.list_addon_names()
    if error:
        gaps.append(
            gap(
                "EKS_ADDON_LIST_UNAVAILABLE",
                "EKS-managed add-ons could not be listed.",
                error,
                ("eks:ListAddons via list_eks_resources",),
                (
                    "Confirm the identity holds AmazonEKSMCPReadOnlyAccess.",
                    "Re-run the scan; treat managed add-on inventory as unknown until it succeeds.",
                ),
            )
        )

    for name in addon_names:
        addon_data, addon_error = reader.describe_addon(name)
        if addon_error or addon_data is None:
            gaps.append(
                gap(
                    "EKS_ADDON_VERSION_UNAVAILABLE",
                    f"Version for EKS-managed add-on {name!r} could not be read.",
                    addon_error or "no data",
                    (f"eks:DescribeAddon for {name}",),
                    (
                        f"Run: aws eks describe-addon --cluster-name {reader.cluster_name} "
                        f"--addon-name {name}",
                        "Do not assume a version for this add-on.",
                    ),
                )
            )
            continue
        addons.append(addon_from_response(name, addon_data))

    for addon in addons:
        result = reader.describe_addon_versions(addon.addon_name)
        if result.error:
            gaps.append(
                gap(
                    "EKS_ADDON_CATALOGUE_UNAVAILABLE",
                    f"The version catalogue for EKS-managed add-on "
                    f"{addon.addon_name!r} could not be read.",
                    result.error,
                    (f"eks:DescribeAddonVersions for {addon.addon_name}",),
                    (
                        "Confirm the identity is allowed eks:DescribeAddonVersions.",
                        f"Run: aws eks describe-addon-versions --addon-name "
                        f"{addon.addon_name}",
                        "The add-on falls back to the EKS Insight (single hop) until this "
                        "succeeds.",
                    ),
                )
            )
            continue
        catalogue.extend(catalogue_from_versions(addon.addon_name, result.items))

    return ManagedAddons(
        addons=tuple(sorted(addons, key=lambda a: a.addon_name)),
        catalogue=tuple(
            sorted(catalogue, key=lambda c: (c.addon_name, c.addon_version))
        ),
        gaps=tuple(gaps),
    )
