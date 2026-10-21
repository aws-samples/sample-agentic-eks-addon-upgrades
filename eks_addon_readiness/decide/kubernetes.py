"""Kubernetes minor-version algebra and hop expansion.

Every comparison in Stage 4 runs through here, because comparing `"1.9"` against
`"1.10"` as strings puts 1.10 *before* 1.9 and would silently invert a compatibility
ceiling. Versions are compared as integer tuples, never as text.

Only major and minor matter. EKS control planes are addressed as `1.35`, compatibility
is published per minor, and a patch level never changes whether a component is
supported.
"""

from __future__ import annotations

import re

from ..knowledge import VersionRange
from ..models import KubernetesHop

_MINOR = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)$")

# A ceiling on hop expansion. Not a Kubernetes limit — a guard against a typo like
# `--target 11.36` quietly asking for hundreds of hops and a document nobody can read.
MAX_HOPS = 12


class VersionError(ValueError):
    """A version or a pair of versions that cannot be interpreted."""


def parse_minor(version: str) -> tuple[int, int]:
    """`"1.35"` as `(1, 35)`.

    >>> parse_minor("1.35")
    (1, 35)
    >>> parse_minor("1.9") < parse_minor("1.10")
    True
    """
    match = _MINOR.match(version.strip())
    if not match:
        raise VersionError(
            f"{version!r} is not a Kubernetes minor version; expected a form like '1.35'"
        )
    return int(match.group("major")), int(match.group("minor"))


def format_minor(parsed: tuple[int, int]) -> str:
    """`(1, 35)` as `"1.35"`."""
    return f"{parsed[0]}.{parsed[1]}"


def permits(window: VersionRange, kubernetes_version: str) -> bool:
    """Whether a half-open window contains a Kubernetes version.

    `maximum_exclusive` is exclusive, so a window ending `1.37` permits `1.36` and
    refuses `1.37`.

    >>> permits(VersionRange(minimum_inclusive="1.33", maximum_exclusive="1.37"), "1.36")
    True
    >>> permits(VersionRange(minimum_inclusive="1.33", maximum_exclusive="1.37"), "1.37")
    False
    >>> permits(VersionRange(minimum_inclusive="1.33"), "1.99")
    True
    >>> permits(VersionRange(minimum_inclusive="1.33"), "1.32")
    False
    """
    version = parse_minor(kubernetes_version)
    if version < parse_minor(window.minimum_inclusive):
        return False
    if window.maximum_exclusive is None:
        return True
    return version < parse_minor(window.maximum_exclusive)


def expand_hops(current: str, target: str) -> tuple[KubernetesHop, ...]:
    """The ordered single-minor hops from `current` to `target`.

    One hop per minor, because **EKS does not allow skipping a minor**. Asking for
    1.35 → 1.37 is really asking for 1.35 → 1.36 → 1.37, and a component must survive
    every intermediate version, not just the destination. Collapsing that into a single
    comparison against the final target is the mistake this function exists to prevent:
    a component whose window ends at 1.36 would pass a naive 1.35-to-1.37 check while
    being stranded at the very first hop.

    >>> [ (h.current_version, h.target_version) for h in expand_hops("1.35", "1.37") ]
    [('1.35', '1.36'), ('1.36', '1.37')]
    >>> [ (h.current_version, h.target_version) for h in expand_hops("1.35", "1.36") ]
    [('1.35', '1.36')]
    """
    start, end = parse_minor(current), parse_minor(target)

    if start[0] != end[0]:
        # Kubernetes has published no major other than 1, so the semantics of a major
        # bump are unknown. Refusing beats inventing a hop sequence across it.
        raise VersionError(
            f"cannot plan across Kubernetes major versions: {current} to {target}"
        )
    if end < start:
        raise VersionError(
            f"{target} is older than {current}; EKS does not support downgrades"
        )
    if end == start:
        # Not an empty result. A caller asking to assess the version already running has
        # made a mistake, and returning "no hops, nothing to do" would look like a pass.
        raise VersionError(
            f"target {target} is the version already running; nothing to assess"
        )

    hop_count = end[1] - start[1]
    if hop_count > MAX_HOPS:
        raise VersionError(
            f"{current} to {target} is {hop_count} minor versions, above the {MAX_HOPS} "
            "hop limit; check the target is what you meant"
        )

    return tuple(
        KubernetesHop(
            current_version=format_minor((start[0], minor)),
            target_version=format_minor((start[0], minor + 1)),
        )
        for minor in range(start[1], end[1])
    )
