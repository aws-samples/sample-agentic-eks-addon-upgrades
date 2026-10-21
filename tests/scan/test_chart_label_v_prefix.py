"""A `v`-prefixed chart version must not end up inside the component's identity.

THE DEFECT THIS PINS. `split_chart_label` anchored the version segment on a digit,
so `cert-manager-v1.13.2` did not match and the whole label was returned as the
chart *name*. `_merge_release` uses that name as the component's `identity`, and
`identity` is the fact-store lookup key -- so cert-manager resolved as
`cert-manager-v1.13.2`, and a fact approved for `cert-manager` could never match it.

Observed on a real run against a cluster carrying `jetstack/cert-manager v1.13.2`:

    identity              version   version_source
    cert-manager-v1.13.2  1.13.2    HELM_CHART_LABEL

A `v` prefix on a chart version is ordinary -- cert-manager publishes every chart
that way -- so this is not an edge case.

WHAT IS DELIBERATELY NOT CHANGED. `inventory/versions.py:strip_chart_version` has
the same blind regex and is digest-frozen: its source text is hashed into Stage 2's
ruleset digest, so editing it would break byte-identical replay against every
document produced so far. It feeds the *per-workload* identity rung, which the
release-level fold overwrites for any Helm-managed workload. The residual case --
a workload carrying a chart label but no Helm release annotation, so never folded --
is covered by `test_unfolded_workload_still_carries_the_frozen_behaviour` below,
which pins the limitation rather than pretending it is gone.
"""

from __future__ import annotations

from eks_addon_readiness.inventory.versions import strip_chart_version
from eks_addon_readiness.scan.helm_labels import derive_helm_releases, split_chart_label


class TestSplitChartLabel:
    """The unit that was wrong."""

    def test_v_prefixed_version_is_split_off_the_name(self) -> None:
        """The regression. Was ('cert-manager-v1.13.2', None)."""
        assert split_chart_label("cert-manager-v1.13.2") == ("cert-manager", "v1.13.2")

    def test_v_prefixed_version_with_prerelease_suffix(self) -> None:
        assert split_chart_label("cert-manager-v1.14.0-beta.1") == (
            "cert-manager",
            "v1.14.0-beta.1",
        )

    def test_plain_numeric_version_still_splits(self) -> None:
        """The behaviour that already worked must not regress."""
        assert split_chart_label("metrics-server-3.11.0") == ("metrics-server", "3.11.0")
        assert split_chart_label("keda-2.17.2") == ("keda", "2.17.2")

    def test_hyphenated_prerelease_still_splits(self) -> None:
        assert split_chart_label("cognitoidentity-chart-0.0.0-non-release-version") == (
            "cognitoidentity-chart",
            "0.0.0-non-release-version",
        )

    def test_no_version_suffix_still_returns_the_whole_label(self) -> None:
        """Returning the label rather than guessing is the right answer here."""
        assert split_chart_label("standalone") == ("standalone", None)

    def test_a_name_ending_in_v_is_not_mistaken_for_a_version(self) -> None:
        """`v` alone is not a version, and a bare `v` segment must not split.

        Guards the obvious over-correction: matching `v` followed by anything would
        turn `my-chart-v` into ('my-chart', 'v').
        """
        assert split_chart_label("my-chart-v") == ("my-chart-v", None)

    def test_a_word_starting_with_v_is_not_a_version(self) -> None:
        """`velero` starts with v but is a name, not a version."""
        assert split_chart_label("backup-velero") == ("backup-velero", None)


class TestIdentityThroughTheRelease:
    """What the defect actually cost: the fact-store lookup key."""

    @staticmethod
    def _workload(chart: str, app_version: str) -> dict:
        return {
            "_kind": "Deployment",
            "name": "cert-manager",
            "namespace": "cert-manager",
            "labels": {
                "helm.sh/chart": chart,
                "app.kubernetes.io/version": app_version,
                "app.kubernetes.io/managed-by": "Helm",
            },
            "annotations": {
                "meta.helm.sh/release-name": "cert-manager",
                "meta.helm.sh/release-namespace": "cert-manager",
            },
        }

    def test_release_chart_name_excludes_the_version(self) -> None:
        """`chart_name` is what `_merge_release` uses as the component identity."""
        releases = derive_helm_releases([self._workload("cert-manager-v1.13.2", "v1.13.2")])
        assert len(releases) == 1
        assert releases[0].chart_name == "cert-manager"
        assert releases[0].chart_version == "v1.13.2"
        assert releases[0].application_version == "v1.13.2"

    def test_the_fact_store_key_a_human_would_author_now_matches(self) -> None:
        """The point of the fix, stated as the thing that was broken.

        A knowledge entry is authored with `canonical_id: cert-manager`. Before the
        fix the identity was `cert-manager-v1.13.2`, so the lookup could never hit
        -- approving a fact would have changed nothing, which is far worse than an
        honest gap because it looks like it should have worked.
        """
        releases = derive_helm_releases([self._workload("cert-manager-v1.13.2", "v1.13.2")])
        assert releases[0].chart_name == "cert-manager"

    def test_metrics_server_release_is_unaffected(self) -> None:
        """The component whose fact IS seeded must not move."""
        workload = self._workload("metrics-server-3.11.0", "0.6.4")
        workload["annotations"]["meta.helm.sh/release-name"] = "metrics-server"
        workload["annotations"]["meta.helm.sh/release-namespace"] = "kube-system"
        releases = derive_helm_releases([workload])
        assert releases[0].chart_name == "metrics-server"
        assert releases[0].chart_version == "3.11.0"
        assert releases[0].application_version == "0.6.4"


class TestFrozenCounterpartIsUnchanged:
    """The half of the defect that cannot be fixed, pinned so it is not forgotten."""

    def test_unfolded_workload_still_carries_the_frozen_behaviour(self) -> None:
        """`strip_chart_version` is digest-frozen and still blind to a `v` prefix.

        Editing it would change Stage 2's ruleset digest and break byte-identical
        replay against every document produced so far. It only reaches a record that
        the release-level fold does not overwrite -- a workload with a chart label
        but no Helm release annotation -- so the exposure is narrow. Asserted rather
        than described, so that if the freeze is ever lifted this test fails and
        says why.
        """
        assert strip_chart_version("cert-manager-v1.13.2") == ("cert-manager-v1.13.2", None)
        # And the cases it does handle, which must keep working.
        assert strip_chart_version("metrics-server-3.11.0") == ("metrics-server", "3.11.0")
