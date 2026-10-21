"""A staleness mark must stop withholding once the fact it names has been replaced.

THE DEFECT, observed on a live component across three runs. The sweep marked a fact stale --
correctly: its component had released. A person was emailed, approved the re-research, and the
approval handler wrote a NEW fact at a NEW revision. Nothing touched the mark. `suppresses`
compared nothing, so the mark went on withholding the replacement it had never seen:

    MarkedFactStore.find -> None   (the mark suppresses)
    gap ledger           -> researchable   (a withheld fact is indistinguishable from none)
    research             -> a candidate    (the lazy acquisition path fires)
    approval             -> an email       (to the same person, about the same component)
    approval             -> a new fact, and still nothing touched the mark

-- and round again. Every sweep in that window ended `marked 0, outcome_already_marked 1`: the
sweep saw a mark in force and skipped the fact before reading upstream, so it could not even
discover that the replacement was fine.

WHAT THIS FILE PROTECTS, in the order the fix depends on it:

1. THE LOOP-BREAKER, which is a comparison and nothing else. A mark suppresses a fact only
   while the store still holds the revision the mark names. No write, no table, no grant --
   so it holds even in an environment where every write fails.
2. THE FALL-THROUGH. With the mark spent, the sweep stops reporting ALREADY_MARKED and judges
   the replacement on its own comparison: re-marked with ITS revision recorded if it is also
   behind a release, UNMOVED and usable if it is not.
3. THE EXCLUSION. A REVOCATION is not a staleness mark and does not become spendable: the
   defect that produced a fact may be present in its replacement, so a revoked component stays
   withheld however fresh the new fact is.
4. THE HOUSEKEEPING, and its independence. The spent mark is retired by overwriting its row,
   which needs only the put the sweep role already holds. A refused retirement must not fail
   the run and must not resurrect the loop.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eks_addon_readiness.knowledge import ComponentKnowledge
from eks_addon_readiness.research import freshness as rules
from eks_addon_readiness.research.marks import FactMark, MarkKind, MarkLedger
from eks_addon_readiness.stages import freshness as handler_module
from eks_addon_readiness.storage.marks import DynamoDbMarkStore, MarkedFactStore

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
REPO = "example/repo"
URL = f"https://raw.githubusercontent.com/{REPO}/main/README.md"
# The release that marked the first revision. A fact read before it is behind; one read after
# it is not, so one upstream answer decides a fact and its replacement differently.
RELEASE = {REPO: {"tag": "v2.21.0", "published_at": (NOW - timedelta(days=3)).isoformat()}}
BUCKET = "sweep-retire-bucket"
DIGEST = "sha256:" + "ab" * 32


def _fact(revision: str, refreshed: datetime, component: str = "a-component") -> ComponentKnowledge:
    citation = {
        "url": URL,
        "title": "compatibility table in the component's own source",
        "source_revision": "main",
        "retrieved_at": refreshed.isoformat(),
    }
    return ComponentKnowledge.model_validate(
        {
            "knowledge_base_revision": revision,
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


# The fact the mark was raised against: read BEFORE the release, so marking it was right.
MARKED = _fact("a-component-researched-09-20", NOW - timedelta(days=6))
# What a human approved afterwards: read AFTER the release, so nothing is behind any more.
APPROVED = _fact("a-component-researched-09-26", NOW - timedelta(hours=8))
# A second replacement that is STILL behind, so the re-judgement can be seen to be a judgement.
STILL_BEHIND = _fact("a-component-researched-09-22", NOW - timedelta(days=4))


def _marked_ledger() -> MarkLedger:
    """The ledger the live table holds: one staleness mark, naming the replaced revision."""
    result = rules.sweep(
        (MARKED,), MarkLedger(), NOW, resolver=rules.StaticUpstreamResolver(RELEASE)
    )
    assert result.expired == ("a-component",), "fixture: the first revision must be marked"
    return result.ledger


class _Store:
    """A raw fact store holding exactly one revision of one fact."""

    def __init__(self, fact: ComponentKnowledge) -> None:
        self._fact = fact

    def find(self, identity: str) -> ComponentKnowledge | None:
        return self._fact if identity.strip() == self._fact.canonical_id else None

    def entries(self) -> tuple[ComponentKnowledge, ...]:
        return (self._fact,)

    def digest(self) -> str:
        return "sha256:" + "0" * 64

    def __len__(self) -> int:
        return 1


class TestTheMarkOnlyWithholdsTheRevisionItNames:
    """Part 1, which is a comparison and needs no write of any kind."""

    def test_the_replacement_answers_through_the_store_every_stage_reads(self) -> None:
        """THE RED TEST. Against the pre-fix `suppresses` this returns None forever."""
        store = MarkedFactStore(_Store(APPROVED), _marked_ledger())

        found = store.find("a-component")

        assert found is not None
        assert found.knowledge_base_revision == APPROVED.knowledge_base_revision
        assert store.suppressed == ()

    def test_the_revision_the_mark_names_is_still_withheld(self) -> None:
        """The mark is not weakened, only scoped: it still withholds what it was raised on."""
        store = MarkedFactStore(_Store(MARKED), _marked_ledger())

        assert store.find("a-component") is None
        assert store.suppressed[0][1].kind is MarkKind.STALE

    def test_the_gap_ledger_stops_reporting_the_component_researchable(self) -> None:
        """The consequence the loop actually ran on, read out of the frozen Stage 3 module."""
        from eks_addon_readiness.facts.gaps import _entry_for

        class _Component:
            workload_ref = "deploy/a-component"
            identity = "a-component"
            version = "1.0.2"
            assessment_priority = 1

        withheld = _entry_for(
            _Component(),
            insights={},
            store=MarkedFactStore(_Store(MARKED), _marked_ledger()),
            store_readable=True,
        )
        answered = _entry_for(
            _Component(),
            insights={},
            store=MarkedFactStore(_Store(APPROVED), _marked_ledger()),
            store_readable=True,
        )

        assert str(withheld.status) == "FACT_MISSING"
        assert str(withheld.detail_code) == "NO_ENTRY"
        assert str(answered.status) != "FACT_MISSING"

    def test_an_unanswerable_comparison_keeps_withholding(self) -> None:
        """A mark that recorded no revision cannot be shown spent, so it stays in force.

        Withholding is the safe answer to an undecidable question: the alternative would let a
        mark be discarded on missing evidence, which is how a genuinely stale fact starts
        answering again.
        """
        ledger = MarkLedger(
            marks=(
                FactMark(
                    component_id="a-component",
                    version_series="*",
                    kind=MarkKind.STALE,
                    marked_at=NOW,
                    marked_by="freshness-sweep",
                    reason="marked before the mark recorded which revision it was about",
                ),
            )
        )

        assert ledger.suppresses("a-component", fact_revision="anything") is not None
        assert MarkedFactStore(_Store(APPROVED), ledger).find("a-component") is None

    def test_a_revocation_withholds_every_revision(self) -> None:
        """Deliberate, and stated in `retire_marks`: the defect may survive the re-research."""
        ledger = MarkLedger(
            marks=(
                rules.revoke_mark(
                    "a-component",
                    "produced by logic since found defective",
                    NOW,
                    actor="an-operator",
                    fact=MARKED,
                ),
            )
        )

        assert ledger.suppresses("a-component", fact_revision="anything-at-all") is not None
        assert MarkedFactStore(_Store(APPROVED), ledger).find("a-component") is None

    def test_breaking_the_loop_writes_nothing(self) -> None:
        """Asserted over the call, because "no write" is the property that makes part 1 stand.

        `suppresses` is reached from the read path in three stages and from the sweep. If it
        needed a write -- a retirement, a table, a grant -- then an environment where the write
        fails would keep the loop, which is exactly the failure being fixed.
        """
        import inspect

        source = inspect.getsource(MarkLedger.suppresses)

        assert "save" not in source
        assert "put_item" not in source
        assert list(inspect.signature(MarkLedger.suppresses).parameters) == [
            "self",
            "component_id",
            "fact_revision",
        ]


class TestTheSweepThenJudgesTheReplacementOnItsOwnMerits:
    """The fall-through the loop-breaker opens, and what the sweep does with it."""

    def test_a_replacement_that_is_still_behind_is_re_marked_with_its_own_revision(
        self,
    ) -> None:
        result = rules.sweep(
            (STILL_BEHIND,),
            _marked_ledger(),
            NOW,
            resolver=rules.StaticUpstreamResolver(RELEASE),
        )

        assert result.expired == ("a-component",)
        # The new mark names the revision the store holds NOW, so it is spendable in turn --
        # which is what stops the fix from trading one permanent mark for another.
        assert result.new_marks[0].fact_revision == STILL_BEHIND.knowledge_base_revision
        # Upstream WAS read: the spent mark no longer short-circuits the visit.
        assert result.visits[0].existing_mark is None
        assert result.visits[0].drift is not None
        assert result.visits[0].drift.repository == REPO

    def test_a_replacement_nothing_was_released_past_comes_back_unmoved(self) -> None:
        result = rules.sweep(
            (APPROVED,),
            _marked_ledger(),
            NOW,
            resolver=rules.StaticUpstreamResolver(RELEASE),
        )
        document = rules.sweep_document(
            result,
            mode="scheduled",
            swept_at=NOW,
            store_digest=DIGEST,
            marks_before=_marked_ledger(),
        )

        assert result.expired == ()
        assert document.findings[0].outcome is rules.SweepOutcome.UNMOVED
        # The outcome that reported the loop three times running must be gone.
        assert document.counts().get("outcome_already_marked") is None
        assert MarkedFactStore(_Store(APPROVED), result.ledger).find("a-component") is not None

    def test_a_mark_that_still_names_the_stored_fact_is_still_skipped(self) -> None:
        """Idempotence is unchanged: a mark in force still stops the upstream read."""
        result = rules.sweep(
            (MARKED,),
            _marked_ledger(),
            NOW,
            resolver=rules.StaticUpstreamResolver(RELEASE),
        )
        document = rules.sweep_document(
            result,
            mode="scheduled",
            swept_at=NOW,
            store_digest=DIGEST,
            marks_before=_marked_ledger(),
        )

        assert result.new_marks == ()
        assert result.retired == ()
        assert document.findings[0].outcome is rules.SweepOutcome.ALREADY_MARKED


class TestTheSpentMarkIsRetiredByOverwritingItsRow:
    """Part 2: housekeeping, reportable, and deliberately not load-bearing."""

    def test_the_sweep_retires_the_spent_mark_and_reports_it(self) -> None:
        before = _marked_ledger()
        result = rules.sweep(
            (APPROVED,), before, NOW, resolver=rules.StaticUpstreamResolver(RELEASE)
        )
        document = rules.sweep_document(
            result,
            mode="scheduled",
            swept_at=NOW,
            store_digest=DIGEST,
            marks_before=before,
        )

        assert result.retired == ("a-component",)
        assert document.retired == ("a-component",)
        assert document.counts()["retired"] == 1
        # Reportable rather than silent: the reason names both revisions.
        stamp = result.retired_marks[0].retirement
        assert stamp is not None
        assert MARKED.knowledge_base_revision in stamp.reason
        assert APPROVED.knowledge_base_revision in stamp.reason
        assert stamp.retired_by == "freshness-sweep"

    def test_the_mark_is_kept_and_stops_being_in_force(self) -> None:
        before = _marked_ledger()
        result = rules.sweep(
            (APPROVED,), before, NOW, resolver=rules.StaticUpstreamResolver(RELEASE)
        )

        # Kept: the audit trail is the reason marks exist instead of deletions.
        assert len(result.ledger.marks) == len(before.marks)
        assert result.ledger.marks[0].retirement is not None
        # Not in force: no revision of any fact is withheld by it, not even the one it names.
        assert result.ledger.suppresses("a-component") is None
        assert (
            result.ledger.suppresses(
                "a-component", fact_revision=MARKED.knowledge_base_revision
            )
            is None
        )
        assert len(result.ledger) == 0
        # A write is a write: the store digest moves, so no two runs cite one digest and
        # answer differently.
        assert result.ledger.digest() != before.digest()

    def test_retirement_overwrites_the_row_rather_than_needing_a_delete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reason this needs no IAM change: the stamp lands at the mark's own key."""
        spent = _marked_ledger().marks[0]
        retired = spent.retired(NOW, by="freshness-sweep", reason="its fact was replaced")

        assert retired.row_key == spent.row_key

        puts: list[dict[str, Any]] = []

        class _Table:
            def put_item(self, Item: dict[str, Any]) -> None:
                puts.append(Item)

        from eks_addon_readiness.storage import marks as storage_marks

        monkeypatch.setattr(storage_marks, "_table", lambda name: _Table())
        DynamoDbMarkStore("a-table").save(MarkLedger(marks=(retired,)))

        # ONE row, at the coordinates the in-force mark occupied, now carrying the retirement:
        # a put, not a delete.
        assert len(puts) == 1
        assert puts[0]["component_id"] == spent.component_id
        assert puts[0]["version_series"] == f"{spent.version_series}#{spent.kind}"
        assert "retirement" in puts[0]["mark"]

    def test_a_re_mark_replaces_the_spent_row_instead_of_colliding_with_it(self) -> None:
        """Both marks want `component#*#STALE`, and only one row exists at those coordinates.

        Left to iteration order the outcome would depend on where each mark landed in the
        ledger. The sweep collapses it: the fresh mark takes the row, and the component is
        reported as marked rather than as both marked and retired.
        """
        result = rules.sweep(
            (STILL_BEHIND,),
            _marked_ledger(),
            NOW,
            resolver=rules.StaticUpstreamResolver(RELEASE),
        )

        rows = [m for m in result.ledger.marks if m.component_id == "a-component"]
        assert len(rows) == 1
        assert rows[0].retirement is None
        assert rows[0].fact_revision == STILL_BEHIND.knowledge_base_revision
        assert result.retired == ()
        assert result.expired == ("a-component",)

    def test_retire_marks_leaves_a_revocation_alone(self) -> None:
        revoked = rules.revoke_mark(
            "a-component", "wrong, and its logic may still be", NOW, actor="an-operator"
        )
        ledger = MarkLedger(marks=(revoked, *_marked_ledger().marks))

        retired = rules.retire_marks(
            ledger, "a-component", (MarkKind.STALE,), NOW, reason="its fact was replaced"
        )

        assert retired.suppresses("a-component").kind is MarkKind.REVOKED
        assert [m.retirement is None for m in retired.marks] == [True, False]


class _MarkStore:
    """A mark store double that starts from a given ledger."""

    def __init__(self, ledger: MarkLedger) -> None:
        self.ledger = ledger
        self.saved: list[MarkLedger] = []

    def load(self) -> MarkLedger:
        return self.ledger

    def save(self, ledger: MarkLedger) -> None:
        self.saved.append(ledger)
        self.ledger = ledger


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The handler with its fact store, mark store and object storage replaced.

    The fact store holds the APPROVED revision -- the state the live table is in: a fresh fact
    in the store and a mark naming the revision it replaced.
    """
    marks = _MarkStore(_marked_ledger())
    written: dict[str, str] = {}
    facts: dict[str, ComponentKnowledge] = {"fact": APPROVED}

    def _write(uri: str, document: Any) -> str:
        written[uri] = document.canonical_json()
        return uri

    monkeypatch.setattr(handler_module, "build_raw_fact_store", lambda: _Store(facts["fact"]))
    monkeypatch.setattr(handler_module, "build_mark_store", lambda: marks)
    monkeypatch.setattr(handler_module, "write_document", _write)
    return {"marks": marks, "written": written, "facts": facts}


def _event() -> dict[str, Any]:
    return {"bucket": BUCKET, "components": ["a-component"], "pinned_releases": RELEASE}


class TestTheRetirementIsNotLoadBearing:
    """If the housekeeping write never lands, the loop must still be broken."""

    def test_a_refused_retirement_is_reported_and_does_not_fail_the_run(
        self, wired: dict[str, Any]
    ) -> None:
        def _refuse(ledger: Any) -> None:
            raise RuntimeError("ProvisionedThroughputExceededException")

        wired["marks"].save = _refuse

        result = handler_module.handler(_event())

        # The run continues, and says so loudly rather than swallowing it.
        assert result["retired"] == []
        assert "ProvisionedThroughputExceeded" in result["marks_not_retired"]
        # The document cites the ledger that is ACTUALLY in the table, not the one that failed.
        assert result["store_digest_moves"] is False
        # And the loop stays broken with nothing written at all: the mark names a revision the
        # store no longer holds, so it withholds nothing either way.
        assert (
            MarkedFactStore(_Store(APPROVED), wired["marks"].load()).find("a-component")
            is not None
        )

    def test_a_refused_NEW_mark_still_fails_the_run(self, wired: dict[str, Any]) -> None:
        """The other side of the line, unchanged: a mark is the mechanism, not a report.

        The same refused write, over a fact that IS behind a release: the sweep found drift it
        cannot record, so letting the run proceed would answer from knowledge it knows is
        stale. That is a wrong answer rather than a missing one, and it must raise.
        """

        def _refuse(ledger: Any) -> None:
            raise RuntimeError("marks table is unwritable")

        wired["facts"]["fact"] = STILL_BEHIND
        wired["marks"].save = _refuse

        with pytest.raises(RuntimeError, match="unwritable"):
            handler_module.handler(_event())

    def test_a_successful_retirement_reaches_the_payload(self, wired: dict[str, Any]) -> None:
        result = handler_module.handler(_event())

        assert result["retired"] == ["a-component"]
        assert "marks_not_retired" not in result
        assert result["sweep_counts"]["retired"] == 1
        assert result["marks_in_force"] == 0
        assert "a-component" in result["retirement_reasons"]
        assert wired["marks"].saved, "the retirement must be written"
        assert wired["marks"].ledger.marks[0].retirement is not None


def test_the_sweep_role_can_overwrite_a_mark_and_cannot_delete_one() -> None:
    """Retirement was shaped by this grant, so the grant is asserted rather than assumed.

    An overwrite needs `dynamodb:PutItem`, which the sweep already holds. `DeleteItem` would
    need an operator to approve a new permission, which is why nothing here deletes a row.
    """
    text = (
        Path(__file__).resolve().parent.parent / "infra/terraform/modules/platform/sweep.tf"
    ).read_text()
    grant = text.split('sid    = "ReadAndWriteMarks"', 1)[1].split("}", 1)[0]

    assert '"dynamodb:PutItem"' in grant
    assert "DeleteItem" not in text
