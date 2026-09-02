"""Single-source tool contracts and deterministic provider compilation.

``ToolContractV2`` owns model text, detailed discovery help, parameters,
execution validation, permission, idempotency, errors, and PTC eligibility.
Legacy schemas can be adapted read-only while tool families migrate.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping


ToolPermission = Literal["read", "write", "approval", "external"]
ToolIdempotency = Literal["read_only", "idempotent", "non_idempotent"]

_PROVIDER_SAFE_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+")


class ToolContractError(ValueError):
    """Stable validation error suitable for a short model retry hint."""

    def __init__(self, code: str, message: str, *, path: str = "$",
                 retryable: bool = True,
                 next_action: str = "Match arguments_schema and retry."):
        super().__init__(message)
        self.code = code
        self.path = path
        self.retryable = retryable
        self.next_action = next_action

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "path": self.path,
            "retryable": self.retryable,
            "nextAction": self.next_action,
        }


@dataclass(frozen=True)
class ToolErrorContract:
    code: str
    retryable: bool
    next_action: str


@dataclass(frozen=True)
class ToolContractV2:
    """Provider-neutral authority compiled into every tool representation."""

    name: str
    parameters: Mapping[str, Any]
    model_description: str
    search_metadata: tuple[str, ...] = ()
    detailed_help: str = ""
    permission: ToolPermission = "read"
    idempotency: ToolIdempotency = "read_only"
    errors: tuple[ToolErrorContract, ...] = ()
    ptc_eligible: bool = False
    namespace: str = "general"
    contract_version: str = field(default="tofu.tool-contract/v2", init=False)

    def __post_init__(self) -> None:
        # Provider function names permit ASCII letters, digits, underscores,
        # and hyphens. MCP adapters preserve the server/tool spelling inside
        # names such as ``mcp__12306-train__get-current-date``; rejecting the
        # hyphens here silently removed otherwise executable MCP tools from
        # the request-owned contract map.
        if (
            not isinstance(self.name, str)
            or _PROVIDER_SAFE_TOOL_NAME.fullmatch(self.name) is None
        ):
            raise ValueError(
                "tool contract name must use only ASCII letters, digits, "
                "underscores, or hyphens"
            )
        if not isinstance(self.parameters, Mapping):
            raise ValueError("tool contract parameters must be an object")
        if self.permission in {"write", "approval"} and self.ptc_eligible:
            raise ValueError("write/approval tools cannot be PTC eligible")
        if self.ptc_eligible and self.idempotency == "non_idempotent":
            raise ValueError("non-idempotent tools cannot be PTC eligible")

    def provider_schema(self) -> dict[str, Any]:
        """Compile the short, cache-stable provider function schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.model_description.strip(),
                "parameters": copy.deepcopy(dict(self.parameters)),
            },
        }

    def search_document(self) -> dict[str, Any]:
        """Compile private rich metadata returned only after discovery."""
        return {
            "contractVersion": self.contract_version,
            "name": self.name,
            "namespace": self.namespace,
            "summary": self.model_description.strip(),
            "help": (self.detailed_help or self.model_description).strip(),
            "aliases": list(self.search_metadata),
            "arguments_schema": copy.deepcopy(dict(self.parameters)),
            "permission": self.permission,
            "idempotency": self.idempotency,
            "ptcEligible": self.ptc_eligible,
            "errors": [
                {"code": error.code, "retryable": error.retryable,
                 "nextAction": error.next_action}
                for error in self.errors
            ],
        }

    def validate_arguments(self, value: Any) -> dict[str, Any]:
        validated = _validate(value, dict(self.parameters), path="$")
        if not isinstance(validated, dict):
            raise ToolContractError(
                "invalid_argument_type", "Tool arguments must be an object.")
        return validated


def _matches_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def _validate(value: Any, schema: Mapping[str, Any], *, path: str) -> Any:
    expected = schema.get("type")
    expected_types = expected if isinstance(expected, list) else [expected]
    expected_types = [str(item) for item in expected_types if item]
    if expected_types and not any(_matches_type(value, item)
                                  for item in expected_types):
        raise ToolContractError(
            "invalid_argument_type",
            f"Invalid type at {path}; expected {' | '.join(expected_types)}.",
            path=path,
        )
    if "enum" in schema and value not in schema.get("enum", ()):
        raise ToolContractError(
            "invalid_argument_value", f"Invalid value at {path}.", path=path)
    if isinstance(value, str):
        minimum = int(schema.get("minLength") or 0)
        maximum = schema.get("maxLength")
        if len(value) < minimum or (maximum is not None and len(value) > int(maximum)):
            raise ToolContractError(
                "invalid_argument_length", f"Invalid length at {path}.", path=path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolContractError(
                "invalid_argument_value", f"Value below minimum at {path}.", path=path)
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolContractError(
                "invalid_argument_value", f"Value above maximum at {path}.", path=path)
    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            raise ToolContractError(
                "too_few_items", f"Too few items at {path}.", path=path)
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ToolContractError(
                "too_many_items", f"Too many items at {path}.", path=path)
        child = schema.get("items")
        if isinstance(child, Mapping):
            return [_validate(item, child, path=f"{path}[{index}]")
                    for index, item in enumerate(value)]
    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or ()
        missing = [name for name in required
                   if name not in value or value.get(name) is None]
        if missing:
            raise ToolContractError(
                "missing_required_arguments",
                "Missing required arguments: " + ", ".join(map(str, missing)),
                path=path,
            )
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ToolContractError(
                    "unknown_arguments",
                    "Unknown arguments: " + ", ".join(extras), path=path)
        out = dict(value)
        for key, child_schema in properties.items():
            if key not in out and isinstance(child_schema, Mapping) \
                    and "default" in child_schema:
                out[key] = copy.deepcopy(child_schema["default"])
            if key in out and isinstance(child_schema, Mapping):
                out[key] = _validate(out[key], child_schema,
                                     path=f"{path}.{key}")
        return out
    return value


def adapt_legacy_tool_contract(
    schema: Mapping[str, Any], *, namespace: str = "general",
    search_metadata: tuple[str, ...] = (), permission: ToolPermission = "read",
    idempotency: ToolIdempotency = "read_only", ptc_eligible: bool = False,
) -> ToolContractV2:
    """Read-only compatibility adapter for existing provider schemas."""
    function = schema.get("function") if isinstance(schema, Mapping) else None
    function = function if isinstance(function, Mapping) else schema
    name = str((function or {}).get("name") or "")
    description = str((function or {}).get("description") or "")
    parameters = (function or {}).get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {
            "type": "object", "properties": {},
            "additionalProperties": False,
        }
    return ToolContractV2(
        name=name,
        parameters=parameters,
        model_description=description,
        search_metadata=search_metadata,
        detailed_help=description,
        permission=permission,
        idempotency=idempotency,
        ptc_eligible=ptc_eligible,
        namespace=namespace,
    )


def compile_execution_contract_documents(
    schemas: Iterable[Mapping[str, Any]], *,
    authoritative_documents_by_name: Mapping[str, Any] | None = None,
    namespace: str = "general",
) -> dict[str, dict[str, Any]]:
    """Compile the exact execution surface into v2 contract documents.

    Request assembly already owns rich documents for registry-backed tools.
    Direct workers may add role-local or gateway schemas after that assembly;
    those schemas cross the same legacy adapter here instead of inventing a
    second validator. Duplicate executable names are rejected because an
    ambiguous authority map must never be resolved by list order.
    """
    authoritative = authoritative_documents_by_name or {}
    documents: dict[str, dict[str, Any]] = {}
    for schema in schemas:
        if not isinstance(schema, Mapping):
            continue
        function = schema.get("function")
        function = function if isinstance(function, Mapping) else schema
        name = str(function.get("name") or "")
        if not name:
            continue
        if name in documents:
            raise ValueError(f"duplicate executable tool contract: {name}")
        existing = authoritative.get(name)
        if isinstance(existing, Mapping):
            document = copy.deepcopy(dict(existing))
            if str(document.get("name") or "") != name:
                raise ValueError(
                    f"tool contract name mismatch for executable tool {name}")
        else:
            document = adapt_legacy_tool_contract(
                schema, namespace=namespace).search_document()
        documents[name] = document
    return documents


def validate_tool_arguments_from_documents(
    documents_by_name: Mapping[str, Any] | None,
    tool_name: str,
    value: Any,
) -> dict[str, Any]:
    """Validate one call against its request-owned ToolContractV2 document.

    ``None`` is the explicit read-compatible legacy mode. Once a request owns
    a document map (including an empty map), a missing or malformed contract
    fails closed so discovery, provider schema, and execution cannot drift.
    """
    if documents_by_name is None:
        if not isinstance(value, dict):
            raise ToolContractError(
                "invalid_argument_type", "Tool arguments must be an object.")
        return dict(value)

    document = documents_by_name.get(tool_name)
    if not isinstance(document, Mapping):
        raise ToolContractError(
            "tool_contract_unavailable",
            f"No executable ToolContractV2 is available for {tool_name!r}.",
            retryable=True,
            next_action=(
                "Search tools again and use a tool from the current tool epoch."),
        )
    if document.get("contractVersion") != "tofu.tool-contract/v2":
        raise ToolContractError(
            "invalid_tool_contract",
            f"Unsupported execution contract for {tool_name!r}.",
            retryable=False,
            next_action="Do not execute this tool; report the contract error.",
        )
    schema = document.get("arguments_schema")
    if not isinstance(schema, Mapping):
        raise ToolContractError(
            "invalid_tool_contract",
            f"Execution contract for {tool_name!r} has no arguments schema.",
            retryable=False,
            next_action="Do not execute this tool; report the contract error.",
        )
    validated = _validate(value, schema, path="$")
    if not isinstance(validated, dict):
        raise ToolContractError(
            "invalid_argument_type", "Tool arguments must be an object.")
    return validated


def contract_json(contract: ToolContractV2) -> str:
    """Canonical contract document used for hashing and generated docs."""
    return json.dumps(contract.search_document(), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


__all__ = [
    "ToolContractError", "ToolContractV2", "ToolErrorContract",
    "adapt_legacy_tool_contract", "compile_execution_contract_documents",
    "contract_json", "validate_tool_arguments_from_documents",
]
