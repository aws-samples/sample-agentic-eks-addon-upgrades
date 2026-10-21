"""Where a stage gets its parameters, and the one rule about them.

Everything environment-specific arrives as an environment variable set by
Terraform, or as a field on the Step Functions payload. Nothing is hardcoded: no
account id, no bucket name, no cluster name, no target version. This package is
published, and a reader must be able to point it at their own cluster by changing
a tfvars file and nothing else.

The split between the two sources is deliberate:

    environment   what the *deployment* is: bucket, table, region
    payload       what this *run* is about: cluster, target version, document URIs

So one deployment serves any number of clusters and any number of target versions,
which is what makes the fleet story work.
"""

from __future__ import annotations

import os
from typing import Any, NamedTuple

# The default target. 1.36 is the newest Kubernetes version the EKS add-on
# catalogue offers in us-east-1 as of 2026-09-12, so it is the newest hop that can
# be decided from AWS's own published compatibilities rather than from research.
# Overridable per run; never assume it.
DEFAULT_TARGET_KUBERNETES_VERSION = "1.36"


class MissingParameter(KeyError):
    """A required parameter was absent from both the payload and the environment."""


class Settings(NamedTuple):
    """One stage invocation's parameters."""

    bucket: str
    cluster_name: str
    region: str
    target_kubernetes_version: str

    @property
    def bucket_uri_prefix(self) -> str:
        return f"s3://{self.bucket}"


def _required(event: dict[str, Any], key: str, env_var: str) -> str:
    value = str(event.get(key) or os.environ.get(env_var, "")).strip()
    if not value:
        raise MissingParameter(
            f"{key!r} must be supplied on the payload or as {env_var} in the "
            "environment. Nothing is defaulted, because guessing which cluster to "
            "read would be worse than failing."
        )
    return value


def from_event(event: dict[str, Any]) -> Settings:
    """Resolve this invocation's parameters from the payload, then the environment."""
    return Settings(
        bucket=_required(event, "bucket", "DOCUMENT_BUCKET"),
        cluster_name=_required(event, "cluster_name", "CLUSTER_NAME"),
        region=str(
            event.get("region") or os.environ.get("AWS_REGION") or "us-east-1"
        ).strip(),
        target_kubernetes_version=str(
            event.get("target_kubernetes_version")
            or os.environ.get("TARGET_KUBERNETES_VERSION")
            or DEFAULT_TARGET_KUBERNETES_VERSION
        ).strip(),
    )
