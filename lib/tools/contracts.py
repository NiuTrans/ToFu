"""Single-source tool contracts and deterministic provider compilation.

``ToolContractV2`` owns model text, detailed discovery help, parameters,
execution validation, permission, idempotency, result recovery, errors, PTC
eligibility, and optional producer-declared decoded-result documentation.
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
ToolResultRecovery = Literal["artifact", "source", "none"]
ToolEffectRecovery = Literal["read_only", "idempotent", "reconcile"]
MAX_DECODED_RESULT_SCHEMA_BYTES = 32768


def clone_decoded_result_schema(value: Any) -> dict[str, Any]:
    """Freeze producer-declared decoded JSON metadata; never infer from output.

    This describes successful local tools.invoke values, not transport/MCP
    envelopes. It is documentation, not result validation or evidence coverage.
    """
    try:
        return json.loads(_encode_decoded_result_schema(value))
    except (TypeError, RecursionError) as error:
        raise ValueError("decoded_result_schema must be finite JSON") from error


def _encode_decoded_result_schema(value: Any) -> str:
    """Apply the shared finite-JSON and byte admission before materialization."""
    if not isinstance(value, Mapping):
        raise ValueError("decoded_result_schema must be an object")
    try:
        encoded = json.dumps(dict(value), ensure_ascii=True, sort_keys=True,
                             separators=(',', ':'), allow_nan=False)
        if len(encoded.encode('utf-8')) > MAX_DECODED_RESULT_SCHEMA_BYTES:
            raise ValueError("decoded_result_schema exceeds 32 KiB")
        return encoded
    except (TypeError, RecursionError) as error:
        raise ValueError("decoded_result_schema must be finite JSON") from error


def serialize_decoded_result_schema(value: Any) -> str:
    """Render the canonical declaration without constructing discarded JSON data.

    Ordinary string-keyed JSON already has canonical key order after encoding.
    Non-string keys and extension containers retain normalization through the
    decoder, including the historical order after JSON stringifies numeric keys.
    """
    encoded = _encode_decoded_result_schema(value)
    pending = [value]
    # Encoding owns the result. If a producer changes the borrowed input while
    # we inspect its key types, traversal must still terminate on that snapshot's
    # byte budget and normalize the already frozen bytes.
    remaining = len(encoded)
    while pending and remaining:
        remaining -= 1
        node = pending.pop()
        if type(node) is dict and all(type(key) is str for key in node):
            pending.extend(node.values())
        elif type(node) in (list, tuple):
            pending.extend(node)
        elif type(node) not in (str, int, float, bool, type(None)):
            break
    else:
        if not pending:
            return encoded
    try:
        return json.dumps(json.loads(encoded), ensure_ascii=True,
                          sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, RecursionError) as error:
        raise ValueError("decoded_result_schema must be finite JSON") from error

_PROVIDER_SAFE_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]+")
_JSON_ATOMIC_TYPES = (str, int, float, bool, type(None))


def _clone_contract_value(value: Any, memo: dict[int, Any] | None = None) -> Any:
    """Clone ordinary JSON contract data without ``deepcopy`` dispatch cost.

    Provider schemas are JSON values, so exact built-in dictionaries/lists can
    take a small recursive path. A memo preserves shared references and cycles;
    extension types retain their historical ``deepcopy`` behavior.
    """
    if type(value) in _JSON_ATOMIC_TYPES:
        return value
    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if type(value) is dict:
        cloned: dict[Any, Any] = {}
        memo[identity] = cloned
        cloned.update(
            (_clone_contract_value(key, memo),
             _clone_contract_value(item, memo))
            for key, item in value.items()
        )
        return cloned
    if type(value) is list:
        cloned_list: list[Any] = []
        memo[identity] = cloned_list
        cloned_list.extend(
            _clone_contract_value(item, memo) for item in value)
        return cloned_list
    return copy.deepcopy(value, memo)


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
    result_recovery: ToolResultRecovery = "artifact"
    decoded_result_schema: Mapping[str, Any] | None = None
    shell_command_argument: str | None = None
    effect_recovery: ToolEffectRecovery | None = None
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
        if self.decoded_result_schema is not None:
            object.__setattr__(self, 'decoded_result_schema',
                               clone_decoded_result_schema(self.decoded_result_schema))
        if self.permission in {"write", "approval"} and self.ptc_eligible:
            raise ValueError("write/approval tools cannot be PTC eligible")
        if self.ptc_eligible and self.idempotency == "non_idempotent":
            raise ValueError("non-idempotent tools cannot be PTC eligible")
        if self.result_recovery not in {"artifact", "source", "none"}:
            raise ValueError(
                "result_recovery must be artifact, source, or none")

        policy = (self.effect_recovery if self.effect_recovery is not None else (
            'read_only' if self.permission == 'read' and self.idempotency == 'read_only'
            else 'idempotent' if self.idempotency == 'idempotent' else 'reconcile'))
        if policy not in {'read_only', 'idempotent', 'reconcile'}:
            raise ValueError('invalid effect recovery policy')
        if policy == 'read_only' and (self.permission != 'read' or self.idempotency != 'read_only'):
            raise ValueError('read-only effect recovery requires a read-only contract')
        if policy == 'idempotent' and self.idempotency != 'idempotent':
            raise ValueError('idempotent effect recovery requires declared idempotency')
        object.__setattr__(self, 'effect_recovery', policy)

        if self.shell_command_argument is not None:
            properties = self.parameters.get("properties", {})
            if (not isinstance(self.shell_command_argument, str)
                    or not self.shell_command_argument
                    or not isinstance(properties, Mapping)
                    or not isinstance(properties.get(self.shell_command_argument), Mapping)
                    or properties[self.shell_command_argument].get("type") != "string"):
                raise ValueError("shell_command_argument must name a string parameter")

    def provider_schema(self) -> dict[str, Any]:
        """Compile the short, cache-stable provider function schema."""
        result = {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.model_description.strip(),
                "parameters": _clone_contract_value(dict(self.parameters)),
            },
        }
        if self.decoded_result_schema is not None:
            # Private catalog metadata; the common wire boundary projects it
            # into description text and removes this non-provider field.
            result['decoded_result_schema'] = _clone_contract_value(
                self.decoded_result_schema)
        return result

    def search_document(self) -> dict[str, Any]:
        """Compile private rich metadata returned only after discovery."""
        result = {
            "contractVersion": self.contract_version,
            "name": self.name,
            "namespace": self.namespace,
            "summary": self.model_description.strip(),
            "help": (self.detailed_help or self.model_description).strip(),
            "aliases": list(self.search_metadata),
            "arguments_schema": _clone_contract_value(dict(self.parameters)),
            "permission": self.permission,
            "idempotency": self.idempotency,
            "ptcEligible": self.ptc_eligible,
            "resultRecovery": self.result_recovery,
            "effectRecovery": self.effect_recovery,
            "shellCommandArgument": self.shell_command_argument,
            "errors": [
                {"code": error.code, "retryable": error.retryable,
                 "nextAction": error.next_action}
                for error in self.errors
            ],
        }
        if self.decoded_result_schema is not None:
            result['decoded_result_schema'] = _clone_contract_value(
                self.decoded_result_schema)
        return result

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
            allowed = (f"{minimum}–{int(maximum)}" if maximum is not None
                       else f">= {minimum}")
            raise ToolContractError(
                "invalid_argument_length",
                f"Invalid length at {path} "
                f"(got {len(value)} chars; allowed {allowed}).",
                path=path)
        pattern = schema.get('pattern')
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise ToolContractError(
                'invalid_argument_value', f'Value does not match the declared pattern at {path}.', path=path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ToolContractError(
                "invalid_argument_value",
                f"Value below minimum at {path} "
                f"(got {value}; allowed >= {schema['minimum']}).",
                path=path)
        if "maximum" in schema and value > schema["maximum"]:
            raise ToolContractError(
                "invalid_argument_value",
                f"Value above maximum at {path} "
                f"(got {value}; allowed <= {schema['maximum']}).",
                path=path)
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
    one_of = schema.get("oneOf")
    if isinstance(value, dict) and isinstance(one_of, list) and one_of:
        matched: list[int] = []
        failures: list[str] = []
        for index, branch in enumerate(one_of):
            if not isinstance(branch, Mapping):
                continue
            try:
                _validate(value, branch, path=path)
            except ToolContractError as error:
                failures.append(str(error))
            else:
                matched.append(index)
        if not matched:
            raise ToolContractError(
                "invalid_argument_value",
                f"No allowed target form matches at {path}. "
                + " ".join(failures),
                path=path,
                next_action=(
                    "Choose exactly one target form and remove the other "
                    "form's fields."),
            )
        if len(matched) > 1:
            raise ToolContractError(
                "invalid_argument_value",
                f"Arguments match more than one target form at {path}; "
                "choose exactly one.",
                path=path,
                next_action=(
                    "Choose exactly one target form and remove the other "
                    "form's fields."),
            )
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
                out[key] = _clone_contract_value(child_schema["default"])
            if key in out and isinstance(child_schema, Mapping):
                out[key] = _validate(out[key], child_schema,
                                     path=f"{path}.{key}")
        return out
    return value


def adapt_legacy_tool_contract(
    schema: Mapping[str, Any], *, namespace: str = "general",
    search_metadata: tuple[str, ...] = (), permission: ToolPermission = "read",
    idempotency: ToolIdempotency = "read_only", ptc_eligible: bool = False,
    result_recovery: ToolResultRecovery = "artifact",
    shell_command_argument: str | None = None,
    effect_recovery: ToolEffectRecovery | None = None,
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
        result_recovery=result_recovery,
        decoded_result_schema=schema.get('decoded_result_schema'),
        shell_command_argument=shell_command_argument,
        effect_recovery=effect_recovery,
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
            document = _clone_contract_value(dict(existing))
            if str(document.get("name") or "") != name:
                raise ValueError(
                    f"tool contract name mismatch for executable tool {name}")
        else:
            document = adapt_legacy_tool_contract(
                schema, namespace=namespace, effect_recovery='reconcile').search_document()
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


def _clamp_walk(value: Any, schema: Any, path: str,
                log: list[tuple[str, str]], truncatable: frozenset[str],
                top_key: str) -> Any:
    if isinstance(value, bool) or not isinstance(schema, Mapping):
        return value
    if isinstance(value, (int, float)):
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if isinstance(lo, (int, float)) and not isinstance(lo, bool) \
                and value < lo:
            log.append((path, "clamp_minimum"))
            return lo
        if isinstance(hi, (int, float)) and not isinstance(hi, bool) \
                and value > hi:
            log.append((path, "clamp_maximum"))
            return hi
        return value
    if isinstance(value, str):
        hi = schema.get("maxLength")
        if top_key in truncatable and isinstance(hi, int) \
                and not isinstance(hi, bool) and len(value) > hi:
            log.append((path, "truncate_max_length"))
            return value[:hi]
        return value
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, Mapping):
            walked = [_clamp_walk(item, items, f"{path}[{index}]", log,
                                  truncatable, top_key)
                      for index, item in enumerate(value)]
            if any(new is not old for new, old in zip(walked, value)):
                return walked
        return value
    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, Mapping):
            changed = False
            out = dict(value)
            for key, child in properties.items():
                if key in out and isinstance(child, Mapping):
                    new = _clamp_walk(out[key], child, f"{path}.{key}",
                                      log, truncatable, top_key)
                    if new is not out[key]:
                        changed = True
                        out[key] = new
            return out if changed else value
        return value
    return value


def clamp_arguments_to_schema(
    value: Any,
    schema: Mapping[str, Any],
    *,
    truncatable: frozenset[str] = frozenset(),
    clamp_exempt: frozenset[str] = frozenset(),
) -> tuple[Any, list[tuple[str, str]]]:
    """Clamp numeric arguments to their declared bounds, returning repairs.

    An out-of-range ``limit``/``max_chars``/``cursor`` is a sizing wish,
    never identity data: the schema already states the only sensible value,
    so clamping loses no information and saves a full rejection round-trip.
    Strings over ``maxLength`` are truncated ONLY for top-level keys in
    ``truncatable`` (pure-display captions); identity/content strings (todo
    ids, edit anchors, file content) must never be truncated. Top-level keys
    in ``clamp_exempt`` are skipped entirely (see NEVER_CLAMP_PARAMS).
    Returns ``(clamped, [(json_path, pattern), ...])``; the ORIGINAL object
    is returned unchanged when nothing was repaired, so no-op callers keep
    object identity. Never raises — any internal error returns the input
    untouched so fail-closed validation behaves exactly as before this pass
    existed.
    """
    log: list[tuple[str, str]] = []
    try:
        if not isinstance(value, dict) or not isinstance(schema, Mapping):
            return value, log
        # A oneOf schema shares one properties bag across mutually exclusive
        # branches; field-level clamps stay sound because they only touch
        # declared numeric/string bounds, but skipping keeps reasoning simple
        # when a branch identity would change which bound applies. Clamping is
        # a sizing convenience, never part of target-form authority.
        if isinstance(schema.get("oneOf"), list) and schema["oneOf"]:
            return value, log
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return value, log
        changed = False
        out = dict(value)
        for key, child in properties.items():
            if key not in out or key in clamp_exempt \
                    or not isinstance(child, Mapping):
                continue
            new = _clamp_walk(out[key], child, f"$.{key}", log,
                              truncatable, key)
            if new is not out[key]:
                changed = True
                out[key] = new
        return (out if changed else value), log
    except Exception:
        return value, []


# Top-level parameters whose out-of-range values are SEMANTIC errors, not
# sizing wishes: silently moving a navigation cursor would answer a
# different question than the model asked, so rejection must preserve the
# chance to re-derive the intended position. (get_conversation.before=-1 is
# pinned rejected by tests; its before=0 sibling is a measured
# non-destructive sentinel repaired by a dedicated omission pass upstream.)
NEVER_CLAMP_PARAMS: dict[str, frozenset[str]] = {
    "get_conversation": frozenset({"before"}),
}


def clamp_tool_arguments_from_documents(
    documents_by_name: Mapping[str, Any] | None,
    tool_name: str,
    value: Any,
) -> tuple[Any, list[tuple[str, str]]]:
    """Bounds-clamp one call against its request-owned contract document.

    The static repair index exempts server-dynamic (MCP) schemas; the
    request-owned ToolContractV2 document still knows their bounds, so the
    execution pipeline can clamp them before validation rejects the call.
    Unknown/missing/malformed documents pass through untouched — validation
    downstream remains the fail-closed authority for those cases.
    """
    if documents_by_name is None:
        return value, []
    document = documents_by_name.get(tool_name)
    if not isinstance(document, Mapping):
        return value, []
    if document.get("contractVersion") != "tofu.tool-contract/v2":
        return value, []
    schema = document.get("arguments_schema")
    if not isinstance(schema, Mapping):
        return value, []
    return clamp_arguments_to_schema(
        value, schema,
        clamp_exempt=NEVER_CLAMP_PARAMS.get(tool_name, frozenset()))


def contract_json(contract: ToolContractV2) -> str:
    """Canonical contract document used for hashing and generated docs."""
    return json.dumps(contract.search_document(), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


__all__ = [
    "ToolContractError", "ToolContractV2", "ToolErrorContract",
    "adapt_legacy_tool_contract", "clamp_arguments_to_schema",
    "clamp_tool_arguments_from_documents", "NEVER_CLAMP_PARAMS",
    "compile_execution_contract_documents",
    "contract_json", "validate_tool_arguments_from_documents",
]
