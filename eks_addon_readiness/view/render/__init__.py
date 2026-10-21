"""Views over the value set. Each one is a pure function and derives nothing.

A renderer takes `(ReportView, Provenance)` and returns bytes. It may import
`view.model` and the standard library, and nothing else -- no stage document, no
fact store, no `decide`, no `order`. `tests/view/test_render_boundary.py` walks
these modules' imports and fails on a violation.

That restriction is the whole mechanism. Renderers that import
`..decide.ReadinessAssessment`, `..facts.FactStore` and `..order.UpgradePlan`
directly can each re-derive facts for themselves, three times,
three different ways. With the value set as the only input there is nothing to
re-derive a number from.

The JSON view is not here, deliberately. It is not a rendering: it is
`PresentationDocument.canonical_json()`, the document itself. A hand-assembled JSON subset
can omit the very number its own defect is about;
making JSON the document removes the code path where that could happen again.
"""

from .html import to_html
from .markdown import to_markdown

__all__ = ["to_html", "to_markdown"]
