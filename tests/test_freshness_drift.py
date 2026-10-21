"""Staleness must be decided by what upstream did, not by elapsed time.

The project's decision, made explicitly: a fact goes stale when its COMPONENT SHIPPED
SOMETHING, not when a clock has passed. A time expiry was rejected for two reasons that both
bite here -- with every fact human-approved a TTL means re-approving every fact every day
forever, and it makes the same snapshot produce different verdicts on different days, which is
exactly what the digest chain exists to prevent.

These tests are written to FAIL against a time-expiry implementation. The first one is the red
test: a fact whose component has released nothing since it was read must not be stale, no
matter how old it is.

THE MECHANISM ITSELF lives in `test_the_sweep_asks_github_for_a_release.py`, including the
metrics-server shape that motivated it. What this file protects is the surrounding property:
`now` reaches no decision, the mark reaches Stage 3 through the store adapter, and the mark
table stays separate from the fact table.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eks_addon_readiness.knowledge import ComponentKnowledge
from eks_addon_readiness.research.freshness import (
    DriftResult,
    StaticUpstreamResolver,
    is_stale,
    sweep,
)
from eks_addon_readiness.research.marks import MarkKind, MarkLedger

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
REPO = "example/repo"
URL = f"https://raw.githubusercontent.com/{REPO}/main/README.md"


def _release(tag: str, published_at: datetime) -> dict[str, str]:
    """One frozen upstream release, in the shape the event's `pinned_releases` carries."""
    return {"tag": tag, "published_at": published_at.isoformat()}


def _fact(component: str, refreshed: datetime) -> ComponentKnowledge:
    """One fact on `example/repo`, read at `refreshed`.

    The citation still records a `source_revision`, because facts do -- but nothing here
    compares it. What decides staleness is `refreshed_at` against the repository's newest
    release, which is why a fact pinned to a branch name is now perfectly decidable.
    """
    citation = {
        "url": URL,
        "title": "compatibility table in the component's own source",
        "source_revision": "main",
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
                    "source_revision": "main",
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


class TestAnUpstreamReleaseDecidesStaleness:
    """The mechanism, not the number."""

    def test_a_quiet_repository_leaves_a_fact_fresh_however_old(self) -> None:
        """THE RED TEST. An ancient fact whose component has not released is NOT stale.

        This is the case a time expiry gets wrong and the whole reason the TTL was dropped:
        the fact is 60 days old, and the newest release upstream reports predates it, so
        nothing about the world has changed and there is nothing to re-research.
        """
        refreshed = NOW - timedelta(days=60)
        fact = _fact("unmoved", refreshed)
        resolver = StaticUpstreamResolver(
            {REPO: _release("v1.0.0", refreshed - timedelta(days=200))}
        )

        result = is_stale(fact, resolver)

        assert isinstance(result, DriftResult)
        assert result.stale is False
        assert result.comparable is True
        assert result.repository == REPO
        assert result.release_tag == "v1.0.0"
        assert "nothing to re-research" in result.reason

    def test_a_release_after_the_read_marks_a_fact_minutes_old(self) -> None:
        """The mirror image: a fact minutes old whose component HAS released is stale."""
        refreshed = NOW - timedelta(minutes=5)
        fact = _fact("moved", refreshed)
        published = refreshed + timedelta(minutes=1)
        resolver = StaticUpstreamResolver({REPO: _release("v1.1.0", published)})

        result = is_stale(fact, resolver)

        assert result.stale is True
        assert result.repository == REPO
        assert result.release_tag == "v1.1.0"
        assert result.release_published_at == published
        assert result.fact_refreshed_at == refreshed
        assert published.isoformat() in result.reason
        assert refreshed.isoformat() in result.reason

    def test_an_unreadable_upstream_is_not_stale(self) -> None:
        """An upstream that could not be read is not evidence that anything was released.

        Marking on a failed lookup would withdraw every fact whenever GitHub had a bad
        minute, which is the opposite of what a freshness check is for. A repository that has
        simply never published a release lands here too, and that is equally correct.
        """
        fact = _fact("unknown", NOW - timedelta(days=60))
        resolver = StaticUpstreamResolver({})

        result = is_stale(fact, resolver)

        assert result.stale is False
        assert result.comparable is False
        assert result.repository == REPO
        assert result.release_tag is None
        assert "Not stale" in result.reason


class TestSweepUsesTheRelease:
    """The sweep's report must not mention a time window at all."""

    def test_sweep_marks_only_the_fact_the_release_is_newer_than(self) -> None:
        published = NOW - timedelta(days=1)
        # One fact read AFTER the release and one read before it, on the same repository, so
        # the same single upstream answer decides both -- differently.
        unmoved = _fact("unmoved", published + timedelta(hours=1))
        moved = _fact("moved", published - timedelta(hours=1))
        resolver = StaticUpstreamResolver({REPO: _release("v1.1.0", published)})

        result = sweep((unmoved, moved), MarkLedger(), NOW, resolver=resolver)

        assert result.expired == ("moved",)
        assert set(result.visited) == {"moved", "unmoved"}

    def test_sweep_marks_the_fact_its_component_released_past(self) -> None:
        refreshed = NOW - timedelta(minutes=1)
        stale = _fact("drifted", refreshed)
        resolver = StaticUpstreamResolver(
            {REPO: _release("v1.1.0", refreshed + timedelta(seconds=30))}
        )

        result = sweep((stale,), MarkLedger(), NOW, resolver=resolver)

        assert result.expired == ("drifted",)
        mark = result.new_marks[0]
        assert mark.kind is MarkKind.STALE
        assert REPO in mark.reason and "v1.1.0" in mark.reason
        # The mechanism is an upstream release. Elapsed time must not appear in the
        # justification, and neither must a window.
        assert "hour" not in mark.reason.lower()
        assert "ttl" not in mark.reason.lower()

    def test_sweep_report_carries_no_time_window(self) -> None:
        """`ttl_hours` must be gone from the sweep's own summary."""
        refreshed = NOW - timedelta(days=99)
        fact = _fact("any", refreshed)
        resolver = StaticUpstreamResolver(
            {REPO: _release("v1.0.0", refreshed - timedelta(days=1))}
        )
        result = sweep((fact,), MarkLedger(), NOW, resolver=resolver)

        assert "ttl" not in result.summary().lower()
        assert "hour" not in result.summary().lower()

    def test_a_stale_fact_does_not_block_the_pipeline(self) -> None:
        """Stage 3 reports a marked fact exactly as it reports a missing one.

        The frozen module does the reporting; the mark only withholds the fact. This is why
        the check may not move into the read path.
        """
        from eks_addon_readiness.facts.gaps import _entry_for
        from eks_addon_readiness.storage.marks import MarkedFactStore

        refreshed = NOW - timedelta(minutes=1)
        drifted = _fact("drifted", refreshed)
        resolver = StaticUpstreamResolver(
            {REPO: _release("v1.1.0", refreshed + timedelta(seconds=30))}
        )
        result = sweep((drifted,), MarkLedger(), NOW, resolver=resolver)

        class _Store:
            def find(self, identity: str):
                return drifted if identity.strip() == "drifted" else None

            def entries(self):
                return (drifted,)

            def digest(self) -> str:
                return "sha256:" + "0" * 64

            def __len__(self) -> int:
                return 1

        store = MarkedFactStore(_Store(), result.ledger)
        assert store.find("drifted") is None

        class _Component:
            workload_ref = "deploy/x"
            identity = "drifted"
            version = "1.0.2"
            assessment_priority = 1

        entry = _entry_for(_Component(), insights={}, store=store, store_readable=True)
        assert str(entry.status) == "FACT_MISSING"
        assert str(entry.detail_code) == "NO_ENTRY"

    def test_marks_live_apart_from_the_fact_store(self) -> None:
        """The mark table is separate, so the read path stays a pure function of the store."""
        from eks_addon_readiness.storage import marks as marks_mod

        assert hasattr(marks_mod, "DynamoDbMarkStore")
        # The mark store is addressed by its own env var, never the fact table's.
        import inspect

        src = inspect.getsource(marks_mod.build_mark_store)
        assert "FACT_MARK_TABLE" in src
        assert "FACT_STORE_TABLE" not in src


@pytest.mark.parametrize("component", ["keda", "cert-manager"])
def test_protected_components_are_not_special_cased(component: str) -> None:
    """No component name may change what the sweep does.

    The user's test matrix depends on keda and cert-manager staying FACT_MISSING, and the
    right way to guarantee that is that the sweep has no opinion about any component name.

    Asserted over the CODE with docstrings stripped, not over the file's text -- the same
    distinction `test_no_pipeline_stage_imports_the_sweep` draws by walking the import graph.
    The module explains its scope by naming `cert-manager.io` and `keda.sh` as the
    documentation hosts it excludes, and prose about a component is not a branch on one.
    """
    import ast
    import inspect

    from eks_addon_readiness.research import freshness

    tree = ast.parse(inspect.getsource(freshness))
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        first = node.body[0] if node.body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = node.body[1:] or [ast.Pass()]

    assert component not in ast.unparse(tree)


class TestTheClockNeverReachesTheDecision:
    """`now` is passed to the sweep and must not reach the verdict.

    This is the property that makes an instant comparison different in kind from a TTL: the
    two instants compared are both records of past events -- when the fact was read, and when
    upstream published -- so the answer does not change because a day passed.
    """

    def test_the_verdict_is_the_same_at_any_sweep_instant(self) -> None:
        refreshed = NOW - timedelta(days=10)
        fact = _fact("steady", refreshed)
        resolver = StaticUpstreamResolver(
            {REPO: _release("v1.0.0", refreshed - timedelta(days=5))}
        )

        early = sweep((fact,), MarkLedger(), NOW, resolver=resolver)
        late = sweep((fact,), MarkLedger(), NOW + timedelta(days=365), resolver=resolver)

        assert early.expired == late.expired == ()
        assert early.visits[0].drift is not None
        assert early.visits[0].drift.reason == late.visits[0].drift.reason

    def test_is_stale_takes_no_instant_at_all(self) -> None:
        """Asserted over the signature, because a `now` parameter is how a TTL creeps back."""
        import inspect

        assert list(inspect.signature(is_stale).parameters) == ["fact", "resolver"]
