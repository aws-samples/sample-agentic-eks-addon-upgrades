"""Reading sources, and recording every byte read so the judging layer can check them.

One rule governs this package: **nothing is handed to the model that is not also recorded
in the `PageSet`.** An HTTP client that lives inside the model's tool loop and retains
nothing -- handing `result.text` to the model and keeping none of it -- makes a
quote-versus-source check impossible anywhere downstream; recording every fetch is what
closes that.

`ports.py` states the contract, `web_docs.py` implements it over HTTPS, and
`recording.py` wraps any reader so the recording is not something an implementation has to
remember to do.
"""

from __future__ import annotations

from .ports import FetchResult, SourceReader
from .recording import RecordingReader
from .web_docs import HttpSourceReader, SourceScope

__all__ = [
    "FetchResult",
    "HttpSourceReader",
    "RecordingReader",
    "SourceReader",
    "SourceScope",
]
