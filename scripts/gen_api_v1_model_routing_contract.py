#!/usr/bin/env python3
"""Generate server and browser artifacts from the API v1 model-routing contract.

Canonical sources:
- contracts/api_v1_model_routing.yaml (per-endpoint request/response payloads)
- contracts/model_routing_v2.schema.json (entity-level field authority)
- contracts/provider_offering_recipe_v1.schema.json (onboarding metadata)

Reviewed template metadata property references are inlined before generation;
category/provenance definitions are never duplicated in the API contract.

External ``./model_routing_v2.schema.json#/$defs/<name>`` references are
rewritten to ``#/$defs/<name>`` and resolved against EXTERNAL_DEFS; the bare
document reference becomes ``#/$defs/__document``.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import pprint
import re
import sys
from typing import Any

import yaml


ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
SOURCE = os.path.join(ROOT, "contracts", "api_v1_model_routing.yaml")
ENTITY_SCHEMA = os.path.join(ROOT, "contracts", "model_routing_v2.schema.json")
RECIPE_SCHEMA = os.path.join(ROOT, "contracts", "provider_offering_recipe_v1.schema.json")
_RECIPE_PROPERTY_PREFIX = "./provider_offering_recipe_v1.schema.json#/properties/"
_RECIPE_METADATA_FIELDS = frozenset({"category", "catalog_mode", "catalog_source"})
PYTHON_OUTPUT = os.path.join(ROOT, "lib", "generated_api_v1_model_routing.py")
TYPESCRIPT_OUTPUT = os.path.join(
    ROOT, "frontend", "src", "api", "api-v1-model-routing.generated.ts"
)

CONTRACT_ID = "tofu.api-v1-model-routing/v1"
ROUTES_BLUEPRINT = "routes.api_v1.providers:api_v1_providers_bp"

EXPECTED_OPERATIONS = (
    ("GET", "/api/v1/model-routing", "getModelRouting"),
    ("PUT", "/api/v1/model-routing", "putModelRouting"),
    ("GET", "/api/v1/providers", "listProviders"),
    ("POST", "/api/v1/providers", "createProvider"),
    ("GET", "/api/v1/providers/templates", "listProviderTemplates"),
    ("POST", "/api/v1/providers/templates/compile", "compileProviderTemplate"),
    ("POST", "/api/v1/providers/probe", "probeProvider"),
    (
        "GET",
        "/api/v1/providers/catpaw/local-import",
        "catpawLocalImportStatus",
    ),
    (
        "POST",
        "/api/v1/providers/catpaw/local-import",
        "catpawLocalImportApply",
    ),
    (
        "POST",
        "/api/v1/providers/catpaw/local-import/login",
        "catpawLocalImportLogin",
    ),
    ("GET", "/api/v1/providers/{provider_id}", "getProvider"),
    ("PATCH", "/api/v1/providers/{provider_id}", "updateProvider"),
    ("DELETE", "/api/v1/providers/{provider_id}", "deleteProvider"),
    (
        "POST",
        "/api/v1/providers/{provider_id}/probe-cells/start",
        "startProviderProbeCells",
    ),
    (
        "GET",
        "/api/v1/providers/{provider_id}/probe-cells/status",
        "getProviderProbeCellsStatus",
    ),
    (
        "PUT",
        "/api/v1/model-routing/credentials/{credential_id}/secret",
        "putCredentialSecret",
    ),
    (
        "POST",
        "/api/v1/model-routing/credentials/{credential_id}/secret/reveal",
        "revealCredentialSecret",
    ),
    (
        "POST",
        "/api/v1/model-routing/migration/plan",
        "planModelRoutingMigration",
    ),
    (
        "POST",
        "/api/v1/model-routing/migration/commit",
        "commitModelRoutingMigration",
    ),
)

_HTTP_METHODS = ("get", "post", "put", "patch", "delete")
_LOCAL_REF_PREFIX = "#/components/schemas/"
_EXTERNAL_DEF_PREFIX = "./model_routing_v2.schema.json#/$defs/"
_EXTERNAL_DOCUMENT_REF = "./model_routing_v2.schema.json"


class _UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that turns duplicate contract keys into a hard failure."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            mark = key_node.start_mark
            raise ValueError(f"duplicate contract key {key!r} at line {mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _load_entity_schema() -> dict[str, Any]:
    with open(ENTITY_SCHEMA, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or not isinstance(
        document.get("$defs"),
        dict,
    ):
        raise ValueError("model routing v2 entity schema must declare $defs")
    return document


def _rewrite_refs(node: Any) -> Any:
    """Rewrite external entity-schema references into the local registry."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if ref == _EXTERNAL_DOCUMENT_REF:
            return {"$ref": "#/$defs/__document"}
        if isinstance(ref, str) and ref.startswith(_EXTERNAL_DEF_PREFIX):
            return {"$ref": "#/$defs/" + ref[len(_EXTERNAL_DEF_PREFIX) :]}
        return {key: _rewrite_refs(child) for key, child in node.items()}
    if isinstance(node, list):
        return [_rewrite_refs(child) for child in node]
    return node


def _referenced_local_schemas(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_LOCAL_REF_PREFIX):
            found.add(ref[len(_LOCAL_REF_PREFIX) :])
        for child in node.values():
            _referenced_local_schemas(child, found)
    elif isinstance(node, list):
        for child in node:
            _referenced_local_schemas(child, found)


def _inline_recipe_metadata(node: Any, properties: dict[str, Any]) -> Any:
    """Expand only reviewed recipe metadata; never fetch arbitrary schema refs."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_RECIPE_PROPERTY_PREFIX):
            field = ref[len(_RECIPE_PROPERTY_PREFIX):]
            if field not in _RECIPE_METADATA_FIELDS or field not in properties:
                raise ValueError(f"unsupported template metadata reference {ref!r}")
            return copy.deepcopy(properties[field])
        return {key: _inline_recipe_metadata(value, properties) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_recipe_metadata(value, properties) for value in node]
    return node


def _load_contract() -> dict[str, Any]:
    with open(SOURCE, encoding="utf-8") as handle:
        document = yaml.load(handle, Loader=_UniqueKeyLoader)
    if not isinstance(document, dict):
        raise ValueError("API v1 model-routing contract must be an object")
    if document.get("openapi") != "3.1.0":
        raise ValueError("API v1 model-routing contract must use OpenAPI 3.1.0")
    if document.get("x-tofu-contract") != CONTRACT_ID:
        raise ValueError("unexpected API v1 model-routing contract id")
    if document.get("x-tofu-routes-blueprint") != ROUTES_BLUEPRINT:
        raise ValueError("unexpected x-tofu-routes-blueprint")

    with open(RECIPE_SCHEMA, encoding="utf-8") as handle:
        recipe_properties = json.load(handle)["properties"]
    document = _inline_recipe_metadata(document, recipe_properties)

    schemas = document.get("components", {}).get("schemas")
    if not isinstance(schemas, dict) or not schemas:
        raise ValueError("API v1 model-routing contract has no schemas")

    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ValueError("API v1 model-routing contract has no paths")
    actual_operations: list[tuple[str, str, str]] = []
    operation_specs: dict[str, dict[str, Any]] = {}
    for path, path_item in paths.items():
        if not isinstance(path, str) or not path.startswith("/api/v1/"):
            raise ValueError(f"contract path must be versioned v1: {path!r}")
        if not isinstance(path_item, dict):
            raise ValueError(f"path item {path!r} must be an object")
        unknown_methods = set(path_item) - set(_HTTP_METHODS)
        if unknown_methods:
            raise ValueError(
                f"{path!r} declares unsupported methods: "
                + ", ".join(sorted(unknown_methods))
            )
        for method in _HTTP_METHODS:
            operation = path_item.get(method)
            if operation is None:
                continue
            if not isinstance(operation, dict):
                raise ValueError(f"{method.upper()} {path} must be an object")
            operation_id = operation.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                raise ValueError(f"{method.upper()} {path} has no operationId")
            actual_operations.append((method.upper(), path, operation_id))

            request_schema = None
            request_body = operation.get("requestBody")
            if request_body is not None:
                if (
                    not isinstance(request_body, dict)
                    or request_body.get("required") is not True
                ):
                    raise ValueError(
                        f"{method.upper()} {path} requestBody must be required"
                    )
                request_schema = _response_ref(
                    request_body,
                    context=f"{method.upper()} {path} request body",
                )
                if request_schema not in schemas:
                    raise ValueError(
                        f"{method.upper()} {path} request schema "
                        f"{request_schema!r} is not declared"
                    )

            responses = operation.get("responses")
            if not isinstance(responses, dict) or len(responses) != 1:
                raise ValueError(
                    f"{method.upper()} {path} must declare exactly one success response"
                )
            ((status, response),) = responses.items()
            if status not in ("200", "201"):
                raise ValueError(
                    f"{method.upper()} {path} success status must be 200 or 201"
                )
            response_schema = _response_ref(
                response,
                context=f"{method.upper()} {path} response",
            )
            if response_schema not in schemas:
                raise ValueError(
                    f"{method.upper()} {path} response schema "
                    f"{response_schema!r} is not declared"
                )
            operation_specs[operation_id] = {
                "method": method.upper(),
                "path": path,
                "request": request_schema,
                "response": response_schema,
                "success": int(status),
            }

    expected = sorted(EXPECTED_OPERATIONS)
    actual = sorted(actual_operations)
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        raise ValueError(
            "contract routes drifted from routes/api_v1/providers.py; "
            f"missing: {missing!r}, unexpected: {extra!r}"
        )

    entity_schema = _load_entity_schema()
    entity_defs = entity_schema["$defs"]
    for name, schema in schemas.items():
        _validate_external_refs(schema, entity_defs, context=name)

    reachable: set[str] = set()
    for spec in operation_specs.values():
        for schema_name in (spec["request"], spec["response"]):
            if schema_name is not None:
                reachable.add(schema_name)
    # Transitive reachability over local references.
    frontier = list(reachable)
    seen = set(frontier)
    while frontier:
        name = frontier.pop()
        found: set[str] = set()
        _referenced_local_schemas(schemas[name], found)
        for child in found:
            if child not in schemas:
                raise ValueError(
                    f"schema {name!r} references undeclared schema {child!r}"
                )
            if child not in seen:
                seen.add(child)
                frontier.append(child)
    dead = sorted(set(schemas) - seen)
    if dead:
        raise ValueError("unreferenced component schemas: " + ", ".join(dead))

    document["_operations"] = operation_specs
    document["_entity_schema"] = entity_schema
    return document


def _response_ref(holder: Any, *, context: str) -> str:
    if not isinstance(holder, dict):
        raise ValueError(f"{context} must be an object")
    schema = holder.get("content", {}).get("application/json", {}).get("schema")
    if not isinstance(schema, dict):
        raise ValueError(f"{context} must declare an application/json schema")
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith(_LOCAL_REF_PREFIX):
        raise ValueError(f"{context} must reference a named component schema")
    return ref[len(_LOCAL_REF_PREFIX) :]


def _validate_external_refs(
    node: Any,
    entity_defs: dict[str, Any],
    *,
    context: str,
) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref == _EXTERNAL_DOCUMENT_REF:
                pass
            elif ref.startswith(_EXTERNAL_DEF_PREFIX):
                name = ref[len(_EXTERNAL_DEF_PREFIX) :]
                if name not in entity_defs:
                    raise ValueError(
                        f"{context} references unknown entity $def {name!r}"
                    )
            elif not ref.startswith(_LOCAL_REF_PREFIX):
                raise ValueError(f"{context} has unsupported $ref {ref!r}")
        for child in node.values():
            _validate_external_refs(child, entity_defs, context=context)
    elif isinstance(node, list):
        for child in node:
            _validate_external_refs(child, entity_defs, context=context)


def _external_defs(document: dict[str, Any]) -> dict[str, Any]:
    entity_schema = document["_entity_schema"]
    defs = copy.deepcopy(entity_schema["$defs"])
    defs["__document"] = copy.deepcopy(entity_schema)
    return defs


def _rewritten_schemas(document: dict[str, Any]) -> dict[str, Any]:
    return _rewrite_refs(document["components"]["schemas"])


def render_python(document: dict[str, Any]) -> str:
    schemas = _rewritten_schemas(document)
    external_defs = _external_defs(document)
    operations = document["_operations"]
    return "\n".join(
        (
            '"""AUTO-GENERATED by scripts/gen_api_v1_model_routing_contract.py.',
            "",
            "Canonical sources: contracts/api_v1_model_routing.yaml and",
            "contracts/model_routing_v2.schema.json and provider_offering_recipe_v1.schema.json.",
            "DO NOT EDIT BY HAND.",
            '"""',
            "",
            "from __future__ import annotations",
            "",
            "from collections.abc import Mapping",
            "import math",
            "import re",
            "from typing import Any",
            "",
            f"CONTRACT_ID = {CONTRACT_ID!r}",
            f"ROUTES_BLUEPRINT = {ROUTES_BLUEPRINT!r}",
            f"OPENAPI_SCHEMAS = {pprint.pformat(schemas, sort_dicts=True, width=100)}",
            f"EXTERNAL_DEFS = {pprint.pformat(external_defs, sort_dicts=True, width=100)}",
            f"OPERATIONS = {pprint.pformat(operations, sort_dicts=True, width=100)}",
            "",
            _PYTHON_RUNTIME,
            "",
        )
    )


_PYTHON_RUNTIME = """

class ContractViolation(ValueError):
    def __init__(self, schema_name: str, violations: list[str]):
        self.schema_name = schema_name
        self.violations = tuple(violations)
        super().__init__(
            f"{schema_name} contract violation: " + "; ".join(violations[:3])
        )


def _json_values_equal(left: Any, right: Any) -> bool:
    if left is right or left == right:
        if isinstance(left, bool) != isinstance(right, bool):
            return False
        return type(left) is type(right) or not isinstance(
            left, (int, float),
        )
    return False


def _resolve_ref(ref: str) -> Mapping[str, Any] | None:
    local_prefix = "#/components/schemas/"
    defs_prefix = "#/$defs/"
    if ref.startswith(local_prefix):
        return OPENAPI_SCHEMAS.get(ref[len(local_prefix):])
    if ref.startswith(defs_prefix):
        return EXTERNAL_DEFS.get(ref[len(defs_prefix):])
    return None


def _validate(schema_value: Any, value: Any, path: str) -> list[str]:
    schema = schema_value if isinstance(schema_value, Mapping) else {}
    ref = schema.get("$ref")
    if isinstance(ref, str):
        target = _resolve_ref(ref)
        return (
            _validate(target, value, path)
            if target is not None
            else [f"{path}: unknown schema reference"]
        )
    errors: list[str] = []
    variants = schema.get("oneOf")
    if isinstance(variants, list):
        matches = sum(not _validate(item, value, path) for item in variants)
        if matches != 1:
            errors.append(f"{path}: expected exactly one variant")
    variants = schema.get("anyOf")
    if isinstance(variants, list) and not any(
        not _validate(item, value, path) for item in variants
    ):
        errors.append(f"{path}: expected a declared variant")
    conjunction = schema.get("allOf")
    if isinstance(conjunction, list):
        for item in conjunction:
            errors.extend(_validate(item, value, path))
    condition = schema.get("if")
    if isinstance(condition, Mapping):
        branch = schema.get("then") if not _validate(condition, value, path) \\
            else schema.get("else")
        if isinstance(branch, Mapping):
            errors.extend(_validate(branch, value, path))
    negation = schema.get("not")
    if isinstance(negation, Mapping) and not _validate(negation, value, path):
        errors.append(f"{path}: value matches a forbidden shape")
    if errors:
        return errors
    if "const" in schema and not _json_values_equal(value, schema["const"]):
        return [f"{path}: unexpected constant value"]
    choices = schema.get("enum")
    if isinstance(choices, list) and not any(
        _json_values_equal(value, choice) for choice in choices
    ):
        return [f"{path}: value is outside the declared vocabulary"]
    kind = schema.get("type")
    if isinstance(kind, list):
        return [] if any(
            not _validate(dict(schema, type=variant), value, path)
            for variant in kind
        ) else [f"{path}: expected a declared type"]
    if kind == "null":
        return [] if value is None else [f"{path}: expected null"]
    if kind == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
        if isinstance(schema.get("minLength"), int) \\
                and len(value) < schema["minLength"]:
            return [f"{path}: string is too short"]
        if isinstance(schema.get("maxLength"), int) \\
                and len(value) > schema["maxLength"]:
            return [f"{path}: string is too long"]
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                matches = re.search(pattern, value)
            except re.error:
                return [f"{path}: contract has an invalid string pattern"]
            if matches is None:
                return [f"{path}: string does not match the declared pattern"]
        return []
    if kind in {"integer", "number"}:
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or (kind == "integer" and not isinstance(value, int))):
            return [f"{path}: expected {kind}"]
        if isinstance(value, float) and not math.isfinite(value):
            return [f"{path}: number must be finite"]
        if isinstance(schema.get("minimum"), (int, float)) \\
                and not isinstance(schema["minimum"], bool) \\
                and value < schema["minimum"]:
            return [f"{path}: number is below minimum"]
        if isinstance(schema.get("maximum"), (int, float)) \\
                and not isinstance(schema["maximum"], bool) \\
                and value > schema["maximum"]:
            return [f"{path}: number is above maximum"]
        return []
    if kind == "boolean":
        return [] if isinstance(value, bool) else [f"{path}: expected boolean"]
    if kind == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        if isinstance(schema.get("maxItems"), int) \\
                and len(value) > schema["maxItems"]:
            return [f"{path}: array is too large"]
        if schema.get("uniqueItems") is True:
            for index, item in enumerate(value):
                if any(
                    _json_values_equal(item, other)
                    for other in value[index + 1:]
                ):
                    return [f"{path}: array items are not unique"]
        errors = []
        for index, item in enumerate(value):
            errors.extend(
                _validate(schema.get("items", {}), item, f"{path}[{index}]")
            )
        return errors
    if kind == "object" or any(
        keyword in schema
        for keyword in (
            "properties",
            "required",
            "minProperties",
            "maxProperties",
            "propertyNames",
            "additionalProperties",
        )
    ):
        if not isinstance(value, Mapping):
            # Applicator keywords ignore non-object instances unless the
            # schema explicitly requires an object.
            return [] if kind != "object" else [f"{path}: expected object"]
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        errors = []
        if isinstance(schema.get("minProperties"), int) \\
                and len(value) < schema["minProperties"]:
            errors.append(f"{path}: object has too few properties")
        if isinstance(schema.get("maxProperties"), int) \\
                and len(value) > schema["maxProperties"]:
            errors.append(f"{path}: object has too many properties")
        property_names = schema.get("propertyNames")
        if isinstance(property_names, Mapping):
            for field in value:
                errors.extend(
                    _validate(property_names, field, f"{path}.{field}")
                )
        for field in schema.get("required") or []:
            if isinstance(field, str) and field not in value:
                errors.append(f"{path}.{field}: required")
        for field, child in properties.items():
            if field in value:
                errors.extend(_validate(child, value[field], f"{path}.{field}"))
        if schema.get("additionalProperties") is False:
            errors.extend(
                f"{path}.{field}: undeclared field"
                for field in value
                if field not in properties
            )
        elif isinstance(schema.get("additionalProperties"), Mapping):
            additional_schema = schema["additionalProperties"]
            for field, child_value in value.items():
                if field not in properties:
                    errors.extend(
                        _validate(
                            additional_schema, child_value, f"{path}.{field}",
                        )
                    )
        return errors
    return []


def violations(schema_name: str, value: Any) -> list[str]:
    schema = OPENAPI_SCHEMAS.get(schema_name)
    if schema is None:
        return ["schema is not registered"]
    return _validate(schema, value, "$")


def decode(schema_name: str, value: Any) -> Any:
    found = violations(schema_name, value)
    if found:
        raise ContractViolation(schema_name, found)
    return value


def decode_request(operation_id: str, value: Any) -> Any:
    spec = OPERATIONS.get(operation_id)
    if spec is None or spec["request"] is None:
        raise ContractViolation(operation_id, ["operation has no request schema"])
    return decode(spec["request"], value)


def decode_response(operation_id: str, value: Any) -> Any:
    spec = OPERATIONS.get(operation_id)
    if spec is None:
        raise ContractViolation(operation_id, ["operation is not registered"])
    return decode(spec["response"], value)


__all__ = [
    "CONTRACT_ID",
    "EXTERNAL_DEFS",
    "OPENAPI_SCHEMAS",
    "OPERATIONS",
    "ROUTES_BLUEPRINT",
    "ContractViolation",
    "decode",
    "decode_request",
    "decode_response",
    "violations",
]
""".strip()


def _pascal_case(name: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[_\-]", name))


def _ts_ref_name(ref: str) -> str:
    if ref.startswith(_LOCAL_REF_PREFIX):
        return ref[len(_LOCAL_REF_PREFIX) :]
    defs_prefix = "#/$defs/"
    if ref.startswith(defs_prefix):
        name = ref[len(defs_prefix) :]
        if name == "__document":
            return "ModelRoutingDocument"
        return "ModelRouting" + _pascal_case(name)
    raise ValueError(f"unsupported contract reference {ref!r}")


def _typescript_type(schema: Any) -> str:
    if not isinstance(schema, dict) or not schema:
        return "unknown"
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return _ts_ref_name(ref)
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in enum)
    variants = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(variants, list):
        return " | ".join(f"({_typescript_type(item)})" for item in variants)
    kind = schema.get("type")
    if isinstance(kind, list):
        return " | ".join(_typescript_type(dict(schema, type=variant)) for variant in kind)
    if kind == "string":
        return "string"
    if kind in {"integer", "number"}:
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "null":
        return "null"
    if kind == "array":
        return f"ReadonlyArray<{_typescript_type(schema.get('items', {}))}>"
    if kind == "object" or "properties" in schema:
        properties = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        members = []
        for name, child in properties.items():
            optional = "" if name in required else "?"
            members.append(
                f"  {json.dumps(str(name))}{optional}: {_typescript_type(child)};"
            )
        additional = schema.get("additionalProperties")
        if additional is True:
            members.append("  [key: string]: unknown;")
        elif isinstance(additional, dict):
            members.append(f"  [key: string]: {_typescript_type(additional)};")
        return "{\n" + "\n".join(members) + "\n}"
    return "unknown"


def render_typescript(document: dict[str, Any]) -> str:
    schemas = _rewritten_schemas(document)
    external_defs = _external_defs(document)
    lines = [
        "/* AUTO-GENERATED by scripts/gen_api_v1_model_routing_contract.py.",
        " * Canonical sources: contracts/api_v1_model_routing.yaml and",
        " * contracts/model_routing_v2.schema.json and provider_offering_recipe_v1.schema.json.",
        " * DO NOT EDIT BY HAND.",
        " *",
        " * Type-level mirror only; the server-side generated module is the",
        " * runtime validation authority. Conditional keywords (allOf/if/then/",
        " * else/not) and uniqueness bounds are enforced there, not here.",
        " */",
        "",
        f"export const API_V1_MODEL_ROUTING_CONTRACT_ID = "
        f"{json.dumps(CONTRACT_ID)} as const;",
        "",
    ]
    for name, schema in external_defs.items():
        if name == "__document":
            lines.append(
                f"export type ModelRoutingDocument = {_typescript_type(schema)};"
            )
        else:
            lines.append(
                f"export type ModelRouting{_pascal_case(name)} = "
                f"{_typescript_type(schema)};"
            )
        lines.append("")
    for name, schema in schemas.items():
        if schema == {"$ref": "#/$defs/__document"}:
            # Already emitted above as ModelRoutingDocument; skip the alias
            # to avoid a duplicate identifier in the TS module.
            continue
        lines.append(f"export type {name} = {_typescript_type(schema)};")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    document = _load_contract()
    expected = {
        PYTHON_OUTPUT: render_python(document),
        TYPESCRIPT_OUTPUT: render_typescript(document),
    }
    if args.check:
        stale = []
        for path, content in expected.items():
            try:
                with open(path, encoding="utf-8") as handle:
                    actual = handle.read()
            except FileNotFoundError:
                actual = ""
            if actual != content:
                stale.append(os.path.relpath(path, ROOT))
        if stale:
            print(
                "Generated API v1 model-routing contracts are stale: "
                + ", ".join(stale),
                file=sys.stderr,
            )
            return 1
        print(
            "OK: API v1 model-routing contract is current "
            f"({len(document['components']['schemas'])} schemas, "
            f"{len(document['_operations'])} operations)"
        )
        return 0
    for path, content in expected.items():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        print(os.path.relpath(path, ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
