"""Resolve the chart version that ships the approved application version, from the repository.

`addon_version.py`'S TWIN FOR HELM. An approved step carries an APPLICATION version (`0.9.0`);
`helm upgrade --version` wants a CHART version (`3.14.0`), and the two are not derivable from
each other -- the chart's own release numbering is the publisher's, exactly as the `-eksbuild.<n>`
ordinal is AWS's. So the buildspec runs `helm search repo <ref> --versions -o json` against the
repository the approved fact named, and this module picks the newest chart whose `app_version` is
the approved release. Nothing is transcribed by a model and nothing is committed by hand; the
repository is asked, at the moment the answer is needed, and its listing goes in the bundle as
evidence of what was chosen from.

WHY NEWEST. Two chart versions can ship one application version (a values-only chart fix). The
newer is the publisher's current packaging of that release, and picking it is what `helm install`
would do given the app version. A caller who needs a specific one has a specific reason and can
name it; nothing here guesses one.

THE RESOLUTION RUNS IN `pre_build`, WHICH CARRIES `set -e`, so an application version the
repository does not ship stops the run before `helm template`, before the fence, before the
mutation. Nothing to roll back.

STDLIB ONLY, for `addon_version.py`'s reason: the lane ships an enumerated file list, and an import
from outside `upgrade_lane` would pull the wider package into the bundle -- which
`tests/test_the_upgrade_lane_bundle_carries_what_it_imports.py` refuses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# The numbers out of a chart version that may carry a `v` and a pre-release suffix, for ordering
# rows of one chart newest-first. Anything that does not parse sorts last.
_CHART_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


class ChartVersionUnresolved(Exception):
    """No chart in the repository ships the approved application version. Always names both."""


def _bare(version: str) -> str:
    """`v1.19.1` and `1.19.1` are the same release; Helm's `app_version` column carries either."""
    return version.strip().lstrip("v")


def _key(version: str) -> tuple[int, int, int, str]:
    found = _CHART_VERSION.match(version.strip())
    if found is None:
        return (-1, -1, -1, version)
    major, minor, patch = found.groups()
    # A bare `3.14.0` outranks `3.14.0-rc.1`: the empty suffix sorts after any text.
    suffix = version.strip()[found.end() :]
    return (int(major), int(minor), int(patch), suffix or "~")


def resolve_chart_version(*, chart_ref: str, application_version: str, search: Any) -> str:
    """The newest chart version of `chart_ref` whose `app_version` is the approved release.

    `search` is the parsed `helm search repo <ref> --versions -o json` output: rows with
    `name`, `version` and `app_version`. Rows for a different chart are ignored rather than
    refused -- `helm search` matches the reference as a substring, so `keda/keda` also lists
    `keda/keda-add-ons-http`, and that is the tool's behaviour, not a lane defect.

    >>> rows = [
    ...     {"name": "metrics-server/metrics-server", "version": "3.14.0", "app_version": "0.9.0"},
    ...     {"name": "metrics-server/metrics-server", "version": "3.12.2", "app_version": "0.7.2"},
    ...     {"name": "metrics-server/metrics-server", "version": "3.12.1", "app_version": "0.7.2"},
    ...     {"name": "metrics-server/metrics-server-x", "version": "9.0.0", "app_version": "0.7.2"},
    ... ]
    >>> resolve_chart_version(
    ...     chart_ref="metrics-server/metrics-server", application_version="0.7.2", search=rows
    ... )
    '3.12.2'
    >>> resolve_chart_version(
    ...     chart_ref="metrics-server/metrics-server", application_version="v0.9.0", search=rows
    ... )
    '3.14.0'
    >>> resolve_chart_version(
    ...     chart_ref="metrics-server/metrics-server", application_version="0.8.0", search=rows
    ... )
    Traceback (most recent call last):
    ...
    eks_addon_readiness.upgrade_lane.chart_version.ChartVersionUnresolved: no chart ...
    """
    if not application_version.strip():
        raise ChartVersionUnresolved(
            f"no target version reached the lane for {chart_ref!r}, so there is nothing to "
            "resolve. The version comes from the authorised release step; its absence is a defect "
            "in the lane payload, not something to default."
        )
    if not isinstance(search, list) or not search:
        raise ChartVersionUnresolved(
            f"`helm search repo {chart_ref} --versions` listed nothing, so the repository the "
            "lane added does not publish this chart under that name. Nothing is installed from a "
            "repository that does not answer for the chart."
        )

    rows = [
        row for row in search if isinstance(row, dict) and str(row.get("name") or "") == chart_ref
    ]
    if not rows:
        names = sorted({str(r.get("name") or "") for r in search if isinstance(r, dict)})
        raise ChartVersionUnresolved(
            f"`helm search repo` lists no chart named {chart_ref!r}; it lists {names!r}. The "
            "chart the cluster has installed is not one this repository publishes."
        )

    wanted = _bare(application_version)
    shipping = [
        str(r.get("version") or "")
        for r in rows
        if _bare(str(r.get("app_version") or "")) == wanted and str(r.get("version") or "")
    ]
    if not shipping:
        published = sorted(
            {_bare(str(r.get("app_version") or "")) for r in rows} - {""}, key=_key, reverse=True
        )
        raise ChartVersionUnresolved(
            f"no chart version of {chart_ref!r} ships application version "
            f"{application_version!r}. The repository ships: {published!r}. The approved release "
            "is not one this chart packages, so nothing is installed."
        )
    return max(shipping, key=_key)


def resolve_from_file(*, chart_ref: str, application_version: str, search_path: str) -> str:
    """`resolve_chart_version` over the JSON file the buildspec wrote `helm search` into."""
    path = Path(search_path)
    try:
        search = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as problem:
        raise ChartVersionUnresolved(
            f"the `helm search` output for {chart_ref!r} could not be read from {path}: "
            f"{problem}. Without it no chart version can be resolved, and the lane does not "
            "install an unresolved one."
        ) from problem
    return resolve_chart_version(
        chart_ref=chart_ref, application_version=application_version, search=search
    )


# A top-level `key: value` line of a `Chart.yaml`, as `helm show chart` prints it. Top-level only
# (no leading space), so a `version:` nested under `dependencies:` is never read as the chart's.
_TOP_LEVEL = re.compile(r"^(?P<key>[A-Za-z]+):\s*['\"]?(?P<value>[^'\"#\s]+)['\"]?\s*(?:#.*)?$")


def _chart_yaml_fields(text: str) -> dict[str, str]:
    """`version` and `appVersion` out of a `Chart.yaml`, stdlib only.

    >>> _chart_yaml_fields('name: karpenter\\nversion: 1.15.0\\nappVersion: 1.15.0\\n')
    {'name': 'karpenter', 'version': '1.15.0', 'appVersion': '1.15.0'}
    >>> _chart_yaml_fields('version: 2.0.0\\ndependencies:\\n  - name: x\\n    version: 9.9.9\\n')
    {'version': '2.0.0'}
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        found = _TOP_LEVEL.match(line)
        if found and found["key"] not in fields:
            fields[found["key"]] = found["value"]
    return fields


def resolve_oci_chart_version(*, chart_ref: str, application_version: str, chart_yaml: str) -> str:
    """The chart version of an OCI chart that ships the approved application version.

    AN OCI REGISTRY HAS NO `index.yaml` TO SEARCH, so the lane cannot list every chart and pick
    the one whose `appVersion` matches, as `resolve_chart_version` does for a `helm repo`. What it
    can do is ask for ONE chart version and read its `Chart.yaml`. The buildspec asks for the
    chart version EQUAL to the application version (bare, then `v`-prefixed) -- the convention
    OCI-published charts such as karpenter follow -- and this confirms the `Chart.yaml` it got
    really ships the approved release. Any other answer is a refusal, never a guess: a chart whose
    numbering differs from its application's is reported, and nothing is installed.

    >>> resolve_oci_chart_version(
    ...     chart_ref="oci://public.ecr.aws/karpenter/karpenter",
    ...     application_version="1.15.0",
    ...     chart_yaml="name: karpenter\\nversion: 1.15.0\\nappVersion: 1.15.0\\n",
    ... )
    '1.15.0'
    >>> resolve_oci_chart_version(
    ...     chart_ref="oci://r/x", application_version="1.15.0", chart_yaml=""
    ... )
    Traceback (most recent call last):
    ...
    eks_addon_readiness.upgrade_lane.chart_version.ChartVersionUnresolved: the OCI chart ...
    """
    if not application_version.strip():
        raise ChartVersionUnresolved(
            f"no target version reached the lane for {chart_ref!r}, so there is nothing to resolve."
        )
    fields = _chart_yaml_fields(chart_yaml)
    version = fields.get("version", "")
    app = fields.get("appVersion", "")
    if not version:
        raise ChartVersionUnresolved(
            f"the OCI chart {chart_ref!r} has no chart version matching application version "
            f"{application_version!r} (neither {_bare(application_version)!r} nor "
            f"'v{_bare(application_version)}' could be pulled). This lane resolves an OCI chart "
            "only where the chart version equals the application version; nothing is installed."
        )
    if _bare(app) != _bare(application_version):
        raise ChartVersionUnresolved(
            f"OCI chart {chart_ref!r} version {version!r} ships application version {app!r}, not "
            f"the approved {application_version!r}. Nothing is installed from a chart that does "
            "not package the approved release."
        )
    return version


def resolve_oci_from_file(*, chart_ref: str, application_version: str, chart_yaml_path: str) -> str:
    """`resolve_oci_chart_version` over the file the buildspec wrote `helm show chart` into."""
    try:
        text = Path(chart_yaml_path).read_text(encoding="utf-8")
    except OSError:
        text = ""
    return resolve_oci_chart_version(
        chart_ref=chart_ref, application_version=application_version, chart_yaml=text
    )
