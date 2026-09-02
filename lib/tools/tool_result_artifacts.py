"""Canonical v2 contracts for bounded continuation of large tool results."""

from __future__ import annotations

from lib.tools.contracts import ToolContractV2, ToolErrorContract


READ_TOOL_ARTIFACT = ToolContractV2(
    name="read_tool_artifact",
    namespace="artifacts",
    model_description="Read a bounded range from a prior large tool result.",
    detailed_help=(
        "Use artifact_ref and cursor returned by a ToolResultEnvelopeV2. "
        "The artifact is owner-scoped and expires; request only the next "
        "range needed for the task."
    ),
    search_metadata=(
        "large result continuation range cursor",
        "继续读取 大结果 游标 分段",
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_ref": {"type": "string", "minLength": 1,
                             "maxLength": 96},
            "cursor": {"type": "string", "default": "0",
                       "maxLength": 24},
            "limit": {"type": "integer", "minimum": 1,
                      "maximum": 65536, "default": 8192},
        },
        "required": ["artifact_ref"],
        "additionalProperties": False,
    },
    permission="read",
    idempotency="read_only",
    ptc_eligible=True,
    errors=(
        ToolErrorContract("artifact_unavailable", False,
                          "Re-run the source tool if the evidence is required."),
        ToolErrorContract("invalid_cursor", True,
                          "Use the cursor returned by the previous range."),
    ),
)

SEARCH_TOOL_ARTIFACT = ToolContractV2(
    name="search_tool_artifact",
    namespace="artifacts",
    model_description="Search within a prior large tool result by text.",
    detailed_help=(
        "Search an owner-scoped ToolResultEnvelopeV2 artifact without loading "
        "the whole result. Continue with next_cursor when truncated."
    ),
    search_metadata=(
        "find grep prior tool output artifact",
        "搜索 工具结果 证据 大文本",
    ),
    parameters={
        "type": "object",
        "properties": {
            "artifact_ref": {"type": "string", "minLength": 1,
                             "maxLength": 96},
            "query": {"type": "string", "minLength": 1, "maxLength": 200},
            "cursor": {"type": "string", "default": "0", "maxLength": 24},
            "limit": {"type": "integer", "minimum": 1,
                      "maximum": 20, "default": 8},
        },
        "required": ["artifact_ref", "query"],
        "additionalProperties": False,
    },
    permission="read",
    idempotency="read_only",
    ptc_eligible=True,
    errors=(
        ToolErrorContract("artifact_unavailable", False,
                          "Re-run the source tool if the evidence is required."),
        ToolErrorContract("invalid_cursor", True,
                          "Use the cursor returned by the previous search."),
    ),
)

TOOL_RESULT_ARTIFACT_CONTRACTS = (
    READ_TOOL_ARTIFACT,
    SEARCH_TOOL_ARTIFACT,
)
TOOL_RESULT_ARTIFACT_NAMES = frozenset(
    contract.name for contract in TOOL_RESULT_ARTIFACT_CONTRACTS)


def build_tool_result_artifact_tools() -> list[dict]:
    return [contract.provider_schema()
            for contract in TOOL_RESULT_ARTIFACT_CONTRACTS]


__all__ = [
    "READ_TOOL_ARTIFACT", "SEARCH_TOOL_ARTIFACT",
    "TOOL_RESULT_ARTIFACT_CONTRACTS", "TOOL_RESULT_ARTIFACT_NAMES",
    "build_tool_result_artifact_tools",
]
