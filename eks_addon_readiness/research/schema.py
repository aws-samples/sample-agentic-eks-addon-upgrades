"""The extraction JSON Schema, and the strict tool schemas, as API-level enforcement.

A prompt instruction asks a model not to conflate a tested window with a supported
ceiling. A schema with **no field capable of expressing a conflated answer** makes it
impossible. This module builds the second thing.

That is the same shape-not-trust argument behind the extraction contract -- *a model that
tries to assert 'this is HIGH confidence' has nowhere to put it* -- an argument a model-set
`from_official_matrix` boolean that alone bought HIGH confidence would undermine. The lesson
taken here is that the argument only holds while every field is checked against it, so this
module is generated **from** the
pydantic contract in `boundary.py` rather than written beside it. A field added there
appears here; a field that should not exist cannot be added here without appearing there
first, where the contract test would see it.

Three properties this schema states -- enforced by the API where the endpoint accepts a
schema on the request, and by the pydantic contract behind it where it does not. See
`STRUCTURED_OUTPUT` for which endpoint does which, and for why the guarantee that matters
does not depend on the answer:

**The kind is a required enum.** `BoundKind` becomes `"enum": [...]` with `kind` in
`required`. The model must choose one of six named kinds. There is no free-text bound
field, no "maximum" it can fill in with arithmetic, and no way to return a window without
saying what kind of window it is.

**`additionalProperties: false` everywhere.** A model cannot smuggle a field the
deterministic layer does not read -- which is how an unchecked value reaches a store.

**Tool input schemas are closed too.** Every tool declares `additionalProperties: false`
and lists `required`, so a tool cannot be called with a field nobody reads. `strict: true`
-- which makes the API itself guarantee that -- is included only where the endpoint accepts
it: Bedrock returns `400 tools.0.custom.strict: Extra inputs are not permitted`, so it is
off by default there and `READINESS_TOOL_STRICT=1` enables it for a first-party endpoint.
See `TOOL_STRICT`.

Deliberately not used: API-level document citations (`citations: {enabled: true}`). They
are incompatible with structured outputs and return 400 if combined, and they would be the
weaker mechanism anyway -- a citation the API attaches still describes what the model was
looking at, while `verify.py` checks the quote against bytes this process fetched and
hashed.
"""

from __future__ import annotations

import os
from typing import Any

from .boundary import ResearchExtraction

# Fields the model must always supply. Everything else has a defensible default, and a
# default is better than a required field the model fills with a guess to satisfy a
# schema.
_REQUIRED_TOP_LEVEL = ("found", "component_id", "version_series")

# Per-object required lists. `kind` is here for the reason this module exists: a statement
# that does not say which kind it is cannot be returned at all.
_REQUIRED_BY_OBJECT: dict[str, tuple[str, ...]] = {
    "SourceRef": ("url", "revision"),
    "ExtractedClaimInput": ("field", "value", "source", "quote"),
    "CompatibilityStatementInput": ("version_spec", "kind", "quote", "source"),
    "ReleaseSightingInput": ("version", "quote", "source"),
    "ChartProvenanceInput": ("repository_url", "source", "quote"),
}


def _tighten(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively close every object and set `required` from the tables above.

    Pydantic's generated schema permits omissions the API's strict mode does not, so this
    walks the tree once: every object gets `additionalProperties: false`, and every named
    definition gets the required list its entry in `_REQUIRED_BY_OBJECT` declares.
    """
    if not isinstance(schema, dict):
        return schema

    for defs_key in ("$defs", "definitions", "properties"):
        block = schema.get(defs_key)
        if isinstance(block, dict):
            for name, sub in block.items():
                tightened = _tighten(sub)
                if defs_key in ("$defs", "definitions") and name in _REQUIRED_BY_OBJECT:
                    tightened["required"] = list(_REQUIRED_BY_OBJECT[name])
                block[name] = tightened

    for key in ("items", "additionalItems", "not"):
        if isinstance(schema.get(key), dict):
            schema[key] = _tighten(schema[key])

    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(schema.get(key), list):
            schema[key] = [_tighten(s) for s in schema[key]]

    if schema.get("type") == "object" or "properties" in schema:
        schema["additionalProperties"] = False

    return schema


def extraction_schema() -> dict[str, Any]:
    """The JSON Schema for one extraction, closed and with the kind enum required.

    Generated from `ResearchExtraction`, so the API-level contract and the parsed contract
    cannot drift. The doctest is the guarantee, not a demonstration:

    >>> s = extraction_schema()
    >>> s["additionalProperties"]
    False
    >>> stmt = s["$defs"]["CompatibilityStatementInput"]
    >>> "kind" in stmt["required"]
    True
    >>> sorted(stmt["properties"]["kind"]["$ref"].split("/")[-1:])
    ['BoundKind']
    >>> len(s["$defs"]["BoundKind"]["enum"])
    7
    >>> any("maximum_exclusive" in p for p in stmt["properties"])
    False
    """
    schema = ResearchExtraction.model_json_schema()
    schema = _tighten(schema)
    schema["required"] = list(_REQUIRED_TOP_LEVEL)
    return schema


# Whether `output_config` carries the structured-output `format`.
#
# **Off by default, because the Bedrock endpoint rejects it.** Probed directly against
# `us.anthropic.claude-sonnet-5` on Bedrock:
#
#     thinking={"type": "adaptive"}          accepted
#     output_config={"effort": "low"}        accepted
#     tools with a closed input_schema       accepted
#     output_config={"format": {...}}        400 output_config.format: Extra inputs
#                                                are not permitted
#     tools[].strict = true                  400 tools.0.custom.strict: Extra inputs
#                                                are not permitted
#
# So API-level structured outputs are unavailable on the endpoint this deployment targets.
# `READINESS_STRUCTURED_OUTPUT=1` turns it on for a first-party endpoint that supports it.
#
# WHAT THIS DOES NOT COST, stated precisely because it is the important part. The guarantee
# that a tested window cannot become a supported ceiling never depended on the API. It rests
# on two things that hold at every endpoint: the extraction contract has NO field capable of
# expressing an exclusive maximum, and `bounds.py` refuses to derive a support ceiling from a
# TESTED_RANGE. The API schema was a second wall in front of those, not the wall itself.
#
# What replaces it, one layer in: the same schema is emitted in the prompt, and the response
# is validated against the pydantic contract, which is `extra="forbid"` -- so an extra field
# is rejected and a missing `kind` is rejected, by the contract instead of by the API. A
# validation failure is fed back and retried a bounded number of times, so a near-miss
# recovers rather than becoming a gap.
STRUCTURED_OUTPUT = os.environ.get(
    "READINESS_STRUCTURED_OUTPUT", ""
).strip() in ("1", "true", "yes")


def output_config(effort: str = "low", *, structured: bool | None = None) -> dict[str, Any]:
    """The `output_config` for the extraction call: effort, and `format` where supported.

    `format` is the current parameter; the older top-level `output_format` is deprecated.
    It is included only when enabled -- see `STRUCTURED_OUTPUT`.

    `effort` defaults to **low**, and that is a considered choice rather than a cost one.
    This task is faithful transcription -- copy the numbers a table states and name what
    kind of statement it is. Over-reasoning is the failure mode that produced the defect
    being fixed: the model that turned a tested window into a supported ceiling did so by
    reasoning past what the page said, with perfect arithmetic. Reasoning effort spent here
    buys inference nobody asked for. It also consolidates tool calls, which shortens the
    loop.

    >>> output_config()["effort"]
    'low'
    >>> "format" in output_config()
    False
    >>> output_config(structured=True)["format"]["type"]
    'json_schema'
    """
    config: dict[str, Any] = {"effort": effort}
    if structured if structured is not None else STRUCTURED_OUTPUT:
        config["format"] = {
            "type": "json_schema",
            "name": "research_extraction",
            "schema": extraction_schema(),
        }
    return config


def schema_prompt_block() -> str:
    """The extraction schema, rendered for the prompt.

    The fallback for an endpoint that will not take a schema on the request. Weaker than the
    API enforcing it -- a prompt is advice -- which is why the contract validation behind it
    is what actually rejects a malformed extraction. Generated from the same schema, so the
    prompt and the contract cannot drift.
    """
    import json

    return json.dumps(extraction_schema(), indent=2, sort_keys=True)


def thinking_config() -> dict[str, Any]:
    """Adaptive thinking. Paired with low effort by `output_config`.

    >>> thinking_config()
    {'type': 'adaptive'}
    """
    return {"type": "adaptive"}


# Whether tool definitions carry `strict: true`.
#
# **Off by default, because the Bedrock endpoint rejects it.** `strict` is a top-level field
# on the tool object beside `name`, `description` and `input_schema` -- not on `tool_choice`
# -- and on the first-party API it guarantees tool inputs validate exactly. Bedrock does not
# accept it, and a live invocation returns
#
#   400 tools.0.custom.strict: Extra inputs are not permitted
#
# Only a deployed run finds this; the local checks drive the loop with a double. Since the
# deployment target is Bedrock, the default is off, and `READINESS_TOOL_STRICT=1` turns it on
# for a first-party endpoint.
#
# What is NOT lost: the schema still sets `additionalProperties: false` and lists `required`,
# so a tool input carrying an unexpected field is still rejected -- by the handler's own
# parse rather than by the API. And the enforcement that actually matters for the defect this
# stage was rebuilt around lives in `output_config.format`, not here: the required `kind`
# enum is on the extraction schema, which Bedrock DOES accept. Tool strictness only guards
# the two fetch tools' inputs.
TOOL_STRICT = os.environ.get("READINESS_TOOL_STRICT", "").strip() in ("1", "true", "yes")


def strict_tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: tuple[str, ...],
    *,
    strict: bool | None = None,
) -> dict[str, Any]:
    """One tool definition with a closed input schema, and `strict` where the endpoint takes it.

    The schema is always closed and always lists `required`, so a tool can never be declared
    strict-but-open. `strict` itself is included only when enabled -- see `TOOL_STRICT`.

    >>> t = strict_tool("fetch", "d", {"url": {"type": "string"}}, ("url",), strict=True)
    >>> t["strict"], t["input_schema"]["additionalProperties"], t["input_schema"]["required"]
    (True, False, ['url'])
    >>> "strict" in strict_tool("fetch", "d", {"url": {"type": "string"}}, ("url",),
    ...                         strict=False)
    False
    """
    tool: dict[str, Any] = {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    }
    if strict if strict is not None else TOOL_STRICT:
        tool["strict"] = True
    return tool
