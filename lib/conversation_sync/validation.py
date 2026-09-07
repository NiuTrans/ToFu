"""Runtime decoding against the generated conversation-sync schemas."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from lib.conversation_sync.compiled_validation import (
    compile_success_predicates,
    json_values_equal,
)
from lib.conversation_sync.generated_contract import OPENAPI_SCHEMAS


_SUCCESS_PREDICATES = compile_success_predicates(OPENAPI_SCHEMAS)


class ContractViolation(ValueError):
    def __init__(self, schema_name: str, violations: list[str]):
        self.schema_name = schema_name
        self.violations = tuple(violations)
        super().__init__(
            f"{schema_name} contract violation: " + "; ".join(violations[:3])
        )


def _validate(schema_value: Any, value: Any, path: str) -> list[str]:
    schema = schema_value if isinstance(schema_value, Mapping) else {}
    ref = schema.get("$ref")
    if isinstance(ref, str):
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            return [f"{path}: unsupported schema reference"]
        target = OPENAPI_SCHEMAS.get(ref[len(prefix):])
        return (
            _validate(target, value, path)
            if target is not None
            else [f"{path}: unknown schema reference"]
        )
    variants = schema.get("oneOf")
    if isinstance(variants, list):
        matches = sum(not _validate(item, value, path) for item in variants)
        return [] if matches == 1 else [f"{path}: expected exactly one variant"]
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        return (
            []
            if any(not _validate(item, value, path) for item in variants)
            else [f"{path}: expected a declared variant"]
        )
    if "const" in schema and not json_values_equal(value, schema["const"]):
        return [f"{path}: unexpected constant value"]
    choices = schema.get("enum")
    if isinstance(choices, list) and not any(
        json_values_equal(value, choice) for choice in choices
    ):
        return [f"{path}: value is outside the declared vocabulary"]
    kind = schema.get("type")
    if kind == "null":
        return [] if value is None else [f"{path}: expected null"]
    if kind == "string":
        if not isinstance(value, str):
            return [f"{path}: expected string"]
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            return [f"{path}: string is too short"]
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
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
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            return [f"{path}: number is below minimum"]
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            return [f"{path}: number is above maximum"]
        return []
    if kind == "boolean":
        return [] if isinstance(value, bool) else [f"{path}: expected boolean"]
    if kind == "array":
        if not isinstance(value, list):
            return [f"{path}: expected array"]
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            return [f"{path}: array is too large"]
        errors: list[str] = []
        for index, item in enumerate(value):
            errors.extend(_validate(schema.get("items", {}), item, f"{path}[{index}]"))
        return errors
    if kind == "object" or "properties" in schema:
        if not isinstance(value, Mapping):
            return [f"{path}: expected object"]
        properties = schema.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        errors = []
        if isinstance(schema.get("minProperties"), int) and len(value) < schema["minProperties"]:
            errors.append(f"{path}: object has too few properties")
        if isinstance(schema.get("maxProperties"), int) and len(value) > schema["maxProperties"]:
            errors.append(f"{path}: object has too many properties")
        property_names = schema.get("propertyNames")
        if isinstance(property_names, Mapping):
            for field in value:
                errors.extend(_validate(property_names, field, f"{path}.{field}"))
        for field in schema.get("required") or []:
            if isinstance(field, str) and field not in value:
                errors.append(f"{path}.{field}: required")
        for field, child in properties.items():
            if field in value:
                errors.extend(_validate(child, value[field], f"{path}.{field}"))
        if schema.get("additionalProperties") is False:
            errors.extend(
                f"{path}.{field}: undeclared field"
                for field in value if field not in properties
            )
        elif isinstance(schema.get("additionalProperties"), Mapping):
            additional_schema = schema["additionalProperties"]
            for field, child_value in value.items():
                if field not in properties:
                    errors.extend(
                        _validate(additional_schema, child_value, f"{path}.{field}")
                    )
        return errors
    return []


def decode(schema_name: str, value: Any) -> Any:
    schema = OPENAPI_SCHEMAS.get(schema_name)
    if schema is None:
        raise ContractViolation(schema_name, ["schema is not registered"])
    if _SUCCESS_PREDICATES[schema_name](value):
        return value
    violations = _validate(schema, value, "$")
    if violations:
        raise ContractViolation(schema_name, violations)
    return value


__all__ = ["ContractViolation", "decode"]
