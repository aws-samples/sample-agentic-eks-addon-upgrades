"""Whether two names denote the same component, when one of them was written by a reader.

    in    the name the lane is keyed on, and the name a triage answered with
    out   whether the two denote the same component

**AN EKS ADD-ON NAME IS NOT THE WORKLOAD IDENTITY A READER REPORTS, AND NOTHING RECONCILED THEM.**
A lane is keyed on the add-on name EKS takes -- `vpc-cni`, `coredns`, `kube-proxy` -- because that
string is an argument to `eks update-addon`. An agent sent to look at the cluster names what it
found there: the project's own name, usually with the workload and namespace it read. Measured on
one test run's step 10, four checkpoints in a row:
`'aws-vpc-cni (aws-node DaemonSet)'`, then `'aws-vpc-cni (aws-node DaemonSet, kube-system)'` twice,
then `'amazon-vpc-cni (aws-node DaemonSet, kube-system)'` -- every one of them refused against
`'vpc-cni'`, on an add-on that had not been touched. The whole 900-second window and a rollback ran
downstream of a name.

**THE GUARD IS RIGHT TO CHECK AND WRONG TO COMPARE STRINGS.** A triage about a DIFFERENT component
must still be refused: it is evidence the ask is wrong, and filing it under this lane would put one
component's reading in another's record. What the guard may not do is refuse a triage about THIS
component for describing it more fully than the key does.

**NO COMPONENT IS NAMED IN THIS MODULE, AND THAT IS A STANDING REQUIREMENT RATHER THAN A
PREFERENCE.** The lane's RBAC and its fences are generic so that any CNCF add-on or Helm release can
use them, and a reconciliation keyed on `vpc-cni` would be exactly the per-component table that
requirement exists to forbid. What is implemented instead is a rule about DECORATION -- a vendor
prefix, a parenthetical qualifier, a different separator, a different case -- and the candidate
names come from the lane's own payload, so a component nobody has added yet needs no entry anywhere.

**WHAT THIS DOES NOT DO, STATED SO IT IS NOT DISCOVERED LATER.** It cannot reconcile a name that
shares no text with the key: `aws-node` is the DaemonSet vpc-cni ships and no string rule relates
the two. That case needs DATA, and the lane already holds some -- `addon_pod_selector` is
`k8s-app=aws-node` in the catalogue -- so a caller that has it may pass it as an alias. Nothing in
this module knows it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Everything that is not a letter or a digit is a separator. `vpc-cni`, `vpc_cni`, `VPC CNI` and
# `vpccni` are one name spelled four ways, and none of the four is more correct than the others.
_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")


def comparable_name(name: str) -> str:
    """One component name reduced to what every spelling of it shares.

    Two things go. The PARENTHETICAL QUALIFIER, because a reader appends what it looked at -- the
    workload kind, the namespace -- and that is a description of the component rather than part of
    its name. Then CASE AND SEPARATORS, because `CoreDNS` and `coredns` and `core-dns` are one
    component and nothing distinguishes them but typography.

    THE VENDOR PREFIX IS DELIBERATELY KEPT. Dropping `amazon` here would be a rule about which
    prefixes are decoration, which is a list of vendors -- and a list is the thing this module must
    not have. `denotes_same_component` handles it positionally instead.

    >>> comparable_name("amazon-vpc-cni (aws-node DaemonSet, kube-system)")
    'amazonvpccni'
    >>> comparable_name("vpc-cni")
    'vpccni'
    >>> comparable_name("CoreDNS")
    'coredns'
    >>> comparable_name("core-dns")
    'coredns'
    >>> comparable_name("   ")
    ''
    """
    head = name.split("(", 1)[0]
    return _NOT_ALPHANUMERIC.sub("", head.lower())


def denotes_same_component(
    answered: str, *, expected: str, aliases: Iterable[str] = ()
) -> bool:
    """Whether `answered` names the component `expected` (or one of its `aliases`) denotes.

    THE RULE IS POSITIONAL: one reduced name must END with the other. A vendor prefix precedes the
    component's name -- `amazonvpccni` ends with `vpccni` -- whereas a DIFFERENT component that
    merely starts with this one's name extends it at the tail, and `vpccnimetricshelper` does not
    end with `vpccni`. That is the one asymmetry that separates decoration from a neighbour, and it
    is why this is not a containment test.

    BOTH DIRECTIONS, because the decoration can be on either side: a catalogue keyed on
    `amazon-vpc-cni` asking a reader that answers `vpc-cni` is the same reconciliation mirrored.

    `aliases` are further names for the SAME component that the caller already holds -- the lane
    payload carries the release name beside the component key. Passing them costs nothing and keeps
    every component-specific string in the caller's data rather than in this module.

    >>> denotes_same_component("vpc-cni", expected="vpc-cni")
    True
    >>> denotes_same_component("amazon-vpc-cni (aws-node DaemonSet, kube-system)",
    ...                        expected="vpc-cni")
    True
    >>> denotes_same_component("aws-vpc-cni (aws-node DaemonSet)", expected="vpc-cni")
    True
    >>> denotes_same_component("CoreDNS (Deployment, kube-system)", expected="coredns")
    True
    >>> denotes_same_component("coredns", expected="vpc-cni")
    False
    >>> denotes_same_component("vpc-cni-metrics-helper", expected="vpc-cni")
    False
    >>> denotes_same_component("aws-node", expected="vpc-cni")
    False
    >>> denotes_same_component("aws-node (DaemonSet)", expected="vpc-cni",
    ...                        aliases=("aws-node",))
    True
    >>> denotes_same_component("", expected="vpc-cni")
    False
    """
    reduced = comparable_name(answered)
    if not reduced:
        return False
    for candidate in (expected, *aliases):
        other = comparable_name(candidate)
        if not other:
            continue
        if reduced.endswith(other) or other.endswith(reduced):
            return True
    return False
