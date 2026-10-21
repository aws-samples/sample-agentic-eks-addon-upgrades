"""Resolve the reversibility of a component move from the breaking changes it crosses.

A move from one series to another crosses every breaking change introduced in between.
The move is only as reversible as its least reversible crossing: one `ONE_WAY` change —
a CRD schema migration, a stored-version bump — makes the whole move one-way regardless
of how benign the others are.

The default is the safe reading. A move that crosses a change of `UNKNOWN` reversibility
is reported `UNKNOWN`, never `REVERSIBLE`, because wrongly assuming a move can be undone
risks an unrecoverable cluster while wrongly assuming it cannot costs only an extra
approval. Absence of a classified change is treated the same way when the knowledge base
records breaking changes for the component but none is classified across the span.
"""

from __future__ import annotations

from ..knowledge import ComponentKnowledge, Reversibility


def _series_of(version: str) -> tuple[int, int]:
    """The (major, minor) a version or series belongs to.

    Accepts a series (`1.16.x`), a full version (`1.16.0`), or a bare minor (`1.16`):
    `breaking_changes[].introduced_in` is a full version, while the series arguments are
    `major.minor.x`. All reduce to the same comparable (major, minor) pair.
    """
    parts = version.removesuffix(".x").split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        raise ValueError(f"cannot read a major.minor from {version!r}")
    return int(parts[0]), int(parts[1])


def resolve_reversibility(
    knowledge: ComponentKnowledge, from_series: str | None, to_series: str
) -> tuple[Reversibility, str]:
    """The reversibility of moving from one series to another, with a reason.

    A breaking change counts when it is `introduced_in` a version above the starting
    series and at or below the destination — i.e. the move crosses it. `from_series` of
    None (installed version unknown as a series) is treated as "from the beginning", so
    every change up to the destination is considered.
    """
    upper = _series_of(to_series)
    lower = _series_of(from_series) if from_series else (-1, -1)

    crossed = [
        change
        for change in knowledge.breaking_changes
        if lower < _series_of(change.introduced_in) <= upper
    ]

    if not crossed:
        # No breaking change recorded across the span. Reversible only if the knowledge
        # base is one that records breaking changes at all; if the component has never
        # populated the field, we cannot claim reversibility from silence.
        if knowledge.breaking_changes:
            origin = from_series or "the installed version"
            return (
                Reversibility.REVERSIBLE,
                f"No breaking change is recorded between {origin} and {to_series}.",
            )
        return (
            Reversibility.UNKNOWN,
            (
                "The knowledge base records no breaking changes for this component, so "
                "reversibility cannot be confirmed. Treated as needing approval."
            ),
        )

    if any(c.reversibility is Reversibility.ONE_WAY for c in crossed):
        one_way = next(c for c in crossed if c.reversibility is Reversibility.ONE_WAY)
        return (
            Reversibility.ONE_WAY,
            (
                f"Crosses a one-way change introduced in {one_way.introduced_in}: "
                f"{one_way.summary} A version rollback will not undo it."
            ),
        )

    if any(c.reversibility is Reversibility.UNKNOWN for c in crossed):
        unknown = next(c for c in crossed if c.reversibility is Reversibility.UNKNOWN)
        return (
            Reversibility.UNKNOWN,
            (
                f"Crosses a change introduced in {unknown.introduced_in} whose "
                "reversibility is not classified. Treated as needing approval."
            ),
        )

    return (
        Reversibility.REVERSIBLE,
        f"Every breaking change crossed between {from_series or 'the installed version'} "
        f"and {to_series} is recorded as reversible.",
    )
