"""Version string handling: normalization, comparison, and placeholder detection.

Placeholder detection is the important part. A string can be perfectly
well-formed and still carry no version information — `0.0.0-non-release-version`
and `alpine` are both real examples from a live cluster. Such values must be
reported as unusable rather than as low-confidence versions, because a later
stage can compare a low-confidence version against a compatibility range but
cannot compare an unusable one at all.
"""

from __future__ import annotations

import re

# Tags that name a channel or a base image rather than a version.
PLACEHOLDER_TAGS = frozenset(
    {
        "latest",
        "stable",
        "edge",
        "dev",
        "devel",
        "development",
        "main",
        "master",
        "head",
        "nightly",
        "snapshot",
        "canary",
        "test",
        "alpine",
        "slim",
        "debian",
        "ubuntu",
        "distroless",
        "scratch",
        "release",
        "current",
    }
)

# A version must contain at least one digit group. Anchored on a leading digit
# after an optional "v" prefix.
_VERSION = re.compile(r"^v?(?P<core>\d+(?:\.\d+)*)(?P<rest>[-+.].*)?$")

# Chart labels are "<name>-<version>"; the name may contain hyphens and the
# version may carry a hyphenated prerelease suffix, so the split anchors on the
# first segment beginning with a digit.
_CHART_LABEL = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^\s]*)$")

# A core of all zeros conveys no release information, however well-formed.
_ALL_ZERO_CORE = re.compile(r"^0(?:\.0)*$")


def strip_chart_version(chart_label: str) -> tuple[str, str | None]:
    """Split a `helm.sh/chart` value into chart name and chart version.

    >>> strip_chart_version("metrics-server-3.11.0")
    ('metrics-server', '3.11.0')
    >>> strip_chart_version("cognitoidentity-chart-0.0.0-non-release-version")
    ('cognitoidentity-chart', '0.0.0-non-release-version')
    >>> strip_chart_version("standalone")
    ('standalone', None)
    """
    text = chart_label.strip()
    match = _CHART_LABEL.match(text)
    if not match:
        return text, None
    return match.group("name"), match.group("version")


def normalize_version(raw: str | None) -> str | None:
    """Return a comparable version, or `None` when the value is not one.

    Strips a leading `v` so that `v0.6.4` and `0.6.4` compare equal. Rejects
    placeholders and all-zero cores.

    >>> normalize_version("v0.6.4")
    '0.6.4'
    >>> normalize_version("2.17.2")
    '2.17.2'
    >>> normalize_version("alpine") is None
    True
    >>> normalize_version("0.0.0-non-release-version") is None
    True
    >>> normalize_version("1.13.2-eksbuild.7")
    '1.13.2-eksbuild.7'
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.casefold() in PLACEHOLDER_TAGS:
        return None

    match = _VERSION.match(text)
    if not match:
        return None
    if _ALL_ZERO_CORE.match(match.group("core")):
        return None

    return text[1:] if text.startswith("v") else text


def is_placeholder(raw: str | None) -> bool:
    """True when a value is present but carries no version information."""
    return bool(raw and raw.strip()) and normalize_version(raw) is None


def versions_agree(left: str | None, right: str | None) -> bool:
    """True when two version strings denote the same version once normalized.

    Comparison is on the normalized form, so `v0.6.4` agrees with `0.6.4`.
    Two unusable values never agree — agreement on a placeholder proves nothing.

    >>> versions_agree("v0.6.4", "0.6.4")
    True
    >>> versions_agree("2.17.2", "2.17.0")
    False
    >>> versions_agree("alpine", "alpine")
    False
    """
    normalized_left, normalized_right = normalize_version(left), normalize_version(right)
    if normalized_left is None or normalized_right is None:
        return False
    return normalized_left == normalized_right
