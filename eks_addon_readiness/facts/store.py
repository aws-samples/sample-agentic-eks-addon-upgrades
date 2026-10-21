"""The compatibility fact store, and the port that keeps it replaceable.

Facts are looked up, never recalled. That distinction is why this is a plain keyed
store and not AgentCore Memory: a fact retrieved by model-mediated recall can differ
between runs, which would void the replay guarantee the whole design exists to
provide.

`FileFactStore` reads a directory of `ComponentKnowledge` entries, which is the right
shape at POC scale. DynamoDB replaces it behind `FactStore` without anything above
noticing.

**Absence is a value here, not an error.** The pulled-forward `KnowledgeRepository`
protocol declares `load(component_id, revision) -> ComponentKnowledge`, which can only
raise when a component is unknown. Gap detection is *about* the unknown case, so this
port returns `None` instead. That is why `ports.py` was not pulled forward with
`knowledge.py`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from ..knowledge import ComponentKnowledge

# Where a `FileFactStore` looks by default: one directory per component, each holding
# `entry.json`. Mirrors how the entries were already laid out.
#
# Resolved relative to this package rather than the working directory. The deployed
# bundle is not run from the project root, so a CWD-relative default would silently
# report an unreadable store in AWS while working perfectly on a laptop — the failure
# mode hardest to notice, because every component would still be reported, just
# uniformly as having no known fact.
DEFAULT_STORE_ROOT = Path(__file__).resolve().parent.parent.parent / "knowledge" / "components"

ENTRY_FILENAME = "entry.json"

# Digest reported when the store could not be read at all. `verify` and the ledger
# must be able to distinguish "no facts" from "facts unknown"; an empty-store digest
# would claim the former while meaning the latter.
STORE_UNAVAILABLE = "UNAVAILABLE"


class FactStore(Protocol):
    """Lookup of stored compatibility knowledge by component identity."""

    def find(self, identity: str) -> ComponentKnowledge | None:
        """The entry for `identity`, matching canonical id or alias, or None.

        Returning `None` rather than raising is deliberate: a missing fact is the
        normal case Stage 3 exists to report, not an exceptional one.
        """
        ...

    def digest(self) -> str:
        """Content digest of every stored fact.

        The ledger cites this so a later reader can tell whether two different
        results came from a changed cluster or from changed knowledge. Without it,
        gaining a fact and losing a component are indistinguishable.
        """
        ...


class FileFactStore:
    """A `FactStore` over a directory of JSON entries.

    Loads eagerly on construction. At POC scale the store is a handful of files, and
    loading eagerly means a malformed entry is reported when the store is opened
    rather than when some unrelated component happens to be looked up.
    """

    def __init__(self, root: Path | str = DEFAULT_STORE_ROOT) -> None:
        self._root = Path(root)
        self._by_identity: dict[str, ComponentKnowledge] = {}
        self._entries: list[ComponentKnowledge] = []
        self._load_errors: list[str] = []
        self._readable = self._root.is_dir()
        if self._readable:
            self._load()

    def _load(self) -> None:
        # Sorted so the digest does not depend on filesystem enumeration order.
        for path in sorted(self._root.glob(f"*/{ENTRY_FILENAME}")):
            try:
                entry = ComponentKnowledge.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                # Recorded, not raised. One malformed entry must not make every
                # other component unknowable — that would turn a bad file into a
                # cluster-wide outage of the fact store.
                self._load_errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
            self._entries.append(entry)
        self._index()

    def _index(self) -> None:
        """Build the lookup in two passes: every canonical id, then every alias.

        Two passes rather than one because a component's own canonical id must beat
        another component's alias for the same string, regardless of load order. A
        single pass gives the win to whichever file sorted first, so `alpha` claiming
        the alias `beta` would shadow the real `beta` entry — and the shadowing would
        silently flip if either were renamed.
        """
        for entry in self._entries:
            self._by_identity[entry.canonical_id] = entry
        for entry in self._entries:
            for alias in entry.aliases:
                self._by_identity.setdefault(alias, entry)

    @property
    def load_errors(self) -> tuple[str, ...]:
        """Entries that failed to parse. Surfaced by the caller as typed gaps."""
        return tuple(self._load_errors)

    @property
    def readable(self) -> bool:
        """False when the store root does not exist.

        Distinct from an empty store: a missing directory means the facts are
        unknown, while an empty directory means there are none. The first is a
        configuration problem, the second is the expected initial state.
        """
        return self._readable

    def find(self, identity: str) -> ComponentKnowledge | None:
        return self._by_identity.get(identity.strip())

    def digest(self) -> str:
        """Digest over the canonical form of every entry, ordered by canonical id."""
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


class EmptyFactStore:
    """A store holding nothing, for proving the all-unknown path.

    Not a test double. Stage 3's most important property is that an empty store
    yields a complete, correct, all-unknown ledger rather than a failure, and naming
    that configuration makes it something a caller can deliberately choose.
    """

    def find(self, identity: str) -> ComponentKnowledge | None:  # noqa: ARG002
        return None

    def digest(self) -> str:
        # A real digest of genuine emptiness, not the UNAVAILABLE sentinel: an empty
        # store is a known state, and two runs against it must agree.
        return "sha256:" + hashlib.sha256(b"").hexdigest()

    def __len__(self) -> int:
        return 0
