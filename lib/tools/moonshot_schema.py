"""Moonshot-flavoured JSON Schema wire projection.

Responsibility: turn a valid, canonical tool-argument schema into the small
MFJS subset accepted by Kimi, and validate that final wire copy.  The caller
must retain the canonical schema as the execution-time authority: projection
deliberately removes constraints that MFJS cannot express, so it may only be
used for model-facing tool descriptions.

Entry points are :func:`project_mfjs_schema` and :func:`mfjs_schema_error`.
This module is pure and depends only on the Python standard library.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from typing import Any


_MFJS_TYPES = frozenset({
    'array', 'boolean', 'integer', 'null', 'number', 'object', 'string',
})
_MFJS_KEYS = frozenset({
    '$defs', '$ref', 'additionalProperties', 'anyOf', 'default',
    'description', 'enum', 'items', 'properties', 'required', 'type',
})


def _pointer_escape(value: str) -> str:
    return value.replace('~', '~0').replace('/', '~1')


def _pointer_unescape(value: str) -> str:
    return value.replace('~1', '/').replace('~0', '~')


def _enum_kind(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ''
    if isinstance(value, str):
        return 'string'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float) and math.isfinite(value):
        return 'number'
    return ''


def _valid_mfjs_enum(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    kinds = {_enum_kind(item) for item in value}
    return '' not in kinds and len(kinds) == 1


def _root_definitions(
    schema: Mapping[str, Any], *, path: str,
) -> tuple[dict[str, Any], dict[str, str], list[str], str]:
    """Combine root ``$defs`` and legacy ``definitions`` without collisions."""
    combined: dict[str, Any] = {}
    aliases: dict[str, str] = {}
    repairs: list[str] = []

    modern = schema.get('$defs')
    if modern is not None:
        if not isinstance(modern, Mapping):
            repairs.append(f'{path}.$defs')
        else:
            for name, child in modern.items():
                if not isinstance(name, str):
                    return {}, {}, repairs, f'{path}.$defs names must be strings'
                combined[name] = child

    legacy = schema.get('definitions')
    if legacy is None:
        return combined, aliases, repairs, ''
    if not isinstance(legacy, Mapping):
        repairs.append(f'{path}.definitions')
        return combined, aliases, repairs, ''

    repairs.append(f'{path}.definitions')
    for name, child in legacy.items():
        if not isinstance(name, str):
            return {}, {}, repairs, f'{path}.definitions names must be strings'
        target = name
        if target in combined and combined[target] != child:
            stem = f'legacy_{name}'
            target = stem
            suffix = 2
            while target in combined:
                target = f'{stem}_{suffix}'
                suffix += 1
        combined.setdefault(target, child)
        aliases[f'#/definitions/{_pointer_escape(name)}'] = (
            f'#/$defs/{_pointer_escape(target)}'
        )
    return combined, aliases, repairs, ''


def _project_ref(
    value: Any,
    *,
    aliases: Mapping[str, str],
    root_definition_names: frozenset[str],
) -> tuple[str, bool]:
    if not isinstance(value, str):
        return '', True
    rewritten = value
    for old_prefix, new_prefix in aliases.items():
        if value == old_prefix or value.startswith(old_prefix + '/'):
            rewritten = new_prefix + value[len(old_prefix):]
            break
    if rewritten == '#':
        return rewritten, rewritten != value
    prefix = '#/$defs/'
    if not rewritten.startswith(prefix):
        return '', True
    first_segment = rewritten[len(prefix):].split('/', 1)[0]
    if _pointer_unescape(first_segment) not in root_definition_names:
        return '', True
    return rewritten, rewritten != value


def _project_node(
    schema: Any,
    *,
    path: str,
    root: bool,
    root_definitions: Mapping[str, Any],
    aliases: Mapping[str, str],
    root_definition_names: frozenset[str],
) -> tuple[Mapping[str, Any], list[str], str]:
    if isinstance(schema, bool):
        # MFJS has no boolean-schema form. Both values become a sound wire
        # relaxation; the canonical validator still enforces ``false``.
        return {}, [path], ''
    if not isinstance(schema, Mapping):
        return {}, [], f'{path} must be an object schema'

    out: dict[str, Any] = {}
    repairs: list[str] = []
    definitions_emitted = False
    saw_pattern_properties = False
    saw_tuple_items = False

    def project_child(child: Any, child_path: str):
        return _project_node(
            child,
            path=child_path,
            root=False,
            root_definitions=root_definitions,
            aliases=aliases,
            root_definition_names=root_definition_names,
        )

    for key, value in schema.items():
        key_path = f'{path}.{key}'
        if key == 'definitions':
            if not root:
                repairs.append(key_path)
                continue
            if isinstance(value, Mapping) and not definitions_emitted:
                projected_defs: dict[str, Any] = {}
                for name, child in root_definitions.items():
                    projected, child_repairs, error = project_child(
                        child, f'{path}.$defs.{name}')
                    if error:
                        return schema, repairs, error
                    projected_defs[name] = projected
                    repairs.extend(child_repairs)
                out['$defs'] = projected_defs
                definitions_emitted = True
            continue
        if key == '$defs':
            if not root:
                repairs.append(key_path)
                continue
            if definitions_emitted:
                continue
            projected_defs = {}
            for name, child in root_definitions.items():
                projected, child_repairs, error = project_child(
                    child, f'{path}.$defs.{name}')
                if error:
                    return schema, repairs, error
                projected_defs[name] = projected
                repairs.extend(child_repairs)
            if projected_defs or isinstance(value, Mapping):
                out['$defs'] = projected_defs
            definitions_emitted = True
            if not isinstance(value, Mapping):
                repairs.append(key_path)
            continue
        if key == 'patternProperties':
            saw_pattern_properties = True
            repairs.append(key_path)
            continue
        if key == 'prefixItems' or (
                key == 'items' and isinstance(value, list)):
            saw_tuple_items = True
            repairs.append(key_path)
            continue
        if key not in _MFJS_KEYS:
            repairs.append(key_path)
            continue

        if key == '$ref':
            projected_ref, changed = _project_ref(
                value,
                aliases=aliases,
                root_definition_names=root_definition_names,
            )
            if projected_ref:
                out[key] = projected_ref
            if changed:
                repairs.append(key_path)
            continue
        if key == 'description':
            if isinstance(value, str):
                out[key] = value
            else:
                repairs.append(key_path)
            continue
        if key == 'default':
            out[key] = copy.deepcopy(value)
            continue
        if key == 'enum':
            enum_value = list(value) if isinstance(value, tuple) else value
            if _valid_mfjs_enum(enum_value):
                out[key] = copy.deepcopy(enum_value)
                if enum_value is not value:
                    repairs.append(key_path)
            else:
                repairs.append(key_path)
            continue
        if key == 'type':
            out[key] = copy.deepcopy(value)
            continue
        if key == 'required':
            if isinstance(value, tuple):
                out[key] = list(value)
                repairs.append(key_path)
            else:
                out[key] = copy.deepcopy(value)
            continue
        if key == 'properties':
            if not isinstance(value, Mapping):
                return schema, repairs, f'{key_path} must be an object'
            projected_properties: dict[str, Any] = {}
            for name, child in value.items():
                if not isinstance(name, str):
                    return schema, repairs, f'{key_path} names must be strings'
                projected, child_repairs, error = project_child(
                    child, f'{key_path}.{name}')
                if error:
                    return schema, repairs, error
                projected_properties[name] = projected
                repairs.extend(child_repairs)
            out[key] = projected_properties
            continue
        if key == 'anyOf':
            if not isinstance(value, list) or not value:
                return schema, repairs, f'{key_path} must be a non-empty array'
            projected_branches: list[Mapping[str, Any]] = []
            unconstrained = False
            for index, child in enumerate(value):
                if child is False:
                    repairs.append(f'{key_path}[{index}]')
                    continue
                if child is True:
                    repairs.append(f'{key_path}[{index}]')
                    unconstrained = True
                    break
                projected, child_repairs, error = project_child(
                    child, f'{key_path}[{index}]')
                if error:
                    return schema, repairs, error
                projected_branches.append(projected)
                repairs.extend(child_repairs)
                if not projected:
                    unconstrained = True
                    break
            if unconstrained or not projected_branches:
                repairs.append(key_path)
            else:
                out[key] = projected_branches
            continue
        if key == 'additionalProperties':
            if isinstance(value, bool):
                out[key] = value
            elif isinstance(value, Mapping):
                projected, child_repairs, error = project_child(
                    value, key_path)
                if error:
                    return schema, repairs, error
                out[key] = projected
                repairs.extend(child_repairs)
            else:
                out[key] = True
                repairs.append(key_path)
            continue
        if key == 'items':
            if isinstance(value, Mapping):
                projected, child_repairs, error = project_child(
                    value, key_path)
                if error:
                    return schema, repairs, error
                out[key] = projected
                repairs.extend(child_repairs)
            else:
                out[key] = {}
                repairs.append(key_path)

    if root and root_definitions and not definitions_emitted:
        projected_defs = {}
        for name, child in root_definitions.items():
            projected, child_repairs, error = project_child(
                child, f'{path}.$defs.{name}')
            if error:
                return schema, repairs, error
            projected_defs[name] = projected
            repairs.extend(child_repairs)
        out['$defs'] = projected_defs

    if saw_pattern_properties:
        # A pattern-matched property is not an "additional" property in JSON
        # Schema. Once patterns are removed, retaining false or a restrictive
        # schema here could reject a canonical value, so open this lane.
        if out.get('additionalProperties') is not True:
            out['additionalProperties'] = True
            repairs.append(f'{path}.additionalProperties')
    if saw_tuple_items:
        # Applying the tail ``items`` schema to prefix positions could narrow
        # a tuple. An unconstrained homogeneous item schema is the safe MFJS
        # relaxation.
        out['items'] = {}

    required = out.get('required')
    if isinstance(required, (list, tuple)) \
            and all(isinstance(name, str) for name in required):
        properties = out.get('properties')
        projected_properties = dict(properties) \
            if isinstance(properties, Mapping) else {}
        missing = [name for name in required
                   if name not in projected_properties]
        if missing:
            for name in missing:
                projected_properties[name] = {}
            out['properties'] = projected_properties
            repairs.append(f'{path}.required')

    if not repairs and out == schema:
        return schema, repairs, ''
    return out, repairs, ''


def project_mfjs_schema(
    schema: Mapping[str, Any], *, path: str = '$',
) -> tuple[Mapping[str, Any], tuple[str, ...], str]:
    """Return a copy projected to documented MFJS, repair paths, and error."""
    if not isinstance(schema, Mapping):
        return schema, (), f'{path} must be an object schema'
    definitions, aliases, repairs, error = _root_definitions(
        schema, path=path)
    if error:
        return schema, tuple(repairs), error
    projected, child_repairs, error = _project_node(
        schema,
        path=path,
        root=True,
        root_definitions=definitions,
        aliases=aliases,
        root_definition_names=frozenset(definitions),
    )
    repairs.extend(child_repairs)
    return projected, tuple(dict.fromkeys(repairs)), error


def _schema_error(
    schema: Any,
    *,
    path: str,
    root: bool,
    root_definition_names: frozenset[str],
) -> str:
    if not isinstance(schema, Mapping):
        return f'{path} must be an object schema'
    unsupported = [str(key) for key in schema if key not in _MFJS_KEYS]
    if unsupported:
        return f'{path} has unsupported MFJS keyword: {unsupported[0]}'
    if root:
        if schema.get('type') != 'object':
            return f'{path}.type is required and must be object for Kimi'
        if 'anyOf' in schema:
            return f'{path}.anyOf cannot remain at a tool-parameter root'
    elif '$defs' in schema:
        return f'{path}.$defs is only supported at the schema root'

    raw_type = schema.get('type')
    if raw_type is not None \
            and (not isinstance(raw_type, str) or raw_type not in _MFJS_TYPES):
        return f'{path}.type must be one MFJS type string'
    if 'type' in schema and 'anyOf' in schema:
        return f'{path}.type must be declared inside anyOf branches'
    any_of_value = schema.get('anyOf')
    if isinstance(any_of_value, list):
        # Mirror the vendor rule: an object applicator defined on BOTH the
        # parent and inside anyOf branches is a hard 400 ("conflicting
        # keywords found in anyOf with parent"). One side alone is legal.
        for key in ('properties', 'required', 'additionalProperties'):
            if key in schema and any(
                    isinstance(branch, Mapping) and key in branch
                    for branch in any_of_value):
                return (f'{path}.{key} conflicts with {key} inside anyOf '
                        f'branches; declare it on one side only')
    if 'description' in schema and not isinstance(
            schema.get('description'), str):
        return f'{path}.description must be a string'
    if 'enum' in schema and not _valid_mfjs_enum(schema.get('enum')):
        return f'{path}.enum must contain one supported scalar type'

    ref = schema.get('$ref')
    if ref is not None:
        projected_ref, changed = _project_ref(
            ref, aliases={}, root_definition_names=root_definition_names)
        if changed or not projected_ref:
            return f'{path}.$ref must target # or a root $defs entry'

    required = schema.get('required')
    if required is not None:
        if not isinstance(required, list) \
                or any(not isinstance(name, str) for name in required):
            return f'{path}.required must be an array of strings'
        properties = schema.get('properties')
        names = set(properties) if isinstance(properties, Mapping) else set()
        missing = [name for name in required if name not in names]
        if missing:
            return f'{path}.required has undeclared property: {missing[0]}'

    properties = schema.get('properties')
    if properties is not None:
        if not isinstance(properties, Mapping):
            return f'{path}.properties must be an object'
        for name, child in properties.items():
            if not isinstance(name, str):
                return f'{path}.properties names must be strings'
            error = _schema_error(
                child,
                path=f'{path}.properties.{name}',
                root=False,
                root_definition_names=root_definition_names,
            )
            if error:
                return error

    any_of = schema.get('anyOf')
    if any_of is not None:
        if not isinstance(any_of, list) or not any_of:
            return f'{path}.anyOf must be a non-empty schema array'
        for index, child in enumerate(any_of):
            error = _schema_error(
                child,
                path=f'{path}.anyOf[{index}]',
                root=False,
                root_definition_names=root_definition_names,
            )
            if error:
                return error

    additional = schema.get('additionalProperties')
    if additional is not None and not isinstance(additional, bool):
        error = _schema_error(
            additional,
            path=f'{path}.additionalProperties',
            root=False,
            root_definition_names=root_definition_names,
        )
        if error:
            return error
    items = schema.get('items')
    if items is not None:
        error = _schema_error(
            items,
            path=f'{path}.items',
            root=False,
            root_definition_names=root_definition_names,
        )
        if error:
            return error

    definitions = schema.get('$defs')
    if definitions is not None:
        if not isinstance(definitions, Mapping):
            return f'{path}.$defs must be an object'
        for name, child in definitions.items():
            if not isinstance(name, str):
                return f'{path}.$defs names must be strings'
            error = _schema_error(
                child,
                path=f'{path}.$defs.{name}',
                root=False,
                root_definition_names=root_definition_names,
            )
            if error:
                return error
    return ''


def mfjs_schema_error(
    schema: Mapping[str, Any], *, path: str = '$', root: bool = True,
) -> str:
    """Return the first documented MFJS violation, or an empty string."""
    definitions = schema.get('$defs') if isinstance(schema, Mapping) else None
    names = frozenset(definitions) if isinstance(definitions, Mapping) else frozenset()
    return _schema_error(
        schema,
        path=path,
        root=root,
        root_definition_names=names,
    )
