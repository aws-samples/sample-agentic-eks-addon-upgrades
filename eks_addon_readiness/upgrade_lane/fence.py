"""The lane fence: the pre-mutation checks on one component's rendered manifest.

    in    one Helm release's rendered manifest, and the lane the run has open
    out   a refusal, before any mutation, if the render cannot be read or does not ship the
          approved version

WHAT THIS FILE NO LONGER DOES, IN ORDER. It generated a Role and ClusterRole per component from
the release's manifest, naming each object in `resourceNames`; that could not generalise to a new
add-on and RBAC cannot match a label anyway. Then one generic type-scoped role, which refused three
real `helm upgrade` operations in a row for permissions Helm needs and nobody had enumerated. Then a
code fence over the render that judged each object's instance label, manager label, kind and
namespace; a chart with a Role in `kube-system` (cert-manager) and a chart with no instance
label on its CRDs (KEDA) were both correct charts refused by rules about how a chart ought to
label itself. Then a list of reviewed KINDS -- which was still a list of what the charts seen so far
happened to ship, needing an edit for the first chart that shipped something else. A fence that
needs a code change per add-on is a list of add-ons, not a fence.

WHAT BOUNDS THE LANE INSTEAD. The lane's identity holds `AmazonEKSClusterAdminPolicy` for the life
of one access entry, so the API server permits whatever it asks. What it asks is `helm upgrade` of
the APPROVED chart version from the APPROVED repository with this release's own recorded values,
from a buildspec committed here and pinned by S3 object version, for one component at a time under a
deadline. Helm applies only what is in that render, and refuses -- before changing anything -- to
adopt an object that already exists under another release or under nobody ("invalid ownership
metadata"). That is the "off its neighbours" boundary, and it is Helm's, not this file's.

WHAT IS LEFT HERE, both run on `helm template` output before `helm upgrade`:

  * `refuse_a_render_this_code_cannot_read`: every document must carry a `kind`. Not a rule about
    which kinds -- a parse the code does not understand must not mean "apply it".
  * `refuse_a_manifest_that_does_not_ship`: the rendered workloads must carry the approved
    application version. A values file that pins `image.tag` would move the chart and leave the
    component where it is, and every check after the mutation would call that healthy.

The agent never holds the lane's identity: it reads through an AgentCore Gateway with a standing
view-only access entry of its own, and cannot open a lane or write to one.
`tests/test_the_lane_grant_is_bounded_where_rbac_no_longer_bounds_it.py` holds the remaining bound:
that the grant is bounded in time, in scope, and to one cluster.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field, model_validator

from ..models import ContractModel

# THE GROUP THE LANE'S ACCESS ENTRY IS OPENED INTO. No role binds it -- the lane's authority is the
# cluster-admin policy association, not RBAC -- but the name is still load-bearing: `platform`'s IAM
# puts a `ForAllValues:StringEquals` condition on `eks:CreateAccessEntry` naming it, which is what
# stops an entry being minted into `system:masters`. Spelled here and in that condition, so a test
# compares the two.
LANE_GROUP = "upgrade-agent"


class OffLaneObject(Exception):
    """A rendered manifest this lane may not apply. Raised BEFORE any mutation.

    An exception rather than a returned verdict, because there is exactly one correct response and
    it is to stop. A caller that wanted to continue would be choosing to apply a render this code
    could not vouch for, and this package does not offer that.
    """


class LaneFence(ContractModel):
    """The one lane a run has open: whose objects may be touched, and where.

    MUCH SMALLER THAN IT WAS. It used to carry the release's every object, the enumerated Helm
    revision secret names and the roles its bindings referenced -- all of it input to RBAC
    generation
    that no longer happens here. What is left is what the code fence actually needs, and the
    buildspec
    passes every field of it.
    """

    schema_version: Literal["1.0"] = "1.0"
    # `kind` here is the DOCUMENT's kind, as every other contract in this project spells it.
    kind: Literal["UpgradeLaneFence"] = "UpgradeLaneFence"

    component: str = Field(min_length=1)
    # The Helm release name. Separate from `component` because they are not always equal --
    # `component` is this project's canonical id and the release is whatever the operator
    # named it -- and the release name is what the buildspec passes to `helm upgrade`.
    release: str = Field(min_length=1)
    namespace: str = Field(min_length=1)

    # A MANAGED ADD-ON'S LANE PERFORMS NO KUBERNETES WRITE AT ALL, and that is why this flag exists
    # rather than being inferred. `coredns`, `kube-proxy`, `vpc-cni` and `eks-pod-identity-agent`
    # are moved by `eks update-addon` -- an AWS API call -- so there is no rendered manifest to
    # check.
    # The lane still opens, because validation is pod status and pod logs either way: one path
    # through the lane for both kinds of component is what keeps the access-entry cleanup
    # unskippable.
    managed_addon: bool = False

    @property
    def group(self) -> str:
        """The group the write lane binds. A CONSTANT, not per component -- see `LANE_GROUP`."""
        return LANE_GROUP

    @model_validator(mode="after")
    def a_managed_addon_has_no_helm_release_to_fence(self) -> LaneFence:
        """A managed add-on's `release` must be its component name, not something else.

        Not cosmetic. `eks update-addon` has no Helm release, so there is no render to check
        and `fence-check` is never run for one. A fence that carried a
        different release name for a managed add-on would read as though it fenced a Helm manifest
        that does not exist, which is the sort of inert-looking correctness this module is trying to
        avoid.
        """
        if self.managed_addon and self.release != self.component:
            raise ValueError(
                f"lane {self.component!r} is a managed add-on, which has no Helm release, but "
                f"names release {self.release!r}. A managed add-on's release must be its own "
                "component name so nothing reads it as a Helm lane."
            )
        return self


def refuse_a_render_this_code_cannot_read(documents: Sequence[Mapping[str, object]]) -> None:
    """Refuse the whole render if any document lacks a `kind`.

    NOT A RULE ABOUT WHICH KINDS. Which kinds a chart ships is upstream's decision and the lane
    holds cluster-admin either way; a list of acceptable kinds was the last per-chart list this
    project kept and it went the way of the others. This is parse sanity: `helm template` emits
    empty documents for templates that render to nothing and the caller drops those, so a mapping
    that reaches here without a kind is a parse this code does not understand, and "I do not
    understand it" must not mean "apply it".

    RAISES RATHER THAN FILTERS. Dropping the document and applying the rest would produce a partial
    release, which is worse than not upgrading.
    """
    for index, document in enumerate(documents):
        kind = document.get("kind")
        if not isinstance(kind, str) or not kind:
            raise OffLaneObject(
                f"document {index} of the rendered manifest has no `kind`. An object this code "
                "cannot identify is refused rather than applied."
            )


# The kinds whose pod template says what actually runs. A Job's image is a hook or a check, not
# the component, so a chart whose Job moved and whose Deployment did not is still not an upgrade.
_RUNNING_KINDS = frozenset({"Deployment", "DaemonSet", "StatefulSet"})


def _bare_version(text: str) -> str:
    return text.strip().lstrip("vV")


def container_images(documents: Sequence[Mapping[str, object]]) -> tuple[str, ...]:
    """Every container image the rendered workloads would run, in document order, de-duplicated."""
    images: list[str] = []
    for document in documents:
        if document.get("kind") not in _RUNNING_KINDS:
            continue
        spec = document.get("spec")
        template = spec.get("template") if isinstance(spec, Mapping) else None
        pod = template.get("spec") if isinstance(template, Mapping) else None
        if not isinstance(pod, Mapping):
            continue
        for key in ("initContainers", "containers"):
            for container in pod.get(key) or ():
                image = container.get("image") if isinstance(container, Mapping) else None
                if isinstance(image, str) and image and image not in images:
                    images.append(image)
    return tuple(images)


def image_tag(image: str) -> str:
    """`registry/repo:tag@sha256:...` -> `tag`; `""` when the reference carries no tag.

    >>> image_tag("registry.k8s.io/metrics-server/metrics-server:v0.9.0")
    'v0.9.0'
    >>> image_tag("quay.io/jetstack/cert-manager-controller:v1.21.2@sha256:abc")
    'v1.21.2'
    >>> image_tag("localhost:5000/keda")
    ''
    """
    reference = image.split("@", 1)[0]
    last_slash = reference.rfind("/")
    colon = reference.rfind(":")
    return reference[colon + 1 :] if colon > last_slash else ""


def refuse_a_manifest_that_does_not_ship(
    documents: Sequence[Mapping[str, object]], application_version: str
) -> str:
    """Refuse a rendered manifest whose workloads would not run the approved application version.

    THE HOLE THIS CLOSES. The lane renders the NEW chart with the release's EXISTING values, which
    is the right upgrade semantics -- an operator's `--kubelet-insecure-tls` must survive. But a
    values file that pins `image.tag` pins the component too: `helm upgrade` moves the chart,
    Helm reports success, the pods are untouched and therefore healthy, the agent says HEALTHY,
    and the report says the approved version was applied. A green report over a component that
    did not move. So before anything mutates, the rendered workloads' image tags are read and at
    least one must carry the approved version. A pinned or digest-only image is refused with the
    tags found, and a person decides whether to drop the override.

    Returns the matching image, so the buildspec can log which one carried the version.

    >>> deploy = {"kind": "Deployment", "spec": {"template": {"spec": {"containers": [
    ...     {"image": "registry.k8s.io/metrics-server/metrics-server:v0.9.0"}]}}}}
    >>> refuse_a_manifest_that_does_not_ship([deploy], "0.9.0")
    'registry.k8s.io/metrics-server/metrics-server:v0.9.0'
    >>> refuse_a_manifest_that_does_not_ship([deploy], "0.6.4")
    Traceback (most recent call last):
    ...
    eks_addon_readiness.upgrade_lane.fence.OffLaneObject: no workload in the rendered manifest ...
    """
    wanted = _bare_version(application_version)
    if not wanted:
        raise OffLaneObject(
            "no application version reached the image check, so the rendered manifest cannot be "
            "confirmed to ship what was approved. The version comes from the authorised step."
        )
    images = container_images(documents)
    if not images:
        raise OffLaneObject(
            "the rendered manifest declares no Deployment, DaemonSet or StatefulSet container "
            "image, so nothing in it runs the component. Refused: an upgrade that runs nothing "
            "is not an upgrade."
        )
    for image in images:
        tag = _bare_version(image_tag(image))
        # Equal, or the version with a build suffix (`v0.9.0-eks-1`). A tag that merely
        # starts with the digits (`0.9.01`) does not count.
        if tag == wanted or tag.startswith(wanted + "-"):
            return image
    raise OffLaneObject(
        f"no workload in the rendered manifest runs application version {application_version!r}; "
        f"the images it would run are {list(images)!r}. The release's values pin the image the "
        "chart would otherwise move, so `helm upgrade` would change the chart and not the "
        "component -- and report success. Refused before anything moves; a person decides "
        "whether to drop the override."
    )
