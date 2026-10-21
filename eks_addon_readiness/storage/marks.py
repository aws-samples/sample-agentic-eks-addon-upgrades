"""The mark store, and the store decorator that makes a marked fact stop answering.

This is where the freshness design meets the frozen spine. Two constraints shaped it:

**`facts/gaps.py` and `facts/store.py` are digest-frozen.** Their source text is hashed into
Stage 3's ruleset digest, which the ledger, assessment and plan digests all cover, so an
edit -- even a comment -- moves four documents' digests. Stage 3 therefore cannot learn about
marks by being changed.

**It does not need to.** Stage 3 asks the `FactStore` port one question: `find(identity)`,
whose contract already says `None` means "no fact for this component, which is the normal
case this stage exists to report". A decorator that returns `None` for a marked fact makes a
stale fact indistinguishable from an absent one *to Stage 3's logic*, which is exactly the
intent -- "reports that component as researchable exactly as it already does for a missing
fact" -- while the mark itself stays fully visible to a reader through `suppressed`.

**The digest is the load-bearing part.** `digest()` folds the mark ledger's digest in, so a
fact going stale moves `store_digest`. Stage 3 cites that digest, Stage 4 refuses to decide
against a store whose digest differs from the ledger's, and every document key contains it.
So marks are covered by the reproducibility machinery that already exists: two runs either
side of a mark are visibly different documents, not one document that quietly changed. The
alternative -- suppressing facts without moving the digest -- would let two runs cite the
same store digest while answering differently, which is worse than either behaviour alone.

Marks live in their own DynamoDB table rather than as an attribute on the fact item. A mark
is written by a sweep and by a human revoking a fact, while facts are written by Stage 6;
separate tables mean the sweep needs no write permission on facts at all, so a sweep bug
cannot corrupt one.
"""

from __future__ import annotations

import hashlib
import os
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from ..facts import STORE_UNAVAILABLE, FactStore
from ..knowledge import ComponentKnowledge
from ..research.marks import FactMark, MarkLedger


class MarkStore(Protocol):
    """Read and write the marks in force. Small on purpose: a sweep needs nothing else."""

    def load(self) -> MarkLedger:
        ...

    def save(self, ledger: MarkLedger) -> None:
        ...


class EmptyMarkStore:
    """No marks, and none can be written.

    The configuration for an environment with no mark table: every fact answers, and the
    store digest is unchanged from an undecorated store. Named rather than implicit so a
    deployment without freshness is a deliberate choice a reader can see, not an accident of
    a missing variable.
    """

    def load(self) -> MarkLedger:
        return MarkLedger()

    def save(self, ledger: MarkLedger) -> None:
        raise RuntimeError(
            "no mark store is configured (set FACT_MARK_TABLE or FACT_MARK_PATH); "
            "refusing to discard marks silently"
        )


class FileMarkStore:
    """Marks in one JSON file. The local and test configuration."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def load(self) -> MarkLedger:
        if not self._path.is_file():
            return MarkLedger()
        try:
            return MarkLedger.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            # A corrupt mark file must not be read as "no marks in force", which would
            # silently un-expire every fact. Refusing is the only safe answer.
            raise RuntimeError(
                f"mark file {self._path} could not be read as a MarkLedger: "
                f"{type(exc).__name__}: {exc}. Refusing to proceed as though no marks "
                "were in force."
            ) from exc

    def save(self, ledger: MarkLedger) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(ledger.model_dump_json(indent=2), encoding="utf-8")


@cache
def _table(name: str) -> Any:
    import boto3

    return boto3.resource("dynamodb").Table(name)


class DynamoDbMarkStore:
    """Marks in one DynamoDB table, keyed like the fact table.

    `component_id` (HASH) + `version_series` (RANGE), plus `mark` holding the `FactMark`
    JSON. Deliberately the same key shape as the fact table so an operator reads one schema:
    the mark for a fact is at the same coordinates as the fact.
    """

    def __init__(self, table_name: str) -> None:
        self._table_name = table_name

    def load(self) -> MarkLedger:
        table = _table(self._table_name)
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {}
        while True:
            page = table.scan(**kwargs)
            items.extend(page.get("Items") or [])
            key = page.get("LastEvaluatedKey")
            if not key:
                break
            kwargs["ExclusiveStartKey"] = key

        marks: list[FactMark] = []
        for item in items:
            raw = item.get("mark")
            if not isinstance(raw, str):
                raise RuntimeError(
                    f"mark item {item.get('component_id')!r} has no 'mark' JSON string; "
                    "refusing to proceed as though it were not in force"
                )
            marks.append(FactMark.model_validate_json(raw))
        return MarkLedger(marks=tuple(marks))

    def save(self, ledger: MarkLedger) -> None:
        """Put every mark in the ledger. A PUT ONLY -- nothing here removes a row.

        Which is why a spent mark is RETIRED rather than dropped: a mark missing from the
        ledger is not deleted from the table, it is simply not written, so the next `load`
        reads the old row back and the component is withheld again. A retirement is a put at
        the same coordinates (`version_series#kind` is unchanged by the stamp), so it
        overwrites the row that was withholding the fact -- and the role needs no
        `dynamodb:DeleteItem`, which is a grant an operator would have to approve.

        The ledger is iterated in order, so when a fresh mark and a retirement share a row the
        later one wins. `research.freshness.sweep` collapses that collision before it gets
        here rather than relying on the order.
        """
        table = _table(self._table_name)
        for mark in ledger.marks:
            table.put_item(
                Item={
                    "component_id": mark.component_id,
                    "version_series": f"{mark.version_series}#{mark.kind}",
                    "mark": mark.model_dump_json(),
                }
            )


class MarkedFactStore:
    """A `FactStore` that withholds marked facts and folds marks into its digest.

    Wraps any `FactStore`. Delegates everything; changes two things:

    - `find` returns `None` for a component whose fact is marked, so Stage 3's existing
      missing-fact path reports it as researchable with no change to Stage 3.
    - `digest` covers the marks, so a mark is a visible change to the store rather than an
      invisible one.

    **A mark only withholds the revision it was raised against.** `find` passes the found
    fact's `knowledge_base_revision` to `suppresses`, so a staleness mark whose fact has since
    been re-researched and approved withholds nothing and the fresh fact answers. Without that
    argument a mark withheld every later revision forever, and the component was sent back to
    a human every run -- see `research/marks.py`. A REVOCATION still withholds every revision,
    deliberately.

    `suppressed` exposes what was withheld and why, so the withholding is reportable. A
    stage that reports "no fact" without being able to say "because it expired 3h ago"
    would be accurate and useless.
    """

    def __init__(self, inner: FactStore, ledger: MarkLedger) -> None:
        self._inner = inner
        self._ledger = ledger
        self._suppressed: dict[str, FactMark] = {}

    @property
    def ledger(self) -> MarkLedger:
        return self._ledger

    @property
    def suppressed(self) -> tuple[tuple[str, FactMark], ...]:
        """Components whose fact was withheld during this store's lifetime, and the mark."""
        return tuple(sorted(self._suppressed.items()))

    def find(self, identity: str) -> ComponentKnowledge | None:
        found = self._inner.find(identity)
        if found is None:
            return None
        # Marks are keyed on the fact's canonical id, so an alias lookup is suppressed too
        # -- a stale fact must not answer under one of its other names.
        #
        # THE REVISION IS PASSED, and that is the whole of the loop-breaker on the read path.
        # A staleness mark is about the revision it was raised against; once a human has
        # approved a replacement, the store holds a different revision and the old mark is
        # about a fact that is no longer here. Without this argument the mark went on
        # withholding the replacement, `find` went on answering None, the gap ledger went on
        # reporting the component researchable, and the same person was emailed to approve the
        # same component every run. `MarkLedger.suppresses` carries the reasoning.
        mark = self._ledger.suppresses(
            found.canonical_id, fact_revision=found.knowledge_base_revision
        )
        if mark is None:
            return found
        self._suppressed[found.canonical_id] = mark
        return None

    def digest(self) -> str:
        """The inner store's digest, combined with the marks in force.

        An unavailable inner store stays unavailable: the sentinel must survive, because
        `facts/gaps.py` distinguishes "no facts" from "facts unknown" by comparing against
        it, and hashing the sentinel into something else would collapse that distinction.

        With no marks the digest is the inner digest unchanged, so adding the decorator to
        an environment with an empty mark ledger moves nothing.
        """
        inner = self._inner.digest()
        if inner == STORE_UNAVAILABLE:
            return STORE_UNAVAILABLE
        if not self._ledger.marks:
            return inner
        hasher = hashlib.sha256()
        hasher.update(inner.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(self._ledger.digest().encode("utf-8"))
        return "sha256:" + hasher.hexdigest()

    @property
    def load_errors(self) -> tuple[str, ...]:
        """Forwarded, so Stage 3 still surfaces an unreadable inner store."""
        return tuple(getattr(self._inner, "load_errors", ()))

    @property
    def readable(self) -> bool:
        return bool(getattr(self._inner, "readable", True))

    def entries(self) -> tuple[ComponentKnowledge, ...]:
        """Every fact in the inner store, marks ignored.

        The sweep needs to see expired facts in order to expire them, so this deliberately
        bypasses suppression. Named distinctly from `find` for that reason. Delegates to
        `facts.store_entries`, so there is one place that knows how to enumerate a store
        and one place that raises when it cannot.
        """
        from .facts import store_entries

        return store_entries(self._inner)

    def __len__(self) -> int:
        return len(self._inner)  # type: ignore[arg-type]


def build_mark_store() -> MarkStore:
    """The mark store this environment is configured for.

    Environment-driven for the same reason the fact store is: every stage and the sweep must
    see the *same* marks, and threading a store through handlers as a parameter invites two
    of them disagreeing.

        FACT_MARK_TABLE set   DynamoDbMarkStore, the deployed path
        FACT_MARK_PATH set    FileMarkStore, local and test
        neither               EmptyMarkStore -- no marks, and none writable
    """
    table = os.environ.get("FACT_MARK_TABLE", "").strip()
    if table:
        return DynamoDbMarkStore(table)
    path = os.environ.get("FACT_MARK_PATH", "").strip()
    if path:
        return FileMarkStore(path)
    return EmptyMarkStore()


def marked(store: FactStore, marks: MarkStore | None = None) -> MarkedFactStore:
    """Wrap a fact store so marked facts stop answering.

    The single composition point. `build_fact_store` in `storage/facts.py` calls this, so
    every stage gets the decoration without any stage knowing about it.
    """
    return MarkedFactStore(store, (marks or build_mark_store()).load())
