"""Finding the issues many people hit after an upgrade, and never guessing when we could not look.

**THE ONE PROPERTY THIS MODULE EXISTS TO GUARANTEE: "no known issues" and "we could not look"
must never be the same output.** A brief that quietly reports an empty issue list because the
call was rate-limited is worse than a brief with no issue section at all -- the first reads as
reassurance, the second as a gap. So there is no boolean and no bare list. Every lookup returns
an `IssueLookupOutcome`, and two of its three values mean "do not read this as reassurance".
The agent is told the same thing in its tool description, because it is the one that writes the
section.

**WHY THIS IS A SEPARATE CAPABILITY FROM THE FETCH TOOL.** Release notes are a page; issues are
a query. `GET /search/issues` is a different endpoint with a different response shape, a
different rate limit and a different failure vocabulary, and the fetch tool -- whose contract is
"one pinned URL, recorded, scoped to hosts the component declares" -- cannot express it.

**THE TRAIL SIGNALS ARE THE POINT, NOT A NICETY.** The ask was not "list open bugs"; every
project has hundreds. It was the ones **many people have hit after upgrading**. That is a
measurable thing -- comment count, reaction count, how long it has been open, whether it names
the version -- and every one of those numbers is in the search response. So they are carried
through to the agent, which cites them in `IssueFinding.trail` rather than estimating. An issue
with one comment and no reactions is one person's environment and does not belong in a document
a maintenance window will be planned against.

**NO MODEL DECIDES WHETHER THE SEARCH SUCCEEDED.** Parsing a response and classifying its
status is deterministic, and it happens here. The agent judges which issues matter; it never
gets to turn a failed call into an empty list.

**THE RATE LIMITS ARE TWO DIFFERENT LIMITS, AND THE SEARCH ONE IS THE TIGHTER.** GitHub's core
REST endpoints allow 60 requests per hour per IP unauthenticated and 5,000 per hour with a
token. The SEARCH endpoint this module uses has its own, much shorter window: 10 requests per
minute unauthenticated, 30 per minute authenticated. An agent that issues several searches for
one component can exhaust the unauthenticated window inside a minute, so `GITHUB_TOKEN` is
load-bearing rather than an optimisation, and whether one was present is recorded on every
lookup.

**A LOOKUP IS NEVER FATAL.** Every failure path returns `NOT_LOOKED` with a reason. Nothing
here raises for a network condition, because the brief is advisory and a run that failed
because GitHub was busy would be the blocking behaviour this whole design refuses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import quote_plus, urlparse

# How many issues one lookup returns. A cap rather than everything: the agent is looking for
# the few issues with a long trail, and a hundred rows of noise makes that harder, not easier.
# `total_count` from the response is carried alongside, so a capped list says so.
DEFAULT_LIMIT = 15

# Applied when the caller names no labels. Not a hard filter on the query -- see `build_query`,
# which drops it when the caller asks for every label -- because a project that files upgrade
# regressions under `kind/bug` or nothing at all would return zero for a `label:bug` query and
# the zero would be indistinguishable from health.
DEFAULT_LABELS = ()

_SEARCH_ENDPOINT = "https://api.github.com/search/issues"


class IssueLookupOutcome(StrEnum):
    """What became of one issue lookup. Three values, never two.

    The distinction between the second and the third is the entire point of this module.
    """

    # The API answered and returned at least one matching issue.
    FOUND = "FOUND"
    # The API answered and returned none. This one, and ONLY this one, may be read as
    # "nothing known to watch for" -- and only for the query that was actually asked.
    NONE_OPEN = "NONE_OPEN"
    # No answer was obtained: no resolvable repository, no network, a rate limit, a token that
    # was absent or rejected, or a response that could not be parsed. The reason says which.
    # NOT evidence of an absence of issues.
    NOT_LOOKED = "NOT_LOOKED"


@dataclass(frozen=True)
class IssueSighting:
    """One issue, with the numbers that say how widely it is hit.

    `title` and `body` are verbatim from the response and nothing rewrites them: the agent
    quotes from them and the quote is checked against the recorded bytes, so a paraphrase here
    would break verification for a claim that was actually true.

    `comments` and `reactions` are the trail. They are returned rather than judged because
    "63 comments" is checkable and "widely reported" is not.
    """

    number: int
    title: str
    url: str
    labels: tuple[str, ...]
    state: str
    comments: int
    reactions: int
    created_at: str | None
    updated_at: str | None
    body: str

    @property
    def trail(self) -> str:
        """The one-line trail summary, in numbers. What `IssueFinding.trail` should cite.

        >>> IssueSighting(1, "t", "u", (), "open", 63, 21, "2026-01-01T00:00:00Z",
        ...               "2026-09-01T00:00:00Z", "b").trail
        '63 comments, 21 reactions, open since 2026-01-01, last active 2026-09-01'
        """
        parts = [f"{self.comments} comments", f"{self.reactions} reactions"]
        if self.created_at:
            parts.append(f"{self.state} since {self.created_at[:10]}")
        if self.updated_at:
            parts.append(f"last active {self.updated_at[:10]}")
        return ", ".join(parts)

    def recorded_body(self) -> str:
        """The bytes recorded for this issue, so a quote from it can be verified.

        Title and body together, because the agent may quote either and both are the issue's
        own words. The heading line is part of the recording rather than decoration -- it is
        what makes a quote of the title verifiable.

        >>> print(IssueSighting(7, "metrics gone", "u", ("bug",), "open", 3, 1, None, None,
        ...                     "after upgrading to 0.9 the API returns nothing").recorded_body())
        # 7: metrics gone
        <BLANKLINE>
        after upgrading to 0.9 the API returns nothing
        """
        return f"# {self.number}: {self.title}\n\n{self.body}"


@dataclass(frozen=True)
class IssueLookup:
    """One lookup: the outcome, what was asked, and what came back.

    `query` and `repository` are carried even on `NOT_LOOKED`, so a reader can see what was
    attempted. `total_count` is the API's own total, which is how a capped list of fifteen out
    of forty-one says so rather than implying it is all of them.
    """

    outcome: IssueLookupOutcome
    repository: str | None
    query: str
    reason: str
    issues: tuple[IssueSighting, ...] = ()
    total_count: int | None = None
    http_status: int | None = None
    authenticated: bool = False

    @property
    def looked(self) -> bool:
        """Whether an answer was actually obtained. False for `NOT_LOOKED` alone."""
        return self.outcome is not IssueLookupOutcome.NOT_LOOKED

    @property
    def truncated(self) -> bool:
        """Whether the API reported more matches than this lookup carries."""
        return self.total_count is not None and self.total_count > len(self.issues)


def repository_slug(repository: str) -> str | None:
    """`owner/repo` from a GitHub URL or from an already-bare slug, or None.

    Accepts both shapes because the agent supplies this and will supply either. None is a
    first-class answer, not a failure to handle: a component whose repository is on GitLab or a
    vendor host has no GitHub issues to search, and the brief says exactly that instead of
    searching the wrong place.

    >>> repository_slug("https://github.com/kubernetes-sigs/metrics-server")
    'kubernetes-sigs/metrics-server'
    >>> repository_slug("https://github.com/kubernetes-sigs/metrics-server.git")
    'kubernetes-sigs/metrics-server'
    >>> repository_slug("kubernetes-sigs/metrics-server")
    'kubernetes-sigs/metrics-server'
    >>> repository_slug("https://gitlab.com/o/r") is None
    True
    >>> repository_slug("kubernetes-sigs") is None
    True
    """
    candidate = repository.strip()
    if not candidate:
        return None
    if "://" not in candidate and candidate.count("/") == 1:
        owner, _, repo = candidate.partition("/")
        if owner and repo:
            return f"{owner}/{repo.removesuffix('.git')}"
        return None

    parsed = urlparse(candidate)
    if (parsed.hostname or "").casefold() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def build_query(
    slug: str,
    *,
    search_terms: str = "",
    labels: tuple[str, ...] = DEFAULT_LABELS,
    state: str = "open",
    min_comments: int = 0,
) -> str:
    """The search query. The TOOL owns the syntax; the agent supplies intent.

    Deliberately not a free-text query parameter. An agent writing raw GitHub search syntax
    gets a 422 for a stray colon and reads it as "no issues", which is the failure this whole
    module is built to prevent. So the qualifiers are assembled here from typed inputs.

    `comments:>=N` is GitHub's own qualifier, so the "long trail" filter runs server-side
    rather than by fetching everything and discarding most of it -- which matters against a
    ten-per-minute rate limit.

    >>> build_query("o/r")
    'repo:o/r is:issue is:open'
    >>> build_query("o/r", labels=("bug", "regression"), min_comments=10)
    'repo:o/r is:issue is:open label:bug label:regression comments:>=10'
    >>> build_query("o/r", search_terms="0.9.0 crash", state="all")
    'repo:o/r is:issue 0.9.0 crash'
    """
    parts = [f"repo:{slug}", "is:issue"]
    if state.strip().casefold() in ("open", "closed"):
        parts.append(f"is:{state.strip().casefold()}")
    parts += [f"label:{label.strip()}" for label in labels if label.strip()]
    if min_comments > 0:
        parts.append(f"comments:>={min_comments}")
    if search_terms.strip():
        parts.append(search_terms.strip())
    return " ".join(parts)


def search_url(query: str, limit: int = DEFAULT_LIMIT) -> str:
    """The full search URL, most-commented first.

    Sorted by comment count rather than by recency, because the section asks for the issues
    MANY PEOPLE have hit and the cap means the ordering decides what the agent sees. Recency
    is still available to it through `search_terms` and is reported per issue either way.

    >>> search_url("repo:o/r is:issue is:open", 2)
    'https://api.github.com/search/issues?q=repo%3Ao%2Fr+is%3Aissue+is%3Aopen&sort=comments&order=desc&per_page=2'
    """
    return (
        f"{_SEARCH_ENDPOINT}?q={quote_plus(query)}"
        f"&sort=comments&order=desc&per_page={limit}"
    )


class IssueSearch(Protocol):
    """Answers "which issues here have a long trail?" -- one query, no model, never raises."""

    def search(
        self,
        repository: str,
        *,
        search_terms: str = "",
        labels: tuple[str, ...] = (),
        state: str = "open",
        min_comments: int = 0,
    ) -> IssueLookup:
        ...


class StaticIssueSearch:
    """An `IssueSearch` backed by prepared lookups, keyed by `owner/repo`.

    For tests, and for reproducing a past brief without re-querying -- but also for a
    deployment that wants the issue section switched off deliberately: hand it an empty map and
    every lookup is `NOT_LOOKED` with a reason saying so, which is honest, where an empty list
    would have been a false all-clear.
    """

    def __init__(self, lookups: dict[str, IssueLookup]) -> None:
        self._lookups = dict(lookups)

    def search(
        self,
        repository: str,
        *,
        search_terms: str = "",
        labels: tuple[str, ...] = (),
        state: str = "open",
        min_comments: int = 0,
    ) -> IssueLookup:
        slug = repository_slug(repository)
        if slug is None:
            return not_looked(None, "", f"{repository!r} is not a GitHub repository")
        prepared = self._lookups.get(slug)
        if prepared is not None:
            return prepared
        return not_looked(
            slug,
            build_query(
                slug,
                search_terms=search_terms,
                labels=labels,
                state=state,
                min_comments=min_comments,
            ),
            "no prepared lookup for this repository, so no issue query was performed. "
            "Reported as not-looked rather than as an empty result, which would be a false "
            "all-clear.",
        )


def not_looked(slug: str | None, query: str, reason: str, **extra: object) -> IssueLookup:
    """A lookup that did not happen, naming why. The only way this module reports a failure.

    >>> not_looked("o/r", "q", "rate limited").outcome
    <IssueLookupOutcome.NOT_LOOKED: 'NOT_LOOKED'>
    >>> not_looked("o/r", "q", "rate limited").looked
    False
    """
    return IssueLookup(
        outcome=IssueLookupOutcome.NOT_LOOKED,
        repository=slug,
        query=query,
        reason=reason,
        **extra,  # type: ignore[arg-type]
    )


def sightings_from_payload(
    payload: str, limit: int = DEFAULT_LIMIT
) -> tuple[tuple[IssueSighting, ...], int | None, str | None]:
    """Parse a search response into sightings, its total, and a parse error if any.

    Three returns rather than raising, because a payload that cannot be parsed is a
    `NOT_LOOKED` with a reason and not an exception -- the caller must be able to say what went
    wrong in the document.

    A pull request is dropped. GitHub's issue search returns PRs as issues, and an open PR is
    work in progress rather than something to watch for after installing.

    >>> body = '''{"total_count": 2, "items": [
    ...   {"number": 7, "title": "metrics unavailable after restart", "state": "open",
    ...    "html_url": "https://github.com/o/r/issues/7", "comments": 41,
    ...    "reactions": {"total_count": 12}, "labels": [{"name": "bug"}],
    ...    "created_at": "2026-02-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z",
    ...    "body": "after upgrading"},
    ...   {"number": 8, "title": "a pull request", "pull_request": {},
    ...    "html_url": "https://github.com/o/r/pull/8"}
    ... ]}'''
    >>> found, total, error = sightings_from_payload(body)
    >>> [(s.number, s.comments, s.reactions) for s in found], total, error
    ([(7, 41, 12)], 2, None)
    >>> sightings_from_payload("not json")[2]
    'the response body is not JSON'
    """
    try:
        parsed = json.loads(payload)
    except ValueError:
        return (), None, "the response body is not JSON"
    if not isinstance(parsed, dict):
        return (), None, "the response body is not a JSON object"

    items = parsed.get("items")
    if not isinstance(items, list):
        return (), None, "the response object carries no `items` array"

    total = parsed.get("total_count")
    sightings: list[IssueSighting] = []
    for item in items:
        if not isinstance(item, dict) or item.get("pull_request") is not None:
            continue
        title = str(item.get("title") or "").strip()
        url = str(item.get("html_url") or "").strip()
        if not title or not url:
            continue
        reactions = item.get("reactions")
        sightings.append(
            IssueSighting(
                number=int(item.get("number") or 0),
                title=title,
                url=url,
                labels=tuple(
                    str(label.get("name"))
                    for label in item.get("labels") or []
                    if isinstance(label, dict) and label.get("name")
                ),
                state=str(item.get("state") or "open"),
                comments=int(item.get("comments") or 0),
                reactions=int(
                    (reactions or {}).get("total_count", 0)
                    if isinstance(reactions, dict)
                    else 0
                ),
                created_at=(str(item["created_at"]) if item.get("created_at") else None),
                updated_at=(str(item["updated_at"]) if item.get("updated_at") else None),
                body=str(item.get("body") or ""),
            )
        )
        if len(sightings) >= limit:
            break
    return tuple(sightings), (int(total) if isinstance(total, int) else None), None


def classify_response(
    slug: str,
    query: str,
    status: int,
    payload: str,
    *,
    authenticated: bool,
    rate_limit_remaining: str | None = None,
    retry_after: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> IssueLookup:
    """Turn one HTTP response into a lookup outcome. The tri-state decision, in one place.

    Separated from the transport so the decision is testable with no network, and so every
    status GitHub returns has one documented reading:

        200 with items        FOUND
        200 with none         NONE_OPEN   -- the only readable all-clear
        403/429, remaining 0  NOT_LOOKED  -- primary rate limit
        403/429 with a wait   NOT_LOOKED  -- secondary rate limit
        401                   NOT_LOOKED  -- the token was rejected
        404                   NOT_LOOKED  -- the repository is not visible to this caller
        422                   NOT_LOOKED  -- the query was rejected
        anything else         NOT_LOOKED  -- named by status

    >>> ok = classify_response("o/r", "q", 200, '{"total_count":0,"items":[]}',
    ...                        authenticated=False)
    >>> ok.outcome
    <IssueLookupOutcome.NONE_OPEN: 'NONE_OPEN'>
    >>> limited = classify_response("o/r", "q", 403, "", authenticated=False,
    ...                             rate_limit_remaining="0")
    >>> limited.outcome, "10 requests per minute" in limited.reason
    (<IssueLookupOutcome.NOT_LOOKED: 'NOT_LOOKED'>, True)
    """
    common = {"authenticated": authenticated, "http_status": status}

    if status in (403, 429):
        window = (
            "30 requests per minute with a token"
            if authenticated
            else "10 requests per minute without one, which is why GITHUB_TOKEN matters here "
            "more than on the core endpoints"
        )
        which = (
            "the primary rate limit is exhausted"
            if (rate_limit_remaining or "").strip() == "0"
            else "a secondary rate limit asked the caller to wait"
            if retry_after
            else "the request was refused, most likely by a rate limit"
        )
        return not_looked(
            slug,
            query,
            f"HTTP {status}: {which}. GitHub's search endpoint allows {window}. No issue list "
            "was obtained, so this component's issues are UNKNOWN rather than absent.",
            **common,
        )
    if status == 401:
        return not_looked(
            slug, query,
            "HTTP 401: the GitHub token was rejected. No issue list was obtained.",
            **common,
        )
    if status == 404:
        return not_looked(
            slug, query,
            f"HTTP 404: {slug} is not visible to this caller. It may be renamed, private or "
            "wrong; either way no issue list was obtained.",
            **common,
        )
    if status == 422:
        return not_looked(
            slug, query,
            f"HTTP 422: GitHub rejected the query {query!r}. No issue list was obtained.",
            **common,
        )
    if not 200 <= status < 300:
        return not_looked(
            slug, query,
            f"HTTP {status} from the issue search. No issue list was obtained.",
            **common,
        )

    sightings, total, error = sightings_from_payload(payload, limit)
    if error is not None:
        return not_looked(
            slug, query,
            f"the search answered HTTP {status} but {error}, so nothing could be read from "
            "it. Reported as not-looked rather than as an empty result.",
            **common,
        )

    if sightings:
        of_total = f" of {total} matching" if total is not None else ""
        reason = f"{len(sightings)} issue(s){of_total} for {query!r}, most-commented first"
    else:
        reason = (
            f"the search ran and matched no issue for {query!r}. This is an answer, not a "
            "failure to look -- but it is an answer about THIS query only."
        )

    return IssueLookup(
        outcome=IssueLookupOutcome.FOUND if sightings else IssueLookupOutcome.NONE_OPEN,
        repository=slug,
        query=query,
        reason=reason,
        issues=sightings,
        total_count=total,
        **common,
    )
