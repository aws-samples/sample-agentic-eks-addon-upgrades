"""Reading and writing stage documents in S3.

Every stage reads its input document(s) from S3 and writes its output document to
S3. That is the only inter-stage channel: no stage passes a Python object to
another, and no stage holds state between invocations. A document on S3 is the
handoff, and its digest is how the next stage proves it read the right one.

Documents are written with `ContentType: application/json` and SSE. Reads are
validated back into their contract type, so a truncated or tampered object fails
at the boundary rather than halfway through a stage.
"""

from __future__ import annotations

import json
import re
from functools import cache
from typing import Any, TypeVar

from ..models import ContractModel

_S3_URI = re.compile(r"^s3://(?P<bucket>[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9])/(?P<key>.+)$")

M = TypeVar("M", bound=ContractModel)


class DocumentUriError(ValueError):
    """A destination was not a well-formed `s3://bucket/key` URI."""


@cache
def _client() -> Any:
    # Imported lazily and cached: one client per Lambda container, created on first
    # use rather than at import time so a cold start pays for it only if it reads.
    import boto3

    return boto3.client("s3")


def parse_uri(uri: str) -> tuple[str, str]:
    """Split an `s3://bucket/key` URI, or raise.

    >>> parse_uri("s3://my-bucket/snapshots/a.json")
    ('my-bucket', 'snapshots/a.json')
    """
    match = _S3_URI.match(uri.strip())
    if not match:
        raise DocumentUriError(
            f"expected an s3://bucket/key URI, got {uri!r}. Stage documents are "
            "exchanged only through object storage."
        )
    return match.group("bucket"), match.group("key")


def build_uri(bucket: str, key: str) -> str:
    """Compose an `s3://` URI.

    >>> build_uri("b", "k/1.json")
    's3://b/k/1.json'
    """
    return f"s3://{bucket}/{key}"


def write_document(uri: str, document: ContractModel) -> str:
    """Write a contract's canonical JSON to S3. Returns the URI it landed at.

    Canonical JSON, not `model_dump_json`: sorted keys and no insignificant
    whitespace, so the bytes are a function of the content alone and the object's
    digest is stable across runs and across Python versions.
    """
    bucket, key = parse_uri(uri)
    _client().put_object(
        Bucket=bucket,
        Key=key,
        Body=document.canonical_json().encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    return uri


def write_text(uri: str, body: str, content_type: str) -> str:
    """Write a rendered view to S3. Returns the URI it landed at.

    Separate from `write_document` because a rendering is not a document: it
    carries no digest of its own and cannot be validated back into a contract on
    read. What makes it trustworthy is that its digest is recorded *inside* the
    document it was rendered from, so a reader can re-render and compare rather
    than take the bytes on faith.

    Same encryption and the same explicit content type, so the report is served
    correctly straight out of the bucket.
    """
    bucket, key = parse_uri(uri)
    _client().put_object(
        Bucket=bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )
    return uri


def read_text(uri: str) -> str:
    """Read a rendered view back, for verifying its digest against the document."""
    bucket, key = parse_uri(uri)
    return _client().get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")


def read_document(uri: str, contract: type[M]) -> M:
    """Read and validate one stage document from S3.

    Validation is not optional. `ContractModel` forbids unknown fields, so an object
    that is not the document this stage expected fails here, naming the field, rather
    than producing a plausible-looking result from partial data.
    """
    bucket, key = parse_uri(uri)
    body = _client().get_object(Bucket=bucket, Key=key)["Body"].read()
    return contract.model_validate_json(body.decode("utf-8"))


def read_json(uri: str) -> Any:
    """Read raw JSON from S3, for payloads that are not contract documents."""
    bucket, key = parse_uri(uri)
    return json.loads(_client().get_object(Bucket=bucket, Key=key)["Body"].read())


def list_uris(prefix_uri: str) -> list[str]:
    """Every object under an `s3://bucket/prefix/` as full URIs, sorted.

    The one current caller is the upgrade report, which has to find the per-pod log files in a
    diagnosis bundle and cannot know their names in advance. Paginated, because a bundle with
    one log per container per stream can pass a thousand keys on a large DaemonSet.
    """
    bucket, prefix = parse_uri(prefix_uri)
    found: list[str] = []
    paginator = _client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for entry in page.get("Contents") or []:
            key = entry.get("Key")
            if isinstance(key, str):
                found.append(build_uri(bucket, key))
    return sorted(found)


def write_json(uri: str, payload: Any) -> str:
    """Write raw JSON to S3, for payloads that are not contract documents.

    The one current caller is Stage 6's evidence capture: the fetched page bodies a
    judgement rests on. Those are foreign bytes rather than a contract this system defines,
    so there is no `ContractModel` to write them through — but they still deserve the same
    canonical encoding, the same encryption and the same key discipline as every other
    artifact, which is why this lives here rather than in the stage.
    """
    bucket, key = parse_uri(uri)
    _client().put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )
    return uri
