"""Which researched candidates are still awaiting a human, and which have aged out.

ONE RULE, TWO CALLERS, AND THAT IS WHY THIS FILE EXISTS. `stages/resolve.py` asks "may I
skip researching this component?" and `stages/approval_gate.py` asks "what should I put on
the page?" -- and those two questions MUST be answered from the same set. They were not:
resolve counted the pending prefix and the gate read only the current run's Map results, so a
candidate could suppress research while being invisible to the surface that could clear it.
A component in that state is stuck forever, and nothing reports it.

    resolve says   "skip it, an answer is waiting"
    the gate says  "I see nothing to ask about"
    the operator   never gets asked, and the fact never arrives

That is not a hypothetical. A double-start produced two executions; one was
stopped, its research Lambda kept running and wrote a candidate anyway, and metrics-server
was withheld from every later run by a candidate no page would ever offer.

**AGE-OUT DOES NOT DELETE.** A candidate older than `PENDING_TTL_HOURS` stops suppressing
research and stops being offered, but the object and its evidence bundle stay exactly where
they are. An S3 lifecycle rule would have been fewer lines and would have destroyed the
evidence that made the bug above findable -- and a rejected candidate is deliberately kept as
the record of a component that was researched and yielded no storable fact. Aging is a
question asked at read time, never a mutation.

**NEWEST PER (COMPONENT, SERIES).** Two runs researching one component write two candidates
answering one question. Offering both asks a person to decide the same thing twice, and
`view/build.py::approvals_from_research` would silently drop the second anyway -- keeping the
FIRST it saw, which is arbitrary. Choosing the newest here makes it deliberate: the most
recent research is the one that saw the most releases.

**ONE LIST CALL, NO GETS.** The component and series are path segments of `candidate_key`, so
counting and grouping need nothing opened. `LastModified` arrives on the same listing, so the
age filter is free too. Only the gate opens objects, and only for the ones it will offer.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "PENDING_PREFIX",
    "PENDING_TTL_HOURS",
    "REJECTED_PREFIX",
    "PendingCandidate",
    "list_pending",
    "newest_per_series",
    "pending_counts",
    "rejected_series",
]

# The two prefixes `candidate_key` writes under. Named here rather than rebuilt from
# `candidate_key` because a prefix is a LISTING concern -- `candidate_key` composes a whole
# key including the digest and the date, and neither is known when you want to list.
PENDING_PREFIX = "facts-candidates/decision=PENDING_APPROVAL/"
REJECTED_PREFIX = "facts-candidates/decision=REJECTED/"

# How long a candidate stays actionable.
#
# ONE DAY, and the number is a judgement about people rather than about storage. A candidate
# is a question asked of an operator; if it has gone unanswered for a day, the run that asked
# is long gone, the releases it read may have moved, and re-researching is both cheap enough
# and more honest than offering a stale answer. Overridable so a deployment that reviews
# weekly does not have to fork the module.
PENDING_TTL_HOURS = float(os.environ.get("PENDING_CANDIDATE_TTL_HOURS", "24") or 24)


@dataclass(frozen=True)
class PendingCandidate:
    """One candidate object awaiting a decision, as the LISTING describes it.

    Deliberately NOT the candidate document. Everything here comes from the key and the
    listing metadata, so a caller can group, count and age-filter without opening anything.
    The gate opens `key` only for the candidates it decides to offer.
    """

    component: str
    version_series: str
    key: str
    last_modified: datetime

    def age(self, *, now: datetime) -> timedelta:
        return now - self.last_modified

    def aged_out(self, *, now: datetime, ttl_hours: float = PENDING_TTL_HOURS) -> bool:
        return self.age(now=now) > timedelta(hours=ttl_hours)


def _segment(key: str, prefix: str) -> str:
    """The value of one `name=value` path segment, or "" when absent."""
    for part in key.split("/"):
        if part.startswith(prefix):
            return part.removeprefix(prefix).strip()
    return ""


def _list(bucket: str, prefix: str, client: Any = None) -> list[dict[str, Any]]:
    """Every object under one prefix, paginated.

    AN UNREADABLE PREFIX YIELDS NOTHING, and every caller must treat that as "suppress
    nothing / offer nothing" rather than as an error. The cost of a failed list is a
    component researched again; the cost of the opposite default is a real gap silently
    never researched because one API call failed.
    """
    if not bucket:
        return []
    if client is None:
        import boto3

        client = boto3.client("s3")
    out: list[dict[str, Any]] = []
    token: str | None = None
    try:
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            out.extend(page.get("Contents") or [])
            token = page.get("NextContinuationToken")
            if not token:
                return out
    except Exception as exc:
        # LOGGED, BECAUSE FAILING SAFE AND FAILING SILENTLY ARE NOT THE SAME THING. Returning
        # nothing is the right behaviour -- the alternative default stops researching real gaps
        # because one API call failed -- but the first deploy of this code hit exactly that path
        # and produced no evidence whatsoever: the approval role was missing `s3:ListBucket`, the
        # denial was swallowed here, and the run rendered "Nothing is awaiting approval" for a
        # run that had stopped precisely because something was. Nothing in any log said why.
        print(
            f"pending candidates under {prefix!r} could not be listed, so none is "
            f"offered and none is suppressed: {type(exc).__name__}: {exc}"
        )
        return []


def list_pending(
    bucket: str,
    *,
    now: datetime | None = None,
    ttl_hours: float = PENDING_TTL_HOURS,
    client: Any = None,
) -> tuple[PendingCandidate, ...]:
    """Every candidate still awaiting a decision AND still within its TTL.

    Only `.json` candidate objects: the `.evidence.json`, `.extraction.json` and
    `.report.json` written beside each one share the prefix, and counting them would report
    four pending answers where there is one -- which is exactly what the first version of the
    suppression did, and why `withheld_awaiting_approval` said `{"metrics-server": 4}`.
    """
    moment = now or datetime.now(UTC)
    found: list[PendingCandidate] = []
    for obj in _list(bucket, PENDING_PREFIX, client):
        key = str(obj.get("Key") or "")
        if not key.endswith(".json") or key.endswith(
            (".evidence.json", ".extraction.json", ".report.json")
        ):
            continue
        component = _segment(key, "component=")
        series = _segment(key, "series=")
        if not component:
            continue
        stamp = obj.get("LastModified")
        if not isinstance(stamp, datetime):
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        row = PendingCandidate(
            component=component,
            version_series=series,
            key=key,
            last_modified=stamp,
        )
        if row.aged_out(now=moment, ttl_hours=ttl_hours):
            continue
        found.append(row)
    return tuple(sorted(found, key=lambda r: (r.component, r.version_series, r.key)))


def newest_per_series(
    candidates: tuple[PendingCandidate, ...],
) -> tuple[PendingCandidate, ...]:
    """One candidate per `(component, series)` -- the most recently written.

    Ties broken on the key, so two objects written in the same second still produce a
    deterministic choice rather than one that depends on listing order.
    """
    best: dict[tuple[str, str], PendingCandidate] = {}
    for row in candidates:
        key = (row.component, row.version_series)
        held = best.get(key)
        if held is None or (row.last_modified, row.key) > (held.last_modified, held.key):
            best[key] = row
    return tuple(sorted(best.values(), key=lambda r: (r.component, r.version_series)))


def pending_counts(candidates: tuple[PendingCandidate, ...]) -> dict[str, int]:
    """How many actionable candidates each component has, for the withhold rule.

    Counts SERIES, not objects: the rule's question is "does an unanswered answer exist for
    this component", and two objects for one series are one question.
    """
    per_component: dict[str, set[str]] = {}
    for row in newest_per_series(candidates):
        per_component.setdefault(row.component, set()).add(row.version_series)
    return {name: len(series) for name, series in per_component.items()}


def rejected_series(bucket: str, client: Any = None) -> set[tuple[str, str]]:
    """Every `(component, series)` a person has already rejected.

    FLEET-WIDE, because that is what the prefix records and what a rejected FACT means: the
    claim was judged wrong, and a claim is not wrong on one cluster and right on another.
    A rejection that should apply to one cluster only is a different thing living in a
    different place -- see the per-cluster decision records, which is where "do not ask me
    again about this cluster" belongs.
    """
    found: set[tuple[str, str]] = set()
    for obj in _list(bucket, REJECTED_PREFIX, client):
        key = str(obj.get("Key") or "")
        if not key.endswith(".json") or key.endswith(
            (".evidence.json", ".extraction.json", ".report.json")
        ):
            continue
        component = _segment(key, "component=")
        if component:
            found.add((component, _segment(key, "series=")))
    return found
