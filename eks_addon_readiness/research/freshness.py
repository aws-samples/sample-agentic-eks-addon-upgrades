"""The freshness sweep: which stored facts have gone stale, decided by an UPSTREAM RELEASE.

**A fact goes stale when its component cuts a release the fact never saw.** The sweep derives
the component's GitHub repository, asks GitHub for the newest release there, and compares that
release's `published_at` against the fact's own `refreshed_at`. Newer means a release landed
after we read: that is drift, and it marks.

**Why a RELEASE and not a commit, which is the narrowing that makes this usable.** A release
is what can change a support window -- a new minor arrives, an old one leaves support, and the
compatibility table the fact records is out of date. A commit on the default branch is mostly
CI and documentation churn: "has master changed since yesterday" is true almost always on an
active repository, so marking on it would withdraw every fact on an unrelated workflow tweak
and send a human back to re-approve facts that did not change. There is deliberately NO
branch-head check as a second trigger.

**Why only GitHub.** Releases happen on GitHub; a documentation site re-renders on its own
schedule and says nothing about whether the component moved. So `cert-manager.io`, `keda.sh`
and every other non-GitHub citation are out of scope, and a fact with no GitHub repository
anywhere is reported NOT_COMPARABLE rather than guessed at.

**WHAT THIS REPLACED, and it is worth keeping written down because the replaced version
looked right.** The first implementation compared REVISIONS: each fact pinned the revision it
was read at, and the sweep resolved the source's branch head and marked when the two differed.
A live sweep document showed why it could never work --

    component_id metrics-server, outcome NOT_COMPARABLE,
    pinned_revision "master", upstream_revision null, marked []

-- because the fact had recorded the ref NAME, `master`, as its pinned revision. The resolver
answered with a commit sha, and a sha never equals a branch name, so the comparison was
between two different kinds of value. All three of that fact's sources were in that state: the
mechanism ran, cost nothing, and could never fire. Comparing a release's publication instant
against the fact's read instant needs nothing to have been recorded in advance, which is why
the research side needed no change at all.

**Two instants, and still not a time expiry.** The decision compares the fact's own
`refreshed_at` against upstream's `published_at`. `now` never enters it. So the same fact and
the same upstream state give the same verdict on any day -- which is the property a TTL
destroyed, and the reason a TTL was implemented first and was wrong:

- Every fact is human-approved. A time expiry therefore means re-approving every fact every
  day, forever -- a treadmill, not a safety property. The evidence was on the ground within
  minutes of deploying it: the one seeded fact went stale immediately because it had been
  authored 56 days earlier, despite its source being untouched.
- It makes the same snapshot produce different verdicts on different days. The digest chain
  exists precisely to prevent that, so a mechanism that reintroduces it defeats the thing it
  was meant to protect.

A release comparison has neither problem. A fact is re-researched when the component shipped
something, which is the only occasion on which re-approving it tells anyone anything new.

**Where the check runs is still the load-bearing constraint, and it has not moved.** The check
does NOT go in the assessment read path. Stage 3 is a pure function of already-fetched
evidence plus the fact store, and that purity is what makes replay work. A network call inside
Stage 3 would make the gap ledger depend on GitHub's state at the instant it ran: the same
snapshot and the same store would produce two different ledgers minutes apart, both citing the
same digests. Reproducibility would be gone and the digests would still claim it.

So the sweep runs beside the pipeline and **writes a mark into a separate table**. Stage 3
reads the mark and reports the component as researchable exactly as it already does for a
component with no fact at all -- a pure read, no frozen module changed. Properties this keeps:

- **Reproducibility intact.** The ledger depends only on the store, whose digest is already
  cited, and the mark is folded into that digest.
- **Cost scales with distinct REPOSITORIES**, not with facts, clusters or runs. The resolver
  caches per repository for the life of the sweep, so a hundred clusters sharing one fact --
  or several facts sharing one repository -- cost one request.
- **The mark is auditable** -- which repository was asked, which release it reported, when that
  release was published, and when the fact was read.
- **It reuses the existing lazy-acquisition path** rather than adding a second trigger.
- **A stale fact never blocks the pipeline.** It is withheld, not awaited; the run completes
  and answers every other component.

**Two cadences, one mechanism.** A scheduled sweep keeps the store honest in the background; a
**pre-flight sweep** covers only the components in an imminent run, so a run can be fresh at
its moment without any stage doing I/O. They differ in which facts they visit and in nothing
else.

**A MARK IS SPENT ONCE ITS FACT HAS BEEN REPLACED, and missing that cost three runs and three
emails to the same person.** The mark records the revision it was raised against. When a human
approves the re-research, the store holds a NEW revision and the old mark is about a fact that
is no longer there -- so it withholds nothing (`MarkLedger.suppresses`), this sweep stops
treating the component as already marked, and it re-reads upstream and judges the replacement
on its own comparison. A component whose replacement is also behind a newer release is
re-marked with ITS revision recorded, so the new mark is spendable in turn; one that is not
comes back UNMOVED and its fact answers. The spent mark is then stamped retired
(`retire_marks`) so it does not linger in the table -- housekeeping the loop-breaker does not
depend on, because a mark that names an absent revision already withholds nothing whether or
not the stamp is ever written.

**No model, ever.** Reading a repository's newest release is one cheap request. Nothing here
calls a model, and the sweep's IAM role grants no Bedrock.

**THE SWEEP PUBLISHES ITS OWN DOCUMENT** (`SweepDocument`, below), digested and keyed like
every stage document. A mark says a fact stopped answering; the document says *why*, in the
only terms that are checkable -- the repository, the release tag, when that release was
published, and when the fact was read. That is the question a person asks of a report, and a
prose `reason` on a DynamoDB item is not where they can ask it.

It is the ONE document in this system whose content is legitimately a function of the instant
it was produced, and that is not a loosening of the reproducibility rule but the reason the
rule places this work outside every stage. A stage document must be a pure function of
already-fetched evidence; this document is *about* a network read, so a reader who finds two
sweeps disagreeing has learned something true rather than found a defect. Its key carries its
own digest, so two sweeps that disagree are two objects and neither erases the other.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from pydantic import Field, model_validator

from ..knowledge import ComponentKnowledge
from ..models import ClusterReference, ContractModel
from .marks import FactMark, MarkKind, MarkLedger
from .ruleset import ruleset_digest

# Identifies marks this module wrote, so an auditor can separate an automatic staleness mark
# from a human revocation without reading the reason.
SWEEP_ACTOR = "freshness-sweep"


@dataclass(frozen=True)
class UpstreamRelease:
    """The newest release GitHub reports for one repository.

    Named `UpstreamRelease` rather than `ReleaseSighting` on purpose: this package already has
    a `ReleaseSightingInput`, which is a MODEL EXTRACTION -- a release a judge read out of a
    document. This is a live read of the GitHub API, and giving two different things names one
    word apart is the homonym defect the resolve/resolve rename exists to remove.

    `published_at` is the decision. `tag` and `url` are the evidence that lets a human check
    the decision without re-running the sweep.
    """

    repository: str
    tag: str
    published_at: datetime
    url: str | None = None
    # Whether GitHub flags this as a pre-release. Recorded rather than filtered: a pre-release
    # is still a release the fact never saw, and reporting it lets a reader see which kind
    # decided their fact's outcome.
    prerelease: bool = False


class UpstreamResolver(Protocol):
    """Answers "what is the newest release in this repository?" -- one cheap read, no model.

    A Protocol rather than a concrete client so the whole decision is testable with no
    network: `StaticUpstreamResolver` answers from a dict, and the deployed
    `GitHubUpstreamResolver` answers from the GitHub API. Nothing above this port knows which
    it is talking to.

    Takes `owner/repo` rather than a URL, because the question is about the COMPONENT and not
    about any one page it cites. Deriving the repository is `github_repository`'s job, on the
    rules' side of this port, so the resolver has no opinion about what a fact looks like.
    """

    def latest_release(self, repository: str) -> UpstreamRelease | None:
        """The newest release in `repository`, or None if none could be established."""
        ...


@dataclass(frozen=True)
class DriftResult:
    """Whether one fact's component has released since the fact was read, and what decided it.

    Carries the whole comparison rather than just the verdict, because the mark's reason has
    to name it: "kubernetes-sigs/metrics-server released v0.9.1 on X, after this fact was read
    on Y" is auditable, "stale" is not.

    `comparable` is separate from `stale` because the two not-stale cases mean different
    things. "We asked and nothing has been released since" is knowledge; "we could not
    establish a newest release" is the absence of it, and a reader acts differently on each.
    Collapsing them into one boolean is how "we could not read GitHub" comes to look like
    "nothing changed".
    """

    stale: bool
    comparable: bool
    repository: str | None
    release_tag: str | None
    release_published_at: datetime | None
    fact_refreshed_at: datetime | None
    url: str | None
    reason: str


class StaticUpstreamResolver:
    """An `UpstreamResolver` backed by a dict, keyed by `owner/repo`.

    Not only a test double: a caller reproducing a past sweep supplies one of these through
    the event's `pinned_releases`, and a deployment that wants to freeze staleness detection
    entirely can supply an empty one. An unknown repository resolves to None, which is treated
    as "could not establish a release" and never as drift.

    Values may be an `UpstreamRelease` or the JSON shape the event carries --
    `{"tag": ..., "published_at": ..., "url": ..., "prerelease": ...}` -- so a frozen sweep
    is expressible in a Step Functions payload without constructing anything.
    """

    def __init__(
        self, releases: Mapping[str, UpstreamRelease | Mapping[str, Any]]
    ) -> None:
        self._releases = {
            _normalise_repository(repository): _as_release(
                _normalise_repository(repository), value
            )
            for repository, value in releases.items()
        }

    def latest_release(self, repository: str) -> UpstreamRelease | None:
        return self._releases.get(_normalise_repository(repository))


def _as_release(
    repository: str, value: UpstreamRelease | Mapping[str, Any]
) -> UpstreamRelease:
    """One frozen release, from an `UpstreamRelease` or from the event's JSON shape."""
    if isinstance(value, UpstreamRelease):
        return value
    published = value["published_at"]
    return UpstreamRelease(
        repository=repository,
        tag=str(value["tag"]),
        # `_aware` on both paths: a frozen release supplied as a naive datetime would raise
        # TypeError inside the comparison instead of producing a verdict.
        published_at=(
            _aware(published)
            if isinstance(published, datetime)
            else _parse_instant(str(published))
        ),
        url=str(value["url"]) if value.get("url") else None,
        prerelease=bool(value.get("prerelease", False)),
    )


class GitHubUpstreamResolver:
    """Reads a repository's newest release from the GitHub API.

    ONE REQUEST PER REPOSITORY, cached for the life of the sweep -- including a None answer,
    so a repository that has published nothing is not asked twice. Several facts sharing one
    repository therefore cost one request, and so does the same fact seen by a hundred
    clusters.

    TWO ENDPOINTS, IN THAT ORDER, and the second is not redundant:

        /repos/{repo}/releases/latest      the newest FULL release
        /repos/{repo}/releases?per_page=1  the newest release of any kind

    `releases/latest` excludes pre-releases and 404s for a repository whose only releases are
    pre-releases -- which would silently report "no release" for a component that ships them,
    and a component that ships only pre-releases still ships. So the list endpoint answers
    when the first finds nothing. The common case is one request; the fallback costs a second
    only for repositories `releases/latest` cannot answer for.

    THE LIMIT THIS LEAVES, stated rather than hidden: a repository with an older full release
    AND a newer pre-release is answered from `releases/latest`, so the pre-release is not
    seen. That direction of error withholds nothing -- it can only fail to mark -- which is
    the side this module errs on everywhere else too.
    """

    def __init__(self, token: str | None = None, timeout: int = 15) -> None:
        self._token = token
        self._timeout = timeout
        self._cache: dict[str, UpstreamRelease | None] = {}

    def latest_release(self, repository: str) -> UpstreamRelease | None:
        key = _normalise_repository(repository)
        if not key:
            return None
        if key in self._cache:
            return self._cache[key]
        release = self._fetch_latest_release(key)
        self._cache[key] = release
        return release

    def _fetch_latest_release(self, repository: str) -> UpstreamRelease | None:
        newest = self._get(f"https://api.github.com/repos/{repository}/releases/latest")
        if not isinstance(newest, dict) or not newest.get("published_at"):
            listed = self._get(
                f"https://api.github.com/repos/{repository}/releases?per_page=1"
            )
            newest = listed[0] if isinstance(listed, list) and listed else None
        if not isinstance(newest, dict):
            return None
        # A draft is not a release anyone upstream can have consumed, and its `published_at`
        # is null anyway. Both guards are needed: an unauthenticated read never sees drafts,
        # but a token with push access does.
        if newest.get("draft") or not newest.get("published_at"):
            return None
        tag = newest.get("tag_name") or newest.get("name")
        if not tag:
            return None
        try:
            published_at = _parse_instant(str(newest["published_at"]))
        except ValueError:
            # An unparseable instant is a read we cannot act on, which is the same answer as
            # a read that failed: None, which never marks.
            return None
        return UpstreamRelease(
            repository=repository,
            tag=str(tag),
            published_at=published_at,
            url=str(newest["html_url"]) if newest.get("html_url") else None,
            prerelease=bool(newest.get("prerelease", False)),
        )

    def _get(self, api: str) -> Any:
        import json
        import urllib.error
        import urllib.request

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "eks-addon-readiness/2 (freshness sweep; read-only)",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            request = urllib.request.Request(api, headers=headers)
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError):
            # Unreadable upstream is NOT evidence that a release happened. Returning None
            # means "could not establish", which never marks -- otherwise a bad minute at
            # GitHub, a rate limit or a 404 would withdraw every fact in the store.
            return None


def _normalise_repository(repository: str) -> str:
    """`owner/repo`, casefolded. GitHub is case-insensitive here, so the cache key is too."""
    return repository.strip().strip("/").casefold()


def _parse_instant(value: str) -> datetime:
    """One ISO-8601 instant, always offset-aware. GitHub sends `Z`; a fact may send an offset.

    >>> _parse_instant("2026-07-13T12:00:00Z").isoformat()
    '2026-07-13T12:00:00+00:00'
    >>> _parse_instant("2026-07-13T12:00:00").tzinfo is not None
    True
    """
    return _aware(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))


def _aware(moment: datetime) -> datetime:
    """The same instant, offset-aware. A naive one is read as UTC, as it is written.

    Stored facts have carried naive `refreshed_at` values, and comparing one against an
    offset-aware release instant raises `TypeError` -- a crash in the sweep rather than a
    verdict. Normalising both sides here is what keeps that out of every comparison.
    """
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def cited_urls(fact: ComponentKnowledge) -> tuple[str, ...]:
    """Every URL this fact cites, deduplicated and ordered.

    From the citations on the compatibility rules, the releases and the verification checks --
    everywhere a source is recorded. Ordered so repository derivation is deterministic: two
    sweeps over the same fact must ask about the same repository.

    Unlike the revision-comparing version this replaced, a citation with no recorded
    `source_revision` is INCLUDED. The revision is no longer part of any decision, so
    excluding a citation for lacking one would only narrow where the repository can be found.
    """
    seen: dict[str, None] = {}
    for rule in fact.compatibility_rules:
        for citation in rule.evidence:
            seen.setdefault(str(citation.url), None)
    for release in fact.releases:
        for citation in release.evidence:
            seen.setdefault(str(citation.url), None)
    for check in fact.verification_checks:
        for citation in check.evidence:
            seen.setdefault(str(citation.url), None)
    return tuple(sorted(seen))


def github_repository(fact: ComponentKnowledge) -> str | None:
    """The GitHub `owner/repo` whose releases decide this fact's freshness, or None.

    `canonical_repository` FIRST, because it is the fact's own answer to "where does this
    component live" and it is the only candidate that cannot be a documentation repository.
    keda is the case that settles the order: it cites `kedacore/keda-docs` and
    `kedacore/governance` as well as its own repo, and a release in a docs repository is not a
    release of keda.

    Citations are the fallback, in `cited_urls` order, for a component whose canonical
    repository is not on GitHub but which still cites one. A fact with GitHub nowhere returns
    None and is reported NOT_COMPARABLE -- never guessed at, and never stale.

    NOT SHARED WITH `authority._repo_path`, which parses the same shape. The sweep's ruleset
    digest covers `freshness` and nothing else (see `research/ruleset.py`), so importing the
    parse would let an edit in `authority.py` change what the sweep decides while the sweep's
    own digest stayed put -- a changed rule passing as the original, which is the exact
    failure these digests exist to prevent.
    """
    for candidate in (str(fact.canonical_repository), *cited_urls(fact)):
        repository = _github_repository_of(candidate)
        if repository:
            return repository
    return None


def _github_repository_of(url: str) -> str:
    """The `owner/repo` in a GitHub URL, or the empty string.

    >>> _github_repository_of("https://github.com/kubernetes-sigs/metrics-server")
    'kubernetes-sigs/metrics-server'
    >>> _github_repository_of("https://github.com/kedacore/keda/releases?page=2")
    'kedacore/keda'
    >>> _github_repository_of("https://raw.githubusercontent.com/o/r/main/README.md")
    'o/r'
    >>> _github_repository_of("https://cert-manager.io/docs/installation/")
    ''
    >>> _github_repository_of("https://raw.githubusercontent.com.evil.test/o/r/x/y.md")
    ''
    """
    host = (urlparse(url).hostname or "").casefold()
    if host not in ("github.com", "www.github.com", "raw.githubusercontent.com"):
        return ""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2:
        return ""
    return _normalise_repository(f"{parts[0]}/{parts[1]}")


def is_stale(fact: ComponentKnowledge, resolver: UpstreamResolver) -> DriftResult:
    """Whether this fact's component has released since the fact was read.

    `now` never enters this function. The comparison is the fact's own `refreshed_at` against
    the release's `published_at`, so the same fact and the same upstream state give the same
    verdict on any day -- the property a 24-hour window destroyed.

    STRICTLY NEWER marks. A release published at the same instant the fact was read is one the
    fact already saw, and re-approving on it would be a treadmill of exactly one step.

    Two ways to be not-comparable, kept apart in the reason because they tell a reader to do
    different things: no GitHub repository could be derived (nothing to ask), or GitHub
    established no newest release (nothing was answered). Neither is evidence of a release,
    so neither marks.
    """
    refreshed = _aware(fact.refreshed_at)
    repository = github_repository(fact)
    if repository is None:
        return DriftResult(
            stale=False,
            comparable=False,
            repository=None,
            release_tag=None,
            release_published_at=None,
            fact_refreshed_at=refreshed,
            url=None,
            reason=(
                "no GitHub repository could be derived for this fact: its canonical "
                f"repository ({fact.canonical_repository}) is not a GitHub URL and none of "
                "its citations name one. What could not be determined is whether a release "
                "landed after the fact was read, because GitHub releases are the only signal "
                "this sweep reads -- a documentation site re-renders on its own schedule and "
                "says nothing about whether the component moved. Not stale: an unanswerable "
                "question is not evidence of a change."
            ),
        )

    release = resolver.latest_release(repository)
    if release is None:
        return DriftResult(
            stale=False,
            comparable=False,
            repository=repository,
            release_tag=None,
            release_published_at=None,
            fact_refreshed_at=refreshed,
            url=None,
            reason=(
                f"no newest release could be established for {repository}: either the "
                "repository has published none, or GitHub could not be read. What could not "
                f"be determined is whether a release landed after {refreshed.isoformat()}, "
                "when this fact was read. Not stale -- marking on an unread upstream would "
                "withdraw every fact in the store whenever GitHub had a bad minute."
            ),
        )

    kind = "pre-release" if release.prerelease else "release"
    if release.published_at > refreshed:
        return DriftResult(
            stale=True,
            comparable=True,
            repository=repository,
            release_tag=release.tag,
            release_published_at=release.published_at,
            fact_refreshed_at=refreshed,
            url=release.url,
            reason=(
                f"{repository} published {kind} {release.tag} at "
                f"{release.published_at.isoformat()}, after this fact was read at "
                f"{refreshed.isoformat()}. A release can move the support window this fact "
                "records, so the fact is withheld and the component is reported as "
                "researchable; the next run re-researches it against the new release."
            ),
        )

    return DriftResult(
        stale=False,
        comparable=True,
        repository=repository,
        release_tag=release.tag,
        release_published_at=release.published_at,
        fact_refreshed_at=refreshed,
        url=release.url,
        reason=(
            f"the newest {kind} {repository} reports is {release.tag}, published at "
            f"{release.published_at.isoformat()}, which is not after this fact was read at "
            f"{refreshed.isoformat()}. Nothing has been released since, so there is nothing "
            "to re-research however old the fact is."
        ),
    )




def stale_mark(
    fact: ComponentKnowledge,
    drift: DriftResult,
    now: datetime,
    *,
    version_series: str = "*",
    actor: str = SWEEP_ACTOR,
) -> FactMark:
    """The staleness mark for a fact whose component released, naming the whole comparison.

    The reason states the repository, the release tag, when that release was published and when
    the fact was read, so the mark is auditable on its own terms: a reader can re-run the
    comparison by hand. "Stale" alone would not be checkable, and a mark nobody can check is a
    mark nobody should trust.
    """
    refreshed = _aware(fact.refreshed_at)
    return FactMark(
        component_id=fact.canonical_id,
        version_series=version_series,
        kind=MarkKind.STALE,
        marked_at=now,
        marked_by=actor,
        reason=drift.reason,
        fact_refreshed_at=refreshed,
        fact_revision=fact.knowledge_base_revision,
    )


def revoke_mark(
    component_id: str,
    reason: str,
    now: datetime,
    *,
    version_series: str = "*",
    actor: str,
    fact: ComponentKnowledge | None = None,
    consumed_by: tuple[str, ...] = (),
) -> FactMark:
    """A revocation: this fact is wrong and must stop answering now.

    Distinct from staleness because the two mean different things and imply different
    follow-up. Stale means "the source moved, re-read it"; revoked means "this is wrong,
    re-research it AND recompute everything that consumed it", which is why `consumed_by`
    exists.

    `actor` is required. A revocation with no accountable author is how a fact quietly
    disappears, which is what the audit trail exists to prevent.
    """
    return FactMark(
        component_id=component_id,
        version_series=version_series,
        kind=MarkKind.REVOKED,
        marked_at=now,
        marked_by=actor,
        reason=reason,
        fact_refreshed_at=fact.refreshed_at if fact is not None else None,
        fact_revision=fact.knowledge_base_revision if fact is not None else None,
        consumed_by=consumed_by,
    )


@dataclass(frozen=True)
class FactVisit:
    """One fact the sweep looked at, and everything the document needs to explain it.

    Retained per fact rather than summarised into counts, because the document is evidence:
    a reader asking "why is metrics-server withheld" needs this fact's own read instant and
    the release its own repository reported, not a total.

    `drift` is None exactly when a mark was already in force, and that distinction is
    load-bearing rather than incidental: an already-marked fact is skipped BEFORE the
    resolver is consulted, so there is no upstream read and no answer to report. Recording
    None rather than a fabricated "unchanged" keeps the document honest about what the
    sweep actually did.

    `retired_marks` are the spent marks this visit stamped: raised against a revision the
    store no longer holds, so they were already withholding nothing before the sweep touched
    them. Recorded on the visit so the retirement is reportable rather than a silent tidy-up.
    """

    component_id: str
    fact_revision: str | None
    fact_refreshed_at: datetime | None
    drift: DriftResult | None
    existing_mark: FactMark | None
    retired_marks: tuple[FactMark, ...] = ()


class SweepResult:
    """What a sweep visited, what it found drifted, and the ledger to write.

    Carries the counts so a sweep that changed nothing is distinguishable from a sweep that
    failed to run -- an operational difference an empty ledger hides.
    """

    def __init__(
        self,
        visited: tuple[str, ...],
        new_marks: tuple[FactMark, ...],
        ledger: MarkLedger,
        unresolved: tuple[str, ...] = (),
        visits: tuple[FactVisit, ...] = (),
        retired_marks: tuple[FactMark, ...] = (),
    ) -> None:
        self.visited = visited
        self.new_marks = new_marks
        self.ledger = ledger
        self.unresolved = unresolved
        self.visits = visits
        self.retired_marks = retired_marks

    @property
    def expired(self) -> tuple[str, ...]:
        """Components marked stale by this sweep. Named `expired` for the caller's shape."""
        return tuple(m.component_id for m in self.new_marks)

    @property
    def retired(self) -> tuple[str, ...]:
        """Components whose spent mark this sweep retired, deduplicated and ordered."""
        return tuple(sorted({m.component_id for m in self.retired_marks}))

    def summary(self) -> str:
        """A one-line summary. Names no time window: an upstream release is the mechanism."""
        drifted = len(self.new_marks)
        unresolved = f", {len(self.unresolved)} upstream unresolved" if self.unresolved else ""
        retired = f", {len(self.retired_marks)} spent mark(s) retired" if self.retired_marks else ""
        return (
            f"visited {len(self.visited)} fact(s), {drifted} whose component released"
            f"{unresolved}{retired}, {len(self.ledger)} mark(s) now in force"
        )


def sweep(
    facts: tuple[ComponentKnowledge, ...],
    existing: MarkLedger,
    now: datetime,
    *,
    resolver: UpstreamResolver,
    only: tuple[str, ...] | None = None,
    actor: str = SWEEP_ACTOR,
) -> SweepResult:
    """Mark every visited fact whose component has released since. Pure but for the resolver.

    `only` selects the pre-flight form: pass the identities in an imminent run and just those
    facts are visited. Omit it for the scheduled form, which visits everything. The two
    cadences are this one parameter, which is what keeps them the same mechanism.

    A fact already carrying a mark is not re-marked, so a sweep is idempotent and running it
    twice neither accumulates marks nor moves the store digest a second time. A revoked fact
    is skipped entirely: revocation is the stronger statement and a staleness mark beside it
    would dilute the audit trail.

    **A SPENT MARK IS RETIRED AND THE FACT IS THEN JUDGED ON ITS OWN MERITS.** "Already
    marked" is asked of the revision the store holds now, not of the component: a staleness
    mark raised against a revision that a human has since replaced withholds nothing
    (`MarkLedger.suppresses`), so this sweep falls through to `is_stale` and re-reads upstream
    for it. The fresh fact is then marked on its own comparison -- with ITS revision recorded,
    so the mark is spendable in turn -- or comes back UNMOVED and answers. The spent mark is
    stamped retired so it does not linger in the table; that stamp is housekeeping, and
    nothing here depends on it succeeding.
    """
    wanted = {c.strip() for c in only} if only is not None else None
    visited: list[str] = []
    new_marks: list[FactMark] = []
    unresolved: list[str] = []
    visits: list[FactVisit] = []
    # Keyed by row rather than listed, because a retirement and a fresh mark can land on the
    # SAME row (`component#*#STALE`) and only one of them can be there. See `_rows_after`.
    retirements: dict[tuple[str, str, str], FactMark] = {}
    replacements: dict[tuple[str, str, str], FactMark] = {}

    for fact in sorted(facts, key=lambda f: f.canonical_id):
        identities = {fact.canonical_id, *fact.aliases}
        if wanted is not None and not (identities & wanted):
            continue
        visited.append(fact.canonical_id)
        revision = fact.knowledge_base_revision
        in_force = existing.suppresses(fact.canonical_id, fact_revision=revision)
        if in_force is not None:
            visits.append(
                FactVisit(
                    component_id=fact.canonical_id,
                    fact_revision=revision,
                    fact_refreshed_at=fact.refreshed_at,
                    drift=None,
                    existing_mark=in_force,
                )
            )
            continue
        spent = tuple(
            mark.retired(now, by=actor, reason=_retirement_reason(mark, revision))
            for mark in existing.obsolete_marks(fact.canonical_id, revision)
        )
        for mark in spent:
            retirements[mark.row_key] = mark
        drift = is_stale(fact, resolver)
        visits.append(
            FactVisit(
                component_id=fact.canonical_id,
                fact_revision=revision,
                fact_refreshed_at=fact.refreshed_at,
                drift=drift,
                existing_mark=None,
                retired_marks=spent,
            )
        )
        if drift.stale:
            fresh = stale_mark(fact, drift, now, actor=actor)
            new_marks.append(fresh)
            replacements[fresh.row_key] = fresh
        elif not drift.comparable and drift.repository is not None:
            # `unresolved` means "we knew which repository to ask and could not get an
            # answer" -- an operational signal. A fact with no GitHub repository at all is
            # NOT unresolved: nothing failed, there was simply nothing to ask, and folding
            # the two together would make a permanent scope decision look like an outage.
            unresolved.append(fact.canonical_id)

    # A retirement whose row a fresh mark has taken is not reported as retired: the row is in
    # force again, for a reason of its own, and reporting the component as both retired and
    # marked in one sweep would describe two states of one row as though they coexisted.
    retired_marks = tuple(
        mark for key, mark in retirements.items() if key not in replacements
    )
    return SweepResult(
        tuple(visited),
        tuple(new_marks),
        MarkLedger(marks=_rows_after(existing, retirements, replacements)),
        tuple(unresolved),
        tuple(visits),
        retired_marks,
    )


def _retirement_reason(mark: FactMark, fact_revision: str) -> str:
    """Why a mark was retired, in terms a reader can check against the two revisions."""
    return (
        f"retired: this mark was raised against revision {mark.fact_revision}, and the store "
        f"now holds revision {fact_revision}. The fact the mark names is no longer there, so "
        "the mark withheld a replacement it had said nothing about -- which reported the "
        "component researchable run after run and sent its approval back to a person every "
        "time. The replacement is judged on its own comparison against upstream by this same "
        "sweep."
    )


def _rows_after(
    existing: MarkLedger,
    retirements: dict[tuple[str, str, str], FactMark],
    replacements: dict[tuple[str, str, str], FactMark],
) -> tuple[FactMark, ...]:
    """The ledger the sweep leaves behind, one mark per row.

    `MarkStore.save` writes one item per mark at `component_id` + `version_series#kind`, so
    two marks sharing those coordinates share one row and the later put silently wins.
    Collapsing the collision here rather than leaving it to iteration order keeps the ledger
    and the table the same document: a fresh mark REPLACES the spent one it supersedes, in the
    spent mark's own position, and a retirement with no replacement keeps its row with the
    stamp on it.
    """
    rows: list[FactMark] = []
    pending = dict(replacements)
    for mark in existing.marks:
        key = mark.row_key
        if key in pending:
            rows.append(pending.pop(key))
        elif key in retirements:
            rows.append(retirements[key])
        else:
            rows.append(mark)
    rows.extend(pending.values())
    return tuple(rows)


def retire_marks(
    existing: MarkLedger,
    component_id: str,
    kinds: tuple[MarkKind, ...],
    now: datetime,
    *,
    reason: str,
    actor: str = SWEEP_ACTOR,
) -> MarkLedger:
    """End the force of a component's marks of the given kinds, keeping their rows.

    Called when a fresh fact replaces a drifted one: the new fact is pinned at the revision
    upstream now reports, so it is not stale and the mark that withheld its predecessor must
    stop being in force, or the component would be reported researchable forever.

    **RETIRES RATHER THAN DROPS, and the difference is a live defect.** This function used to
    remove the mark from the ledger, which does not remove it from the table: `MarkStore.save`
    is a put per mark and deletes nothing, so a dropped mark was simply not rewritten and the
    next `load` read the original row straight back. Stamping the mark retired means the put
    overwrites the row that was doing the withholding -- and needs no `dynamodb:DeleteItem`,
    which is a grant an operator would have to approve.

    A revocation is NOT retired by a re-research unless a caller asks explicitly, because the
    reason a fact was revoked may well apply to its replacement -- if the logic that produced
    it was defective, re-running that logic reproduces the defect.
    """
    return MarkLedger(
        marks=tuple(
            m.retired(now, by=actor, reason=reason)
            if (m.component_id == component_id.strip() and m.kind in kinds and m.in_force)
            else m
            for m in existing.marks
        )
    )


# ---------------------------------------------------------------------------
# THE SWEEP'S OWN DOCUMENT
# ---------------------------------------------------------------------------


class SweepMode(StrEnum):
    """Which cadence produced a sweep. The same mechanism, one argument apart.

    In the path as well as in the body (see `storage/keys.sweep_key`), for the reason
    `candidate_key` puts the acceptance decision in the path: an operator can list what a
    run swept without opening anything, and a fleet-wide sweep cannot overwrite a
    run-scoped one.
    """

    PRE_FLIGHT = "pre-flight"
    SCHEDULED = "scheduled"


class SweepOutcome(StrEnum):
    """What the sweep concluded about one fact.

    Four outcomes rather than a boolean, because the three not-stale cases mean different
    things and a reader acts differently on each. Collapsing them is how "we could not read
    GitHub" comes to look like "nothing changed".
    """

    # The component released after the fact was read. This sweep wrote a staleness mark; the
    # fact stops answering.
    MARKED_STALE = "MARKED_STALE"
    # The repository was asked and its newest release is not newer than the fact's read.
    UNMOVED = "UNMOVED"
    # Nothing could be compared: no GitHub repository could be derived from the fact, or no
    # newest release could be established for the one that was. NOT evidence of a release --
    # marking on either would withdraw facts for reasons that are not evidence.
    NOT_COMPARABLE = "NOT_COMPARABLE"
    # A mark was already in force, so the fact was not re-marked and upstream was not read.
    ALREADY_MARKED = "ALREADY_MARKED"


class SweepFinding(ContractModel):
    """One visited fact, and the comparison that decided its outcome.

    Every part of the comparison is recorded even when nothing was released, because "why is
    this fact still trusted" is as much a question as "why was it withdrawn", and an answer a
    reader can re-check by hand is worth more than a verdict they must take on faith.
    """

    component_id: str = Field(min_length=1)
    outcome: SweepOutcome
    # The fact's own identity at the time of the sweep, so the finding still means
    # something after the fact is re-researched and replaced.
    fact_revision: str | None = None
    # The left-hand side of the comparison: when this fact was read.
    fact_refreshed_at: str | None = None
    # The repository that was asked. Null when none could be derived from the fact, which is
    # itself the answer to "why was nothing compared".
    repository: str | None = None
    # The right-hand side: the newest release that repository reported, and when it was
    # published. Null when no release could be established.
    release_tag: str | None = None
    release_published_at: str | None = None
    # The release's own page, when GitHub gave one. Evidence, not a decision.
    url: str | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def a_marked_finding_names_the_whole_comparison(self) -> SweepFinding:
        """A staleness claim missing either side of the comparison is not checkable.

        The mark says a release landed after the fact was read. Both instants and the release
        that carries one of them must be on the finding, or nobody can re-check it -- and a
        mark nobody can check is a mark nobody should trust.
        """
        if self.outcome is SweepOutcome.MARKED_STALE and not (
            self.repository
            and self.release_tag
            and self.release_published_at
            and self.fact_refreshed_at
        ):
            raise ValueError(
                "a MARKED_STALE finding must name the repository, the release tag, when that "
                "release was published and when the fact was read. Without all four the mark "
                "cannot be re-checked by hand, and a mark nobody can check is a mark nobody "
                "should trust."
            )
        return self


class SweepDocument(ContractModel):
    """The freshness sweep's document: what released upstream, what was marked, and why.

    Cites what it reasoned against -- the fact store's digest, the marks in force before and
    after, its own ruleset, and (for a pre-flight sweep) the inventory that scoped it -- so a
    reader can tell whether a changed sweep came from the knowledge, from the run, or from
    the rules.

    NOT A STAGE DOCUMENT, and the difference is the whole design. No stage reads this; the
    stages read the marks, through the store adapter they already use. So the digest chain
    stays a chain of pure functions and this document sits beside it, carrying the one thing
    that is honestly time-dependent.
    """

    # 1.1: the finding's shape changed with the mechanism. `pinned_revision` and
    # `upstream_revision` are gone -- there is no sha in the decision any more -- and
    # `repository`, `release_tag` and `release_published_at` record the comparison that
    # replaced them. Bumped rather than reused so an object stored under either shape says
    # which one it is, instead of leaving a reader to infer it from which fields are null.
    schema_version: Literal["1.1"] = "1.1"
    # Not a number. In this project's vocabulary the numbered stages are the pipeline's
    # document chain, and giving a number a second meaning is the homonym defect the
    # resolve/resolve rename exists to remove.
    stage: Literal["sweep"] = "sweep"
    mode: SweepMode
    # Stated in the document rather than left to the reader's inference, because a sweep that
    # reported a TTL -- or a commit comparison, which this deliberately is not -- would
    # describe a mechanism this one does not use.
    staleness: Literal["upstream_release"] = "upstream_release"
    # When the comparison was made. The one clock reading in the system that belongs in a
    # document, because the document is a claim about the world at an instant.
    swept_at: str = Field(min_length=1)

    # The run this sweep was flown for, when there is one.
    cluster: ClusterReference | None = None
    scope: tuple[str, ...] = ()
    inventory_digest: str | None = None

    store_digest: str = Field(min_length=1)
    marks_digest_before: str = Field(min_length=1)
    marks_digest_after: str = Field(min_length=1)
    ruleset_digest: str = Field(min_length=1)

    findings: tuple[SweepFinding, ...] = ()
    # Components this sweep marked. Redundant with `findings` by construction and validated
    # against it below: it is the field the state machine and an operator read first, and a
    # headline that can disagree with its own detail is worse than no headline.
    marked: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    # Components whose SPENT mark this sweep retired -- raised against a revision the store no
    # longer holds, so it was withholding nothing by the time the sweep ran. Reported because a
    # reader asking "why did this component stop being researchable" is owed the answer, and a
    # silent tidy-up is how the loop went three runs without anybody seeing the mechanism.
    # Additive and empty by default, so every stored 1.1 document still validates.
    retired: tuple[str, ...] = ()

    @model_validator(mode="after")
    def the_headline_agrees_with_the_findings(self) -> SweepDocument:
        """`marked` must be exactly the MARKED_STALE findings, in the same order.

        The same discipline `ReportView` applies to its totals and `DispatchDocument` to its
        empty queue: the document is evidence, so an inconsistent one must not be
        constructible.
        """
        drifted = tuple(
            f.component_id
            for f in self.findings
            if f.outcome is SweepOutcome.MARKED_STALE
        )
        if self.marked != drifted:
            raise ValueError(
                f"marked={self.marked!r} disagrees with the findings, which record "
                f"{drifted!r} as having drifted. The headline and the detail must be the "
                "same claim."
            )
        return self

    @model_validator(mode="after")
    def a_preflight_sweep_says_what_scoped_it(self) -> SweepDocument:
        """A run-scoped sweep must record its scope, or the cluster it was flown for.

        Either answers "why these facts and not the others". Neither would leave a sweep
        that visited a subset unable to say which subset, or on whose behalf --
        indistinguishable from a fleet sweep that silently missed facts.

        A cluster with an EMPTY scope is legitimate and is not this defect: it is a run
        whose inventory identified no component, which visited nothing precisely because
        the run asked about nothing.
        """
        if self.mode is SweepMode.PRE_FLIGHT and self.cluster is None and not self.scope:
            raise ValueError(
                "a pre-flight sweep must record either the cluster it was flown for or "
                "the scope it visited: with neither, a scoped sweep is indistinguishable "
                "from a fleet sweep that missed facts."
            )
        if self.mode is SweepMode.SCHEDULED and self.scope:
            raise ValueError(
                "a scheduled sweep visits every stored fact, so it must record no scope. "
                f"Got {self.scope!r}."
            )
        return self

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()

    @property
    def store_digest_moves(self) -> bool:
        """Whether this sweep changed what the stages will see. Derived, never stored."""
        return self.marks_digest_before != self.marks_digest_after

    def counts(self) -> dict[str, int]:
        by_outcome: dict[str, int] = {}
        for finding in self.findings:
            by_outcome[str(finding.outcome)] = by_outcome.get(str(finding.outcome), 0) + 1
        return {
            "visited": len(self.findings),
            "marked": len(self.marked),
            # Counted beside `marked` so a sweep that spent its housekeeping is visible in the
            # one line an operator reads. `marked 0, outcome_already_marked 1` for three runs
            # running is what the loop looked like from here, and a retirement count is what
            # distinguishes "the mark is spent and gone" from it.
            "retired": len(self.retired),
            "unresolved": len(self.unresolved),
            **{f"outcome_{k.lower()}": v for k, v in sorted(by_outcome.items())},
        }


def _outcome_of(visit: FactVisit) -> SweepOutcome:
    """Classify one visit. The three not-stale cases are kept apart deliberately."""
    if visit.existing_mark is not None:
        return SweepOutcome.ALREADY_MARKED
    drift = visit.drift
    if drift is None:
        # Unreachable through `sweep`, which always records one or the other. Kept as an
        # explicit answer rather than an assertion, because a document is better than a
        # crash and NOT_COMPARABLE is the honest reading of "nothing was compared".
        return SweepOutcome.NOT_COMPARABLE
    if drift.stale:
        return SweepOutcome.MARKED_STALE
    if not drift.comparable:
        return SweepOutcome.NOT_COMPARABLE
    return SweepOutcome.UNMOVED


def _finding_for(visit: FactVisit) -> SweepFinding:
    """One visit as a finding, taking its reason from whichever decided the outcome."""
    outcome = _outcome_of(visit)
    if visit.existing_mark is not None:
        mark = visit.existing_mark
        return SweepFinding(
            component_id=visit.component_id,
            outcome=outcome,
            fact_revision=visit.fact_revision,
            fact_refreshed_at=(
                visit.fact_refreshed_at.isoformat() if visit.fact_refreshed_at else None
            ),
            reason=(
                f"a {mark.kind} mark written by {mark.marked_by} at "
                f"{mark.marked_at.isoformat()} was already in force, so the fact was not "
                f"re-marked and upstream was not read. Its reason: {mark.reason}"
            ),
        )
    drift = visit.drift
    if drift is None:
        # Unreachable through `sweep`, which records a mark or a comparison for every
        # visit. Answered rather than asserted: a document is more useful than a crash,
        # and "nothing was compared" is exactly what NOT_COMPARABLE means.
        return SweepFinding(
            component_id=visit.component_id,
            outcome=SweepOutcome.NOT_COMPARABLE,
            fact_revision=visit.fact_revision,
            fact_refreshed_at=(
                visit.fact_refreshed_at.isoformat() if visit.fact_refreshed_at else None
            ),
            reason=(
                "the sweep recorded no upstream comparison for this fact and no mark "
                "against it, so nothing is known about whether its component released."
            ),
        )
    return SweepFinding(
        component_id=visit.component_id,
        outcome=outcome,
        fact_revision=visit.fact_revision,
        # From the DRIFT rather than the visit: `is_stale` normalised a naive stored instant
        # to UTC, and the finding must show the value the comparison actually used.
        fact_refreshed_at=(
            drift.fact_refreshed_at.isoformat() if drift.fact_refreshed_at else None
        ),
        repository=drift.repository,
        release_tag=drift.release_tag,
        release_published_at=(
            drift.release_published_at.isoformat() if drift.release_published_at else None
        ),
        url=drift.url,
        reason=drift.reason,
    )


def sweep_document(
    result: SweepResult,
    *,
    mode: SweepMode | str,
    swept_at: datetime,
    store_digest: str,
    marks_before: MarkLedger,
    cluster: ClusterReference | None = None,
    scope: tuple[str, ...] = (),
    inventory_digest: str | None = None,
) -> SweepDocument:
    """Turn a sweep into its document. A pure function of the result and the run.

    Separate from `sweep` so the sweep stays testable without a document and the document
    stays constructible from a stored result -- and so that publishing is a decision the
    handler makes, not something the rules do as a side effect.

    `marks_digest_after` and `retired` come from the result, so a handler that could not write
    the retirements must report the result it actually achieved (see `stages/freshness.py`)
    rather than this document -- a document claiming a retirement that no row carries would be
    the overclaim the digest chain exists to prevent.
    """
    return SweepDocument(
        mode=SweepMode(mode),
        swept_at=swept_at.isoformat(),
        cluster=cluster,
        scope=tuple(scope),
        inventory_digest=inventory_digest,
        store_digest=store_digest,
        marks_digest_before=marks_before.digest(),
        marks_digest_after=result.ledger.digest(),
        ruleset_digest=ruleset_digest(),
        findings=tuple(_finding_for(visit) for visit in result.visits),
        marked=tuple(mark.component_id for mark in result.new_marks),
        unresolved=tuple(result.unresolved),
        retired=result.retired,
    )
