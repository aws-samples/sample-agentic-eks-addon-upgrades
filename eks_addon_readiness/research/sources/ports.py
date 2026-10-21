"""The source-reading port: one shape for every way a source can be read.

Two readers with no shared protocol -- a GitHub MCP client and an official-docs fetcher --
cannot be substituted, wrapped or recorded uniformly. One Protocol here means the recording
wrapper in `recording.py` is written once and applies to every reader, present and future,
including a test double.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class FetchResult:
    """One read: what was asked for, what came back, and whether it was allowed.

    `refused` is not an error. A reader that declines a URL outside the component's own
    hosts has done its job, and the model needs to be told so it looks somewhere else.
    Without recording the refusal, the diagnosis cannot say after the fact whether a
    third-party page was fetched or invented.
    """

    url: str
    revision: str
    body: str
    status: int
    refused: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.refused and 200 <= self.status < 300 and bool(self.body)


class SourceReader(Protocol):
    """Read one pinned source. Read-only by construction: there is no write method."""

    def fetch(self, url: str, revision: str = "") -> FetchResult:
        """Fetch `url`, pinned at `revision` where the host supports pinning."""
        ...
