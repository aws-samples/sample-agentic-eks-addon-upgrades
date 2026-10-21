"""Turning what a source said into a half-open window -- or refusing to.

This module is the fix for the defect that produced both live wrong facts. It is
deterministic, pure, and it is the only place a `VersionRange` is derived from research.

The rule it enforces, stated once:

> A ceiling may be written only where a source stated one — as supported or as tested —
> or where the shape of the source's own table implies one. The word the source used
> travels with the window, and an absent ceiling is never taken to mean *every later
> version* when the source's own table shows otherwise.

Per kind:

| kind                 | floor                  | ceiling                                        |
|----------------------|------------------------|------------------------------------------------|
| `SUPPORTED_RANGE`    | stated minimum         | next minor above the stated inclusive top       |
| `TESTED_RANGE`       | stated minimum         | next minor above the stated inclusive top       |
| `EXPLICIT_MAXIMUM`   | stated minimum or none | next minor above the stated inclusive top       |
| `ENUMERATED_MINORS`  | lowest listed minor    | next minor above the top of each listed block   |
| `PUBLISHED_MINIMUM`  | stated minimum         | floor of the next spec up, else none             |
| `SUPPORT_POLICY`     | -- no window --        | -- no window --                                 |
| `UNSTATED`           | -- no window --        | -- no window --                                 |

**`TESTED_RANGE` derives a window.** A tested window — "tested on 1.30 to 1.32, no
guarantee" — states neither a support ceiling nor open-ended support, and the strictly
honest window — *unknown above 1.32* — is inexpressible in the frozen `VersionRange`.
Refusing to derive anything leaves a project whose only published matrix is a tested one
permanently UNRESOLVED — an answer no platform engineer could act on. So the window is
derived, and the CLAIM stays visible: the basis sentence says TESTED, the qualifier
travels on the receipt via `tested_windows()`, and the report prints the word next to the
number. The distinction between tested and supported is carried, not enforced by refusal.

**Why `SUPPORT_POLICY` yields nothing.** An N-2 style rule carries no absolute numbers,
so there is nothing here to turn into a window. If the model derived numbers from the
policy, they live in `Assessment.derivation`, and `judge.py` recomputes that arithmetic
from the stated anchor and window size rather than trusting it.

**Why `PUBLISHED_MINIMUM` looks at its neighbours.** `0.6.x | 1.25+` read alone is a
project claiming open-ended support from 1.25, and `None` is the faithful reading of
that. But it does not stand alone: the same table says `0.7.x | 1.27+`, `0.8.x | 1.31+`,
`0.9.x | 1.34+`. A table of rising floors is how a project says *which* version to run at
a given Kubernetes minor, so a row's effective ceiling is the floor of the next spec up.
Reading 0.6.x's floor as unbounded ignores three rows that were right there, and is what
declared metrics-server 0.6.4 fit for Kubernetes 1.36.

The topmost row keeps `None`. That row genuinely is the project's open-ended claim, and
nothing in the table contradicts it.

This narrows some windows below what a project would tolerate in practice -- 0.7.x
probably does run on 1.31. That direction is chosen deliberately: a narrowed window
causes an unnecessary upgrade recommendation, a widened one causes an upgrade onto a
version nobody supports. `acceptance.py` documents the same asymmetry as a confidence
bar; this is the same reasoning applied to the numbers themselves.

The reasoning about discrete listings is not new here. `facts/catalogue.py:64-71`, which
is digest-frozen and handles AWS's add-on catalogue, states it exactly: *"The ceiling is
always the exclusive next-minor of the block's top, because the catalogue lists discrete
minors, not open-ended support -- AWS has not stated the add-on works on a version it
does not list."* Applying the correct rule to catalogue data and the wrong one to
researched data would get two different answers from the same AWS table, so the rule is
stated the same way in both places.
"""

from __future__ import annotations

from ..knowledge import VersionRange
from .boundary import BoundKind, CompatibilityStatementInput


def _minor_tuple(minor: str) -> tuple[int, int] | None:
    """`1.33` -> `(1, 33)`, or None when it is not a `major.minor`.

    >>> _minor_tuple("1.33")
    (1, 33)
    >>> _minor_tuple("1.33.2") is None
    True
    """
    parts = minor.split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    return int(parts[0]), int(parts[1])


def next_minor(minor: str) -> str | None:
    """The exclusive ceiling for a window whose inclusive top is `minor`.

    `1.32` -> `1.33`. This is the one place the +1 happens, and it happens in code rather
    than in a prompt: a model asked for this arithmetic can get it right every time and
    still produce a wrong fact -- the number was never the problem.

    >>> next_minor("1.32")
    '1.33'
    >>> next_minor("1.9")
    '1.10'
    >>> next_minor("garbage") is None
    True
    """
    t = _minor_tuple(minor)
    if t is None:
        return None
    return f"{t[0]}.{t[1] + 1}"


def _spec_sort_key(spec: str) -> tuple[int, int, int]:
    """Order a version spec numerically, so `0.9.x` precedes `0.10.x`.

    >>> _spec_sort_key("0.9.x")
    (0, 9, 0)
    >>> _spec_sort_key("0.10.x") > _spec_sort_key("0.9.x")
    True
    """
    parts = [int(p) for p in spec.removesuffix(".x").split(".") if p.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def contiguous_blocks(minors: tuple[str, ...]) -> list[VersionRange]:
    """Collapse listed Kubernetes minors into contiguous half-open windows.

    `('1.35', '1.36')` -> one window `[1.35, 1.37)`. A hole yields two windows rather
    than one that falsely spans it. Malformed entries are dropped.

    Deliberately the same behaviour as the digest-frozen `facts/catalogue.py`
    `_contiguous_ranges`, because it is the same question about the same kind of data.
    It is re-expressed here rather than imported: `catalogue.py` is frozen, its function
    is private, and taking a dependency on a frozen module's private name would make a
    future edit there a silent change here.

    >>> [(r.minimum_inclusive, r.maximum_exclusive) for r in contiguous_blocks(("1.35", "1.36"))]
    [('1.35', '1.37')]
    >>> [(r.minimum_inclusive, r.maximum_exclusive) for r in contiguous_blocks(("1.33", "1.35"))]
    [('1.33', '1.34'), ('1.35', '1.36')]
    """
    parsed = sorted({t for m in minors if (t := _minor_tuple(m)) is not None})
    if not parsed:
        return []
    windows: list[VersionRange] = []
    block_start = parsed[0]
    prev = parsed[0]
    for cur in parsed[1:]:
        if cur[0] == prev[0] and cur[1] == prev[1] + 1:
            prev = cur
            continue
        windows.append(
            VersionRange(
                minimum_inclusive=f"{block_start[0]}.{block_start[1]}",
                maximum_exclusive=f"{prev[0]}.{prev[1] + 1}",
            )
        )
        block_start = cur
        prev = cur
    windows.append(
        VersionRange(
            minimum_inclusive=f"{block_start[0]}.{block_start[1]}",
            maximum_exclusive=f"{prev[0]}.{prev[1] + 1}",
        )
    )
    return windows


class DerivedWindow:
    """One statement resolved into a window, or into a stated reason there is none.

    A plain carrier: it is an intermediate of the derivation, not a contract that travels.
    `basis` is the sentence the receipt shows a reader to explain where the ceiling came
    from -- or why the statement produced nothing.
    """

    def __init__(
        self,
        statement: CompatibilityStatementInput,
        window: VersionRange | None,
        basis: str,
    ) -> None:
        self.statement = statement
        self.window = window
        self.basis = basis

    @property
    def spec(self) -> str:
        return self.statement.version_spec

    @property
    def storable(self) -> bool:
        return self.window is not None


def _floor_table_ceilings(
    statements: tuple[CompatibilityStatementInput, ...],
) -> dict[str, str | None]:
    """For each `PUBLISHED_MINIMUM` spec, the floor of the next spec up in the same table.

    Returns spec -> exclusive ceiling, with `None` for the topmost spec, whose
    open-ended claim nothing in the table contradicts.

    Only `PUBLISHED_MINIMUM` statements participate. A table mixing kinds does not make
    one kind bound another: a project stating `0.3.x | 1.8-1.21` alongside `0.6.x | 1.25+`
    has said nothing about where 0.6.x ends, and the 0.3.x row must not be read as if it
    had.

    Two or more rows sharing a floor cannot bound each other; the ceiling comes from the
    next *strictly higher* floor.
    """
    minimums: dict[str, tuple[int, int]] = {}
    for s in statements:
        if s.kind is not BoundKind.PUBLISHED_MINIMUM or s.stated_minimum is None:
            continue
        t = _minor_tuple(s.stated_minimum)
        if t is not None:
            minimums[s.version_spec] = t

    ceilings: dict[str, str | None] = {}
    for spec, floor in minimums.items():
        higher = sorted(f for f in minimums.values() if f > floor)
        ceilings[spec] = f"{higher[0][0]}.{higher[0][1]}" if higher else None
    return ceilings


def derive_windows(
    statements: tuple[CompatibilityStatementInput, ...],
) -> tuple[DerivedWindow, ...]:
    """Resolve every statement into a window, or into a reason it has none.

    Pure: same statements in, same windows out, no clock, no network, no model. The whole
    set is resolved together because one row's ceiling can depend on another's floor --
    which is the point, and what reading a row in isolation got wrong.
    """
    ceilings = _floor_table_ceilings(statements)
    derived: list[DerivedWindow] = []

    for s in sorted(statements, key=lambda s: _spec_sort_key(s.version_spec)):
        if s.kind is BoundKind.SUPPORT_POLICY:
            derived.append(
                DerivedWindow(
                    s,
                    None,
                    (
                        "a relative rule (N-2 style) with no absolute numbers in the "
                        "row. The numbers, if the model derived any, live in the "
                        "assessment's derivation and are recomputed there."
                    ),
                )
            )
            continue

        if s.kind is BoundKind.UNSTATED:
            derived.append(
                DerivedWindow(
                    s, None, "the source makes no compatibility statement for this spec"
                )
            )
            continue

        if s.kind is BoundKind.ENUMERATED_MINORS:
            blocks = contiguous_blocks(s.stated_minors)
            if not blocks:
                derived.append(
                    DerivedWindow(s, None, "no well-formed Kubernetes minor was listed")
                )
                continue
            # One statement can yield several windows when the listing has a hole. Each
            # is its own DerivedWindow so the assembled rules keep the hole rather than
            # spanning it.
            for block in blocks:
                derived.append(
                    DerivedWindow(
                        s,
                        block,
                        (
                            f"the source lists {len(s.stated_minors)} discrete Kubernetes "
                            f"minor(s) for this spec; this block runs from "
                            f"{block.minimum_inclusive} and is bounded at "
                            f"{block.maximum_exclusive}, the exclusive next minor above "
                            "the top of the block, because a source that lists discrete "
                            "minors has not stated support for one it omits"
                        ),
                    )
                )
            continue

        floor = s.stated_minimum
        ceiling: str | None
        basis: str

        if s.kind is BoundKind.SUPPORTED_RANGE:
            ceiling = next_minor(s.stated_maximum_inclusive or "")
            basis = (
                f"the source states {s.stated_minimum}-{s.stated_maximum_inclusive} as a "
                f"SUPPORTED window; {s.stated_maximum_inclusive} is inclusive, so the "
                f"exclusive ceiling is {ceiling}"
            )
        elif s.kind is BoundKind.TESTED_RANGE:
            ceiling = next_minor(s.stated_maximum_inclusive or "")
            # The word travels with the number. "Tested" is not "supported", and the
            # reader sees which claim the project actually made — on this sentence, on
            # the receipt's tested_windows, and in the report.
            basis = (
                f"the source states {s.stated_minimum}-{s.stated_maximum_inclusive} as "
                f"a TESTED window; treated as the project's matrix with the basis kept "
                f"visible, so the exclusive ceiling is {ceiling} and the claim remains "
                f"'tested', not 'supported'"
            )
        elif s.kind is BoundKind.EXPLICIT_MAXIMUM:
            ceiling = next_minor(s.stated_maximum_inclusive or "")
            basis = (
                f"the source states an explicit maximum of "
                f"{s.stated_maximum_inclusive} (inclusive), so the exclusive ceiling is "
                f"{ceiling}"
            )
        else:  # PUBLISHED_MINIMUM
            ceiling = ceilings.get(s.version_spec)
            if ceiling is None:
                basis = (
                    f"the source states a floor of {s.stated_minimum} and no ceiling, "
                    "and this is the highest floor in the source's own table, so the "
                    "project's open-ended claim stands"
                )
            else:
                basis = (
                    f"the source states a floor of {s.stated_minimum} and no ceiling. "
                    f"The same table states a floor of {ceiling} for a higher spec, so "
                    f"this spec is bounded at {ceiling} rather than read as open-ended: "
                    "a table of rising floors states which version to run at a given "
                    "Kubernetes minor"
                )

        if floor is None:
            # EXPLICIT_MAXIMUM with no stated floor. A window needs one, and inventing a
            # floor is the same class of error as inventing a ceiling.
            derived.append(
                DerivedWindow(
                    s,
                    None,
                    "the source states a ceiling but no floor, and a floor is not "
                    "inferable from a ceiling",
                )
            )
            continue

        try:
            window = VersionRange(minimum_inclusive=floor, maximum_exclusive=ceiling)
        except ValueError as exc:
            # An inverted window is a data error the frozen contract already rejects.
            # Reported, not raised: one bad row must not sink a fact whose other rows are
            # sound.
            derived.append(
                DerivedWindow(s, None, f"the stated numbers form no valid window: {exc}")
            )
            continue

        derived.append(DerivedWindow(s, window, basis))

    return tuple(derived)


def tested_windows(
    statements: tuple[CompatibilityStatementInput, ...],
) -> tuple[str, ...]:
    """The tested windows, rendered for the receipt.

    They drive no verdict, and that is exactly why they must be visible: a reader looking
    at a component with no stored fact deserves to see that its project publishes tested
    windows and what they say, rather than an unexplained absence.
    """
    return tuple(
        f"{s.version_spec}: tested on {s.stated_minimum}-{s.stated_maximum_inclusive}"
        + (f" ({s.qualifier_quote.strip()})" if s.qualifier_quote else "")
        for s in sorted(statements, key=lambda s: _spec_sort_key(s.version_spec))
        if s.kind is BoundKind.TESTED_RANGE
    )


def recompute_policy_window(
    derivation,
) -> tuple[VersionRange | None, bool, str]:
    """Redo the N-2 arithmetic in an assessment's derivation. Never trust, always recompute.

    Returns (window, ok, why). `ok` is False when the model's stated minimum or maximum
    disagrees with the recomputation -- the window returned is always OURS, from the
    anchor and the window size, so a model that did the arithmetic wrong contributes a
    failed check and not a wrong number.

    Pure arithmetic on the derivation's own fields; whether the anchor itself deserves
    trust (source-stated versus task-supplied) is `signals.final_confidence`'s concern.
    """
    anchor = _minor_tuple(derivation.anchor_kubernetes_minor)
    if anchor is None:
        return None, False, (
            f"anchor {derivation.anchor_kubernetes_minor!r} is not a major.minor"
        )
    n = derivation.window_size
    expected_min = f"{anchor[0]}.{anchor[1] - (n - 1)}"
    expected_max = derivation.anchor_kubernetes_minor
    ceiling = next_minor(expected_max)

    mismatches = []
    if derivation.derived_minimum != expected_min:
        mismatches.append(
            f"model wrote minimum {derivation.derived_minimum}, recomputed {expected_min}"
        )
    if derivation.derived_maximum_inclusive != expected_max:
        mismatches.append(
            f"model wrote maximum {derivation.derived_maximum_inclusive}, "
            f"recomputed {expected_max}"
        )

    try:
        window = VersionRange(
            minimum_inclusive=expected_min, maximum_exclusive=ceiling
        )
    except ValueError as exc:
        return None, False, f"the recomputed numbers form no valid window: {exc}"

    if mismatches:
        return window, False, "; ".join(mismatches)
    return window, True, (
        f"recomputed {expected_min}..{expected_max} (inclusive) from anchor "
        f"{derivation.anchor_kubernetes_minor} and window size {n}; matches the model"
    )
