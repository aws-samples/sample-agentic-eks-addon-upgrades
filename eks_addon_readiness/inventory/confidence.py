"""Compute a confidence rating from checkable signals.

Design: every signal declares the best rating its evidence permits — a **cap** —
and the final rating is the minimum of all caps.

`min` is commutative, associative and idempotent, so:

  * rule evaluation order cannot change the result
  * evaluating a rule twice cannot change the result
  * adding a rule later can only ever lower a rating, never silently raise one

A decrementing scheme ("two downgrades means drop two levels") has none of those
properties and is much harder to reason about.

Confidence describes the **strength of the evidence**, not the probability that a
number is correct. HIGH means the claim is asserted by an authority that would
have to be wrong for the claim to be wrong.

No model participates. Same inputs produce the same rating, always.
"""

from __future__ import annotations

from enum import StrEnum
from typing import NamedTuple

from ..models import Confidence

# Ordered worst to best. UNRESOLVED is not merely "low confidence": it means no
# usable version was established, so a later stage cannot compare it against a
# compatibility range at all.
_ORDER: tuple[Confidence, ...] = (
    Confidence.UNRESOLVED,
    Confidence.LOW,
    Confidence.MEDIUM,
    Confidence.HIGH,
)
_RANK = {level: index for index, level in enumerate(_ORDER)}


class VersionSource(StrEnum):
    """Where a version came from, ordered most to least authoritative."""

    EKS_ADDON_API = "EKS_ADDON_API"
    HELM_CHART_LABEL = "HELM_CHART_LABEL"
    IMAGE_TAG = "IMAGE_TAG"
    NONE = "NONE"


# The ceiling each source permits. An image tag is chosen by whoever built the
# image and has no binding to its contents, so it can never support better than
# LOW.
SOURCE_CEILING: dict[VersionSource, Confidence] = {
    VersionSource.EKS_ADDON_API: Confidence.HIGH,
    VersionSource.HELM_CHART_LABEL: Confidence.HIGH,
    VersionSource.IMAGE_TAG: Confidence.LOW,
    VersionSource.NONE: Confidence.UNRESOLVED,
}


class Signal(NamedTuple):
    """One piece of reasoning, and the best rating it permits."""

    code: str
    caps_at: Confidence
    detail: str


def worst(*levels: Confidence) -> Confidence:
    """Lowest of the given confidence levels."""
    return min(levels, key=lambda level: _RANK[level])


def combine(source: VersionSource, signals: tuple[Signal, ...]) -> Confidence:
    """Final rating: the source's ceiling, lowered by every signal's cap.

    Order-independent by construction.
    """
    return worst(SOURCE_CEILING[source], *(signal.caps_at for signal in signals))


# --- signal constructors -------------------------------------------------
# Each is a named factory rather than an inline literal so that a code appearing
# in a report can be traced to exactly one place in the source.


def no_version_found() -> Signal:
    return Signal(
        "NO_VERSION_FOUND",
        Confidence.UNRESOLVED,
        "No version could be read from any source.",
    )


def version_is_placeholder(raw: str, source: VersionSource) -> Signal:
    return Signal(
        "VERSION_IS_PLACEHOLDER",
        Confidence.UNRESOLVED,
        f"{raw!r} from {source} names a channel or base image, not a version.",
    )


def digest_pinned_without_tag(repository: str) -> Signal:
    return Signal(
        "IMAGE_DIGEST_ONLY",
        Confidence.UNRESOLVED,
        f"{repository} is pinned by digest with no tag, so it carries no version.",
    )


def ambiguous_containers(count: int) -> Signal:
    return Signal(
        "MULTIPLE_CONTAINERS_AMBIGUOUS",
        Confidence.UNRESOLVED,
        f"{count} containers with differing tags and no authoritative source to "
        "identify which represents the component.",
    )


def label_image_disagree(label_version: str, image_version: str) -> Signal:
    return Signal(
        "LABEL_IMAGE_DISAGREE",
        Confidence.MEDIUM,
        f"Chart label declares {label_version} but the running image is "
        f"{image_version}. The label records what Helm installed, not what is "
        "running.",
    )


def label_is_chart_version(
    label_version: str, chart_version: str, image_version: str
) -> Signal:
    """The `app.kubernetes.io/version` label carried the CHART version, not the app version.

    Some charts stamp their OWN chart version into `app.kubernetes.io/version` rather than
    the application (image) version. kyverno is the live example: the label and the
    `helm.sh/chart` suffix both read 3.3.9 while every workload image is v1.13.6. The whole
    pipeline resolves compatibility in APPLICATION versions, so recording 3.3.9 as the
    installed version makes every later stage look up a version space the project does not
    publish a matrix for. When the label disagrees with the image AND equals the chart
    version parsed from `helm.sh/chart`, the image tag is the honest installed version.

    This caps at MEDIUM, like `LABEL_IMAGE_DISAGREE`: the image tag is corroborated by the
    chart-label equality (the disagreement is explained, not unresolved), but an image tag
    is still chosen by whoever built the image, so it does not earn HIGH on its own.
    """
    return Signal(
        "LABEL_IS_CHART_VERSION",
        Confidence.MEDIUM,
        f"The app.kubernetes.io/version label {label_version} equals the Helm chart "
        f"version from helm.sh/chart, not the application version; the running image is "
        f"{image_version}. Using the image tag as the installed version.",
    )


def label_uncorroborated(label_version: str, reason: str) -> Signal:
    """A chart label was the only usable source and nothing could check it.

    This is the same situation as `LABEL_IMAGE_DISAGREE` with the answer unknown.
    A chart label records what Helm was asked to install, which is not necessarily
    what is running — KEDA on the reference cluster declares 2.17.2 and runs
    2.17.0. Where an image tag agrees with the label, that drift is ruled out and
    HIGH is earned; where no comparison is possible, it is not.
    """
    return Signal(
        "LABEL_UNCORROBORATED",
        Confidence.MEDIUM,
        f"Chart label declares {label_version} and no image tag could confirm it "
        f"({reason}). The label records what Helm installed, not what is running.",
    )


def inconsistent_release_labels(versions: tuple[str, ...]) -> Signal:
    return Signal(
        "INCONSISTENT_RELEASE_LABELS",
        Confidence.MEDIUM,
        "Workloads in this release declare differing versions: "
        + ", ".join(sorted(versions)),
    )


def addon_mid_transition(status: str) -> Signal:
    """The add-on is being created, updated or deleted, so its version is in flux.

    This replaces an earlier `ADDON_UNHEALTHY` signal that fired on any health
    issue, which was a category error: confidence here rates the **evidence for a
    version number**, and `InsufficientNumberOfReplicas` says how many pods are
    running, not which version they run. EKS reports `addonVersion` because that
    is what it installed; a drained node does not make that number less reliable.

    A transition is different. Mid-update, some pods run the old version and some
    the new, so the single reported version genuinely may not describe what is
    running.

    Health remains important — you should not upgrade a degraded add-on — but that
    is upgrade *readiness*, decided in a later stage from the health fields carried
    on the record, not version *evidence* rated here.
    """
    return Signal(
        "ADDON_MID_TRANSITION",
        Confidence.MEDIUM,
        f"Add-on status is {status}, so pods may be running a mix of the old and "
        "new versions and the single reported version may not describe all of them.",
    )


def addon_workload_not_correlated(addon_name: str) -> Signal:
    return Signal(
        "ADDON_WORKLOAD_NOT_CORRELATED",
        Confidence.MEDIUM,
        f"No workload declares the name {addon_name!r}, so the version reported by "
        "the EKS API could not be checked against anything running. EKS installs "
        "some add-ons under a different workload name.",
    )


def workload_detail_unavailable() -> Signal:
    return Signal(
        "WORKLOAD_DETAIL_UNAVAILABLE",
        Confidence.MEDIUM,
        "Container images could not be read, so the reported version could not be "
        "corroborated.",
    )


def model_interpreted(detail: str) -> Signal:
    """Reserved for evidence a model had to interpret.

    Unused at present. Present so that if a model is ever introduced into this
    path its involvement caps the rating and appears in the output, rather than
    being invisible.
    """
    return Signal("MODEL_INTERPRETED", Confidence.MEDIUM, detail)


# --- positive signals ----------------------------------------------------
# These cap at HIGH, so they never raise a rating. They exist to record why a
# rating was earned, which is what makes the output auditable.


def sources_agree(source_a: VersionSource, source_b: VersionSource, version: str) -> Signal:
    return Signal(
        "SOURCES_AGREE",
        Confidence.HIGH,
        f"{source_a} and {source_b} both report {version}.",
    )


def authoritative_source(source: VersionSource, version: str) -> Signal:
    return Signal(
        "AUTHORITATIVE_SOURCE",
        Confidence.HIGH,
        f"{version} is reported by {source}.",
    )
