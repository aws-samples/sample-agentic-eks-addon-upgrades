"""Reading a component's own sources over HTTPS, scoped to hosts it declares as its own.

Two jobs, and the second is the interesting one.

**Fetching.** Plain HTTPS GET with a timeout and a size cap. GitHub `blob` URLs are
rewritten to `raw.githubusercontent.com` so the bytes fetched are the file's bytes rather
than a HTML page wrapping them -- which matters for the byte-for-byte quote check, since a
quote from a rendered page will not match a raw file and vice versa.

**Scoping.** Only hosts belonging to the component are readable, and the scope starts from
what the component itself declares. A blog post, a vendor article or a forum answer is
refused. The refusal is returned rather than raised, so the model is told and can look
elsewhere -- and the refusal is recorded, so afterwards it is possible to say
whether a page was refused or read.

Note what this does *not* do: it does not decide how much a source is worth.
`authority.py` does that, from identity and shape. A page being *readable* here says only
that it is on a host the component claims. A component's own docs site can still be prose
and still cap at MEDIUM.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .ports import FetchResult

# Hosts that serve a GitHub repository's own bytes.
_GITHUB_HOSTS = ("github.com", "raw.githubusercontent.com", "api.github.com")

# Cap on ONE response, and the only cap here that is structural rather than chosen: the body
# is read into memory, so an unbounded read is an out-of-memory failure at the least helpful
# moment. Raised well past any real source -- a releases payload of 300 KB and a matrix page
# of 15 KB are typical -- so it bounds the pathological case and never the research.
MAX_BYTES = 32 * 1024 * 1024

DEFAULT_TIMEOUT_SECONDS = 20

_USER_AGENT = "eks-addon-readiness/2 (+compatibility research; read-only)"


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").casefold()
    except ValueError:
        return ""


def _host_matches(host: str, declared: str) -> bool:
    """Exact host or a subdomain of it. Never a substring, so a lookalike host fails.

    >>> _host_matches("docs.example.test", "example.test")
    True
    >>> _host_matches("example.test.evil.test", "example.test")
    False
    """
    if not host or not declared:
        return False
    return host == declared or host.endswith("." + declared)


def raw_github_url(url: str) -> str:
    """Rewrite a GitHub `blob` URL to the raw bytes it displays.

    >>> raw_github_url("https://github.com/o/r/blob/abc123/README.md")
    'https://raw.githubusercontent.com/o/r/abc123/README.md'
    >>> raw_github_url("https://raw.githubusercontent.com/o/r/abc123/README.md")
    'https://raw.githubusercontent.com/o/r/abc123/README.md'
    >>> raw_github_url("https://example.test/x")
    'https://example.test/x'
    """
    parsed = urlparse(url)
    if _host(url) != "github.com":
        return url
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 5 and parts[2] == "blob":
        org, repo, _, rev = parts[0], parts[1], parts[2], parts[3]
        rest = "/".join(parts[4:])
        return f"https://raw.githubusercontent.com/{org}/{repo}/{rev}/{rest}"
    return url


@dataclass
class SourceScope:
    """Which hosts count as the component's own, and how that set grows.

    Seeded from a prior fact when one exists, so re-researching a known component can go
    straight to fetching. For a new component the scope starts from whatever repository the
    model confirms, added through `permit_repository` -- so the model can widen the scope
    only to the component's own hosts, never to an arbitrary one.
    """

    canonical_repository: str | None = None
    official_documentation: tuple[str, ...] = ()
    _extra_hosts: set[str] = field(default_factory=set)

    def permit_repository(self, repository_url: str) -> bool:
        """Accept a canonical repository, which permits the hosts that serve it.

        Returns whether it was accepted. Only a GitHub repository URL is: the point is to
        widen the scope to *this component's* bytes, and "any host the model names" would
        be no scope at all.
        """
        if _host(repository_url) not in _GITHUB_HOSTS:
            return False
        self.canonical_repository = repository_url
        return True

    def permit_documentation(self, docs_url: str) -> bool:
        """Accept a documentation host **only if the repository already vouches for it**.

        A component's docs site rarely shares a host with its repository, so the host has
        to come from somewhere. It comes from the entry's own declared
        `official_documentation`, or from a repository-hosted URL. A bare host the model
        asserts is not enough -- that is the hole through which a third-party page becomes
        a first-party one.
        """
        host = _host(docs_url)
        if not host:
            return False
        if any(_host_matches(host, _host(d)) for d in self.official_documentation):
            self._extra_hosts.add(host)
            return True
        # A GitHub-hosted docs path is the repository's own bytes, so it needs no
        # separate vouching.
        return host in _GITHUB_HOSTS

    def permits(self, url: str) -> bool:
        """Whether this URL is on a host the component declares as its own."""
        host = _host(url)
        if not host:
            return False
        if host in _GITHUB_HOSTS and self.canonical_repository:
            return True
        if any(_host_matches(host, _host(d)) for d in self.official_documentation):
            return True
        return any(_host_matches(host, h) for h in self._extra_hosts)

    def describe(self) -> str:
        hosts = sorted(
            {_host(d) for d in self.official_documentation if _host(d)} | self._extra_hosts
        )
        repo = self.canonical_repository or "(none confirmed yet)"
        return f"repository {repo}; documentation hosts {hosts or '(none)'}"


class HttpSourceReader:
    """A `SourceReader` over HTTPS, scoped and size-capped.

    HTTPS only. A plaintext fetch would let anything on the path rewrite the bytes a
    compatibility fact is derived from, and the fact carries the URL as its citation, so the
    citation would be a claim the transport does not support.
    """

    def __init__(
        self,
        scope: SourceScope,
        *,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._scope = scope
        self._timeout = timeout
        self._max_bytes = max_bytes

    def fetch(self, url: str, revision: str = "") -> FetchResult:
        target = raw_github_url(url.strip())
        if urlparse(target).scheme != "https":
            return FetchResult(
                url=target,
                revision=revision,
                body="",
                status=0,
                refused=True,
                reason=(
                    "only https is read: a plaintext fetch cannot support the citation "
                    "the resulting fact would carry"
                ),
            )
        if not self._scope.permits(target):
            return FetchResult(
                url=target,
                revision=revision,
                body="",
                status=0,
                refused=True,
                reason=(
                    f"{_host(target)} is not a host this component declares as its own. "
                    f"In scope: {self._scope.describe()}. Read the component's own "
                    "repository or documentation instead; a third-party page cannot "
                    "support a compatibility fact."
                ),
            )

        # The https scheme is checked above, so the url open below cannot reach a
        # file:// or other local scheme.
        request = urllib.request.Request(
            target, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read(self._max_bytes + 1)
                status = getattr(response, "status", 200) or 200
        except urllib.error.HTTPError as exc:
            return FetchResult(
                url=target,
                revision=revision,
                body="",
                status=exc.code,
                reason=f"HTTP {exc.code}",
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return FetchResult(
                url=target,
                revision=revision,
                body="",
                status=0,
                reason=f"{type(exc).__name__}: {exc}",
            )

        if len(raw) > self._max_bytes:
            return FetchResult(
                url=target,
                revision=revision,
                body="",
                status=status,
                refused=True,
                reason=(
                    f"response exceeds {self._max_bytes} bytes; a compatibility statement "
                    "is not in a file this large"
                ),
            )

        # Decoded as UTF-8 with replacement rather than strictly: a source with one bad
        # byte is still a readable source, and the quote check compares the same decoded
        # text the model was shown, so the two cannot disagree about what the bytes were.
        return FetchResult(
            url=target,
            revision=revision,
            body=raw.decode("utf-8", errors="replace"),
            status=status,
        )
