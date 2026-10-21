"""Marks must not be a silent no-op in the stages that read the fact store.

THE DEFECT THIS PINS, measured on the deployed system: `FACT_MARK_TABLE` was set on the
three functions in `infra/terraform/modules/research` and on none of the stage functions, so
`build_mark_store()` returned `EmptyMarkStore` inside `3 Gaps`, `4 Decide` and `5 Plan`.
Every mark the sweep wrote was therefore invisible exactly where it was supposed to act:
a fact marked stale kept answering, and the store digest the ledger cited did not move.

Nothing failed. That is what makes it worth a test -- the mechanism reported success while
having no effect, which is the failure mode this codebase keeps naming.

TWO LAYERS, BOTH NEEDED, AND THEIR LIMITS STATED. The behavioural tests below prove the
code path is real and env-driven. The configuration tests read the Terraform source, which
is a weaker check than reading a deployed function -- it cannot prove an apply happened.
The deployed side is proved by the plan captured beside this change and by
`aws lambda get-function-configuration` after it; asserting it here would make the suite
depend on AWS, which the rest of it deliberately does not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from eks_addon_readiness.facts import FileFactStore
from eks_addon_readiness.research.marks import FactMark, MarkKind, MarkLedger
from eks_addon_readiness.storage import build_fact_store
from eks_addon_readiness.storage import facts as facts_module
from eks_addon_readiness.storage.marks import (
    DynamoDbMarkStore,
    EmptyMarkStore,
    build_mark_store,
)

REPO = Path(__file__).resolve().parent.parent
LAMBDAS_TF = REPO / "infra" / "terraform" / "modules" / "platform" / "lambdas.tf"

# The three stages that read the fact store, and therefore the three that must see marks.
FACT_STORE_READERS = ("gaps", "decide", "plan")
# The two that never read it, and so must NOT be handed the table.
NON_READERS = ("scan", "inventory")


class TestTheEnvironmentDecidesWhetherMarksExist:
    def test_the_table_variable_selects_the_dynamodb_mark_store(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No AWS call: the adapter resolves its table lazily, on first read."""
        monkeypatch.setenv("FACT_MARK_TABLE", "eks-addon-upgrades-fact-marks")
        monkeypatch.delenv("FACT_MARK_PATH", raising=False)

        assert isinstance(build_mark_store(), DynamoDbMarkStore)

    def test_without_it_marks_are_a_silent_no_op(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deployed defect, stated as a property so it cannot come back unnoticed."""
        monkeypatch.delenv("FACT_MARK_TABLE", raising=False)
        monkeypatch.delenv("FACT_MARK_PATH", raising=False)

        store = build_mark_store()
        assert isinstance(store, EmptyMarkStore)
        assert len(store.load()) == 0

    def test_a_mark_withholds_a_fact_from_the_stage_read_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, one_entry_store_root: Path
    ) -> None:
        """End to end through `build_fact_store`, the call every stage makes.

        Uses the file mark store rather than DynamoDB, because what is under test is the
        composition -- stage -> build_fact_store -> marked -> build_mark_store -- and not
        which adapter is behind the port.

        THE STORE IS POINTED AT AN AUTHORED ENTRY, and it has to be. No file store ships
        with this sample, so the default file store holds nothing, and `find`
        returning None would then prove nothing at all -- it would be the empty store
        answering, not the mark withholding. That is the vacuous pass this suite exists to
        prevent, so the test supplies the fact it then expects to be withheld. Only the raw
        store's ROOT is redirected: the env-driven file-vs-DynamoDB branch, the `marked`
        decoration and the mark store all stay exactly as a stage gets them.
        """
        monkeypatch.setattr(
            facts_module, "FileFactStore", lambda: FileFactStore(one_entry_store_root)
        )
        marks = tmp_path / "marks.json"
        marks.write_text(
            MarkLedger(
                marks=(
                    FactMark(
                        component_id="metrics-server",
                        version_series="*",
                        kind=MarkKind.STALE,
                        marked_at="2026-09-12T12:00:00+00:00",
                        marked_by="freshness-sweep",
                        reason=(
                            "the source has moved: the fact is pinned at "
                            "4062beeed6ef996ba3ff7164967edafd3470e2e4 and upstream is now "
                            "a1418f94a060043cacb43cc8f0aeb7f4ac1eb94d"
                        ),
                    ),
                )
            ).model_dump_json(),
            encoding="utf-8",
        )
        monkeypatch.delenv("FACT_STORE_TABLE", raising=False)
        monkeypatch.delenv("FACT_MARK_TABLE", raising=False)
        monkeypatch.setenv("FACT_MARK_PATH", str(marks))

        # The premise, asserted rather than assumed: the fact IS readable without the mark.
        # Without this line the two assertions below would also pass against an empty store,
        # which is what made this test fail loudly when the seeded fact was removed.
        assert facts_module.build_raw_fact_store().find("metrics-server") is not None

        store = build_fact_store()

        assert store.find("metrics-server") is None
        assert store.suppressed and store.suppressed[0][0] == "metrics-server"

    def test_the_mark_moves_the_store_digest_the_ledger_cites(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, one_entry_store_root: Path
    ) -> None:
        """Otherwise two runs either side of a mark would cite one digest and disagree."""
        monkeypatch.setattr(
            facts_module, "FileFactStore", lambda: FileFactStore(one_entry_store_root)
        )
        monkeypatch.delenv("FACT_STORE_TABLE", raising=False)
        monkeypatch.delenv("FACT_MARK_TABLE", raising=False)
        monkeypatch.delenv("FACT_MARK_PATH", raising=False)
        undecorated = build_fact_store().digest()

        marks = tmp_path / "marks.json"
        marks.write_text(
            MarkLedger(
                marks=(
                    FactMark(
                        component_id="metrics-server",
                        version_series="*",
                        kind=MarkKind.STALE,
                        marked_at="2026-09-12T12:00:00+00:00",
                        marked_by="freshness-sweep",
                        reason="the source has moved",
                    ),
                )
            ).model_dump_json(),
            encoding="utf-8",
        )
        monkeypatch.setenv("FACT_MARK_PATH", str(marks))

        assert build_fact_store().digest() != undecorated


@pytest.fixture(scope="module")
def lambdas_tf() -> str:
    """The Terraform that owns the five stage Lambdas, as text."""
    return LAMBDAS_TF.read_text(encoding="utf-8")


class TestTheStageFunctionsAreHandedTheMarkTable:
    """Configuration, in the module that owns the three stage Lambdas."""

    def test_the_mark_table_reaches_the_environment_of_the_readers_only(
        self, lambdas_tf: str
    ) -> None:
        assert "FACT_MARK_TABLE" in lambdas_tf
        # Set from the same list that decides who may read the fact store, so the two
        # cannot drift apart: a stage that reads facts sees the marks against them.
        assert re.search(
            r"contains\(local\.fact_store_readers, each\.key\)\s*\?\s*\{\s*"
            r"FACT_MARK_TABLE\s*=",
            lambdas_tf,
        ), "FACT_MARK_TABLE must be conditioned on local.fact_store_readers"

    def test_the_reader_list_is_still_the_three_document_stages(
        self, lambdas_tf: str
    ) -> None:
        match = re.search(r"fact_store_readers\s*=\s*\[([^\]]*)\]", lambdas_tf)
        assert match is not None
        declared = tuple(re.findall(r'"([a-z]+)"', match.group(1)))
        assert declared == FACT_STORE_READERS

    def test_the_grant_on_the_mark_table_is_read_only(self, lambdas_tf: str) -> None:
        """A stage may see that a fact was withdrawn. No stage may withdraw one."""
        statement = re.search(
            r'sid\s*=\s*"ReadMarks".*?resources\s*=\s*\[[^\]]*\]',
            lambdas_tf,
            re.DOTALL,
        )
        assert statement is not None, "no ReadMarks statement in the stage policy"
        body = statement.group(0)
        for forbidden in ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem"):
            assert forbidden not in body, f"the stage grant must not include {forbidden}"

    def test_the_non_readers_are_not_handed_the_table(self, lambdas_tf: str) -> None:
        """scan and inventory never open the fact store, so they get neither table."""
        match = re.search(r"fact_store_readers\s*=\s*\[([^\]]*)\]", lambdas_tf)
        assert match is not None
        for stage in NON_READERS:
            assert f'"{stage}"' not in match.group(1)
