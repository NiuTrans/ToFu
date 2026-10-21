"""Native artifact query contracts and producer-owned typed failure projection.

The handler retains native query JSON for canonical/program evidence. This
adapter uses the common error envelope for model recovery, without interpreting
application documents or changing the decoded result schema.
"""

from __future__ import annotations


from lib.tools.contracts import ToolContractV2, ToolErrorContract
from lib.tool_results.tool_result_query_generated import CONTRACT as _QUERY_CONTRACT
from lib.tools.result_surfaces_generated import CONTRACT as _SURFACE_CONTRACT


_QUERY_ERRORS = tuple(
    ToolErrorContract(code, failure['retryable'], failure['nextAction'])
    for code, failure in _QUERY_CONTRACT['failures'].items())


def _decoded_artifact_result_schema(operation):
    """Declare handler JSON, including pages, indexed batches and refusals.

    Coverage refers to the retained source, independently of page truncation.
    Extra backend provenance/media fields stay opaque; this grants no authority.
    """
    common = {
        "status": {"enum": ["ok"]},
        "artifactRef": {"type": "string"},
        "nextCursor": {"type": ["string", "null"]},
        "truncated": {"type": "boolean"},
        "coverage": {"enum": ["complete", "incomplete", "unavailable"]},
        "sourceCoverage": {"type": "string"},
        "retentionCoverage": {"type": "string"},
    }
    required = ["status", "artifactRef", "nextCursor", "truncated", "coverage"]
    if operation == "read":
        fields = {"content": {"type": "string"}, "offset": {"type": "integer"},
                  "sizeBytes": {"type": "integer"}}
        required += ["content", "offset"]
    else:
        fields = {"query": {"type": "string"}, "items": {"type": "array", "items": {
            "type": "object", "properties": {"offset": {"type": "integer"},
                "text": {"type": "string"}, "artifactRef": {"type": "string"}},
            "required": ["offset", "text", "artifactRef"]}}}
        required += ["query", "items"]
    page = {"type": "object", "properties": {**common, **fields}, "required": required}
    error = {"type": "object", "properties": {
        "status": {"enum": ["error"]}, "artifactRef": {"type": "string"},
        "error": {"type": "object", "properties": {"code": {"type": "string"}},
                  "required": ["code"]}}, "required": ["status", "error"]}
    # Define page/error once. Indexed batch items add their independent
    # constraint without duplicating the full nested producer declarations.
    variants = [{"$ref": "#/$defs/page"}, {"$ref": "#/$defs/error"}]
    indexed = {"allOf": [{"anyOf": variants}, {"type": "object", "properties": {
        "index": {"type": "integer", "minimum": 0}}, "required": ["index"]}]}
    batch = {"type": "object", "properties": {
        "status": {"enum": ["ok", "partial_failure"]},
        "items": {"type": "array", "items": indexed}},
        "required": ["status", "items"]}
    return {"$defs": {"page": page, "error": error}, "anyOf": [*variants, batch]}


READ_TOOL_ARTIFACT = ToolContractV2(
    name="read_tool_artifact",
    namespace="artifacts",
    model_description=(
        "Read one or more bounded ranges from prior large tool results."
    ),
    detailed_help=(
        "For one artifact, use artifact_ref plus optional cursor/limit. For "
        "independent ranges, pass reads (maximum 16); they run with bounded "
        "parallelism and return in input order. Each artifact is owner-scoped "
        "and expires. Canonical batches traverse retained children, not previews; "
        "selector is an optional RFC6901 pointer. Pass nextCursor unchanged. "
        "Missing evidence is not recovered by repeating the original tool."
    ),
    search_metadata=(
        "large result continuation range cursor",
        "继续读取 大结果 游标 分段",
    ),
    parameters={
        "type": "object",
        "properties": {
            "reads": {
                "type": "array", "minItems": 1, "maxItems": 16,
                "items": {
                    "type": "object",
                    "properties": {
                        "artifact_ref": {"type": "string", "minLength": 1,
                                         "maxLength": 96},
                        "cursor": {"type": "string", "default": "0",
                                   "maxLength": 2048},
                        "limit": {"type": "integer", "minimum": 1,
                                  "maximum": 65536, "default": 8192,
                                  "description": "UTF-8 bytes to return (1–65536); codepoints stay whole."},
                    },
                    "required": ["artifact_ref"],
                    "additionalProperties": False,
                },
            },
            "artifact_ref": {"type": "string", "minLength": 1,
                             "maxLength": 96},
            "cursor": {"type": "string", "default": "0",
                       "maxLength": 2048},
            "limit": {"type": "integer", "minimum": 1,
                      "maximum": 65536, "default": 8192,
                      "description": "UTF-8 bytes to return (1–65536); codepoints stay whole."},
        },
        "additionalProperties": False,
    },
    decoded_result_schema=_decoded_artifact_result_schema("read"),
    permission="read",
    idempotency="read_only",
    ptc_eligible=True,
    errors=(
        ToolErrorContract("artifact_unavailable", False,
                          "Historical evidence is unavailable; a separately authorized source call is a new observation."),
        *_QUERY_ERRORS,
    ),
)

SEARCH_TOOL_ARTIFACT = ToolContractV2(
    name="search_tool_artifact",
    namespace="artifacts",
    model_description=(
        "Search retained text; grouped hits list all "
        f"{_SURFACE_CONTRACT['artifactProjectionMatchGroups']['offsetsField']}."
    ),
    detailed_help=(
        "For one artifact, use artifact_ref and query. For independent "
        "searches, pass searches (maximum 16); they run with bounded "
        "parallelism and return in input order. Continue each item with its "
        "nextCursor when truncated. Search covers retained text, not image pixels. "
        "Check coverage/unavailable: zero hits on an incomplete scope cannot "
        "prove absence in unseen content. Model views group only identical "
        "adjacent excerpts from the same source/path; decoded program results "
        "retain every hit separately."
    ),
    search_metadata=(
        "find grep prior tool output artifact",
        "搜索 工具结果 证据 大文本",
    ),
    parameters={
        "type": "object",
        "properties": {
            "searches": {
                "type": "array", "minItems": 1, "maxItems": 16,
                "items": {
                    "type": "object",
                    "properties": {
                        "artifact_ref": {"type": "string", "minLength": 1,
                                         "maxLength": 96},
                        "query": {"type": "string", "minLength": 1,
                                  "maxLength": 200},
                        "cursor": {"type": "string", "default": "0",
                                   "maxLength": 2048},
                        "limit": {"type": "integer", "minimum": 1,
                                  "maximum": 20, "default": 8,
                                  "description": "Hits to return (1–20)."},
                    },
                    "required": ["artifact_ref", "query"],
                    "additionalProperties": False,
                },
            },
            "artifact_ref": {"type": "string", "minLength": 1,
                             "maxLength": 96},
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "cursor": {"type": "string", "default": "0", "maxLength": 2048},
            "limit": {"type": "integer", "minimum": 1,
                      "maximum": 20, "default": 8,
                      "description": "Hits to return (1–20)."},
        },
        "additionalProperties": False,
    },
    decoded_result_schema=_decoded_artifact_result_schema("search"),
    permission="read",
    idempotency="read_only",
    ptc_eligible=True,
    errors=(
        ToolErrorContract("artifact_unavailable", False,
                          "Historical evidence is unavailable; a separately authorized source call is a new observation."),
        *_QUERY_ERRORS,
    ),
)

# The existing read family also supports bounded RFC6901 structural selection.
_SELECTOR = {"type": "string", "maxLength": 1024,
             "description": "Optional RFC6901 JSON pointer into JSON parts."}
READ_TOOL_ARTIFACT.parameters['properties']['selector'] = _SELECTOR
READ_TOOL_ARTIFACT.parameters['properties']['reads']['items']['properties']['selector'] = _SELECTOR

TOOL_RESULT_ARTIFACT_CONTRACTS = (
    READ_TOOL_ARTIFACT,
    SEARCH_TOOL_ARTIFACT,
)
TOOL_RESULT_ARTIFACT_NAMES = frozenset(
    contract.name for contract in TOOL_RESULT_ARTIFACT_CONTRACTS)


def build_tool_result_artifact_tools() -> list[dict]:
    return [contract.provider_schema()
            for contract in TOOL_RESULT_ARTIFACT_CONTRACTS]


def _artifact_error(value):
    from lib.tools.result_envelope import typed_tool_error
    error = value.get('error')
    if (value.get('status') != 'error' or not isinstance(error, dict)
            or type(error.get('retryable')) is not bool
            or not isinstance(error.get('code'), str) or not error['code'].strip()):
        return None
    return typed_tool_error(error['code'], retryable=error['retryable'],
        message=error.get('message', ''),
        next_action=error.get('nextAction') or error.get('next_action') or '').error


def artifact_failure_envelope(tool_name: str, value: dict, source: str):
    """Project only this producer's explicit single/all-terminal failure facts."""
    from dataclasses import replace
    from lib.tools.result_envelope import ToolResultEnvelopeV2, nonretryable_batch_error
    error = _artifact_error(value)
    if error is not None:
        metadata = {key: item for key, item in value.items() if key not in {'status', 'error'}}
        extra = {key: item for key, item in value['error'].items()
                 if key not in {'code', 'retryable', 'nextAction', 'next_action', 'message'}}
        if extra:
            metadata['error'] = extra
        return ToolResultEnvelopeV2(status='error', error=error,
            summary=error.message or error.code,
            items=(metadata,) if metadata else ()).with_visible_bytes()
    if value.get('status') == 'partial_failure' and isinstance(value.get('items'), list):
        failure = nonretryable_batch_error(_artifact_error(item) for item in value['items'])
        if failure is not None:
            from lib.tools.result_surfaces import project_model_source
            return replace(failure, summary=project_model_source(tool_name, source)).with_visible_bytes()
    return None


__all__ = [
    "READ_TOOL_ARTIFACT", "SEARCH_TOOL_ARTIFACT",
    "TOOL_RESULT_ARTIFACT_CONTRACTS", "TOOL_RESULT_ARTIFACT_NAMES",
    "build_tool_result_artifact_tools",
    "artifact_failure_envelope",
]
