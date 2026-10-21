"""Reduce a resolved version to the series a compatibility rule is keyed on.

Compatibility is published per *series*, not per patch. Upstream projects say
"0.8.x requires Kubernetes 1.31 or later", never "0.8.1 requires 1.31 and 0.8.2
requires 1.31". `CompatibilityRule.component_version_series` reflects that with its
`^\\d+\\.\\d+\\.x$` pattern, so a lookup has to map `0.6.4` onto `0.6.x` before it can
match anything.

Deriving this rather than storing it keeps the fact store keyed the way upstream
publishes, instead of forcing a fact per patch release that nobody publishes.
"""

from __future__ import annotations

import re

from ..inventory.versions import normalize_version
from ..knowledge import is_exact_spec, is_series_spec

# A series needs at least a major and a minor. A bare major (`2`) cannot be mapped:
# `2.x` is not a series any rule is keyed on, and inventing `2.0.x` would be a guess
# about which minor was meant.
_MAJOR_MINOR = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)")
_MAJOR_MINOR_PATCH = re.compile(r"^(?P<mmp>\d+\.\d+\.\d+)")


def version_series(version: str | None) -> str | None:
    """The `major.minor.x` series a version belongs to, or None.

    Returns None rather than guessing whenever the input cannot be reduced to a
    major and a minor. That includes an already-`UNRESOLVED` version from Stage 2,
    a placeholder such as `alpine`, and a bare major.

    Normalization is applied first, so build metadata and `v` prefixes do not
    prevent a match. This matters on EKS: `v1.14.3-eksbuild.16` has to reach
    `1.14.x` for a rule to apply.

    >>> version_series("0.6.4")
    '0.6.x'
    >>> version_series("2.17.0")
    '2.17.x'
    >>> version_series("v1.14.3-eksbuild.16")
    '1.14.x'
    >>> version_series("alpine") is None
    True
    >>> version_series(None) is None
    True
    >>> version_series("2") is None
    True
    """
    normalized = normalize_version(version)
    if not normalized:
        return None
    match = _MAJOR_MINOR.match(normalized)
    if not match:
        return None
    return f"{match.group('major')}.{match.group('minor')}.x"


def exact_version(version: str | None) -> str | None:
    """The `major.minor.patch` an installed version reduces to, or None.

    The exact-release counterpart of `version_series`, used to match an installed
    version against a release-keyed compatibility rule (`3.2.0`). Normalization strips
    `v` prefixes and build metadata first, so `v3.2.0-eksbuild.1` reduces to `3.2.0`.

    Returns None when there is no patch level to pin — a bare `3.2`, a placeholder, or an
    unresolved version cannot match an exact-release rule.

    >>> exact_version("3.2.0")
    '3.2.0'
    >>> exact_version("v3.2.0-eksbuild.1")
    '3.2.0'
    >>> exact_version("3.2") is None
    True
    >>> exact_version("alpine") is None
    True
    """
    normalized = normalize_version(version)
    if not normalized:
        return None
    match = _MAJOR_MINOR_PATCH.match(normalized)
    if not match:
        return None
    return match.group("mmp")


def spec_matches_version(spec: str, installed_version: str | None) -> bool:
    """Whether an installed version satisfies a compatibility rule's spec.

    The single place that decides series-vs-exact matching, so Stage 3 (gap detection)
    and Stage 4 (the verdict) agree by construction:

    - a **series** spec (`0.6.x`) matches when the installed version's series is `0.6.x`;
    - an **exact** spec (`3.2.0`) matches when the installed version reduces to `3.2.0`.

    A version that cannot be reduced to the needed granularity does not match — no
    guessing.

    >>> spec_matches_version("0.6.x", "0.6.4")
    True
    >>> spec_matches_version("0.6.x", "0.7.1")
    False
    >>> spec_matches_version("3.2.0", "v3.2.0-eksbuild.1")
    True
    >>> spec_matches_version("3.2.0", "3.2.1")
    False
    >>> spec_matches_version("0.6.x", None)
    False
    """
    if is_series_spec(spec):
        return version_series(installed_version) == spec
    if is_exact_spec(spec):
        return exact_version(installed_version) == spec
    return False
