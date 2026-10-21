"""Fetched page bodies, carried from the fetch layer into the judging layer.

This module supplies the page bytes the judging layer needs. A judging layer given only
the model's extraction and any prior fact — with `ResearchExtraction` carrying no field
for page text and the only HTTP client living inside the model's own tool loop — can never
compare a quote against the bytes it was supposedly quoting. A promise that *"an injected
claim with no supporting quote in the actual page text cannot pass"* is then
unimplementable, not merely unimplemented.

`PageSet` is the argument that makes it implementable. Every fetch the model performs is
recorded here, keyed by URL and revision, and the judging layer receives it alongside the
extraction. That turns "does the value appear in the quote the model wrote" -- a question
both halves of which the model authored in one turn -- into "does the quote appear in the
bytes we fetched", which the model cannot influence.

Bodies are held as fetched, undecoded and unnormalised, because the check that matters is
byte-for-byte. A whitespace-tolerant second pass exists in `verify.py` and is reported
separately, never silently.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FetchedPage:
    """One page body, as it came off the wire, with what it took to get it.

    `revision` is the commit or tag the URL was pinned to, or the empty string for an
    unpinned documentation URL. It participates in the key: the same path at two commits
    is two different pages, and conflating them is precisely how a held candidate can come
    to carry a table spliced from two commits of one README with every line real and the
    whole thing fictional.
    """

    url: str
    revision: str
    body: str
    status: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.url, self.revision)

    @property
    def digest(self) -> str:
        """Content digest of the body, so a receipt can pin the exact bytes judged."""
        return "sha256:" + hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def contains(self, text: str) -> bool:
        """Byte-for-byte substring presence. No normalisation, no tolerance."""
        return text in self.body


@dataclass
class PageSet:
    """Every page fetched during one research run.

    Mutable during fetching and read-only in effect afterwards: the judging layer only
    reads. Keyed by (url, revision) with the first fetch winning, so a page re-read in the
    same run cannot change under a quote that already cited it.
    """

    pages: dict[tuple[str, str], FetchedPage] = field(default_factory=dict)

    def record(self, page: FetchedPage) -> None:
        self.pages.setdefault(page.key, page)

    def get(self, url: str, revision: str) -> FetchedPage | None:
        """The page for this exact citation, or None if it was never fetched.

        Exact on both parts. A quote citing a revision nobody fetched is unverifiable,
        and reporting that is the point -- it is not an excuse to fall back to a
        same-URL-different-revision body, which would re-open the splice.
        """
        return self.pages.get((url, revision))

    def any_contains(self, text: str) -> FetchedPage | None:
        """The first page whose body contains `text`, in a stable order.

        Used to distinguish two failures that deserve different words: a quote that is
        real but attributed to the wrong page, and a quote that exists nowhere at all.
        """
        for key in sorted(self.pages):
            page = self.pages[key]
            if page.contains(text):
                return page
        return None

    def __len__(self) -> int:
        return len(self.pages)

    def digest(self) -> str:
        """Digest over every page fetched, so a receipt pins the evidence set as a whole."""
        hasher = hashlib.sha256()
        for key in sorted(self.pages):
            page = self.pages[key]
            hasher.update(page.url.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(page.revision.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(page.digest.encode("utf-8"))
            hasher.update(b"\x00")
        return "sha256:" + hasher.hexdigest()
