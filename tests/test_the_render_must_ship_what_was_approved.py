"""The render must RUN the approved version; afterwards the report says what runs.

THE HOLE. The lane renders the new chart with the release's existing values -- correctly, an
operator's `--kubelet-insecure-tls` must survive -- but a values file that pins `image.tag` pins the
component: `helm upgrade` moves the chart, Helm reports success, the pods are untouched and
therefore healthy, the agent says HEALTHY, and the report says the approved version was applied. A
green report over a component that did not move. Nothing had hit it yet; it was found reading the
buildspec.

TWO CLOSURES. Before anything moves, `fence-check --application-version` reads the rendered
workloads' image tags and refuses unless one carries the approved version. After everything, the
lane reads the running image tag (or the add-on version) into the result's `resulting`, and the
report notes "current: X" only when that is a different release from `applied`.

Checked against the real cert-manager v1.21.2 render: three workload images tagged `v1.21.2`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eks_addon_readiness.upgrade_lane import __main__ as cli
from eks_addon_readiness.upgrade_lane.fence import (
    OffLaneObject,
    container_images,
    refuse_a_manifest_that_does_not_ship,
)
from eks_addon_readiness.upgrade_lane.report_view import same_release

ROOT = Path(__file__).resolve().parent.parent
TARGET = json.loads(
    (ROOT / "tests" / "fixtures" / "charts" / "cert-manager-v1.21.2.rendered.json").read_text()
)
BUILDSPEC = ROOT / "infra" / "terraform" / "modules" / "platform" / "upgrade_buildspec.yml"


def test_the_real_render_ships_the_approved_version() -> None:
    images = container_images(TARGET)
    assert len(images) == 3, "controller, webhook, cainjector -- the Job's image is not a workload"
    assert all(i.endswith(":v1.21.2") for i in images)
    assert refuse_a_manifest_that_does_not_ship(TARGET, "1.21.2").endswith(":v1.21.2")


def test_a_pinned_image_is_refused_before_anything_moves() -> None:
    """The values file says `image.tag: v1.13.2`; the new chart renders the old component."""
    pinned = json.loads(json.dumps(TARGET))
    for d in pinned:
        if d["kind"] == "Deployment":
            for c in d["spec"]["template"]["spec"]["containers"]:
                c["image"] = c["image"].replace("v1.21.2", "v1.13.2")
    with pytest.raises(OffLaneObject, match="would change the chart and not the component"):
        refuse_a_manifest_that_does_not_ship(pinned, "1.21.2")


def test_a_render_with_no_workload_image_is_refused() -> None:
    rbac_only = [d for d in TARGET if d["kind"] in ("Role", "ClusterRole")]
    with pytest.raises(OffLaneObject, match="runs nothing"):
        refuse_a_manifest_that_does_not_ship(rbac_only, "1.21.2")


def test_the_buildspec_passes_the_approved_version_and_reads_what_runs() -> None:
    text = BUILDSPEC.read_text()
    real_check = text[text.index("--manifest /tmp/rendered.yaml") :]
    assert '--application-version "${TARGET_VERSION}"' in real_check[:300]
    assert "running-version --pods" in text and '"resulting": resulting' in text


def test_running_version_reads_the_tags_off_the_pods(tmp_path: Path, capsys) -> None:
    pods = {
        "items": [
            {
                "spec": {
                    "containers": [{"image": "quay.io/jetstack/cert-manager-controller:v1.21.2"}]
                }
            },
            {"spec": {"containers": [{"image": "quay.io/jetstack/cert-manager-webhook:v1.21.2"}]}},
        ]
    }
    path = tmp_path / "pods.json"
    path.write_text(json.dumps(pods))
    assert cli.main(["running-version", "--pods", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "v1.21.2"


@pytest.mark.parametrize(
    ("resulting", "applied", "same"),
    [
        ("v1.21.2", "1.21.2", True),
        ("v1.23.1-eksbuild.1", "1.23.1", True),
        ("v1.13.2", "1.21.2", False),
        ("", "1.21.2", False),
    ],
)
def test_the_report_notes_current_only_when_what_runs_differs(
    resulting: str, applied: str, same: bool
) -> None:
    assert same_release(resulting, applied) is same
