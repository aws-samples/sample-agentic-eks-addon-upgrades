"""A cited source's href is the GitHub page, not the raw bytes the agent fetched.

The fact keeps the raw URL (that is what the quote verified against); only the link a reader
clicks changes, and only for shapes the renderer recognises.
"""

from __future__ import annotations

from eks_addon_readiness.view.render.html import _browse_url


def test_raw_github_becomes_blob() -> None:
    assert (
        _browse_url("https://raw.githubusercontent.com/kedacore/keda/main/README.md")
        == "https://github.com/kedacore/keda/blob/main/README.md"
    )


def test_api_release_listing_and_tag_become_the_releases_pages() -> None:
    assert (
        _browse_url("https://api.github.com/repos/argoproj/argo-rollouts/releases?per_page=30")
        == "https://github.com/argoproj/argo-rollouts/releases"
    )
    assert (
        _browse_url("https://api.github.com/repos/cert-manager/cert-manager/releases/tags/v1.21.2")
        == "https://github.com/cert-manager/cert-manager/releases/tag/v1.21.2"
    )


def test_anything_else_passes_through_untouched() -> None:
    for url in (
        "https://github.com/kedacore/keda/blob/main/README.md",
        "https://keda.sh/docs/2.17/operate/cluster/",
        "https://example.test/x",
    ):
        assert _browse_url(url) == url
    assert _browse_url(None) == ""
