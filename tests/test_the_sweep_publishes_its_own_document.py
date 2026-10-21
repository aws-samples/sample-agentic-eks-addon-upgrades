"""The freshness sweep publishes its own document into the digest chain.

WHY THE DOCUMENT EARNS ITS PLACE. Marks alone answer "does this fact answer?" and nothing
else. The question a person asks of a report is *why* -- which repository was asked, which
release it reported, when that release was published, when the fact itself was read, and when
the comparison was made. A mark carries that in a prose `reason`; a document carries it as
evidence with a digest, at a stable key, beside every other document the run produced.

WHY THIS ONE DOCUMENT MAY BE TIME-DEPENDENT, when no other may be. Every stage document is
a pure function of already-fetched evidence, and that purity is what makes replay work. The
sweep is the opposite by construction: it exists to read the network and report what moved.
Its content is honestly a function of the instant it ran, which is exactly why the sweep may
not live inside Stage 3 or Stage 7 -- their outputs would inherit that dependency while
still citing digests that promise reproducibility.

RED FIRST. Every assertion here fails against a sweep that wrote marks and returned a dict.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from eks_addon_readiness.knowledge import ComponentKnowledge
from eks_addon_readiness.research import freshness as rules
from eks_addon_readiness.research.marks import MarkKind, MarkLedger
from eks_addon_readiness.stages import freshness as handler_module
from eks_addon_readiness.storage import keys

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
REPO = "example/repo"
URL = f"https://raw.githubusercontent.com/{REPO}/main/README.md"
# The one upstream release every fixture here is decided against. A fact read BEFORE it has
# been superseded; a fact read after it has not.
RELEASE_TAG = "v1.1.0"
RELEASE_PUBLISHED = NOW - timedelta(days=30)
FROZEN_RELEASE = {
    REPO: {"tag": RELEASE_TAG, "published_at": RELEASE_PUBLISHED.isoformat()}
}
DIGEST = "sha256:" + "ab" * 32
BUCKET = "sweep-doc-bucket"


def _fact(component: str, revision: str, refreshed: datetime) -> ComponentKnowledge:
    citation = {
        "url": URL,
        "title": "compatibility table in the component's own source",
        "source_revision": revision,
        "retrieved_at": refreshed.isoformat(),
    }
    return ComponentKnowledge.model_validate(
        {
            "knowledge_base_revision": f"{component}-researched-2026-09-12",
            "canonical_id": component,
            "canonical_repository": f"https://github.com/{REPO}",
            "official_documentation": [f"https://github.com/{REPO}"],
            "compatibility_rules": [
                {
                    "component_version_series": "1.0.x",
                    "kubernetes_versions": {
                        "minimum_inclusive": "1.30",
                        "maximum_exclusive": "1.40",
                    },
                    "evidence": [citation],
                }
            ],
            "releases": [
                {
                    "version": "1.0.0",
                    "released_at": "2020-01-01T00:00:00Z",
                    "source_revision": revision,
                    "evidence": [citation],
                }
            ],
            "verification_checks": [
                {
                    "check_id": "serves-its-api",
                    "description": "The component serves its documented API.",
                    "evidence": [citation],
                }
            ],
            "default_upgrade_strategy": "IN_PLACE",
            "refreshed_at": refreshed.isoformat(),
            "evidence_confidence": "HIGH",
        }
    )


class _Store:
    """A minimal raw fact store: enough for `store_entries` and `digest`."""

    def __init__(self, facts: tuple[ComponentKnowledge, ...]) -> None:
        self._facts = facts

    def find(self, identity: str) -> ComponentKnowledge | None:
        return next((f for f in self._facts if f.canonical_id == identity.strip()), None)

    def entries(self) -> tuple[ComponentKnowledge, ...]:
        return self._facts

    def digest(self) -> str:
        import hashlib

        hasher = hashlib.sha256()
        for fact in sorted(self._facts, key=lambda f: f.canonical_id):
            hasher.update(fact.canonical_json().encode("utf-8"))
        return "sha256:" + hasher.hexdigest()

    def __len__(self) -> int:
        return len(self._facts)


class _MarkStore:
    def __init__(self) -> None:
        self.ledger = MarkLedger()
        self.saved: list[MarkLedger] = []

    def load(self) -> MarkLedger:
        return self.ledger

    def save(self, ledger: MarkLedger) -> None:
        self.saved.append(ledger)
        self.ledger = ledger


def _inventory(*identities: str) -> dict[str, Any]:
    """A Stage 2 inventory document body, as JSON, naming the run's components."""
    return {
        "schema_version": "1.0",
        "stage": "2",
        "cluster": {"name": "sweep-demo", "region": "us-east-1"},
        "kubernetes_version": "1.35",
        "snapshot_digest": DIGEST,
        "snapshot_captured_at": "2026-09-12T06:29:43+00:00",
        "ruleset_digest": DIGEST,
        "components": [
            {
                "workload_ref": f"deploy/{identity}",
                "identity": identity,
                "identity_source": "HELM_RELEASE",
                "identity_detail": "chart name from the release labels",
                "version": "1.0.2",
                "version_source": "HELM_RELEASE",
                "confidence": "HIGH",
                "classification": "SELF_MANAGED",
                "classification_reason": "installed by Helm, not present in the add-on catalogue",
                "install_method": "HELM",
                "assessment_priority": "high",
            }
            for identity in identities
        ],
    }


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The handler with its three ports replaced: facts, marks, and object storage."""
    written: dict[str, str] = {}
    inventories: dict[str, dict[str, Any]] = {}
    marks = _MarkStore()
    # `drifted-one` was read BEFORE the frozen release and `unmoved-one` after it, so one
    # upstream answer decides the two differently. Both are the same age: age decides nothing.
    store = _Store(
        (
            _fact("drifted-one", "main", RELEASE_PUBLISHED - timedelta(days=26)),
            _fact("unmoved-one", "main", RELEASE_PUBLISHED + timedelta(days=1)),
        )
    )

    monkeypatch.setattr(handler_module, "build_raw_fact_store", lambda: store)
    monkeypatch.setattr(handler_module, "build_mark_store", lambda: marks)

    def _write(uri: str, document: Any) -> str:
        written[uri] = document.canonical_json()
        return uri

    def _read(uri: str, contract: Any) -> Any:
        return contract.model_validate(inventories[uri])

    monkeypatch.setattr(handler_module, "write_document", _write)
    monkeypatch.setattr(handler_module, "read_document", _read)
    return {
        "written": written,
        "inventories": inventories,
        "marks": marks,
        "store": store,
    }


def _preflight_event(wired: dict[str, Any], *identities: str) -> dict[str, Any]:
    uri = f"s3://{BUCKET}/inventories/cluster=sweep-demo/date=2026-09-12/one.json"
    wired["inventories"][uri] = _inventory(*identities)
    return {
        "bucket": BUCKET,
        "cluster_name": "sweep-demo",
        "snapshot_uri": f"s3://{BUCKET}/snapshots/cluster=sweep-demo/date=2026-09-12/s.json",
        "snapshot_digest": DIGEST,
        "snapshot_captured_at": "2026-09-12T06:29:43+00:00",
        "inventory_uri": uri,
        "inventory_digest": DIGEST,
        # Freezes what upstream reports, so the whole sweep is reproducible with no network.
        "pinned_releases": FROZEN_RELEASE,
    }


class TestTheKeyIsDigestStampedLikeEveryOtherStage:
    def test_the_preflight_key_carries_store_rules_and_its_own_digest(self) -> None:
        key = keys.sweep_key(
            "pre-flight", "prod-1", DIGEST, DIGEST, DIGEST, "2026-09-12T12:00:00+00:00"
        )
        assert key == (
            "sweeps/mode=pre-flight/cluster=prod-1/date=2026-09-12/"
            "store-abababababab.rules-abababababab.sweep-abababababab.json"
        )

    def test_the_scheduled_key_names_no_cluster(self) -> None:
        """A fleet-wide sweep is about no cluster, so the path must not invent one."""
        key = keys.sweep_key(
            "scheduled", None, DIGEST, DIGEST, DIGEST, "2026-09-12T12:00:00+00:00"
        )
        assert key.startswith("sweeps/mode=scheduled/date=2026-09-12/")
        assert "cluster=" not in key

    def test_the_key_lives_under_its_own_prefix(self) -> None:
        """Its own prefix, so no stage's IAM grant reaches it and a listing is readable."""
        key = keys.sweep_key(
            "pre-flight", "p1", DIGEST, DIGEST, DIGEST, "2026-09-12T12:00:00+00:00"
        )
        assert key.startswith("sweeps/")

    def test_two_sweeps_that_disagree_are_two_objects(self) -> None:
        """The document's own digest is in the key, so a new outcome cannot overwrite an old.

        Same reasoning as `dispatch_key` carrying the loop bound: an object that quietly
        replaces the evidence of a different answer destroys the before-and-after.
        """
        other = "sha256:" + "cd" * 32
        first = keys.sweep_key(
            "pre-flight", "p1", DIGEST, DIGEST, DIGEST, "2026-09-12T12:00:00+00:00"
        )
        second = keys.sweep_key(
            "pre-flight", "p1", DIGEST, DIGEST, other, "2026-09-12T12:00:00+00:00"
        )
        assert first != second


class TestTheDocumentIsEvidenceRatherThanASummary:
    def test_a_finding_names_the_whole_comparison(self) -> None:
        refreshed = RELEASE_PUBLISHED - timedelta(days=26)
        result = rules.sweep(
            (_fact("drifted-one", "main", refreshed),),
            MarkLedger(),
            NOW,
            resolver=rules.StaticUpstreamResolver(FROZEN_RELEASE),
        )
        document = rules.sweep_document(
            result,
            mode="scheduled",
            swept_at=NOW,
            store_digest=DIGEST,
            marks_before=MarkLedger(),
        )

        assert len(document.findings) == 1
        finding = document.findings[0]
        assert finding.component_id == "drifted-one"
        assert finding.repository == REPO
        assert finding.release_tag == RELEASE_TAG
        assert finding.release_published_at == RELEASE_PUBLISHED.isoformat()
        assert finding.fact_refreshed_at == refreshed.isoformat()
        assert finding.outcome is rules.SweepOutcome.MARKED_STALE
        assert document.marked == ("drifted-one",)

    def test_an_unmoved_fact_is_recorded_too(self) -> None:
        """"Why is this fact NOT stale" is a question the document must also answer."""
        result = rules.sweep(
            (_fact("unmoved-one", "main", RELEASE_PUBLISHED + timedelta(days=1)),),
            MarkLedger(),
            NOW,
            resolver=rules.StaticUpstreamResolver(FROZEN_RELEASE),
        )
        document = rules.sweep_document(
            result,
            mode="scheduled",
            swept_at=NOW,
            store_digest=DIGEST,
            marks_before=MarkLedger(),
        )

        assert document.marked == ()
        assert document.findings[0].outcome is rules.SweepOutcome.UNMOVED
        assert document.findings[0].repository == REPO
        assert document.findings[0].release_tag == RELEASE_TAG

    def test_the_document_names_no_time_window(self) -> None:
        result = rules.sweep(
            (_fact("any-one", "main", RELEASE_PUBLISHED + timedelta(days=1)),),
            MarkLedger(),
            NOW,
            resolver=rules.StaticUpstreamResolver(FROZEN_RELEASE),
        )
        document = rules.sweep_document(
            result,
            mode="scheduled",
            swept_at=NOW,
            store_digest=DIGEST,
            marks_before=MarkLedger(),
        )
        body = document.canonical_json().casefold()

        assert document.staleness == "upstream_release"
        assert "ttl" not in body
        assert "hours" not in body

    def test_an_inconsistent_document_cannot_be_constructed(self) -> None:
        """The same discipline every other document applies: refuse to be evidence of a lie."""
        with pytest.raises(ValueError, match="marked"):
            rules.SweepDocument(
                mode="scheduled",
                swept_at=NOW.isoformat(),
                store_digest=DIGEST,
                marks_digest_before=DIGEST,
                marks_digest_after=DIGEST,
                ruleset_digest=DIGEST,
                findings=(),
                marked=("a-component-no-finding-mentions",),
            )

    def test_a_preflight_document_must_say_what_scoped_it(self) -> None:
        with pytest.raises(ValueError, match="pre-flight"):
            rules.SweepDocument(
                mode="pre-flight",
                swept_at=NOW.isoformat(),
                store_digest=DIGEST,
                marks_digest_before=DIGEST,
                marks_digest_after=DIGEST,
                ruleset_digest=DIGEST,
            )

    def test_the_document_carries_its_own_ruleset_digest(self) -> None:
        assert rules.ruleset_digest().startswith("sha256:")
        result = rules.sweep((), MarkLedger(), NOW, resolver=rules.StaticUpstreamResolver({}))
        document = rules.sweep_document(
            result,
            mode="scheduled",
            swept_at=NOW,
            store_digest=DIGEST,
            marks_before=MarkLedger(),
        )
        assert document.ruleset_digest == rules.ruleset_digest()


class TestTheHandlerPublishesIt:
    def test_the_handler_writes_one_document_and_returns_its_uri_and_digest(
        self, wired: dict[str, Any]
    ) -> None:
        result = handler_module.handler(_preflight_event(wired, "drifted-one"))

        assert list(wired["written"]) == [result["sweep_uri"]]
        assert result["sweep_uri"].startswith(f"s3://{BUCKET}/sweeps/mode=pre-flight/")
        assert result["sweep_digest"].startswith("sha256:")
        assert result["sweep_ruleset_digest"] == rules.ruleset_digest()

    def test_the_written_bytes_validate_back_into_the_contract(
        self, wired: dict[str, Any]
    ) -> None:
        result = handler_module.handler(_preflight_event(wired, "drifted-one"))
        body = wired["written"][result["sweep_uri"]]

        document = rules.SweepDocument.model_validate_json(body)
        assert document.digest() == result["sweep_digest"]
        assert document.marked == ("drifted-one",)
        assert document.cluster is not None and document.cluster.name == "sweep-demo"

    def test_the_document_digest_is_over_the_bytes_that_were_stored(
        self, wired: dict[str, Any]
    ) -> None:
        result = handler_module.handler(_preflight_event(wired, "drifted-one"))
        body = wired["written"][result["sweep_uri"]]
        # Canonical JSON: sorted keys, no insignificant whitespace, like every document.
        assert body == json.dumps(json.loads(body), sort_keys=True, separators=(",", ":"))

    def test_the_payload_survives_the_state_so_stage_three_still_has_its_inputs(
        self, wired: dict[str, Any]
    ) -> None:
        """The sweep sits BETWEEN 2 Inventory and 3 Gaps, so it must pass the payload on.

        A state that returned only its own result would strip `snapshot_uri` and
        `inventory_uri` and Stage 3 would fail with a KeyError -- the same integration
        defect the `6 Research` Map state was introduced to fix.
        """
        event = _preflight_event(wired, "drifted-one")
        result = handler_module.handler(event)

        for field in ("snapshot_uri", "inventory_uri", "snapshot_digest", "bucket"):
            assert result[field] == event[field]

    def test_the_marks_are_written_before_the_document(self, wired: dict[str, Any]) -> None:
        """Marks first: the document reports what is in force, so it must be in force."""
        handler_module.handler(_preflight_event(wired, "drifted-one"))

        assert wired["marks"].ledger.suppresses("drifted-one") is not None
        assert wired["marks"].ledger.suppresses("drifted-one").kind is MarkKind.STALE

    def test_a_failed_write_does_not_fail_the_run(
        self, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE DEFECT THE REVIEW CAUGHT. The sweep is a STATE now, so a raise fails the run.

        The line this asserts: the sweep fails on anything that compromises the MARKS, and
        never on anything that only compromises the REPORT of them. By the time the document
        is written the marks are already in force, so a failed write costs a reader an
        explanation and costs the run nothing -- while raising would turn "the sweep's
        report could not be stored" into "this cluster gets no readiness report at all".

        It is also the failure most likely to happen: `infra/terraform/modules/research` owns the sweep's
        role and has no state file, so the `s3:PutObject` grant on `sweeps/*` cannot be
        applied yet. Under the raising version, wiring the state in before that grant landed
        would fail EVERY run at the Sweep state in a pipeline that worked the day before.
        """

        class _AccessDenied(Exception):
            pass

        def _deny(uri: str, document: Any) -> str:
            raise _AccessDenied(
                "An error occurred (AccessDenied) when calling the PutObject operation"
            )

        monkeypatch.setattr(handler_module, "write_document", _deny)
        event = _preflight_event(wired, "drifted-one")

        result = handler_module.handler(event)

        # The run continues, with its payload intact for Stage 3.
        assert result["inventory_uri"] == event["inventory_uri"]
        # The MARKS took effect regardless -- the mechanism ran, only its report is missing.
        assert wired["marks"].ledger.suppresses("drifted-one") is not None
        assert result["expired"] == ["drifted-one"]
        # And the failure is loud rather than swallowed: named, with the exception in it.
        assert result["sweep_uri"] is None
        assert "AccessDenied" in result["sweep_not_published"]
        assert "_AccessDenied" in result["sweep_not_published"]
        assert "sweeps/*" in result["sweep_not_published"]
        # The document still exists and is still digested; only the store rejected it.
        assert result["sweep_digest"].startswith("sha256:")

    def test_a_failed_mark_write_DOES_fail_the_run(self, wired: dict[str, Any]) -> None:
        """The other side of the same line, so it is a rule rather than one lenient case.

        A sweep that found drift and cannot record it must NOT let the run proceed: the
        ledger would then answer from a fact the sweep knows is stale, which is a wrong
        answer rather than a missing one. Failing loudly is the only safe response.
        """

        def _refuse(ledger: Any) -> None:
            raise RuntimeError("marks table is unwritable")

        wired["marks"].save = _refuse

        with pytest.raises(RuntimeError, match="unwritable"):
            handler_module.handler(_preflight_event(wired, "drifted-one"))

    def test_no_bucket_means_no_document_and_the_result_says_so(
        self, wired: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DOCUMENT_BUCKET", raising=False)
        result = handler_module.handler(
            {"components": ["drifted-one"], "pinned_releases": FROZEN_RELEASE}
        )

        assert wired["written"] == {}
        assert result["sweep_uri"] is None
        assert "bucket" in result["sweep_not_published"]
        # The document still exists and is still digested; only the store was absent.
        assert result["sweep_digest"].startswith("sha256:")


class TestTheRunScopesTheSweep:
    def test_an_inventory_scopes_the_sweep_to_the_components_in_this_run(
        self, wired: dict[str, Any]
    ) -> None:
        result = handler_module.handler(_preflight_event(wired, "drifted-one"))

        assert result["mode"] == "pre-flight"
        assert result["visited"] == ["drifted-one"]
        assert "unmoved-one" not in result["visited"]

    def test_the_document_cites_the_inventory_that_scoped_it(
        self, wired: dict[str, Any]
    ) -> None:
        event = _preflight_event(wired, "drifted-one")
        result = handler_module.handler(event)
        document = rules.SweepDocument.model_validate_json(
            wired["written"][result["sweep_uri"]]
        )

        assert document.scope == ("drifted-one",)
        assert document.inventory_digest is not None

    def test_an_inventory_with_no_identified_components_sweeps_nothing(
        self, wired: dict[str, Any]
    ) -> None:
        """An empty scope must visit NOTHING, not everything.

        The whole-store sweep is one `or` away from an empty scope, and getting it wrong
        would resolve every fact's upstream on a run that asked about none of them.
        """
        result = handler_module.handler(_preflight_event(wired))

        assert result["visited"] == []
        assert result["expired"] == []


def test_no_pipeline_stage_imports_the_sweep() -> None:
    """THE HARD CONSTRAINT, machine-checked.

    A network read inside a stage that publishes a digest-keyed document would make that
    document depend on GitHub's state at the instant it ran: two runs would cite the same
    digests while disagreeing. The sweep is a separate function with a separate role, and
    no stage may reach it.

    Asserted over the import graph rather than the text, so a docstring may still explain
    where staleness is decided -- which the stages need to say, precisely because the answer
    is "not here".
    """
    import ast

    from eks_addon_readiness.stages import decide, gaps, plan, resolve

    for stage in (gaps, decide, plan, resolve):
        tree = ast.parse(inspect.getsource(stage))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(f"{node.module or ''}.{','.join(a.name for a in node.names)}")
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        offenders = [name for name in imported if "freshness" in name or "sweep" in name]
        assert offenders == [], f"{stage.__name__} imports the sweep: {offenders}"
