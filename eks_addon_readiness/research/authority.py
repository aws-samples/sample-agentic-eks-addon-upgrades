"""How authoritative a source is -- decided by code, from the source's identity and shape.

This closes a boundary leak. A contract whose thesis is that the model *does not rate
confidence* is undone by a single field like `from_official_matrix` — a boolean the model
sets that on its own lifts a single source to HIGH. One quote of an AWS *default-version*
table, marked as an official compatibility matrix by the model, can be rated HIGH and
wrong: that field is the model rating itself through a side door.

Authority here is derived from two things code can establish without asking:

**Identity** -- whose page is this? Comparing the source URL's host and path against the
component's own `canonical_repository` and declared documentation hosts answers it. A
project's own repository or its own documentation site is first-party. Anything else is
third-party, whatever it claims about itself, and a page that is not even on a host the
component declares is unrelated.

**Shape** -- what kind of text is the quote sitting in? Because the judging layer now
receives fetched bodies (`pages.py`), this is a question about bytes: does the quote lie
inside a table in the page we fetched? A markdown pipe table or an HTML table is a table;
a sentence in prose is not. The model's opinion is not consulted.

Note what authority deliberately does **not** do: it never widens a window and never
supplies a ceiling. It scores evidence strength for `signals.py`. The window comes from
`bounds.py`, which reads what the source said. A first-party table stating a tested range
is strong evidence of a tested range -- not a licence to treat it as a supported one.

Host comparison is exact-or-subdomain and never substring: `github.com.evil.test` is not
`github.com`, and `raw.githubusercontent.com` is accepted for a `github.com` repository
because it serves that repository's own bytes.
"""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlparse

from .pages import FetchedPage, PageSet


class SourceAuthority(StrEnum):
    """How much weight a source's identity and shape earn it.

    `FIRST_PARTY_STRUCTURED`
        The component's own repository or documentation, and the quote was read from a
        machine-readable file -- a chart manifest, a version constraint, a releases
        payload. The strongest evidence available: no prose was interpreted.

    `FIRST_PARTY_TABULAR`
        The component's own repository or documentation, and the quote sits inside a
        table in the fetched bytes. This is what a `from_official_matrix` flag was reaching
        for, established here rather than asserted.

    `FIRST_PARTY_PROSE`
        The component's own source, quote in running text. Honest and weaker: prose is
        where "requires 1.25+" and "tested against N-2" look alike.

    `THIRD_PARTY`
        A real page on a host the component does not declare as its own. AWS's EKS
        documentation about a component is third-party *about that component* -- which is
        not an insult to AWS, it is the reason a default-version table must not be read as
        the project's support matrix.

    `UNVERIFIABLE`
        No fetched page backs this citation, so neither identity nor shape can be
        established. Never earns anything.
    """

    FIRST_PARTY_STRUCTURED = "FIRST_PARTY_STRUCTURED"
    FIRST_PARTY_TABULAR = "FIRST_PARTY_TABULAR"
    FIRST_PARTY_PROSE = "FIRST_PARTY_PROSE"
    THIRD_PARTY = "THIRD_PARTY"
    UNVERIFIABLE = "UNVERIFIABLE"


# Hosts that serve a GitHub repository's own bytes. Not an allowlist of trusted sites --
# a mapping from "this repository" to "the hosts that can serve it", used only to decide
# whether a URL belongs to the component's own repository.
_GITHUB_CONTENT_HOSTS = ("github.com", "raw.githubusercontent.com", "api.github.com")

# Path suffixes whose content is machine-readable rather than prose. Structural, generic,
# and about file formats -- no component appears here, and none may.
_STRUCTURED_SUFFIXES = (
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".lock",
    ".cfg",
    ".ini",
)

# A GitHub releases payload is structured even though its path has no extension.
_STRUCTURED_PATH_MARKERS = ("/releases/tags/", "/releases/latest", "/api/v3/")

# A markdown table row: at least two cell separators on one line. Deliberately loose --
# the question is "is this tabular", not "is this valid GFM".
_MARKDOWN_ROW = re.compile(r"^\s*\|?[^|\n]*\|[^|\n]*\|")
# A markdown table's alignment/divider row, which is what makes a block a table rather
# than a sentence containing a pipe.
_MARKDOWN_DIVIDER = re.compile(r"^\s*\|?[\s:-]*-{2,}[\s:|-]*\|?\s*$")


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").casefold()
    except ValueError:
        return ""


def _host_matches(host: str, declared: str) -> bool:
    """Exact host, or a subdomain of it. Never a substring.

    >>> _host_matches("docs.keda.sh", "keda.sh")
    True
    >>> _host_matches("keda.sh.evil.test", "keda.sh")
    False
    >>> _host_matches("keda.sh", "keda.sh")
    True
    """
    if not host or not declared:
        return False
    return host == declared or host.endswith("." + declared)


def _repo_path(url: str) -> str:
    """The `org/repo` part of a GitHub URL, casefolded, or the empty string.

    >>> _repo_path("https://github.com/kedacore/keda-docs/blob/abc/x.md")
    'kedacore/keda-docs'
    >>> _repo_path("https://raw.githubusercontent.com/org/repo/sha/README.md")
    'org/repo'
    >>> _repo_path("https://docs.aws.amazon.com/eks/latest/userguide/x.md")
    ''
    """
    host = _host(url)
    if not any(_host_matches(host, h) for h in _GITHUB_CONTENT_HOSTS):
        return ""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if host == "api.github.com" and parts[:1] == ["repos"]:
        parts = parts[1:]
    if len(parts) < 2:
        return ""
    return f"{parts[0]}/{parts[1]}".casefold()


def is_first_party(
    url: str, canonical_repository: str | None, official_documentation: tuple[str, ...]
) -> bool:
    """Whether `url` is served by the component's own repository or documentation site.

    A GitHub URL is first-party when it names the same `org/repo` as the canonical
    repository. Everything else is first-party when its host matches, or is a subdomain
    of, a declared documentation host.

    >>> is_first_party("https://raw.githubusercontent.com/kedacore/keda-docs/a/x.md",
    ...               "https://github.com/kedacore/keda-docs", ())
    True
    >>> is_first_party("https://docs.aws.amazon.com/eks/latest/userguide/managing-coredns.md",
    ...               "https://github.com/coredns/coredns", ())
    False
    >>> is_first_party("https://keda.sh/docs/2.20/operate/cluster/", (),
    ...               ("https://keda.sh",))
    True
    """
    repo = _repo_path(url)
    if repo and canonical_repository:
        return repo == _repo_path(canonical_repository)
    host = _host(url)
    return any(_host_matches(host, _host(doc)) for doc in official_documentation)


def is_structured_path(url: str) -> bool:
    """Whether the path names a machine-readable file rather than a document.

    >>> is_structured_path("https://raw.githubusercontent.com/o/r/v1/Chart.yaml")
    True
    >>> is_structured_path("https://api.github.com/repos/o/r/releases/tags/v2.8.3")
    True
    >>> is_structured_path("https://raw.githubusercontent.com/o/r/v1/README.md")
    False
    """
    path = urlparse(url).path.casefold()
    if any(path.endswith(s) for s in _STRUCTURED_SUFFIXES):
        return True
    return any(m in path for m in _STRUCTURED_PATH_MARKERS)


def quote_is_tabular(quote: str, page: FetchedPage) -> bool:
    """Whether the quote sits inside a table in the page's own bytes.

    Two shapes are recognised, both established from the fetched body rather than from
    the quote alone -- a quote can be typed to look like a table row while the page it
    cites contains no table:

    - **Markdown**: the quote's lines look like table rows, and the page contains a
      divider row within the block the quote occupies.
    - **HTML**: the quote falls between a `<table` and the next `</table>` in the body.

    >>> from .pages import FetchedPage
    >>> body = "x\\n| A | B |\\n|---|---|\\n| 0.6.x | 1.25+ |\\ny"
    >>> page = FetchedPage("u", "r", body, 200)
    >>> quote_is_tabular("| 0.6.x | 1.25+ |", page)
    True
    >>> quote_is_tabular("y", page)
    False
    """
    body = page.body
    index = body.find(quote)
    if index == -1:
        # Not byte-present. Shape is a claim about the page, so it cannot be established
        # from a quote the page does not contain.
        return False

    # HTML: is the quote inside a table element?
    open_tag = body.rfind("<table", 0, index)
    if open_tag != -1:
        close_tag = body.find("</table", open_tag)
        if close_tag == -1 or close_tag > index:
            return True

    # Markdown: the quote must look like rows, and its neighbourhood must hold a divider.
    quote_lines = [ln for ln in quote.splitlines() if ln.strip()]
    if not quote_lines or not any(_MARKDOWN_ROW.match(ln) for ln in quote_lines):
        return False
    if any(_MARKDOWN_DIVIDER.match(ln) for ln in quote_lines):
        return True
    # A quote of body rows only: look upward in the page for the divider that makes the
    # block a table, stopping at the first line that is not a row.
    before = body[:index].splitlines()
    for line in reversed(before[-6:]):
        if _MARKDOWN_DIVIDER.match(line):
            return True
        if not _MARKDOWN_ROW.match(line):
            break
    return False


def classify_source(
    url: str,
    revision: str,
    quote: str,
    *,
    canonical_repository: str | None,
    official_documentation: tuple[str, ...],
    pages: PageSet,
) -> SourceAuthority:
    """Decide one source's authority from its identity and its shape. No model input.

    Order matters: unverifiable first, because identity and shape are both claims about
    bytes and there are none; then structured, then tabular, then prose.
    """
    page = pages.get(url, revision)
    if page is None:
        return SourceAuthority.UNVERIFIABLE

    if not is_first_party(url, canonical_repository, official_documentation):
        return SourceAuthority.THIRD_PARTY

    if is_structured_path(url):
        return SourceAuthority.FIRST_PARTY_STRUCTURED
    if quote_is_tabular(quote, page):
        return SourceAuthority.FIRST_PARTY_TABULAR
    return SourceAuthority.FIRST_PARTY_PROSE
