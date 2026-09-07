"""Compile generated conversation-sync schemas into success predicates.

Responsibility
--------------
Build one bounded set of allocation-light validators at process import.  The
predicates answer only whether a JSON value satisfies the generated schema;
the diagnostic validator remains the authority for public violation details.

Entry points
------------
``compile_success_predicates`` compiles every named schema exactly once.

Dependencies
------------
The caller supplies the generated schema mapping.  This module performs no
I/O, retains no request values, and implements only the schema keywords used
by the canonical conversation-sync contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import re
from typing import Any


SuccessPredicate = Callable[[Any], bool]
_SCHEMA_REFERENCE_PREFIX = "#/components/schemas/"


def _accept_all(_value: Any) -> bool:
    return True


def _reject_all(_value: Any) -> bool:
    return False


def json_values_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's ``True == 1`` coercion."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        return (
            isinstance(left, (int, float))
            and isinstance(right, (int, float))
            and left == right
        )
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, list) or isinstance(right, list):
        return (
            isinstance(left, list)
            and isinstance(right, list)
            and len(left) == len(right)
            and all(
                json_values_equal(left_item, right_item)
                for left_item, right_item in zip(left, right, strict=True)
            )
        )
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        return (
            isinstance(left, Mapping)
            and isinstance(right, Mapping)
            and len(left) == len(right)
            and all(
                field in right
                and json_values_equal(child, right[field])
                for field, child in left.items()
            )
        )
    return type(left) is type(right) and left == right


def _compile_json_value_match(expected: Any) -> SuccessPredicate:
    if isinstance(expected, bool):
        return lambda value: isinstance(value, bool) and value is expected
    if isinstance(expected, (int, float)):
        return lambda value: (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value == expected
        )
    if expected is None:
        return lambda value: value is None
    if isinstance(expected, str):
        return lambda value: isinstance(value, str) and value == expected
    return lambda value: json_values_equal(value, expected)


def _compile_json_choices(choices: list[Any]) -> SuccessPredicate:
    if all(isinstance(choice, str) for choice in choices):
        allowed_strings = frozenset(choices)
        return lambda value: (
            isinstance(value, str) and value in allowed_strings
        )
    if all(isinstance(choice, bool) for choice in choices):
        allowed_booleans = frozenset(choices)
        return lambda value: (
            isinstance(value, bool) and value in allowed_booleans
        )
    if all(
        isinstance(choice, (int, float)) and not isinstance(choice, bool)
        for choice in choices
    ):
        allowed_numbers = tuple(choices)
        return lambda value: (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value in allowed_numbers
        )
    choice_predicates = tuple(
        _compile_json_value_match(choice) for choice in choices
    )
    return lambda value: any(check(value) for check in choice_predicates)


class _SuccessPredicateCompiler:
    """Compile the supported schema subset without retaining request data."""

    def __init__(self, schemas: Mapping[str, Any]) -> None:
        self._schemas = schemas
        self._compiled_nodes: dict[int, SuccessPredicate] = {}
        self._compiling_nodes: set[int] = set()
        self._root_predicates: dict[str, SuccessPredicate] = {}

    def compile_all(self) -> dict[str, SuccessPredicate]:
        for schema_name, schema in self._schemas.items():
            self._root_predicates[schema_name] = self._compile_node(schema)
        return dict(self._root_predicates)

    def _compile_node(self, schema_value: Any) -> SuccessPredicate:
        if not isinstance(schema_value, Mapping):
            return _accept_all
        schema_identity = id(schema_value)
        compiled = self._compiled_nodes.get(schema_identity)
        if compiled is not None:
            return compiled

        # Named-reference cycles are handled inside ``_compile_reference``.
        # Any direct object cycle is malformed generated input and fails closed.
        if schema_identity in self._compiling_nodes:
            return _reject_all
        self._compiling_nodes.add(schema_identity)
        try:
            compiled = self._compile_uncached(schema_value)
        finally:
            self._compiling_nodes.remove(schema_identity)
        self._compiled_nodes[schema_identity] = compiled
        return compiled

    def _compile_uncached(self, schema: Mapping[str, Any]) -> SuccessPredicate:
        reference = schema.get("$ref")
        if isinstance(reference, str):
            return self._compile_reference(reference)

        variants = schema.get("oneOf")
        if isinstance(variants, list):
            return self._compile_one_of(variants)
        variants = schema.get("anyOf")
        if isinstance(variants, list):
            return self._compile_any_of(variants)

        base_predicate = self._compile_typed_predicate(schema)
        has_constant = "const" in schema
        constant = schema.get("const")
        raw_choices = schema.get("enum")
        constant_predicate = (
            _compile_json_value_match(constant) if has_constant else None
        )
        choices_predicate = (
            _compile_json_choices(raw_choices)
            if isinstance(raw_choices, list)
            else None
        )
        if constant_predicate is None and choices_predicate is None:
            return base_predicate

        def validate_declared_value(
            value: Any,
            *,
            check_base: SuccessPredicate = base_predicate,
            check_constant: SuccessPredicate | None = constant_predicate,
            check_choices: SuccessPredicate | None = choices_predicate,
        ) -> bool:
            return (
                (check_constant is None or check_constant(value))
                and (check_choices is None or check_choices(value))
                and check_base(value)
            )

        return validate_declared_value

    def _compile_reference(self, reference: str) -> SuccessPredicate:
        if not reference.startswith(_SCHEMA_REFERENCE_PREFIX):
            return _reject_all
        target_name = reference[len(_SCHEMA_REFERENCE_PREFIX):]
        target_schema = self._schemas.get(target_name)
        if target_schema is None:
            return _reject_all
        target_identity = id(target_schema)
        if target_identity not in self._compiling_nodes:
            return self._compile_node(target_schema)

        def validate_recursive_reference(
            value: Any,
            *,
            schema_name: str = target_name,
        ) -> bool:
            target = self._root_predicates.get(schema_name)
            return target is not None and target(value)

        return validate_recursive_reference

    def _compile_one_of(self, variants: list[Any]) -> SuccessPredicate:
        checks = tuple(self._compile_node(item) for item in variants)

        def validate_one_of(
            value: Any,
            *,
            variant_checks: tuple[SuccessPredicate, ...] = checks,
        ) -> bool:
            matches = 0
            for check in variant_checks:
                if check(value):
                    matches += 1
                    if matches > 1:
                        return False
            return matches == 1

        return validate_one_of

    def _compile_any_of(self, variants: list[Any]) -> SuccessPredicate:
        checks = tuple(self._compile_node(item) for item in variants)

        def validate_any_of(
            value: Any,
            *,
            variant_checks: tuple[SuccessPredicate, ...] = checks,
        ) -> bool:
            return any(check(value) for check in variant_checks)

        return validate_any_of

    def _compile_typed_predicate(
        self, schema: Mapping[str, Any]
    ) -> SuccessPredicate:
        kind = schema.get("type")
        if kind == "null":
            return lambda value: value is None
        if kind == "string":
            return self._compile_string(schema)
        if kind in {"integer", "number"}:
            return self._compile_number(schema, integer=kind == "integer")
        if kind == "boolean":
            return lambda value: isinstance(value, bool)
        if kind == "array":
            return self._compile_array(schema)
        if kind == "object" or "properties" in schema:
            return self._compile_object(schema)
        return _accept_all

    @staticmethod
    def _compile_string(schema: Mapping[str, Any]) -> SuccessPredicate:
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        raw_pattern = schema.get("pattern")
        try:
            pattern = re.compile(raw_pattern) if isinstance(raw_pattern, str) else None
        except re.error:
            return _reject_all

        def validate_string(
            value: Any,
            *,
            minimum: Any = minimum_length,
            maximum: Any = maximum_length,
            declared_pattern: re.Pattern[str] | None = pattern,
        ) -> bool:
            return (
                isinstance(value, str)
                and (not isinstance(minimum, int) or len(value) >= minimum)
                and (not isinstance(maximum, int) or len(value) <= maximum)
                and (
                    declared_pattern is None
                    or declared_pattern.search(value) is not None
                )
            )

        return validate_string

    @staticmethod
    def _compile_number(
        schema: Mapping[str, Any], *, integer: bool
    ) -> SuccessPredicate:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")

        def validate_number(
            value: Any,
            *,
            require_integer: bool = integer,
            declared_minimum: Any = minimum,
            declared_maximum: Any = maximum,
        ) -> bool:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (not require_integer or isinstance(value, int))
                and (
                    not isinstance(declared_minimum, (int, float))
                    or not value < declared_minimum
                )
                and (
                    not isinstance(declared_maximum, (int, float))
                    or not value > declared_maximum
                )
            )

        return validate_number

    def _compile_array(self, schema: Mapping[str, Any]) -> SuccessPredicate:
        item_predicate = self._compile_node(schema.get("items"))
        maximum_items = schema.get("maxItems")

        def validate_array(
            value: Any,
            *,
            check_item: SuccessPredicate = item_predicate,
            maximum: Any = maximum_items,
        ) -> bool:
            return (
                isinstance(value, list)
                and (not isinstance(maximum, int) or len(value) <= maximum)
                and all(check_item(item) for item in value)
            )

        return validate_array

    def _compile_object(self, schema: Mapping[str, Any]) -> SuccessPredicate:
        raw_properties = schema.get("properties")
        raw_properties = (
            raw_properties if isinstance(raw_properties, Mapping) else {}
        )
        property_predicates = {
            field: self._compile_node(child)
            for field, child in raw_properties.items()
        }
        required_fields = tuple(
            field
            for field in schema.get("required") or []
            if isinstance(field, str)
        )
        minimum_properties = schema.get("minProperties")
        maximum_properties = schema.get("maxProperties")
        raw_property_names = schema.get("propertyNames")
        property_name_predicate = (
            self._compile_node(raw_property_names)
            if isinstance(raw_property_names, Mapping)
            else None
        )
        additional_properties = schema.get("additionalProperties")
        reject_additional = additional_properties is False
        additional_predicate = (
            self._compile_node(additional_properties)
            if isinstance(additional_properties, Mapping)
            else None
        )

        def validate_object(
            value: Any,
            *,
            declared_properties: dict[Any, SuccessPredicate] = property_predicates,
            required: tuple[str, ...] = required_fields,
            minimum: Any = minimum_properties,
            maximum: Any = maximum_properties,
            check_property_name: SuccessPredicate | None = property_name_predicate,
            deny_additional: bool = reject_additional,
            check_additional: SuccessPredicate | None = additional_predicate,
        ) -> bool:
            if not isinstance(value, Mapping):
                return False
            property_count = len(value)
            if isinstance(minimum, int) and property_count < minimum:
                return False
            if isinstance(maximum, int) and property_count > maximum:
                return False
            if check_property_name is not None and not all(
                check_property_name(field) for field in value
            ):
                return False
            if any(field not in value for field in required):
                return False
            for field, child_value in value.items():
                child_predicate = declared_properties.get(field)
                if child_predicate is not None:
                    if not child_predicate(child_value):
                        return False
                elif deny_additional:
                    return False
                elif check_additional is not None and not check_additional(
                    child_value
                ):
                    return False
            return True

        return validate_object


def compile_success_predicates(
    schemas: Mapping[str, Any],
) -> dict[str, SuccessPredicate]:
    """Return one immutable-by-convention predicate table for named schemas."""
    return _SuccessPredicateCompiler(schemas).compile_all()


__all__ = [
    "SuccessPredicate",
    "compile_success_predicates",
    "json_values_equal",
]
