"""Checks over the model loop, the API-level schema and the sweep handler.

Separate from `stage6_checks.py` because these exercise the *model side* -- the loop, the
request it builds, the tools it exposes -- while that file exercises the deterministic side.
The split is the package's own boundary, and keeping the checks split proves the boundary
holds: this file needs a fake client, that one needs nothing at all.

The client here is a double, not a mock library: it records the request it was handed and
replays a scripted sequence of responses. What is being checked is the *request* -- that no
`temperature` is sent, that thinking is adaptive, that effort is low, that the schema
carries a required kind enum, that every tool is strict -- and the *loop*, that a fetch is
recorded before its bytes reach the judging layer.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eks_addon_readiness.research import extract as extract_mod  # noqa: E402
from eks_addon_readiness.research.schema import (  # noqa: E402
    extraction_schema,
    output_config,
    strict_tool,
    thinking_config,
)
from eks_addon_readiness.research.sources.recording import RecordingReader  # noqa: E402
from eks_addon_readiness.research.sources.web_docs import (  # noqa: E402
    HttpSourceReader,
    SourceScope,
)

FIXTURES = ROOT / "tests" / "fixtures" / "sources"
MS_URL = (
    "https://raw.githubusercontent.com/kubernetes-sigs/metrics-server/"
    "4062beeed6ef996ba3ff7164967edafd3470e2e4/README.md"
)
MS_REV = "4062beeed6ef996ba3ff7164967edafd3470e2e4"
MS_REPO = "https://github.com/kubernetes-sigs/metrics-server"

_results: list[tuple[bool, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    _results.append((bool(condition), name, detail))
    print(
        f"  {'ok  ' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else "")
    )


class _Block:
    """One content block, shaped like the SDK's."""

    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Response:
    def __init__(self, content, parsed_output=None) -> None:
        self.content = content
        self.parsed_output = parsed_output


class _Messages:
    """Stands in for `client.messages`, exposing the method the loop actually calls.

    Both `parse` and `create` are defined, because the loop chooses between them: `parse`
    when a schema is on the request, `create` when the endpoint will not take one. Both
    record the request, which is what these checks assert against.
    """

    def __init__(self, script) -> None:
        self._script = list(script)
        self.requests: list[dict] = []

    def _record(self, kwargs):
        self.requests.append(kwargs)
        if not self._script:
            raise AssertionError("the loop made more calls than the script provides")
        return self._script.pop(0)

    def parse(self, **kwargs):
        return self._record(kwargs)

    def create(self, **kwargs):
        return self._record(kwargs)


class _FakeClient:
    def __init__(self, script) -> None:
        self.messages = _Messages(script)


class _StubReader:
    """A source reader that serves the committed fixture and nothing else.

    Not a network stub for convenience: it makes the loop check hermetic while still
    exercising the real `RecordingReader`, so the recording behaviour under test is the
    production one.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fetch(self, url: str, revision: str = ""):
        from eks_addon_readiness.research.sources.ports import FetchResult

        self.calls.append((url, revision))
        if url == MS_URL and revision == MS_REV:
            return FetchResult(
                url=url,
                revision=revision,
                body=(FIXTURES / "metrics-server-README.md").read_text(encoding="utf-8"),
                status=200,
            )
        return FetchResult(
            url=url, revision=revision, body="", status=0, refused=True, reason="out of scope"
        )


def lane_request_shape() -> None:
    print("\n===== the request: no temperature, adaptive thinking, low effort =====")
    cfg = output_config()
    check("effort is low, not the default high", cfg["effort"] == "low")
    check(
        "structured output format is OFF by default, because Bedrock returns 400 for it",
        "format" not in cfg,
        "400 output_config.format: Extra inputs are not permitted -- probed live",
    )
    check(
        "and it can still be enabled for an endpoint that supports it",
        output_config(structured=True)["format"]["schema"]["$defs"]["BoundKind"]["enum"],
        "the schema generator is shared, so prompt and contract cannot drift",
    )
    check("thinking is adaptive", thinking_config() == {"type": "adaptive"})
    check(
        "the structured format, when enabled, is a json_schema",
        output_config(structured=True)["format"]["type"] == "json_schema",
    )

    schema = extraction_schema()
    stmt = schema["$defs"]["CompatibilityStatementInput"]
    # The guarantee that matters holds at EVERY endpoint, because it is the contract, not
    # the API: there is no field able to express an exclusive maximum, and bounds.py refuses
    # to derive one from a tested range.
    from eks_addon_readiness.research.boundary import CompatibilityStatementInput as CSI

    check(
        "the CONTRACT has no field for an exclusive maximum, whatever the endpoint accepts",
        not any("maximum_exclusive" in f for f in CSI.model_fields),
        f"contract fields: {sorted(CSI.model_fields)}",
    )
    check(
        "the contract forbids unknown fields, so it is the wall behind the API schema",
        CSI.model_config.get("extra") == "forbid",
    )
    check(
        "the prompt carries the same schema when the API will not take it",
        '"BoundKind"' in __import__(
            "eks_addon_readiness.research.schema", fromlist=["x"]
        ).schema_prompt_block(),
    )
    check(
        "the kind is a REQUIRED enum the model must choose from",
        # 7 kinds: SUPPORT_POLICY is one of them, so a relative N-2 rule is its own
        # typed claim instead of a mis-filed range.
        "kind" in stmt["required"] and len(schema["$defs"]["BoundKind"]["enum"]) == 7,
        f"enum={schema['$defs']['BoundKind']['enum']}",
    )
    check(
        "no field can express an exclusive maximum, so arithmetic is not expressible",
        not any("maximum_exclusive" in p for p in stmt["properties"]),
        f"statement fields={sorted(stmt['properties'])}",
    )
    check(
        "no field can assert confidence or matrix authority",
        not any(
            token in p
            for p in stmt["properties"]
            for token in ("confidence", "official_matrix", "authority")
        ),
    )
    # additionalProperties: false everywhere, checked by walking rather than by spot check.
    opens: list[str] = []

    def walk(node, path="$"):
        if isinstance(node, dict):
            if (
                node.get("type") == "object" or "properties" in node
            ) and node.get("additionalProperties") is not False:
                opens.append(path)
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(schema)
    check(
        "every object in the schema is closed",
        not opens,
        f"open at {opens[:3]}" if opens else "additionalProperties: false throughout",
    )

    tool = strict_tool("t", "d", {"a": {"type": "string"}}, ("a",), strict=True)
    check(
        "when enabled, strict is a top-level field on the tool object, not on tool_choice",
        tool.get("strict") is True and "strict" not in str(tool.get("input_schema", {})),
    )
    check(
        "a tool schema is closed and lists required, strict or not",
        tool["input_schema"]["additionalProperties"] is False
        and tool["input_schema"]["required"] == ["a"],
    )
    check(
        "strict is OFF by default, because Bedrock returns 400 for it",
        "strict" not in strict_tool("t", "d", {"a": {"type": "string"}}, ("a",)),
        "400 tools.0.custom.strict: Extra inputs are not permitted -- found by a live run",
    )
    for t in extract_mod._tools():
        check(
            f"tool {t['name']!r} has a closed schema that lists required",
            t["input_schema"]["additionalProperties"] is False
            and bool(t["input_schema"]["required"]),
        )

    check(
        "the model id is a parameter, defaulting to a Bedrock inference profile",
        extract_mod.DEFAULT_MODEL_ID.endswith("anthropic.claude-sonnet-5")
        and extract_mod.DEFAULT_MODEL_ID.startswith("us."),
        f"{extract_mod.DEFAULT_MODEL_ID} -- the bare model id is INFERENCE_PROFILE-only "
        "on Bedrock and returns 400",
    )
    # An earlier version of the loop docstring claimed determinism from `temperature=0` and both
    # halves were false: that model rejected the parameter, and no signal measured re-extraction
    # stability. The invariant here is about the CODE, not about whether the word appears --
    # this module's docstring quotes that old claim in order to name it as false, so a text
    # search for the word finds the disclaimer and reports it as the offence. So: no source
    # line may SET temperature, and the docstring must state where reproducibility does come
    # from.
    import ast

    source = Path(extract_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    sets_temperature = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.keyword) and node.arg == "temperature"
    ] + [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value == "temperature"
    ]
    check(
        "no code path sets temperature (this model removed it and returns 400)",
        not sets_temperature,
        f"set at lines {sets_temperature}" if sets_temperature else "",
    )
    check(
        "the docstring states where reproducibility actually comes from",
        "Reproducibility does not come from sampling" in (extract_mod.__doc__ or ""),
        "so no reader inherits that old false claim",
    )


def lane_loop_records_before_judging() -> None:
    print("\n===== the loop: bytes are recorded before the judging layer sees them =====")
    statement = {
        "version_spec": "0.6.x",
        "kind": "PUBLISHED_MINIMUM",
        "stated_minimum": "1.25",
        "quote": "0.6.x          | `metrics.k8s.io/v1beta1`  | 1.25+",
        "source": {"url": MS_URL, "revision": MS_REV},
    }
    higher = {
        "version_spec": "0.9.x",
        "kind": "PUBLISHED_MINIMUM",
        "stated_minimum": "1.34",
        "quote": "0.9.x          | `metrics.k8s.io/v1beta1`  | 1.34+",
        "source": {"url": MS_URL, "revision": MS_REV},
    }
    parsed = {
        "found": True,
        "component_id": "metrics-server",
        "version_series": "0.6.x",
        "canonical_repository": MS_REPO,
        "official_documentation": [MS_REPO],
        "statements": [statement, higher],
        "releases": [
            {
                "version": "0.6.4",
                "quote": "0.6.x          | `metrics.k8s.io/v1beta1`  | 1.25+",
                "source": {"url": MS_URL, "revision": MS_REV},
            }
        ],
    }
    script = [
        # Turn 1: the model fetches.
        _Response(
            [
                _Block(
                    type="tool_use",
                    id="tu_1",
                    name="fetch_source",
                    input={"url": MS_URL, "revision": MS_REV},
                )
            ]
        ),
        # Turn 2: the model returns the structured extraction. `parsed_output` sits on a
        # TEXT BLOCK, which is where `messages.parse()` puts it -- not on the message.
        _Response([_Block(type="text", text="", parsed_output=parsed)]),
    ]
    client = _FakeClient(script)
    stub = _StubReader()

    original = extract_mod.HttpSourceReader
    extract_mod.HttpSourceReader = lambda _scope: stub
    try:
        candidate, pages, _extraction = extract_mod.research_component(
            "metrics-server", "0.6.x", None, client=client
        )
    finally:
        extract_mod.HttpSourceReader = original

    check("the loop fetched the source", stub.calls == [(MS_URL, MS_REV)], f"{stub.calls}")
    check(
        "the fetched body was recorded, so the judging layer had bytes to check against",
        len(pages) == 1 and pages.get(MS_URL, MS_REV) is not None,
        f"{len(pages)} page(s), digest {pages.digest()[:19]}",
    )
    check(
        "the candidate was judged from those recorded bytes",
        candidate.receipt.evidence_digest == pages.digest(),
    )
    check(
        "the quotes verified against the fetched page",
        candidate.knowledge is not None,
        candidate.receipt.summary[:60],
    )
    if candidate.knowledge is not None:
        windows = {
            r.component_version_series: (
                r.kubernetes_versions.minimum_inclusive,
                r.kubernetes_versions.maximum_exclusive,
            )
            for r in candidate.knowledge.compatibility_rules
        }
        check(
            "the published minimum was bounded by the next floor up in the same table",
            windows.get("0.6.x") == ("1.25", "1.34"),
            f"{windows}",
        )

    request = client.messages.requests[0]
    check(
        "no temperature was sent (this model removed it and returns 400)",
        "temperature" not in request,
        f"request keys: {sorted(request)}",
    )
    check(
        "no API-level citations were sent (incompatible with structured outputs)",
        "citations" not in request,
    )
    check("thinking was adaptive", request["thinking"] == {"type": "adaptive"})
    check("effort was low", request["output_config"]["effort"] == "low")
    check(
        "every tool in the request has a closed input schema",
        all(
            t["input_schema"]["additionalProperties"] is False for t in request["tools"]
        ),
        "strict itself is endpoint-conditional; the closed schema is not",
    )


def lane_tool_input_parsing() -> None:
    print("\n===== tool inputs are parsed as JSON, never string-matched =====")
    parse = extract_mod._parse_tool_input
    check("a dict input passes through", parse({"url": "u"}) == {"url": "u"})
    check(
        "a JSON string input is parsed, escapes and all",
        parse('{"url": "https://x.test/a\\u0026b"}')["url"] == "https://x.test/a&b",
        "a substring search would have found the escape, not the character",
    )
    for bad, label in (("not json", "non-JSON text"), ("[1,2]", "a JSON array"), (7, "an int")):
        try:
            parse(bad)
            check(f"{label} is rejected", False, "it was accepted")
        except (ValueError, TypeError):
            check(f"{label} is rejected rather than salvaged", True)


def lane_scope_refusals() -> None:
    print("\n===== scope: only the component's own hosts, and refusals are recorded =====")
    scope = SourceScope()
    check(
        "an arbitrary host does not widen the scope",
        not scope.permit_repository("https://evil.test/o/r"),
    )
    check(
        "a GitHub repository does",
        scope.permit_repository(MS_REPO),
    )
    check("its raw host is then in scope", scope.permits(MS_URL))
    check(
        "a third-party documentation host is not in scope",
        not scope.permits("https://docs.aws.amazon.com/eks/latest/userguide/x.md"),
    )
    check(
        "a lookalike host is not in scope",
        not scope.permits("https://raw.githubusercontent.com.evil.test/o/r/x/README.md"),
    )

    reader = RecordingReader(HttpSourceReader(SourceScope()))
    result = reader.fetch("https://evil.test/page.md", "r1")
    check("an out-of-scope fetch is refused", result.refused)
    check("a refused fetch records no page", len(reader.pages) == 0)
    check(
        "but the refusal itself is retained, so provenance is answerable afterwards",
        len(reader.refusals) == 1,
        reader.summary(),
    )
    plain = reader.fetch("http://example.test/x.md", "r1")
    check("a plaintext URL is refused", plain.refused, plain.reason[:50])


def lane_sweep_handler() -> None:
    print("\n===== the sweep handler: both cadences, and revocation =====")
    import json
    import os
    import tempfile

    from eks_addon_readiness.stages import freshness as sweep_handler

    with tempfile.TemporaryDirectory() as tmp:
        store_root = Path(tmp) / "components"
        marks_path = Path(tmp) / "marks.json"
        stale_dir = store_root / "stale-one"
        stale_dir.mkdir(parents=True)
        old = (datetime.now(UTC) - timedelta(hours=40)).isoformat()
        citation = {
            "url": "https://raw.githubusercontent.com/example/repo/main/x.md",
            "title": "t",
            "source_revision": "main",
            "retrieved_at": "2026-09-01T00:00:00Z",
        }
        (stale_dir / "entry.json").write_text(
            json.dumps(
                {
                    "knowledge_base_revision": "stale-one-r1",
                    "canonical_id": "stale-one",
                    "canonical_repository": "https://github.com/example/repo",
                    "official_documentation": ["https://github.com/example/repo"],
                    "compatibility_rules": [
                        {
                            "component_version_series": "1.0.x",
                            "kubernetes_versions": {
                                "minimum_inclusive": "1.30",
                                "maximum_exclusive": "1.40",
                            },
                            "evidence": [citation],
                        }
                    ],
                    "releases": [
                        {
                            "version": "1.0.0",
                            "released_at": "2020-01-01T00:00:00Z",
                            "source_revision": "main",
                            "evidence": [citation],
                        }
                    ],
                    "verification_checks": [
                        {
                            "check_id": "serves-its-api",
                            "description": "d",
                            "evidence": [citation],
                        }
                    ],
                    "default_upgrade_strategy": "IN_PLACE",
                    "refreshed_at": old,
                    "evidence_confidence": "HIGH",
                }
            )
        )

        previous = {
            k: os.environ.get(k) for k in ("FACT_MARK_PATH", "FACT_STORE_TABLE")
        }
        os.environ["FACT_MARK_PATH"] = str(marks_path)
        os.environ.pop("FACT_STORE_TABLE", None)
        # The store root is resolved by the frozen FileFactStore from its own location, so
        # the fixture store is injected by pointing the builder at it.
        from eks_addon_readiness.facts import FileFactStore
        from eks_addon_readiness.storage import facts as facts_mod

        original_builder = facts_mod.build_raw_fact_store
        facts_mod.build_raw_fact_store = lambda: FileFactStore(store_root)
        sweep_handler.build_raw_fact_store = facts_mod.build_raw_fact_store
        try:
            # `pinned_releases` freezes what upstream reports, which is what makes a
            # handler test deterministic and offline -- and is the same mechanism a caller
            # uses to reproduce a past sweep exactly. The fixture fact was read 40 hours ago
            # on github.com/example/repo, so a release published AFTER that read is the
            # component having shipped something the fact never saw.
            released_after = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
            released_before = (datetime.now(UTC) - timedelta(days=90)).isoformat()
            drift_event = {
                "pinned_releases": {
                    "example/repo": {"tag": "v1.1.0", "published_at": released_after}
                }
            }
            scheduled = sweep_handler.handler(drift_event)
            check(
                "a scheduled sweep visits every fact and marks the one whose component released",
                scheduled["mode"] == "scheduled" and scheduled["expired"] == ["stale-one"],
                scheduled["summary"],
            )
            check(
                "the report names an upstream release as the mechanism, with no time window",
                scheduled["staleness"] == "upstream_release" and "ttl_hours" not in scheduled,
                f"staleness={scheduled['staleness']}",
            )
            check(
                "the mark was persisted, so the next stage read sees it",
                marks_path.is_file() and "STALE" in marks_path.read_text(),
            )
            check(
                "the sweep reports that the store digest moves",
                scheduled["store_digest_moves"] is True,
            )
            again = sweep_handler.handler(drift_event)
            check(
                "a second sweep is idempotent",
                again["expired"] == [] and again["store_digest_moves"] is False,
            )
            unchanged = sweep_handler.handler(
                {
                    "pinned_releases": {
                        "example/repo": {"tag": "v1.0.0", "published_at": released_before}
                    }
                }
            )
            check(
                "a QUIET repository marks nothing, however old the fact is",
                unchanged["expired"] == [],
                "the fixture fact is 40h old and still fresh, because nothing was released",
            )
            preflight = sweep_handler.handler(
                {"components": ["some-other-component"], **drift_event}
            )
            check(
                "a pre-flight sweep visits only the named components",
                preflight["mode"] == "pre-flight" and preflight["visited"] == [],
                preflight["summary"],
            )
            revoked = sweep_handler.handler(
                {
                    "revoke": {
                        "component_id": "stale-one",
                        "reason": "produced by the pre-fix bounds logic",
                        "actor": "stage6-remediation",
                        "consumed_by": ["s3://b/decisions/x.json"],
                    }
                }
            )
            check(
                "a revocation records actor, reason and blast radius",
                revoked["actor"] == "stage6-remediation"
                and revoked["consumed_by"] == ["s3://b/decisions/x.json"]
                and revoked["fact_revision"] == "stale-one-r1",
                revoked["effect"][:60],
            )
            try:
                sweep_handler.handler(
                    {"revoke": {"component_id": "stale-one", "reason": "x"}}
                )
                check("a revocation without an actor is refused", False, "it was accepted")
            except ValueError as exc:
                check(
                    "a revocation without an actor is refused",
                    "accountable author" in str(exc),
                )
        finally:
            facts_mod.build_raw_fact_store = original_builder
            sweep_handler.build_raw_fact_store = original_builder
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def main() -> int:
    print("LOOP AND SCHEMA CHECKS")
    print(f"date: {datetime.now(UTC).isoformat()}")
    print(f"python: {sys.version.split()[0]}")
    print(f"model default: {extract_mod.DEFAULT_MODEL_ID}  effort: {extract_mod.DEFAULT_EFFORT}")

    lane_request_shape()
    lane_loop_records_before_judging()
    lane_tool_input_parsing()
    lane_scope_refusals()
    lane_sweep_handler()

    passed = sum(1 for ok, _, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\nLOOP AND SCHEMA CHECKS: {passed} passed, {failed} failed")
    if failed:
        print("FAILURES:")
        for ok, name, detail in _results:
            if not ok:
                print(f"  - {name}  {detail}")
    print(f"LOOP AND SCHEMA CHECKS EXIT={1 if failed else 0}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
