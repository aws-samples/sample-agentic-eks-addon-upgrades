"""Fingerprints of rule-module source text, and the one way they may fail.

Each stage cites the rules that produced its document, as a digest over those
modules' **source text**. That is deliberately conservative: a comment-only edit
moves the digest. A spurious "the rules changed" costs a re-read; a missed one
would let a changed rule pass as the original, which is the failure the digest
exists to prevent.

WHY THIS MODULE EXISTS SEPARATELY FROM THE FOUR STAGES
-----------------------------------------------------
The per-stage module *lists* must stay per-stage. Sharing one digest across
stages would mean an identity-rule edit in Stage 2 moved the Stage 3 digest,
telling a reader the gap rules changed when they did not. Only the ~20-line
hashing *mechanism* is shared here.
The lists stay with their stages, in `report.py`, `facts/ruleset.py`,
`decide/ruleset.py` and `order/ruleset.py`.

The recipe below must stay fixed: for
each module in list order, hash the module's short name, a NUL, its source text,
a NUL. Any change to it changes every stage digest and breaks comparison against
documents already produced.

RAISE, DO NOT SUBSTITUTE, WHEN THE SOURCE IS UNREADABLE
-------------------------------------------------------------------------
Catching the "source is unreadable" case and returning the string
`"UNAVAILABLE"` in place of a digest lets every downstream document carry
`ruleset_digest: "UNAVAILABLE"`, every document digest compute cleanly over it,
and every stage still succeed. The pipeline would look healthy while its
reproducibility claim was empty — a stored document that cannot say which rules
produced it is not evidence of anything, which is the whole reason these digests
exist.

`inspect.getsource` needs the module's `.py` text to be readable at runtime. That
holds when a Lambda deployment package is extracted to `/var/task` with sources
intact, and it does not hold if a build ships only bytecode, imports the package
straight from a zip, or strips sources. That is a packaging property, and a
packaging mistake must not be able to quietly void the guarantee.

So this raises `RulesetUnavailable`. A stage that cannot fingerprint its own rules
fails, loudly, at the point of the defect. Nothing downstream ever sees a
placeholder digest, because none is ever produced.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
from types import ModuleType

DIGEST_PREFIX = "sha256:"


class RulesetUnavailable(RuntimeError):
    """A rule module's own source text could not be read.

    Raised rather than substituted. See the module docstring: the substituted
    value is the failure mode this exception exists to make impossible.
    """


def module_source(module: ModuleType) -> str:
    """The module's source text, or raise.

    `inspect.getsource` raises `OSError` when no source file is available and
    `TypeError` for objects that have none. Both mean the same thing here and both
    are fatal.
    """
    try:
        return inspect.getsource(module)
    except (OSError, TypeError) as exc:
        raise RulesetUnavailable(
            f"cannot read the source of rule module {module.__name__!r}: "
            f"{type(exc).__name__}: {exc}. The ruleset digest would be meaningless, "
            "so no document is produced. This is a packaging defect: the deployment "
            "package must carry the rule modules' .py sources, not only bytecode."
        ) from exc


def ruleset_digest(package: str, module_names: tuple[str, ...]) -> str:
    """Digest over the source text of `package`'s named rule modules, in order.

    `package` is the dotted package the modules live in; `module_names` are their
    short names in a fixed, explicitly-listed order, so the digest never depends on
    filesystem enumeration and adding a rule module is a deliberate act.
    """
    hasher = hashlib.sha256()
    for name in module_names:
        module = importlib.import_module(f"{package}.{name}")
        source = module_source(module)
        hasher.update(name.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(source.encode("utf-8"))
        hasher.update(b"\x00")
    return DIGEST_PREFIX + hasher.hexdigest()
