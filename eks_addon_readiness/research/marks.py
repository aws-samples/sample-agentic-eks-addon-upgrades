"""Marks: how a stored fact stops answering, with an audit trail rather than a deletion.

Two questions this answers:

**How does a fact already in the store ever get re-researched?** It gets marked stale, and
Stage 3 then reports its component as researchable exactly as it already does for a
component with no fact at all. No new trigger mechanism, no second code path -- the lazy
acquisition path that already exists starts firing.

**How does a fact known to be wrong stop driving verdicts?** It gets marked revoked. A bad
fact is bad fleet-wide, so `consumed_by` records the assessments that were computed from it
and are therefore due recomputation.

**Marks are written, never deletions.** A deleted fact leaves no trace of having existed,
so nobody can answer "what did this cluster's 2026-09-10 assessment rest on" afterwards. A
mark preserves the fact, records when it stopped answering and why, and names who or what
decided -- which is the difference between an audit trail and a mystery.

**Where the mark is written from matters, and this is a design constraint.** Marks are
produced by a sweep that runs *outside* the pipeline (`freshness.py`) and are *read* by the
store adapter the stages already use. Stage 3 stays a pure function of already-fetched
evidence plus the fact store; its `store_digest` citation already covers the store's state,
and marks are part of that state, so the digest covers them too. Putting a clock reading or
a network call inside Stage 3 instead would make the gap ledger depend on the instant it
ran, and replay -- the property the whole design exists to provide -- would be gone.

**A MARK IS ABOUT ONE REVISION OF ONE FACT, and forgetting that was a live defect.** A
staleness mark records the revision it was raised against (`fact_revision`), and it stops a
fact answering only while the store still holds *that* revision. The first version compared
nothing: a mark raised against revision X went on withholding revision Y after a human had
approved Y, so the gap ledger reported the component researchable, the research ran again,
a candidate was written again, and the same person was emailed to approve the same component
a third time. One component sat in that loop across three runs. `suppresses` now takes the
revision the store actually holds and a mark that names a different one suppresses nothing --
which fixes every reader at once, because the gap ledger, `MarkedFactStore.find` and the
sweep all ask this one question.

**The revision comparison is for STALENESS ONLY.** A revocation survives re-research by
design: the reason a fact was revoked -- the logic that produced it was defective -- applies
just as well to its replacement, so a revoked component stays withheld however fresh the
new fact is. `freshness.retire_marks` states the same policy from the other side.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from ..models import ContractModel


class MarkKind(StrEnum):
    """Why a fact stopped answering.

    `STALE`
        The component released after this fact was read, so the fact never saw that release.
        It is not known to be wrong; it is known to be superseded, and the policy is that a
        superseded fact is re-researched rather than trusted. NOT a time expiry -- age alone
        marks nothing, and `freshness.py` records at length why it must not.

    `REVOKED`
        The fact is known to be wrong, or was produced by logic since found defective. It
        must stop answering immediately and every assessment that consumed it is due
        recomputation.
    """

    STALE = "STALE"
    REVOKED = "REVOKED"


class MarkRetirement(ContractModel):
    """A mark that has stopped being in force, written OVER the mark rather than deleted.

    `MarkStore.save` is a put only -- it writes every mark in the ledger and removes none --
    so dropping a mark from the ledger leaves its row behind and the next reader loads it
    again. Retirement is therefore a stamp on the mark, at the same coordinates, which the
    put overwrites: one row, its whole history in it, and no `dynamodb:DeleteItem` grant
    needed by anything.

    A retired mark suppresses nothing (`FactMark.applies_to`) and is still there to read,
    which is the same choice the mark itself makes over deleting the fact.
    """

    retired_at: datetime
    retired_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class FactMark(ContractModel):
    """One fact, marked, with everything needed to audit the decision afterwards."""

    component_id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    # `*` marks the whole entry, matching the fact table's range-key convention for an
    # entry that spans several series.
    version_series: str = Field(pattern=r"^(?:\d+\.\d+\.x|\*)$")
    kind: MarkKind
    # When the mark was written, and by what. `marked_by` is a mechanism name -- a sweep
    # id, a human's reason for revoking -- so an auditor can tell an automatic expiry from
    # a deliberate revocation without inferring it from the kind.
    marked_at: datetime
    marked_by: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    # The fact's own `refreshed_at` and revision at the time of marking, so the mark still
    # means something after the fact is re-researched and replaced.
    fact_refreshed_at: datetime | None = None
    fact_revision: str | None = None
    # Documents computed from this fact, for recomputation after a revocation. Empty is
    # honest for a staleness mark, where nothing is known to be wrong.
    consumed_by: tuple[str, ...] = ()
    # Set once this mark has stopped being in force. Absent on every mark ever written before
    # retirement existed, which is why it is optional rather than a second kind: a new
    # `MarkKind` would change the row's range key (`version_series#kind`) and write a SECOND
    # row instead of overwriting the one that is withholding the fact.
    retirement: MarkRetirement | None = None

    @model_validator(mode="after")
    def revocation_names_its_blast_radius(self) -> FactMark:
        """A revocation must say what it invalidates, even if the answer is "nothing yet".

        Enforced as a contract rather than left to a caller's diligence: the requirement
        that a bad fact be traceable to every assessment that consumed it is easy to state
        and easy to skip, and skipping it is only discovered when someone needs the list.
        """
        if self.kind is MarkKind.REVOKED and not self.reason.strip():
            raise ValueError("a revocation must record why")
        return self

    @property
    def key(self) -> tuple[str, str]:
        return (self.component_id, self.version_series)

    @property
    def row_key(self) -> tuple[str, str, str]:
        """The coordinates this mark occupies in the mark table, kind included.

        `DynamoDbMarkStore` writes `version_series#kind` as the range key, so two marks
        sharing this triple share one row and the later put wins. Named here so the sweep can
        reason about that collision instead of discovering it as a lost write.
        """
        return (self.component_id, self.version_series, str(self.kind))

    @property
    def in_force(self) -> bool:
        """Whether this mark still withholds anything at all. A retired mark does not."""
        return self.retirement is None

    def obsolete_for(self, fact_revision: str | None) -> bool:
        """Whether this mark names a fact the store no longer holds.

        The predicate the loop turned on. A staleness mark is raised against ONE revision of
        a fact and recorded it; once a fresh fact replaces that revision the mark is about
        something that is gone, and a mark about a fact that is gone must not withhold its
        replacement.

        Three cases deliberately answer False:

        - a **revocation**, which survives re-research on purpose -- the defect that produced
          the fact may be present in its replacement too;
        - a mark already **retired**, which is not in force to begin with;
        - either revision **unknown**, which is unanswerable rather than a mismatch. A mark
          written before `fact_revision` existed, or a caller that did not say which revision
          it is asking about, leaves the comparison undecidable, and withholding is the safe
          answer to an undecidable question.
        """
        if self.retirement is not None or self.kind is not MarkKind.STALE:
            return False
        if not fact_revision or not self.fact_revision:
            return False
        return fact_revision.strip() != self.fact_revision.strip()

    def applies_to(self, fact_revision: str | None) -> bool:
        """Whether this mark stops the named revision of its fact from answering.

        Defined as "in force and not obsolete" over the same predicate the sweep retires on,
        so suppression and retirement cannot come to disagree about which marks are spent.
        """
        return self.in_force and not self.obsolete_for(fact_revision)

    def retired(self, at: datetime, *, by: str, reason: str) -> FactMark:
        """This mark, stamped retired. The mark itself is preserved -- only its force ends."""
        return self.model_copy(
            update={"retirement": MarkRetirement(retired_at=at, retired_by=by, reason=reason)}
        )


class MarkLedger(ContractModel):
    """Every mark currently in force, as one hashable document.

    The digest is what makes marks safe to consult from the read path. A store adapter
    folds this digest into its own, so a fact going stale moves `store_digest`, which the
    ledger and the assessment already cite -- two runs either side of a mark are visibly
    different documents rather than the same document with different content.
    """

    marks: tuple[FactMark, ...] = ()

    def for_component(self, component_id: str) -> tuple[FactMark, ...]:
        """Marks in force for a component, whole-entry marks included.

        Retired marks are excluded, because "in force" is what the name claims and a retired
        mark is not. They stay in `marks`, and in the digest, for an auditor to read.
        """
        return tuple(
            m
            for m in self.marks
            if m.component_id == component_id.strip() and m.in_force
        )

    def suppresses(
        self, component_id: str, fact_revision: str | None = None
    ) -> FactMark | None:
        """The mark that stops THIS REVISION of a component's fact answering, if any.

        THE ONE PREDICATE EVERY READER PASSES THROUGH. `MarkedFactStore.find` -- and so the
        gap ledger, the assessment and the plan behind it -- and the sweep both ask this, so a
        mark that is spent stops withholding everywhere at once.

        `fact_revision` is the revision the store actually holds. A staleness mark that names
        a different one is obsolete: it was raised against a fact that has since been
        re-researched and approved, and withholding the replacement is what kept sending the
        same component back to a human run after run. Omitting the argument means "I cannot
        say which revision I hold", which leaves every mark in force -- the conservative
        answer, and it keeps a caller that does not have a fact in hand honest rather than
        accidentally lenient.

        A revocation outranks a staleness mark: both stop the fact answering, and the report
        should say the stronger, more specific thing. A revocation is also never obsolete --
        see `FactMark.obsolete_for`.
        """
        marks = tuple(
            m for m in self.for_component(component_id) if m.applies_to(fact_revision)
        )
        revoked = next((m for m in marks if m.kind is MarkKind.REVOKED), None)
        if revoked is not None:
            return revoked
        return next((m for m in marks if m.kind is MarkKind.STALE), None)

    def obsolete_marks(
        self, component_id: str, fact_revision: str | None
    ) -> tuple[FactMark, ...]:
        """The in-force marks for this component that name a revision the store no longer has.

        Exactly the marks `suppresses` just declined to honour, and computed from the same
        predicate so the two cannot drift. The sweep retires these; nothing else depends on
        them, which is why part 1 stands alone without part 2.
        """
        return tuple(
            m for m in self.for_component(component_id) if m.obsolete_for(fact_revision)
        )

    def digest(self) -> str:
        """Digest over every mark, ordered by key then kind, so it is enumeration-stable.

        RETIRED MARKS ARE IN IT. The digest is over the mark table's state, not over what is
        in force: retiring a mark is a write, so it must move the store digest exactly as
        writing one does, or two runs either side of a retirement would cite the same store
        digest while answering differently.
        """
        hasher = hashlib.sha256()
        for mark in sorted(self.marks, key=lambda m: (m.key, str(m.kind))):
            hasher.update(mark.canonical_json().encode("utf-8"))
            hasher.update(b"\x00")
        return "sha256:" + hasher.hexdigest()

    @property
    def in_force(self) -> tuple[FactMark, ...]:
        """Every mark still withholding something. Retired marks are excluded."""
        return tuple(m for m in self.marks if m.in_force)

    def __len__(self) -> int:
        """How many marks are IN FORCE, which is what every caller reports it as.

        `marks_in_force` on the sweep's payload and "N mark(s) now in force" in its summary
        both read this, so a retired mark must not be counted: a ledger that reported a spent
        mark as in force would be the same overclaim the retirement exists to remove.
        `marks` and `digest()` still cover everything.
        """
        return len(self.in_force)
