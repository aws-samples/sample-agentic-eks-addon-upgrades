"""Synthetic component knowledge, for the tests that used to read the packaged store.

WHY THIS FILE EXISTS. This sample ships no file fact store: the deployed store is DynamoDB,
which starts empty, so every tracked component starts FACT_MISSING and a fresh run exercises
the research and approval legs rather than short-circuiting on a fact nobody approved.
`FileFactStore` -- the local fallback when `FACT_STORE_TABLE` is unset -- reports the
`UNAVAILABLE` digest when its directory is missing.

So a test that needs a recorded fact AUTHORS one here instead of reading a tree. That is
strictly better than what it replaces: the fixture no longer silently changes meaning when
the packaged data changes, and the properties the tests actually depend on are stated below
instead of being implied by a data file.

TWO PROPERTIES ARE LOAD-BEARING. Both were previously inherited from the packaged entry by
accident of its contents, and tests fail in confusing ways if they drift:

  SERIES       The rules cover `0.7.x`, `0.8.x` and `0.9.x` and DELIBERATELY NOT `0.6.x`,
               while the captured snapshot runs metrics-server `0.6.4`. Stage 3 asks "do we
               hold a fact for the INSTALLED series", so a fact keyed to another series must
               leave the gap open. That is the whole subject of
               `test_a_fact_for_another_series_does_not_answer_the_question`, and it is why
               `_fact_covering` has to re-point the rule rather than just re-key the entry.

  REFRESHED_AT July, which is genuinely stale against the September reference the loop tests
               use. A just-learned fact is fresh by definition, so helpers that simulate a
               research pass must stamp their own time; reusing this one would have the
               freshness gate immediately re-queue everything the fleet just learned.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from eks_addon_readiness.knowledge import ComponentKnowledge

# One evidence citation, reused wherever the shape is all that matters. A promoted fact
# carries a pinned `source_revision` per citation, so the tests exercise that shape rather
# than a bare URL.
_README = "https://github.com/kubernetes-sigs/metrics-server/blob/80cd152e29806a22bd7f760526f915f96a3b8efc/README.md"
_README_REVISION = "80cd152e29806a22bd7f760526f915f96a3b8efc"


def _evidence(title: str, url: str = _README, revision: str = _README_REVISION) -> dict:
    return [
        {
            "url": url,
            "title": title,
            "source_revision": revision,
            "retrieved_at": "2026-07-18T00:00:00Z",
        }
    ]


def _rule(series: str, minimum: str) -> dict:
    return {
        "component_version_series": series,
        "kubernetes_versions": {"minimum_inclusive": minimum, "maximum_exclusive": None},
        "served_api_versions": ["metrics.k8s.io/v1beta1"],
        "evidence": _evidence("Official compatibility matrix"),
    }


# The recorded fact, as data. Kept close to the shape a real promoted research result has:
# aliases, per-citation pinned revisions, releases, preflight and verification checks.
SYNTHETIC_ENTRY_PAYLOAD: dict = {
    "schema_version": "1.0",
    "knowledge_base_revision": "metrics-server-2026-07-18.1",
    "canonical_id": "metrics-server",
    "aliases": ["metrics-server", "kubernetes-metrics-server"],
    "canonical_repository": "https://github.com/kubernetes-sigs/metrics-server",
    "official_documentation": [_README],
    "chart_repository": "https://artifacthub.io/packages/helm/metrics-server/metrics-server",
    "chart_name": "metrics-server",
    "chart_mappings": [
        {
            "chart_version": "3.13.1",
            "application_version": "0.8.1",
            "evidence": _evidence(
                "Official chart 3.13.1 metadata",
                "https://github.com/kubernetes-sigs/metrics-server/blob/842dcc8c42f22cc19f10c7d5f2d32a9f4b6013b0/charts/metrics-server/Chart.yaml",
                "842dcc8c42f22cc19f10c7d5f2d32a9f4b6013b0",
            ),
        }
    ],
    # SERIES: 0.7/0.8/0.9 and deliberately not 0.6 -- see the module docstring.
    "compatibility_rules": [
        _rule("0.7.x", "1.27"),
        _rule("0.8.x", "1.31"),
        _rule("0.9.x", "1.34"),
    ],
    "releases": [
        {
            "version": "0.8.1",
            "released_at": "2026-01-29T00:00:00Z",
            "source_revision": "9ce65a47f9262e19e482c259ace815f01135a898",
            "evidence": _evidence(
                "Official v0.8.1 release",
                "https://github.com/kubernetes-sigs/metrics-server/releases/tag/v0.8.1",
                "9ce65a47f9262e19e482c259ace815f01135a898",
            ),
        },
        {
            "version": "0.9.0",
            "released_at": "2026-07-13T00:00:00Z",
            "source_revision": "2a7c4b2c7d46552ff47f4aeaa3a735c582587ecd",
            "evidence": _evidence(
                "Official v0.9.0 release",
                "https://github.com/kubernetes-sigs/metrics-server/releases/tag/v0.9.0",
                "2a7c4b2c7d46552ff47f4aeaa3a735c582587ecd",
            ),
        },
    ],
    "breaking_changes": [],
    "removed_api_versions": [],
    "dependencies": [],
    "preflight_checks": [
        {
            "statement_id": "aggregation-layer-enabled",
            "statement": "The Kubernetes API aggregation layer must be enabled.",
            "evidence": _evidence("Official requirements"),
        }
    ],
    "verification_checks": [
        {
            "check_id": "metrics-api-data",
            "description": "The Metrics API returns node and pod resource metrics.",
            "required": True,
            "evidence": _evidence("Official Metrics API behavior"),
        }
    ],
    "default_upgrade_strategy": "SEQUENTIAL_MINOR",
    # REFRESHED_AT: July, deliberately stale against a September reference.
    "refreshed_at": "2026-07-18T00:00:00Z",
    "evidence_confidence": "HIGH",
}


@pytest.fixture
def synthetic_entry_payload() -> dict:
    """A fresh deep copy, so a test that mutates the payload cannot leak into the next."""
    return copy.deepcopy(SYNTHETIC_ENTRY_PAYLOAD)


@pytest.fixture
def recorded_entry(synthetic_entry_payload: dict) -> ComponentKnowledge:
    """Recorded knowledge the test authors itself, replacing the packaged entry.

    Named for what it stands in for: a fact that a research pass promoted and a human
    approved, not a stub. Validated through the real model, so a schema change still breaks
    this fixture rather than letting the tests drift away from the contract.
    """
    return ComponentKnowledge.model_validate(synthetic_entry_payload)


@pytest.fixture
def one_entry_store_root(tmp_path: Path, synthetic_entry_payload: dict) -> Path:
    """A `FileFactStore` root holding exactly one entry, laid out as the real store is.

    `<root>/<component>/entry.json`, which is the layout `FileFactStore` globs for. Used by
    the tests that need the file-store read path to actually find something now that the
    packaged tree is empty.
    """
    root = tmp_path / "knowledge" / "components"
    component = root / str(synthetic_entry_payload["canonical_id"])
    component.mkdir(parents=True)
    (component / "entry.json").write_text(
        json.dumps(synthetic_entry_payload), encoding="utf-8"
    )
    return root
