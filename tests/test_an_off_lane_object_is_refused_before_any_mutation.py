"""The code fence judges nothing about a chart's shape; the preflight proves it on a refused render.

WHAT THE FENCE USED TO BE. Four tests per rendered object -- this release's instance label, Helm as
the manager, a reviewed kind, the lane's own namespace -- written against the first chart the lane
upgraded and defended as the only thing keeping one component off its neighbours once the lane's
RBAC went generic.

WHAT THE NEXT TWO CHARTS SAID ABOUT THAT. cert-manager keeps its leader-election Role and
RoleBinding in `kube-system` (refused). A chart that puts no `app.kubernetes.io/instance`
label on its CRDs at all is also refused. Both correct charts, both refused by a rule about
how a chart ought to label itself. The kind list went last: it was still a list of what the charts
seen so far happened to ship, waiting for the first chart that shipped something else. A fence that
needs a code change per add-on is a list of add-ons, not a fence.

WHAT IS TRUE INSTEAD. The rendered manifest IS the release: `helm template` of the APPROVED chart
version from the APPROVED repository with this release's own recorded values. Every object in it is
this release's whatever its kind, labels or namespace. Helm refuses -- before changing anything --
to adopt an object that already exists under another release, so a neighbour's object cannot be
taken over by this path. What the code still checks before the mutation is that it can READ every
document and that the render SHIPS the approved version
(`test_the_render_must_ship_what_was_approved.py`).

PROVEN AGAINST THE ESTATE, NOT AGAINST A TOY. `tests/fixtures/charts/` holds `helm template` of
cert-manager v1.13.2 -> v1.21.2 and keda 2.17.2 -> 2.21.0, the installed and approved versions on
this cluster. Both renders pass whole. Deliberately no AWS call and no PyYAML: the functions take
already-parsed mappings, which is exactly why the fence is testable at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from eks_addon_readiness.upgrade_lane.fence import (
    LaneFence,
    OffLaneObject,
    refuse_a_render_this_code_cannot_read,
)

ROOT = Path(__file__).resolve().parent.parent
CHARTS = ROOT / "tests" / "fixtures" / "charts"
BUILDSPEC = ROOT / "infra" / "terraform" / "modules" / "platform" / "upgrade_buildspec.yml"


def _docs(name: str) -> list[dict]:
    return json.loads((CHARTS / name).read_text(encoding="utf-8"))


def _document(kind: str, name: str, **labels: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": "kube-system", "labels": labels},
    }


# ---------------------------------------------------------------------------
# The real charts pass whole, whatever their labelling, namespacing or kinds.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture", "quirk"),
    [
        (
            "cert-manager-v1.21.2.rendered.json",
            lambda d: d["metadata"].get("namespace") == "kube-system",
        ),
        (
            "keda-2.21.0.rendered.json",
            lambda d: "app.kubernetes.io/instance" not in d["metadata"].get("labels", {}),
        ),
    ],
    ids=["cert-manager keeps RBAC in kube-system", "keda labels no CRD with its instance"],
)
def test_a_real_chart_passes_whole_with_the_quirk_that_used_to_refuse_it(
    fixture: str, quirk
) -> None:
    docs = _docs(fixture)
    assert any(quirk(d) for d in docs), "the fixture must still carry the quirk this test is about"
    refuse_a_render_this_code_cannot_read(docs)  # does not raise


def test_the_installed_versions_pass_too() -> None:
    for fixture in ("cert-manager-v1.13.2.rendered.json", "keda-2.17.2.rendered.json"):
        refuse_a_render_this_code_cannot_read(_docs(fixture))


def test_kinds_labels_and_namespaces_are_not_judged() -> None:
    """The exact documents the old rules refused, now accepted -- stated so the change is visible.

    Another release's instance label, no labels at all, a foreign namespace, a kind no chart here
    has shipped: none is a reason to refuse a render of the approved chart. Helm's ownership check,
    not this fence, is what keeps the apply off an object another release already holds.
    """
    refuse_a_render_this_code_cannot_read(
        [
            _document("Deployment", "coredns", **{"app.kubernetes.io/instance": "coredns"}),
            {"kind": "ConfigMap", "metadata": {"name": "bare"}},
            {"kind": "Role", "metadata": {"name": "leaderelection", "namespace": "argocd"}},
            _document("ValidatingAdmissionPolicy", "a-kind-nobody-listed"),
            {"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledObject", "metadata": {"name": "x"}},
        ]
    )


def test_a_document_with_no_kind_is_refused_rather_than_skipped() -> None:
    """ "I do not understand this" must not mean "apply it"."""
    with pytest.raises(OffLaneObject, match="no `kind`"):
        refuse_a_render_this_code_cannot_read([{"metadata": {"name": "x"}}])
    with pytest.raises(OffLaneObject, match="no `kind`"):
        refuse_a_render_this_code_cannot_read([{"kind": "", "metadata": {"name": "x"}}])


def test_a_lane_carries_no_list_of_kinds() -> None:
    """The fence model has nothing per-chart on it: a lane is a release name and a namespace."""
    fence = LaneFence(component="keda", release="keda", namespace="keda")
    # `kind` is the document's own kind, as on every contract here; nothing else names kinds.
    assert set(type(fence).model_fields) == {
        "schema_version",
        "kind",
        "component",
        "release",
        "namespace",
        "managed_addon",
    }
    assert not hasattr(fence, "fenced_kinds")


# ---------------------------------------------------------------------------
# Wired: the check runs before the mutation, and the preflight exercises the rule that is left.
# ---------------------------------------------------------------------------


def test_the_check_runs_before_the_mutation_in_the_buildspec() -> None:
    """The code fence is worthless if it runs after `helm upgrade`, so the ORDER is asserted too."""
    buildspec = BUILDSPEC.read_text()
    pre_build = buildspec.index("  pre_build:")
    build = buildspec.index("\n  build:")
    assert pre_build < build
    fence_check = buildspec.index("fence-check")
    assert pre_build < fence_check < build, "`fence-check` must be a pre_build command"


def test_the_preflight_plants_a_render_that_does_not_ship_the_approved_version() -> None:
    """The self-test must exercise the rule the fence still holds, or it proves nothing.

    It planted another release's instance label, then an unlisted kind; each became an ACCEPTED
    document when that rule went, and a preflight expecting refusal would have stopped every lane.
    So the planted document is a Deployment whose image tag is not the approved version, checked
    with `--application-version` -- the one check left that can refuse.
    """
    buildspec = BUILDSPEC.read_text()
    planted = buildspec[buildspec.index("off-lane.yaml") - 600 : buildspec.index("off-lane.yaml")]
    assert "kind: Deployment" in planted
    assert "not-the-approved-version" in planted
    preflight_check = buildspec[
        buildspec.index("off-lane.yaml") : buildspec.index("off-lane.yaml") + 400
    ]
    assert re.search(r"--application-version\s+\"\$\{TARGET_VERSION\}\"", preflight_check), (
        "the preflight fence-check must pass --application-version, or a Deployment with any "
        "image would be accepted and the preflight would refuse every lane"
    )
    assert "--owned" not in buildspec, "the owned-set flag went with the namespace rule"
    assert "ValidatingAdmissionPolicy" not in buildspec, (
        "the planted unlisted kind went with the list"
    )
