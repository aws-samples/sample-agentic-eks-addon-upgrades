"""Whether a planned step can be applied to the cluster AS IT IS TODAY, and if not, why.

    in    one step (component, target version, where the plan placed it), the running minor,
          and the snapshot's EKS add-on catalogue
    out   "" when the step may be attempted now, else one sentence saying why not

**THE FIRST VERSION OF THIS RULE WAS POSITIONAL AND IT WAS WRONG IN ONE DIRECTION.** It withheld
every step whose `at_kubernetes` was above the running minor. For a managed add-on that is EVERY
optional step: its placement comes from the AWS Insight, which only ever assesses the NEXT minor,
so every add-on recommendation is stamped with the target. Checked against the real catalogue on
the 2026-09-26 snapshot, three of the four rows that rule would have withheld -- coredns
`v1.14.6-eksbuild.4`, eks-pod-identity-agent `v1.4.0-eksbuild.2`, vpc-cni `v1.23.1-eksbuild.1` --
list 1.35 as compatible, and vpc-cni had in fact been applied on 1.35 that same morning. Only
kube-proxy `v1.36.0-eksbuild.25` is genuinely 1.36-only.

**SO THE RULE LOOKS THE BUILD UP.** Stage 1 captures `describe-addon-versions` into the snapshot
(`ClusterSnapshot.eks_addon_catalogue`), one row per published build with the Kubernetes minors
AWS lists it for. That is AWS's own answer to "is this build published for the minor the cluster
runs", and it is the answer EKS will give the `update-addon` call. A managed add-on step is
withheld when its build's row does not list the running minor. No guessing from placement.

**A COMPONENT WITH NO CATALOGUE ROW FALLS BACK TO PLACEMENT.** A Helm component has no entry --
AWS does not publish cert-manager -- and for it `at_kubernetes` is a real statement from the
sequencer about when the move must happen, not an Insight artefact. So the positional rule stays
for those, and for a managed add-on whose build the catalogue somehow does not list, because a
step this cannot check must not be attempted on a guess.

**ONE MODULE, TWO CALLERS, ON PURPOSE.** `stages/upgrade_approval.py` uses this to decide which
rows get a button; `stages/upgrade_report.py::_plan` uses it to decide which authorised steps
open a lane. If they disagreed, a person could approve a step the planner then refused, or the
planner could attempt one the page said was unavailable. Same function, same answer.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..decide.kubernetes import VersionError, parse_minor
from ..facts.series import exact_version


def _minor_above(at_kubernetes: str | None, current: str) -> bool:
    """Whether `at_kubernetes` names a minor above `current`. False when either is unreadable."""
    if not at_kubernetes:
        return False
    try:
        return parse_minor(at_kubernetes) > parse_minor(current)
    except VersionError:
        return False


def catalogue_index(entries: Iterable) -> dict[tuple[str, str], tuple[str, ...]]:
    """`(addon_name, exact version)` -> the Kubernetes minors AWS lists that build for.

    Keyed on the `major.minor.patch` core rather than the raw string, so `1.23.1` on a plan step
    and `v1.23.1-eksbuild.1` in the catalogue meet. When several builds share a core (`eksbuild.1`
    and `eksbuild.4`) their minors are UNIONED: the question is whether AWS publishes that version
    for this minor at all, and the lane resolves the exact build itself
    (`upgrade_lane.addon_version`).
    """
    index: dict[tuple[str, str], set[str]] = {}
    for entry in entries:
        core = exact_version(getattr(entry, "addon_version", None))
        if core is None:
            continue
        key = (getattr(entry, "addon_name", ""), core)
        index.setdefault(key, set()).update(getattr(entry, "compatible_kubernetes_versions", ()))
    return {key: tuple(sorted(minors)) for key, minors in index.items()}


def not_applicable_reason(
    *,
    component: str,
    target_version: str | None,
    at_kubernetes: str | None,
    current_minor: str,
    managed_addon: bool,
    catalogue: dict[tuple[str, str], tuple[str, ...]],
) -> str:
    """Why this step cannot be applied to the cluster today, or "" when it can.

    `target_version` is the version the step installs (`recommended_version`, or the exact
    build); `catalogue` is `catalogue_index(snapshot.eks_addon_catalogue)`.
    """
    if not current_minor:
        return ""
    core = exact_version(target_version) if target_version else None
    if managed_addon and core is not None:
        minors = catalogue.get((component, core))
        if minors is not None:
            if current_minor in minors:
                return ""
            published = ", ".join(minors) if minors else "no Kubernetes minor"
            return (
                f"AWS publishes {component} {target_version} for Kubernetes {published}, not for "
                f"{current_minor}, which the cluster is on. EKS would refuse it today. It becomes "
                f"applicable once the control plane is on a listed minor."
            )
    if _minor_above(at_kubernetes, current_minor):
        return (
            f"Applies once the cluster is on Kubernetes {at_kubernetes}; it is on {current_minor}. "
            "Not offered on this run; it becomes approvable on the run after the control-plane "
            "upgrade."
        )
    return ""
