"""Recording every fetch, so the judging layer receives what the model actually read.

This wrapper is the whole fix stated in ten lines. A fetch that hands bytes to the model
and keeps none leaves no later layer able to compare a quote against a source. Here every
successful fetch lands in a `PageSet` before the body is returned, and the same `PageSet`
is what `judge()` takes as its evidence argument.

Because the recording happens in the wrapper rather than in each reader, it is not
something a reader can forget. Adding a new source reader gets the recording for free, and
a reader that somehow bypassed the wrapper would produce quotes that fail verification as
`NOT_FETCHED` -- which fails loudly rather than passing quietly. The safe direction.
"""

from __future__ import annotations

from ..pages import FetchedPage, PageSet
from .ports import FetchResult, SourceReader


class RecordingReader:
    """A `SourceReader` that records what it reads into a `PageSet`.

    Also keeps the refusals. A refused fetch is not evidence, but it is *provenance*: it
    answers "was this third-party page read, or invented?" -- an open question, otherwise,
    about a fact that is already driving verdicts.
    """

    def __init__(self, inner: SourceReader, pages: PageSet | None = None) -> None:
        self._inner = inner
        self.pages = pages if pages is not None else PageSet()
        self._refusals: list[FetchResult] = []

    @property
    def refusals(self) -> tuple[FetchResult, ...]:
        return tuple(self._refusals)

    def fetch(self, url: str, revision: str = "") -> FetchResult:
        result = self._inner.fetch(url, revision)
        if result.refused:
            self._refusals.append(result)
            return result
        if result.ok:
            # Recorded under the revision the CALLER asked for, not one derived from the
            # response, because that is the key a citation will carry and therefore the key
            # verification will look under. A body recorded under a different key is an
            # unverifiable quote, which is the failure this exists to prevent.
            self.pages.record(
                FetchedPage(
                    url=result.url,
                    revision=result.revision,
                    body=result.body,
                    status=result.status,
                )
            )
        return result

    def summary(self) -> str:
        return (
            f"{len(self.pages)} page(s) recorded, {len(self._refusals)} fetch(es) refused"
        )
