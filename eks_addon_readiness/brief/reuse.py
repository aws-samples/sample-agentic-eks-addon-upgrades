"""When a brief already written for one upgrade may be served again instead of re-researched.

**THE UNIT OF REUSE IS THE UPGRADE, NOT THE RUN.** A brief answers exactly one question -- *what
happens when this component goes from this version to that one* -- and that answer does not depend
on which cluster asked or on what else the plan is moving. So a brief is stored under the component
and the version pair, and any later run moving the same component the same way serves it without
paying for the agent again.

An earlier version of this keyed the stored brief on the PLAN's digest, which was wrong in a way
worth recording: a plan digest moves whenever anything in the plan changes, so cert-manager
appearing in the plan would have made metrics-server's brief unfindable and re-researched. The
cache would have missed almost every time it mattered.

**IT EXPIRES ON A CLOCK, AND THAT IS THE OPPOSITE OF THE RULE FOR FACTS -- deliberately.**
`research/freshness.py` rejects a time expiry for stored facts, because every fact is
human-approved and a TTL would mean re-approving every fact forever, and because it would make the
same snapshot produce different verdicts on different days. Neither objection applies here:

- Nobody approves a brief. Re-researching one costs a model call, not a person's attention.
- A brief is ALREADY not reproducible. It says `reproducible: false` in its own contract, because
  its known-issues section is a reading of an issue tracker that changes daily. A mechanism that
  makes it vary with time is not introducing a property it was promising not to have.

So the drift mechanism that is right for facts would be wrong here, and a window that is wrong for
facts is right here. What goes stale is one section: new bugs get filed and old ones get closed.
Breaking changes in an already-published release do not change.

**THE RULESET DIGEST IS PART OF THE ADDRESS, not part of the expiry.** A brief produced under a
different prompt or different rules answers a subtly different question, so it is not the same
brief and must not be served for one. That is why the digest is a path segment rather than a field
to compare: a rules change makes the old briefs invisible rather than found-and-rejected.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# How long a stored brief may be served before it is researched again.
#
# SEVEN DAYS, the user's window. Long enough that a working session or a week's upgrade campaign
# pays for each component once; short enough that the known-issues section is a reading of this
# week's tracker rather than last month's. Overridable because it is a tuning decision rather than
# a correctness one -- every value produces correct output, just staler or costlier.
MAX_REUSE_AGE_DAYS = max(
    0, int(os.environ.get("READINESS_BRIEF_REUSE_MAX_AGE_DAYS", "7"))
)

# `date=2026-09-14.brief-0a83ea88841f.json`, the leaf `storage/keys.py::component_brief_key`
# writes. Parsed rather than assumed so a foreign object under the prefix is skipped instead of
# crashing the lookup.
_LEAF = re.compile(r"date=(\d{4}-\d{2}-\d{2})\.brief-[0-9a-f]{6,}\.json$")


@dataclass(frozen=True)
class StoredBrief:
    """One brief already on disk for this exact upgrade, and when it was written."""

    uri: str
    written_on: datetime

    def age_days(self, now: datetime) -> int:
        """Whole days between when it was written and now. Never negative.

        Clamped at zero because a brief written later than `now` -- a clock skew, or a replay with
        a pinned instant -- is not evidence that it is fresh for minus-one days, and a negative age
        would sort before everything and be served forever.

        >>> StoredBrief("s3://b/k", datetime(2026, 9, 7, tzinfo=UTC)).age_days(
        ...     datetime(2026, 9, 14, tzinfo=UTC))
        7
        >>> StoredBrief("s3://b/k", datetime(2026, 9, 20, tzinfo=UTC)).age_days(
        ...     datetime(2026, 9, 14, tzinfo=UTC))
        0
        """
        return max(0, (now - self.written_on).days)


def written_on(key: str) -> datetime | None:
    """The date a stored brief's key records, or None if the key is not one of ours.

    >>> written_on("briefs/components/component=x/from=1.0.0/to=2.0.0/rules-abc/"
    ...            "date=2026-09-14.brief-0a83ea88841f.json")
    datetime.datetime(2026, 9, 14, 0, 0, tzinfo=datetime.timezone.utc)
    >>> written_on("briefs/components/component=x/notes.txt") is None
    True
    """
    match = _LEAF.search(key)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def serve(
    stored: tuple[StoredBrief, ...], now: datetime, max_age_days: int = MAX_REUSE_AGE_DAYS
) -> StoredBrief | None:
    """The newest stored brief inside the window, or None to research it again.

    NEWEST FIRST, then the window. Serving an older brief when a newer one exists would report
    issues that a later pass had already superseded, and the two would disagree with no way for a
    reader to tell which was current.

    A window of ZERO disables reuse entirely, which is the honest way to express "always
    re-research" -- it is a legitimate setting for a run that must not serve anything cached, and
    it needs no second flag.

    >>> a = StoredBrief("s3://b/old", datetime(2026, 9, 1, tzinfo=UTC))
    >>> b = StoredBrief("s3://b/new", datetime(2026, 9, 12, tzinfo=UTC))
    >>> now = datetime(2026, 9, 14, tzinfo=UTC)
    >>> serve((a, b), now).uri
    's3://b/new'
    >>> serve((a,), now) is None
    True
    >>> serve((b,), now, max_age_days=0) is None
    True
    """
    if max_age_days <= 0 or not stored:
        return None
    newest = max(stored, key=lambda s: s.written_on)
    return newest if newest.age_days(now) <= max_age_days else None


def expiry_note(
    served: StoredBrief | None,
    stored_count: int,
    now: datetime,
    max_age_days: int = MAX_REUSE_AGE_DAYS,
) -> str:
    """One line saying whether a brief was served from storage and how old it is.

    Reaches the document, because a reader must never mistake a cached answer for a fresh one --
    especially in the known-issues section, which is the only part that goes stale.

    >>> now = datetime(2026, 9, 14, tzinfo=UTC)
    >>> expiry_note(StoredBrief("s3://b/k", datetime(2026, 9, 11, tzinfo=UTC)), 2, now)
    'served from a brief written 3 day(s) ago; 2 stored for this upgrade, within the 7-day \
reuse window'
    >>> expiry_note(None, 1, now)
    'researched fresh; 1 stored brief for this upgrade was outside the 7-day reuse window'
    >>> expiry_note(None, 0, now)
    'researched fresh; no brief was stored for this upgrade'
    """
    if served is not None:
        return (
            f"served from a brief written {served.age_days(now)} day(s) ago; "
            f"{stored_count} stored for this upgrade, within the {max_age_days}-day reuse window"
        )
    if stored_count:
        plural = "brief" if stored_count == 1 else "briefs"
        verb = "was" if stored_count == 1 else "were"
        return (
            f"researched fresh; {stored_count} stored {plural} for this upgrade {verb} outside "
            f"the {max_age_days}-day reuse window"
        )
    return "researched fresh; no brief was stored for this upgrade"


def cutoff(now: datetime, max_age_days: int = MAX_REUSE_AGE_DAYS) -> datetime:
    """The oldest instant a stored brief may carry and still be served.

    >>> cutoff(datetime(2026, 9, 14, tzinfo=UTC))
    datetime.datetime(2026, 9, 7, 0, 0, tzinfo=datetime.timezone.utc)
    """
    return now - timedelta(days=max_age_days)
