"""Which releases lie between what is installed and what is recommended, and what to read.

**THE JUMP USUALLY CROSSES SEVERAL MINORS, and a brief that reads only the target release
misses most of what it was written to find.** metrics-server is installed at `0.6.4` with a
recommended `0.9.0`, so the changes that break an operator live in 0.7's notes, 0.8's notes
AND 0.9's notes. Reading `0.9.0`'s alone yields a brief that is confidently wrong by
omission -- the worst shape for an advisory document, because absence of a warning reads as
absence of a hazard.

So the unit here is not the target release; it is the **walk**: every release strictly above
what is installed and no higher than what is recommended, in ascending order, plus an
explicit record of which minors the jump *enters* and which of those the walk actually found
notes for. That second half is the part that keeps the brief honest. A walk that found
0.7.0 and 0.9.0 but nothing in 0.8 must say `0.8` was not read, or the brief silently claims
0.8 introduced nothing.

**TWO KINDS OF ENUMERATION, AND THEY ARE NOT EQUALLY STRONG.** Within one major version the
minors between two releases are enumerable with no source at all -- 0.6 to 0.9 crosses 0.7,
0.8 and 0.9, arithmetically, and a missing one is provably missing. Across a major boundary
that is false: nothing here knows how many minors 0.x had before 1.0, so the only defensible
answer is the minors a release listing actually showed. `MinorEnumeration` records which of
the two applied, so a reader never has to guess whether a short list means "few minors" or
"we could not enumerate them".

**NO NETWORK, NO CLOCK, NO MODEL.** Everything in this module is a pure function of version
strings and of a release listing handed to it. That is what lets the walk be checked without
GitHub, and it is why the release listing arrives as bytes someone else fetched and recorded
rather than being fetched here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

# `0.6.x`. A version *series* spec, as `PlanStep.from_series` and `to_series` carry it.
_SERIES = re.compile(r"^(\d+)\.(\d+)\.x$")
# `0.6.4` or `v0.6.4`. An exact release, however it happens to be spelled upstream.
_EXACT = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class InstalledPrecision(StrEnum):
    """How precisely the low end of the walk is known.

    A plan step carries `from_series`, which may be an exact release (`0.6.4`) or a series
    (`0.6.x`). With a series, the walk's floor is that minor's `.0`, so the walk may include
    patches the cluster is already past -- an over-report, never an under-report.

    Recorded rather than smoothed over, because the two mean different things to a reader
    and only one of them is worth acting on. The minors the jump ENTERS are unaffected
    either way, which is where breaking changes live; see `ReleaseWalk.minors_entered`.
    """

    EXACT = "EXACT"
    SERIES_FLOOR = "SERIES_FLOOR"


class MinorEnumeration(StrEnum):
    """How the crossed minors were determined. See the module docstring.

    `ARITHMETIC` is a complete enumeration and a gap in it is provable. `OBSERVED` is
    whatever a release listing showed and cannot claim completeness.
    """

    ARITHMETIC = "ARITHMETIC"
    OBSERVED = "OBSERVED"


def parse_release(spec: str) -> tuple[int, int, int] | None:
    """`0.6.4` or `v0.6.4` as a comparable triple, or None.

    >>> parse_release("0.6.4")
    (0, 6, 4)
    >>> parse_release("v0.9.0")
    (0, 9, 0)
    >>> parse_release("0.6.x") is None
    True
    """
    match = _EXACT.match(spec.strip())
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def parse_series(spec: str) -> tuple[int, int] | None:
    """`0.6.x` as a comparable pair, or None.

    >>> parse_series("0.6.x")
    (0, 6)
    >>> parse_series("0.6.4") is None
    True
    """
    match = _SERIES.match(spec.strip())
    return None if match is None else (int(match[1]), int(match[2]))


def floor_of(spec: str) -> tuple[tuple[int, int, int], InstalledPrecision] | None:
    """The walk's low end, and how precisely it is known. None if `spec` is neither shape.

    A series floors at `.0`, which widens the walk rather than narrowing it -- see
    `InstalledPrecision`.

    >>> floor_of("0.6.4")
    ((0, 6, 4), <InstalledPrecision.EXACT: 'EXACT'>)
    >>> floor_of("0.6.x")
    ((0, 6, 0), <InstalledPrecision.SERIES_FLOOR: 'SERIES_FLOOR'>)
    >>> floor_of("nonsense") is None
    True
    """
    exact = parse_release(spec)
    if exact is not None:
        return exact, InstalledPrecision.EXACT
    series = parse_series(spec)
    if series is not None:
        return (series[0], series[1], 0), InstalledPrecision.SERIES_FLOOR
    return None


def minor_label(triple: tuple[int, int, int]) -> str:
    """The minor a release belongs to.

    >>> minor_label((0, 7, 3))
    '0.7'
    """
    return f"{triple[0]}.{triple[1]}"


@dataclass(frozen=True)
class ReleaseNote:
    """One release's own notes, addressable exactly as a citation will name them.

    `url` is the release's own page and `revision` is its tag, so each release is recorded
    as its OWN evidence object rather than as a slice of a listing payload. That matters for
    the quote check: `PageSet.get(url, revision)` is exact on both parts, so a quote citing
    `.../releases/tag/v0.7.0` can only be verified against the bytes of *that* release. One
    listing payload recorded under one key would make every per-release citation
    unverifiable, and the fix for that is not to loosen the check.
    """

    version: str
    tag: str
    url: str
    published_at: str | None
    body: str


def releases_from_listing(payload: str) -> tuple[tuple[ReleaseNote, ...], str | None]:
    """Split a GitHub releases listing into one `ReleaseNote` per release.

    Takes the bytes of `GET /repos/{owner}/{repo}/releases` -- ONE call -- and fans them out
    into one addressable page per release. That is the whole reason the listing is fetched
    rather than each release page separately: N releases cost one request, and the recording
    still ends up with N independently citable objects.

    RETURNS AN ERROR RATHER THAN AN EMPTY TUPLE, and that is not tidiness. An earlier version
    returned `()` for both "this repository published no releases" and "the response was not
    JSON", and the caller then reported the second as *"the listing was read but carried notes
    for none of the releases on the walk"* -- an absence dressed up as an answer, in the one
    module whose whole purpose is keeping those apart. Found by a fixture whose JSON was
    invalid, which is exactly the shape of a live malformed response. So the parse failure is
    named and travels to the reader, and the sibling shape in `issues.py::
    sightings_from_payload` is the same for the same reason.

    An entry with no tag is dropped: without a tag there is no revision to pin, so nothing
    quoted from it could be verified. A draft release is dropped too -- it is not a release the
    cluster can install, so notes from it would describe changes that are not in the
    recommended version. Neither is an error: the listing was read and understood.

    >>> notes, error = releases_from_listing('''[
    ...   {"tag_name": "v0.7.0", "html_url": "https://github.com/o/r/releases/tag/v0.7.0",
    ...    "published_at": "2024-02-01T00:00:00Z", "body": "Removed the old flag."},
    ...   {"tag_name": "", "body": "no tag, dropped"}
    ... ]''')
    >>> [(n.version, n.tag, n.body) for n in notes], error
    ([('0.7.0', 'v0.7.0', 'Removed the old flag.')], None)
    >>> releases_from_listing("not json")
    ((), 'the release listing is not JSON')
    >>> releases_from_listing('{"message": "Not Found"}')
    ((), 'the release listing is not a JSON array of releases')
    >>> releases_from_listing("[]")
    ((), None)
    """
    try:
        entries = json.loads(payload)
    except ValueError:
        return (), "the release listing is not JSON"
    if not isinstance(entries, list):
        return (), "the release listing is not a JSON array of releases"

    notes: list[ReleaseNote] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("draft"):
            continue
        tag = str(entry.get("tag_name") or "").strip()
        if not tag:
            continue
        triple = parse_release(tag)
        version = ".".join(str(p) for p in triple) if triple else tag
        notes.append(
            ReleaseNote(
                version=version,
                tag=tag,
                url=str(entry.get("html_url") or "").strip(),
                published_at=(str(entry["published_at"]) if entry.get("published_at") else None),
                body=str(entry.get("body") or ""),
            )
        )
    return tuple(notes), None


@dataclass(frozen=True)
class ReleaseWalk:
    """Every release the brief must read, and an honest account of what it could not.

    `hops` is the ordered work list. `minors_entered` is the completeness claim: the minors
    this jump crosses into, where breaking changes live. `minors_unread` is the difference
    between that claim and what the listing actually supplied, and it is the field that keeps
    a partial brief from reading as a complete one.
    """

    installed: str
    recommended: str
    precision: InstalledPrecision
    enumeration: MinorEnumeration
    hops: tuple[str, ...]
    minors_entered: tuple[str, ...]
    minors_read: tuple[str, ...]
    minors_unread: tuple[str, ...]
    note: str

    @property
    def complete(self) -> bool:
        """Whether every minor the jump enters was represented by at least one release."""
        return not self.minors_unread


def _entered_minors(
    low: tuple[int, int, int], high: tuple[int, int, int], observed: tuple[str, ...]
) -> tuple[tuple[str, ...], MinorEnumeration]:
    """The minors the jump enters, and how that was decided.

    Arithmetic within one major, where the enumeration is complete and a gap is provable.
    Across a major boundary only the observed minors, because nothing here knows how many
    minors the older major published.

    >>> _entered_minors((0, 6, 4), (0, 9, 0), ("0.7", "0.9"))
    (('0.7', '0.8', '0.9'), <MinorEnumeration.ARITHMETIC: 'ARITHMETIC'>)
    >>> _entered_minors((0, 9, 0), (1, 1, 0), ("0.10", "1.0", "1.1"))
    (('0.10', '1.0', '1.1'), <MinorEnumeration.OBSERVED: 'OBSERVED'>)
    """
    if low[0] == high[0]:
        return (
            tuple(f"{low[0]}.{m}" for m in range(low[1] + 1, high[1] + 1)),
            MinorEnumeration.ARITHMETIC,
        )
    return observed, MinorEnumeration.OBSERVED


def plan_walk(
    installed: str, recommended: str, available: tuple[str, ...]
) -> ReleaseWalk | None:
    """The walk from `installed` to `recommended` over the releases `available`.

    `available` is every release version a listing showed, in any order. The walk keeps
    those strictly above the floor and no higher than the recommended version -- strictly
    above, because the release already running introduced nothing this upgrade crosses.

    Returns None when either endpoint is unparseable. That is a refusal rather than a guess:
    a walk with an invented endpoint would produce a brief about releases nobody is moving
    between.

    >>> w = plan_walk("0.6.4", "0.9.0",
    ...               ("0.6.4", "0.7.0", "0.7.1", "0.9.0", "0.10.0"))
    >>> w.hops
    ('0.7.0', '0.7.1', '0.9.0')
    >>> w.minors_entered, w.minors_read, w.minors_unread
    (('0.7', '0.8', '0.9'), ('0.7', '0.9'), ('0.8',))
    >>> w.complete
    False
    >>> plan_walk("0.6.x", "0.7.0", ("0.6.1", "0.7.0")).hops
    ('0.6.1', '0.7.0')
    >>> plan_walk("nonsense", "0.9.0", ()) is None
    True
    """
    floor = floor_of(installed)
    high = parse_release(recommended)
    if floor is None or high is None:
        return None
    low, precision = floor

    triples = sorted(
        {t for t in (parse_release(v) for v in available) if t is not None and low < t <= high}
    )
    hops = tuple(".".join(str(p) for p in t) for t in triples)
    read = tuple(sorted({minor_label(t) for t in triples}, key=_minor_sort))
    entered, enumeration = _entered_minors(low, high, read)
    unread = tuple(m for m in entered if m not in set(read))

    return ReleaseWalk(
        installed=installed,
        recommended=recommended,
        precision=precision,
        enumeration=enumeration,
        hops=hops,
        minors_entered=entered,
        minors_read=read,
        minors_unread=unread,
        note=_walk_note(hops, entered, unread, precision, enumeration),
    )


def _minor_sort(label: str) -> tuple[int, int]:
    """Order minors numerically, so `0.10` follows `0.9` rather than preceding it.

    >>> sorted(("0.9", "0.10"), key=_minor_sort)
    ['0.9', '0.10']
    """
    major, _, minor = label.partition(".")
    return int(major), int(minor)


def _walk_note(
    hops: tuple[str, ...],
    entered: tuple[str, ...],
    unread: tuple[str, ...],
    precision: InstalledPrecision,
    enumeration: MinorEnumeration,
) -> str:
    """One sentence a reader can act on, naming what was walked and what was not."""
    parts = [
        f"{len(hops)} release(s) lie between the installed and the recommended version, "
        f"crossing {len(entered)} minor(s)"
    ]
    if unread:
        parts.append(
            f"no release notes were found for minor(s) {', '.join(unread)}, so anything "
            "those releases introduced is NOT in this brief"
        )
    else:
        parts.append("every minor the jump enters is represented by at least one release")
    if precision is InstalledPrecision.SERIES_FLOOR:
        parts.append(
            "the installed version was given as a series rather than an exact release, so "
            "the walk starts at that minor's .0 and may include patches already running; "
            "the minors the jump enters are unaffected"
        )
    if enumeration is MinorEnumeration.OBSERVED:
        parts.append(
            "the jump crosses a major version, so the minors listed are the ones a release "
            "listing showed rather than a complete enumeration"
        )
    return ". ".join(parts) + "."
