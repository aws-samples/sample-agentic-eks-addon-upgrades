"""Choosing a fact store, and the DynamoDB adapter behind the Stage 3 port.

`facts.FactStore` is the port. It is defined in a digest-frozen module, so it
cannot move — but its adapters can live here, which is where they belong.

Two implementations are selected from the environment:

    FACT_STORE_TABLE set   DynamoDbFactStore, the deployed path
    otherwise              FileFactStore over the packaged `knowledge/` tree

`FileFactStore` is not a fallback for a failed DynamoDB read — that would make a
configuration error look like an empty knowledge base. It is the store when no
table is configured, and nothing else.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC
from functools import cache
from typing import Any, Protocol

from ..facts import FactStore, FileFactStore
from ..knowledge import ComponentKnowledge


def _with_aware_refreshed_at(knowledge: ComponentKnowledge) -> ComponentKnowledge:
    """A stored fact whose `refreshed_at` is guaranteed to carry a UTC offset.

    NORMALISED HERE, AT THE POINT A FACT ENTERS THE SYSTEM, rather than at the point
    something compares it. That is the difference between an invariant and a patch: every
    consumer downstream of `build_fact_store()` can now subtract `refreshed_at` from
    another instant without asking what a writer happened to store.

    THE DEFECT THIS REMOVES. `ComponentKnowledge.refreshed_at` is a bare `datetime` and
    `knowledge.py` is digest-frozen -- pinned by eight frozen rule modules -- so the field
    itself cannot be tightened to an aware type without moving Stage 3's ruleset digest
    and every digest downstream of it. A writer that omits the offset therefore passes
    validation, and Stage 7's freshness gate then raised
    `TypeError: can't subtract offset-naive and offset-aware datetimes`, failing the whole
    execution. Nothing failed while the one seeded fact carried `Z`; the trigger was the
    first fact a future writer stored without an offset, which is what merging Stage 6
    begins doing.

    READING A NAIVE INSTANT AS UTC IS A NORMALISATION, NOT A GUESS. Every producer in this
    system stamps UTC (`datetime.now(UTC)`), the packaged knowledge entry is written with
    `Z`, and the fact table's own sort keys are UTC ISO strings. There is no local-time
    interpretation available for this field to have meant.

    ON THE DIGEST. `store.digest()` hashes each entry's `canonical_json()`, so normalising
    a value that was ALREADY aware changes nothing -- which is why the deployed store
    digest is unmoved by this. It does change the digest for a fact that was stored naive,
    and that is correct: the store now holds a different, well-formed value.
    """
    moment = knowledge.refreshed_at
    if moment.tzinfo is not None:
        return knowledge
    return knowledge.model_copy(update={"refreshed_at": moment.replace(tzinfo=UTC)})


@cache
def _table(name: str) -> Any:
    import boto3

    return boto3.resource("dynamodb").Table(name)


class DynamoDbFactStore:
    """A `FactStore` over one DynamoDB table.

    Loads every entry once on construction, for the same reason `FileFactStore`
    does: `digest()` must cover the whole store, and a lazily-loaded store cannot
    produce a digest without reading everything anyway. At fact-store scale — tens
    of components, not millions — one scan per invocation is the right trade.

    Item shape is a single table so an operator reads one schema, not two:
    `component_id` (HASH), `version_series` (RANGE), and `entry` holding the
    `ComponentKnowledge` JSON.
    """

    def __init__(self, table_name: str) -> None:
        self._table_name = table_name
        self._by_identity: dict[str, ComponentKnowledge] = {}
        self._entries: list[ComponentKnowledge] = []
        self._load_errors: list[str] = []
        self._readable = False
        self._load()

    def _load(self) -> None:
        table = _table(self._table_name)
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {}
        try:
            while True:
                page = table.scan(**kwargs)
                items.extend(page.get("Items") or [])
                key = page.get("LastEvaluatedKey")
                if not key:
                    break
                kwargs["ExclusiveStartKey"] = key
        except Exception as exc:
            # Unreadable is not empty. `digest()` reports STORE_UNAVAILABLE so a
            # ledger cannot claim "no facts" when it means "facts unknown".
            self._load_errors.append(f"{self._table_name}: {type(exc).__name__}: {exc}")
            return

        self._readable = True
        for item in items:
            raw = item.get("entry")
            if not isinstance(raw, str):
                self._load_errors.append(
                    f"{item.get('component_id')!r}: item has no 'entry' JSON string"
                )
                continue
            try:
                self._entries.append(
                    _with_aware_refreshed_at(ComponentKnowledge.model_validate_json(raw))
                )
            except ValueError as exc:
                # Recorded, not raised. One malformed entry must not make every
                # other component unknowable.
                self._load_errors.append(
                    f"{item.get('component_id')!r}: {type(exc).__name__}: {exc}"
                )
        self._index()

    def _index(self) -> None:
        """Canonical ids first, then aliases — the two-pass order matters.

        A component's own canonical id must beat another component's alias for the
        same string, regardless of load order. A single pass gives the win to
        whichever item happened to arrive first.
        """
        for entry in self._entries:
            self._by_identity[entry.canonical_id] = entry
        for entry in self._entries:
            for alias in entry.aliases:
                self._by_identity.setdefault(alias, entry)

    @property
    def load_errors(self) -> tuple[str, ...]:
        return tuple(self._load_errors)

    @property
    def readable(self) -> bool:
        return self._readable

    def entries(self) -> tuple[ComponentKnowledge, ...]:
        """Every loaded entry, in canonical-id order.

        For the freshness sweep, which must see facts a mark is currently withholding in
        order to reason about them. `find` is the read path and honours marks; this is the
        maintenance path and does not.
        """
        return tuple(sorted(self._entries, key=lambda e: e.canonical_id))

    def find(self, identity: str) -> ComponentKnowledge | None:
        return self._by_identity.get(identity.strip())

    def digest(self) -> str:
        """Digest over every entry, ordered by canonical id.

        Byte-compatible with `FileFactStore.digest()` on purpose: the same knowledge
        must produce the same store digest whichever adapter served it, or moving
        from the packaged tree to DynamoDB would move every downstream digest.
        """
        from ..facts import STORE_UNAVAILABLE

        if not self._readable:
            return STORE_UNAVAILABLE
        hasher = hashlib.sha256()
        for entry in sorted(self._entries, key=lambda e: e.canonical_id):
            hasher.update(entry.canonical_id.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(entry.canonical_json().encode("utf-8"))
            hasher.update(b"\x00")
        return "sha256:" + hasher.hexdigest()

    def __len__(self) -> int:
        return len(self._entries)


class StoreNotEnumerable(RuntimeError):
    """Raised when a store's facts cannot be listed, so a sweep must not claim it swept.

    A sweep that silently sees zero facts expires nothing and reports success, which is
    indistinguishable from a store that is genuinely fresh. That is the failure mode this
    project keeps warning about -- it looks like the thing working. So enumeration either
    produces the facts or raises.
    """


def store_entries(store: Any) -> tuple[ComponentKnowledge, ...]:
    """Every fact in a store, for the freshness sweep. Raises rather than returning empty.

    Three shapes are handled, in order:

    1. A public `entries()`, which `DynamoDbFactStore` provides.
    2. `_entries`, which the digest-frozen `FileFactStore` has and which cannot be given a
       public accessor -- editing `facts/store.py` would move Stage 3's ruleset digest and
       therefore the ledger, assessment and plan digests. Reaching for the private name is
       the lesser cost, and it is checked by a test rather than assumed.
    3. Anything else: raise. A store this cannot enumerate is a store a sweep cannot
       reason about, and saying so is the only safe answer.

    `find()` is the read path and honours marks; this is the maintenance path and does not,
    because a sweep has to see a fact in order to expire it.
    """
    public = getattr(store, "entries", None)
    if callable(public):
        return tuple(public())
    private = getattr(store, "_entries", None)
    if isinstance(private, list):
        return tuple(sorted(private, key=lambda e: e.canonical_id))
    raise StoreNotEnumerable(
        f"{type(store).__name__} exposes neither entries() nor _entries, so its facts "
        "cannot be listed. Refusing to report a sweep of zero facts as a successful "
        "sweep."
    )


class FactRejected(RuntimeError):
    """Raised when something asks to write a fact the acceptance gate did not accept.

    The last line of defence, and it exists because of how a single gate dies: one function
    edited to return accept unconditionally, with every other layer going along with it. A
    single gate that a one-line edit can disable is not a gate. So the write path re-asserts
    the decision independently: a candidate that is not APPROVED, or that is APPROVED
    without an ApprovalRecord naming who approved it, gets an exception instead of a stored
    fact.
    """


class FactWriter(Protocol):
    """Write an APPROVED fact to the live store. The only way a fact gets there."""

    def write_approved(self, candidate: Any) -> str:
        ...


class DynamoDbFactWriter:
    """Writes APPROVED facts into the fact table, and refuses anything else.

    Item shape matches what `DynamoDbFactStore` reads: `component_id` (HASH),
    `version_series` (RANGE) and `entry` holding the `ComponentKnowledge` JSON. The
    receipt is written alongside in `receipt`, so the store's own index carries the
    evidence that justified the write. A table that records `confidence=LOW,
    decision=AUTO_ACCEPT` on a fact that failed its quote check leaves the contradiction
    visible in the store with nothing checking it, which is why the check is here.

    The range key is `*`, matching the seeding convention: one entry covers several series,
    so the entry is one item.
    """

    def __init__(self, table_name: str) -> None:
        self._table_name = table_name

    def write_approved(self, candidate: Any) -> str:
        decision = str(candidate.receipt.decision)
        if decision != "APPROVED" or candidate.knowledge is None:
            raise FactRejected(
                f"refusing to write {candidate.component_id}/"
                f"{candidate.version_series}: its decision is {decision}. Only an APPROVED "
                "candidate reaches the live store, and only a person can produce one -- a "
                "pending candidate waits in the candidate prefix and a rejected one is not "
                "stored at all."
            )
        approval = getattr(candidate.receipt, "approval", None)
        if approval is None:
            raise FactRejected(
                f"refusing to write {candidate.component_id}: the receipt says APPROVED but "
                "carries no ApprovalRecord, so there is no accountable approver and no "
                "digest of what they were shown. An approval is the record, not the status."
            )
        _table(self._table_name).put_item(
            Item={
                "component_id": candidate.component_id,
                "version_series": "*",
                "entry": candidate.knowledge.model_dump_json(),
                "receipt": candidate.receipt.model_dump_json(),
                "candidate_digest": candidate.digest(),
                # Denormalised onto the item so an operator scanning the table sees who
                # approved each fact without parsing the receipt JSON. A table showing
                # `confidence=LOW / decision=AUTO_ACCEPT` in plain sight with nothing looking
                # is the failure; the approver being equally visible is the point.
                "approved_by": approval.approver,
                "approved_at": approval.approved_at.isoformat(),
                "report_digest": approval.report_digest,
            }
        )
        return f"dynamodb://{self._table_name}/{candidate.component_id}"


class RefusingFactWriter:
    """A writer for an environment with no fact table. Refuses, rather than dropping.

    A run that cannot store its result must say so. Silently discarding an approved fact
    would look identical to research that found nothing, and the two mean opposite things.
    """

    def write_approved(self, candidate: Any) -> str:
        raise FactRejected(
            "no fact table is configured (FACT_STORE_TABLE is unset), so an approved "
            f"fact for {candidate.component_id} cannot be stored. Refusing rather than "
            "discarding it."
        )


def build_fact_writer() -> FactWriter:
    """The fact writer this environment is configured for.

    Only the approval handler calls this, and only its role holds `PutItem` on the fact
    table, so for every stage -- Stage 6 included -- the write path is closed by IAM as well
    as by code. See `stages/research.py`.
    """
    table_name = os.environ.get("FACT_STORE_TABLE", "").strip()
    if table_name:
        return DynamoDbFactWriter(table_name)
    return RefusingFactWriter()


def build_raw_fact_store() -> FactStore:
    """The undecorated fact store this environment is configured for.

    Deliberately environment-driven rather than argument-driven: every stage that
    needs a store needs the *same* store, and threading it through five Lambda
    handlers as a parameter would invite two stages disagreeing — which the ledger's
    `store_digest` check would catch, but only after the fact.

    "Raw" means marks are not applied. Only the freshness sweep wants this: it has to see
    a fact in order to expire it. Every stage wants `build_fact_store`.
    """
    table_name = os.environ.get("FACT_STORE_TABLE", "").strip()
    if table_name:
        return DynamoDbFactStore(table_name)
    return FileFactStore()


def build_fact_store() -> FactStore:
    """The fact store every stage reads: configured for this environment, marks applied.

    A fact carrying a staleness or revocation mark does not answer, and the marks are
    folded into `digest()` so the suppression is a visible change to the store rather than
    a silent one. See `storage/marks.py` for why the decoration happens here rather than
    inside Stage 3, which is digest-frozen and must stay a pure read.

    With no mark store configured this is `build_raw_fact_store()` with an empty ledger,
    whose digest is byte-identical to the undecorated store — so adding freshness to a
    deployment that has no mark table moves no digest.
    """
    from .marks import marked

    return marked(build_raw_fact_store())
