"""`ClusterReader` backed by the managed Amazon EKS MCP Server.

No model participates. MCP tools are invoked directly, so enumeration stays
deterministic. MCP is used because a target cluster may have a private-only
endpoint — which the Kubernetes client cannot reach from outside the VPC but the
EKS MCP Server's read-only tools can — and because read and write capability are
separated by distinct IAM actions (`eks-mcp:CallReadOnlyTool` vs
`eks-mcp:CallPrivilegedTool`).

Response shape, established empirically against the preview server:

    call_tool_sync(...) -> {
        "toolUseId": str,
        "status": "success" | ...,
        "isError": bool,
        "content": [ { "text": "<python repr of the server payload>" } ],
    }

The inner payload arrives as a Python `repr` string rather than JSON, so
`json.loads` fails and `ast.literal_eval` is required. `literal_eval` evaluates
only literals and cannot execute code, so this is safe on untrusted input. Both
parsers are attempted, in that order, so the collector keeps working if the server
switches to JSON.

A tool can also report failure *inside* a successful response, which is how
Kubernetes RBAC denials surface. Those are returned as errors, not raised.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
from collections.abc import Iterator
from typing import Any

from ..ports import ListResult, ReadResult

MCP_ENDPOINT_TEMPLATE = "https://eks-mcp.{region}.api.aws/mcp"

# The server caps list responses at this many items. Established empirically and
# re-verified deliberately:
#
#   * `list_k8s_resources` accepts only api_version, cluster_name, field_selector,
#     kind, label_selector, namespace. Passing `limit`, `continue` or `_continue`
#     returns "Invalid input for tool list_k8s_resources" — strict schema
#     validation, no passthrough.
#   * The response carries `meta: null` and `count: N`, where `count` is the
#     *returned* count, not the true total. A cluster with 271
#     CustomResourceDefinitions reports `count: 100` with the status text
#     "Successfully listed 100 CustomResourceDefinition resources".
#   * There is therefore no truncation indicator of any kind, and a response of
#     exactly this length cannot be distinguished from a complete one.
#
# `next_token` does exist on this server, but only on AWS-native control-plane
# tools such as `get_eks_insights`. It is not available on the Kubernetes list
# tools. The cap is worked around by narrowing the query with `namespace` or
# `label_selector` rather than by paginating.
#
# RE-VERIFIED 2026-09-12 against eks-mcp server version 1.0.0 (protocol 2025-03-26),
# by reading the server's own declared tool schema rather than guessing parameter
# names. `list_k8s_resources` declares exactly six parameters -- api_version,
# cluster_name, field_selector, kind, label_selector, namespace -- and no pagination
# parameter of any kind. `limit` and `continue` are both rejected with "Invalid input
# for tool list_k8s_resources". The response still carries `meta: null` and a `count`
# that is the returned count, with no total and no continue token.
#
# The cap NUMBER was not re-confirmed: the only reachable cluster has 10 CRDs, far
# under it. That does not matter -- what is confirmed is that nothing can page past
# the ceiling and no field reveals it, which is what makes treating a full-length page
# as possibly truncated correct whatever the number is. Hence the env override below.
LIST_RESULT_CAP = int(os.environ.get("MCP_LIST_RESULT_CAP", "100"))

# Tools this reader is permitted to call. IAM already withholds privileged tools;
# this list is a second, in-process check so a coding error cannot reach a write
# tool.
ALLOWED_TOOLS = frozenset(
    {
        "describe_eks_resource",
        "list_eks_resources",
        "list_k8s_resources",
        "list_api_versions",
        # Required for container images and replica counts: list_k8s_resources
        # returns only name, namespace, labels, annotations and creation
        # timestamp. Read-only.
        "read_k8s_resource",
        # AWS's own upgrade-readiness assessment. Read-only. Needs two calls per
        # insight: the list response omits `category_specific_summary`, which only
        # the by-id detail call returns.
        "get_eks_insights",
    }
)

# Resource kinds this reader must never request, independent of what RBAC would
# permit. Helm release state lives in a Secret and the temptation to read it is
# real, so the refusal is enforced in code rather than left to permissions: an
# access policy can be widened by someone else, this cannot be widened by
# accident.
FORBIDDEN_KINDS = frozenset({"secret"})


class ToolNotPermittedError(RuntimeError):
    """Raised when code attempts a tool outside the read-only allowlist."""


class KindNotPermittedError(RuntimeError):
    """Raised when code attempts to read a resource kind that is never permitted."""


def _assumed_session(role_arn: str, region: str) -> Any:
    """A boto3 session for `role_arn`.

    Stage 1 assumes a per-cluster read-only role rather than reading under the
    Lambda's own execution role. That is what keeps the execution role incapable of
    reading a cluster it was not onboarded for: onboarding a cluster grants that
    cluster's role, and nothing else changes.
    """
    import boto3

    sts = boto3.client("sts", region_name=region)
    credentials = sts.assume_role(
        RoleArn=role_arn, RoleSessionName="eks-addon-readiness-scan"
    )["Credentials"]
    return boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


@contextlib.contextmanager
def cluster_reader(
    *, cluster_name: str, region: str, role_arn: str | None = None
) -> Iterator[McpClusterReader]:
    """An `McpClusterReader` for one cluster, inside the MCP client's context.

    When `role_arn` is given it is assumed first and the resulting credentials are
    used for both the MCP transport's SigV4 signing and the direct EKS control-plane
    calls, so both halves of Stage 1 read as the same least-privileged identity.
    """
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    from strands.tools.mcp import MCPClient

    session = _assumed_session(role_arn, region) if role_arn else None
    if session is not None:
        # `aws_iam_streamablehttp_client` signs from the ambient chain, so the
        # assumed credentials are published into the environment of this process
        # only. Nothing outside this invocation sees them.
        frozen = session.get_credentials().get_frozen_credentials()
        os.environ["AWS_ACCESS_KEY_ID"] = frozen.access_key
        os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
        os.environ["AWS_SESSION_TOKEN"] = frozen.token or ""

    client = MCPClient(
        lambda: aws_iam_streamablehttp_client(
            endpoint=MCP_ENDPOINT_TEMPLATE.format(region=region),
            aws_service="eks-mcp",
            aws_region=region,
        ),
        tool_filters={"allowed": sorted(ALLOWED_TOOLS)},
    )
    with client:
        yield McpClusterReader(client, cluster_name, region=region, session=session)


def parse_tool_result(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the server payload from an MCP tool result.

    Returns `(payload, None)` on success or `(None, message)` on failure. Never
    raises for a malformed or denied response, because a failed read is expected
    and must become a typed gap.
    """
    if not isinstance(raw, dict):
        return None, f"unexpected tool result type: {type(raw).__name__}"

    text = _first_text(raw)

    if raw.get("isError"):
        return None, _readable_error(text)

    if text is None:
        return None, "tool result contained no text block"

    payload: Any = None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        try:
            payload = ast.literal_eval(text)
        except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
            return None, f"could not parse tool payload: {type(exc).__name__}"

    if not isinstance(payload, dict):
        return None, f"tool payload was {type(payload).__name__}, expected object"

    # Failures are also reported inside an otherwise-successful envelope; this is
    # how Kubernetes RBAC denials arrive.
    inner = _first_text(payload)
    if inner and inner.lower().startswith("failed"):
        return None, inner

    return payload, None


def _readable_error(text: str | None) -> str:
    """Pull the human-readable message out of an error response."""
    if not text:
        return "tool reported an error with no detail"
    for parse in (json.loads, ast.literal_eval):
        try:
            inner = parse(text)  # type: ignore[operator]
        except Exception:
            continue
        if isinstance(inner, dict):
            message = _first_text(inner)
            if message:
                return message
    return text if len(text) <= 400 else f"{text[:400]}…"


def _first_text(payload: dict[str, Any]) -> str | None:
    blocks = payload.get("content")
    if not isinstance(blocks, list) or not blocks:
        return None
    first = blocks[0]
    if isinstance(first, dict):
        value = first.get("text")
        return value if isinstance(value, str) else None
    return None


class McpClusterReader:
    """Read-only cluster inventory over the EKS MCP Server.

    Must be used inside the MCP client's context manager, which `cluster_reader`
    handles.
    """

    def __init__(
        self,
        client: Any,
        cluster_name: str,
        *,
        region: str = "us-east-1",
        session: Any = None,
    ) -> None:
        self._client = client
        self._cluster_name = cluster_name
        self._region = region
        self._session = session
        self._call_seq = 0

    @property
    def cluster_name(self) -> str:
        return self._cluster_name

    def _call(
        self, tool: str, **arguments: Any
    ) -> tuple[dict[str, Any] | None, str | None]:
        if tool not in ALLOWED_TOOLS:
            raise ToolNotPermittedError(
                f"tool {tool!r} is not in the read-only allowlist {sorted(ALLOWED_TOOLS)}"
            )
        self._call_seq += 1
        try:
            raw = self._client.call_tool_sync(
                tool_use_id=f"scan-{self._call_seq}",
                name=tool,
                arguments=arguments,
            )
        except Exception as exc:
            return None, f"{tool} call failed: {type(exc).__name__}: {exc}"
        return parse_tool_result(raw)

    def describe_cluster(self) -> ReadResult:
        payload, error = self._call(
            "describe_eks_resource",
            cluster_name=self._cluster_name,
            resource_type="cluster",
        )
        if error:
            return None, error
        data = (payload or {}).get("resource_data")
        if not isinstance(data, dict):
            return None, "cluster description contained no resource_data"
        return data, None

    def list_addon_names(self) -> tuple[tuple[str, ...], str | None]:
        payload, error = self._call(
            "list_eks_resources",
            resource_type="addon",
            cluster_name=self._cluster_name,
        )
        if error:
            return (), error
        resources = (payload or {}).get("resources") or []
        names = [
            item["name"]
            for item in resources
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        return tuple(sorted(names)), None

    def describe_addon(self, addon_name: str) -> ReadResult:
        payload, error = self._call(
            "describe_eks_resource",
            cluster_name=self._cluster_name,
            resource_type="addon",
            resource_name=addon_name,
        )
        if error:
            return None, error
        data = (payload or {}).get("resource_data")
        if not isinstance(data, dict):
            return None, f"addon {addon_name} description contained no resource_data"
        return data, None

    def describe_addon_versions(self, addon_name: str) -> ListResult:
        """The published version catalogue for one add-on, via the EKS API.

        The EKS MCP Server exposes no `describe-addon-versions` tool, so this reads
        the EKS control plane directly under the same assumed credentials the MCP
        transport signs with. It is a global catalogue call — no cluster, no ARNs, no
        account IDs in the response — so nothing needs sanitising. A failure returns
        a typed error rather than raising, matching every other read on this reader.
        """
        try:
            if self._session is not None:
                client = self._session.client("eks", region_name=self._region)
            else:
                import boto3

                client = boto3.client("eks", region_name=self._region)
            versions: list[dict[str, Any]] = []
            paginator = client.get_paginator("describe_addon_versions")
            for page in paginator.paginate(addonName=addon_name):
                for addon in page.get("addons") or []:
                    for av in addon.get("addonVersions") or []:
                        if isinstance(av, dict):
                            versions.append(av)
        except Exception as exc:
            return ListResult(items=[], error=f"{type(exc).__name__}: {exc}")
        return ListResult(items=versions, error=None)

    def list_workloads(
        self,
        kind: str,
        api_version: str,
        *,
        namespace: str | None = None,
        label_selector: str | None = None,
    ) -> ListResult:
        if kind.casefold() in FORBIDDEN_KINDS:
            raise KindNotPermittedError(
                f"reading {kind!r} is never permitted by this reader; "
                "version evidence comes from chart provenance labels, not Secrets"
            )
        arguments: dict[str, Any] = {
            "cluster_name": self._cluster_name,
            "kind": kind,
            "api_version": api_version,
        }
        # Omitted rather than passed as None: the server validates its input
        # strictly and rejects unknown or null-valued arguments.
        if namespace:
            arguments["namespace"] = namespace
        if label_selector:
            arguments["label_selector"] = label_selector
        payload, error = self._call("list_k8s_resources", **arguments)
        if error:
            return ListResult(items=[], error=error)
        items = (payload or {}).get("items") or []
        enriched: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            record = dict(item)
            record["_kind"] = kind
            enriched.append(record)
        return ListResult(
            items=enriched,
            error=None,
            truncated=len(enriched) >= LIST_RESULT_CAP,
        )

    def read_workload_detail(
        self, kind: str, api_version: str, name: str, namespace: str | None
    ) -> ReadResult:
        """One workload's full object, for container images and replicas."""
        if kind.casefold() in FORBIDDEN_KINDS:
            raise KindNotPermittedError(
                f"reading {kind!r} is never permitted by this reader"
            )
        arguments: dict[str, Any] = {
            "cluster_name": self._cluster_name,
            "kind": kind,
            "api_version": api_version,
            "name": name,
        }
        if namespace:
            arguments["namespace"] = namespace
        payload, error = self._call("read_k8s_resource", **arguments)
        if error:
            return None, error
        resource = (payload or {}).get("resource")
        if not isinstance(resource, dict):
            return None, f"{kind}/{name} response contained no resource object"
        return resource, None

    def list_api_versions(self) -> tuple[tuple[str, ...], str | None]:
        payload, error = self._call(
            "list_api_versions", cluster_name=self._cluster_name
        )
        if error:
            return (), error
        for key in ("api_versions", "apiVersions", "versions", "items"):
            value = (payload or {}).get(key)
            if isinstance(value, list):
                versions = [v for v in value if isinstance(v, str)]
                return tuple(sorted(versions)), None
        return (), "api version list not found in tool payload"

    def list_insights(self) -> tuple[list[dict[str, Any]], str | None]:
        """AWS's upgrade-readiness insights, summary form.

        No `category` filter is passed. The categories AWS defines are its own to
        change, and filtering to the ones documented today would silently drop a new
        one — the same failure mode as an unattempted read.
        """
        payload, error = self._call(
            "get_eks_insights", cluster_name=self._cluster_name
        )
        if error:
            return [], error
        items = (payload or {}).get("insights")
        if not isinstance(items, list):
            return [], "insight list not found in tool payload"
        return [i for i in items if isinstance(i, dict)], None

    def read_insight(self, insight_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """One insight in full. Only this form carries `category_specific_summary`."""
        payload, error = self._call(
            "get_eks_insights",
            cluster_name=self._cluster_name,
            insight_id=insight_id,
        )
        if error:
            return None, error
        # The by-id response still nests under `insights`, as a single-element list.
        items = (payload or {}).get("insights")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    return item, None
            return None, "insight detail payload contained no record"
        return payload, None
