"""The diagnosis bundle: what is captured, and the manifest that proves it was captured.

    in    the reads a broken component's own namespace can answer
    out   one `BundleManifest`, written LAST, which is what makes the bundle citable

**THE MANIFEST IS WRITTEN LAST AND THAT IS THE WHOLE MECHANISM.** A rollback destroys the
evidence -- the pods are replaced, the previous container's log goes with them, the release's
current revision changes -- so the human's requirement is that the diagnosis is captured BEFORE
the rollback runs. An instruction to do that in the right order is worth nothing the first time
somebody reorders two lines of a buildspec. So the order is enforced instead: every file is
uploaded, then the manifest naming all of them is uploaded, and `remediate.plan_rollback` refuses
to produce a rollback command unless it is handed a COMPLETE manifest. A bundle that is missing
its manifest is indistinguishable from a bundle that was never written, which is exactly the
reading that should block a rollback.

**EVERY FILE CARRIES THE COMMAND THAT PRODUCED IT.** Not for tidiness: six weeks later the
question about a bundle is always "is this the whole story or did something fail to run", and a
file list cannot answer it. A `command` per entry and an `exit_status` per entry can -- a read
that was DENIED leaves a row saying so, rather than leaving a gap that reads as "there was
nothing there".

**WHAT IS DELIBERATELY NOT IN A BUNDLE.** No Secret contents, no `kubectl get secret` of any
kind, and no nonce, token or presigned URL. The lane identity can list secrets in its namespace
(`fence.py` says why it must), and the bundle does not exercise that: a diagnosis that shipped
another component's secret to an S3 prefix would be a data exfiltration path wearing the face of
an audit trail.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from ..models import ContractModel

# The reads a bundle is made of, in the order the human listed them plus the two that proved
# necessary. Names are the file names in the bundle, so this tuple IS the contract between the
# buildspec that writes them and `manifest_is_complete` that checks them.
#
# `logs/` IS A PREFIX RATHER THAN A FILE, because there is one file per pod per container per
# stream and none of those names is knowable in advance. `REQUIRED_PREFIXES` covers it.
REQUIRED_FILES: tuple[str, ...] = (
    "pods.json",
    "events.json",
    "helm-history.json",
    "helm-status.json",
    "values.yaml",
    "rendered.yaml",
)

# At least one log file. A bundle with no pod log at all has not answered the question the human
# actually asked -- "pod status, and pod logs for errors" -- so its absence is a failure of the
# capture rather than a component that had nothing to say.
REQUIRED_PREFIXES: tuple[str, ...] = ("logs/",)

# For a managed add-on there are no pods this lane owns and no Helm release, so the Helm files
# and the rendered manifest are replaced by the add-on's own health.
REQUIRED_FILES_MANAGED_ADDON: tuple[str, ...] = (
    "pods.json",
    "events.json",
    "describe-addon.json",
)


class BundleFile(ContractModel):
    """One captured read, and whether it succeeded."""

    name: str = Field(min_length=1)
    bytes_written: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    # The command, verbatim, so a reader can re-run it. No credential ever appears in one of
    # these: the commands are `kubectl`/`helm`/`aws` invocations whose identity comes from the
    # environment, never from an argument.
    command: str = Field(min_length=1)
    # 0 means the read succeeded. A NON-ZERO STATUS IS STILL A CAPTURED FILE -- the file holds
    # whatever the command printed, usually a denial or a not-found, and recording it is how a
    # denied read stays distinguishable from an empty one.
    exit_status: int = 0


class BundleManifest(ContractModel):
    """The index of one bundle. Written last; its presence is what licenses a rollback."""

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["UpgradeDiagnosisBundle"] = "UpgradeDiagnosisBundle"

    run_id: str = Field(min_length=1)
    component: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    # `before` or `after`. Both are captured on a run that fails; only `before` on one that does
    # not, because there is nothing to diagnose.
    phase: Literal["before", "after"]
    managed_addon: bool = False

    files: tuple[BundleFile, ...] = Field(min_length=1)
    captured_at: datetime

    @model_validator(mode="after")
    def captured_at_carries_an_offset(self) -> BundleManifest:
        """`StepDecision.decided_at_carries_an_offset`'s argument, and it holds identically here.

        The one comparison this document exists for is "was the bundle captured before the
        rollback", and a naive instant cannot be compared against an aware one.
        """
        moment = self.captured_at
        if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
            raise ValueError(
                "captured_at must carry a UTC offset. This document's whole purpose is to "
                "establish that the diagnosis preceded the rollback, and that is a comparison "
                "of instants."
            )
        return self

    @property
    def names(self) -> frozenset[str]:
        return frozenset(f.name for f in self.files)

    @property
    def failed_reads(self) -> tuple[str, ...]:
        """Reads that returned non-zero. Reported, never silently dropped."""
        return tuple(sorted(f.name for f in self.files if f.exit_status != 0))

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(
            self.canonical_json().encode("utf-8")
        ).hexdigest()


class IncompleteBundle(Exception):
    """The bundle does not hold what a person needs to read. Blocks a rollback."""


def missing_from(manifest: BundleManifest) -> tuple[str, ...]:
    """What a complete bundle would have that this one does not. Empty means complete.

    RETURNS THE GAP RATHER THAN A BOOLEAN, because the caller's job is to say what is missing:
    "the bundle is incomplete" sends somebody to read an S3 prefix, and "pods.json and logs/ are
    missing" tells them the capture could not reach the namespace.
    """
    required = (
        REQUIRED_FILES_MANAGED_ADDON if manifest.managed_addon else REQUIRED_FILES
    )
    present = manifest.names
    missing = [name for name in required if name not in present]
    for prefix in REQUIRED_PREFIXES:
        if not any(name.startswith(prefix) for name in present):
            missing.append(prefix)
    return tuple(missing)


def require_complete(manifest: BundleManifest) -> BundleManifest:
    """Return `manifest` if it is complete; raise if it is not.

    THE GATE, and it is a function rather than a property so that the refusal carries a message
    naming the component and the gap. Called by `remediate.plan_rollback` and by the report
    writer, so a bundle that could not be captured stops a rollback AND shows up in the report
    rather than only one of the two.
    """
    missing = missing_from(manifest)
    if missing:
        raise IncompleteBundle(
            f"the {manifest.phase} diagnosis bundle for {manifest.component!r} is missing "
            f"{list(missing)}. A rollback destroys the evidence, so it does not run until the "
            "evidence is on disk. Capturing it is the fix; skipping this check is not."
        )
    return manifest
