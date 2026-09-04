"""Local multi-provider Tool Search and stable execution contracts.

This module is intentionally pure: it owns the stable gateway schemas,
provider strategy, catalog search, and conservative call normalization.  The
stateful handler that feeds normalized calls through the ordinary approval /
hooks / executor pipeline lives in ``lib.tasks_pkg.handlers.tool_gateway``.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import urlparse

from lib.log import get_logger
from lib.tools.contracts import (
    ToolContractError,
    validate_tool_arguments_from_documents,
)
from lib.tools.discovery_vocabulary import CAPABILITY_SEARCH_CONCEPTS
from lib.tools.resource_policy import tool_search_term_cache_capacity


logger = get_logger(__name__)

SEARCH_TOOLS_NAME = 'search_tools'
EXECUTE_TOOLS_NAME = 'execute_tools'
GATEWAY_TOOL_NAMES = frozenset({SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME})

LOCAL_TOOL_SEARCH_MIN_FUNCTIONS = 12
LOCAL_TOOL_SEARCH_DEFAULT_LIMIT = 8
LOCAL_TOOL_SEARCH_MAX_LIMIT = 20
LOCAL_TOOL_SEARCH_MAX_QUERY_CHARS = 512
LOCAL_TOOL_SEARCH_MAX_NAMESPACE_CHARS = 128
LOCAL_TOOL_SEARCH_MAX_CURSOR_CHARS = 128
# LRU keys retain their original strings. Long catalog descriptions still
# receive full-fidelity tokenization, but bypass this process-wide cache so a
# plugin or request cannot turn an item-count bound into an arbitrary byte
# residency budget.
LOCAL_TOOL_SEARCH_TERM_CACHE_MAX_INPUT_CHARS = 1024
# Model-facing retrieval output is a directory, not a dump of every owning
# contract. Keep it bounded even when a plugin contributes a very wide schema.
LOCAL_TOOL_SEARCH_MAX_RESULT_CHARS = 24_000
# Authoring contract for the full search/execute gateway pair. The pair is
# never compacted at runtime (rewriting its bytes breaks the provider
# prompt-cache prefix); this target is enforced by tests and drift past it
# only logs a warning. Measured at ~522-544 tokens across supported
# tokenizers.
LOCAL_GATEWAY_MAX_TOKENS = 600
CODE_CORE_DIRECT_TOOL_NAMES = frozenset({
    'read_files', 'grep_search', 'find_files', 'edit_file', 'run_command',
})

ToolIsolationReporter = Callable[[dict[str, Any]], None]


def search_tools_schema() -> dict[str, Any]:
    return {
        'type': 'function',
        'function': {
            'name': SEARCH_TOOLS_NAME,
            'description': (
                'Find task-available tools absent from this request. This '
                "only finds tools; call execute_tools with a result's exact "
                'name and arguments_schema.'),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string', 'minLength': 1,
                        'maxLength': LOCAL_TOOL_SEARCH_MAX_QUERY_CHARS,
                    },
                    'namespace': {
                        'type': 'string',
                        'maxLength': LOCAL_TOOL_SEARCH_MAX_NAMESPACE_CHARS,
                    },
                    'limit': {'type': 'integer', 'minimum': 1,
                              'maximum': LOCAL_TOOL_SEARCH_MAX_LIMIT,
                              'default': LOCAL_TOOL_SEARCH_DEFAULT_LIMIT},
                    'cursor': {
                        'type': 'string',
                        'maxLength': LOCAL_TOOL_SEARCH_MAX_CURSOR_CHARS,
                    },
                },
                'required': ['query'],
                'additionalProperties': False,
            },
        },
    }


def execute_tools_schema(*, include_program: bool = True,
                         ptc_note: str = '') -> dict[str, Any]:
    """Return the ``execute_tools`` gateway schema.

    Output is byte-identical to the historical shape and exposes the full
    ToolScript surface to every model.  ``include_program=False`` is the
    explicit ``TOFU_PTC_TIER=batch`` operator/benchmark override shape (the
    model batches parallel ``calls`` instead of authoring ToolScript), and
    ``ptc_note`` (the bounded local routing contract) is spliced into the
    description so the policy travels with the schema the model sees.  The
    schema is never compacted at runtime: rewriting description bytes between
    rounds breaks the provider prompt-cache prefix.
    """
    description = (
            'Run task-available tools with calls or program; search_tools is '
            'optional when the exact name and schema are already known. Use '
            'calls for ordinary or independent work; program only for '
            'data-dependent calls. Do not wrap a call you also invoke '
            'directly in the same response; choose one lane per action. '
            'ToolScript is bounded, not JavaScript. '
            'ToolScript supports '
            'let/const, return, if/else, for..of, while, arrays, objects, '
            'lambdas, operators; catalog.search; tools.call, tools.callMany, '
            'tools.parallel; '
            'array map/filter/reduce/slice/join/push/includes; string '
            'includes/startsWith/endsWith/slice/split/trim/case conversion; '
            'JSON.parse/stringify; Object.keys/values. Calls are synchronous; '
            'no await, destructuring, template literals, optional chaining, '
            'try/catch, async, or class; no eval, import, filesystem, process, or '
            'direct network except tools.*.')
    if not include_program:
        description = (
            'Run task-available tools; search_tools is optional when the exact '
            'name and schema are already known. Provide calls '
            'for one or more independent tool invocations; prefer a single '
            'batched call with execution=parallel over issuing tools one per '
            'turn. Do not wrap a call you also invoke directly in the '
            'same response; choose one lane per action.')
    if ptc_note:
        description = f'{description} {ptc_note}'
    calls_property: dict[str, Any] = {
        'type': 'array',
        'maxItems': 16,
        'description': (
            'Tool calls. Use a task-executable exact name and arguments that '
            'match its schema; search_tools is optional.'),
    }
    calls_property['items'] = {
        'type': 'object',
        'properties': {
            'name': {'type': 'string'},
            'arguments': {'type': 'object'},
            'call_id': {'type': 'string'},
        },
        'required': ['name', 'arguments'],
        'additionalProperties': False,
    }
    properties: dict[str, Any] = {
        'calls': calls_property,
        'execution': {
            'type': 'string',
            'enum': ['auto', 'sequential', 'parallel'],
            'default': 'auto',
        },
    }
    if include_program:
        properties['program'] = {
            'type': 'string',
            'description': (
                'Bounded ToolScript (not JavaScript) for data-dependent '
                'search, synchronous calls, filtering, parsing, and compact '
                'aggregation. Use only the grammar and built-ins listed in '
                'this tool description.'),
        }
    schema = {
        'type': 'function',
        'function': {
            'name': EXECUTE_TOOLS_NAME,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': properties,
                'additionalProperties': False,
            },
        },
    }
    return schema


def _stable_local_execute_tools_schema(*, tier: str = 'program') -> dict[str, Any]:
    """Return the cache-stable local execution gateway for one task tier.

    Local Tool Search exists before the per-round PTC policy resolves.  Its
    ordinary gateway and the later PTC projection must therefore use the same
    bytes for the default program tier; otherwise merely observing (or no
    longer observing) a read fan-out rewrites the hoisted tools prefix.  The
    guidance is conditional on PTC and remains true while the generic gateway
    is active without a PTC latch.
    """
    from lib.tools.programmatic import local_ptc_guidance

    normalized_tier = (
        'batch' if str(tier or '').strip().lower() == 'batch' else 'program')
    return execute_tools_schema(
        include_program=normalized_tier != 'batch',
        ptc_note=local_ptc_guidance(normalized_tier, ()),
    )


def gateway_tool_schemas(*, include_search: bool = True,
                         include_execute: bool | None = None
                         ) -> list[dict[str, Any]]:
    """Return model-visible local gateway schemas.

    Local Tool Search exposes a fixed discovery/execution pair.  The real
    catalog stays server-owned, so searching and executing do not mutate the
    provider's tools array between model rounds.
    """
    if include_execute is None:
        include_execute = include_search
    out = []
    if include_search:
        out.append(search_tools_schema())
    if include_execute:
        out.append(_stable_local_execute_tools_schema())
    return out


def tool_schema_tokens(tools: Any, *, model: str = '') -> int:
    """Count canonical schema JSON with the repository token authority."""
    payload = json.dumps(
        list(tools or ()), ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str)
    try:
        from lib.token_counter import count_text
        return max(0, int(count_text(payload, model=model or '')))
    except Exception as exc:
        logger.debug('[ToolGateway] schema token fallback: %s', exc)
        return max(1, (len(payload) + 3) // 4) if payload else 0


def tool_schema_fingerprint(tools: Any) -> str:
    """Return a bounded digest of the exact final tool projection shape.

    Insertion order is preserved deliberately: description changes, parameter
    key reordering, wrapper changes, and cache-control metadata must all change
    this diagnostic.  The full schema stays request-local; only its SHA-256 is
    persisted by Request Inspector.
    """
    payload = json.dumps(
        list(tools or ()), ensure_ascii=False, sort_keys=False,
        separators=(',', ':'), default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


_SCHEMA_ANNOTATION_KEYS = frozenset({
    '$comment', 'description', 'example', 'examples', 'title',
})

_SCHEMA_MAP_OF_SCHEMAS_KEYS = frozenset({
    '$defs', 'definitions', 'dependentSchemas', 'patternProperties',
    'properties',
})
_SCHEMA_LIST_OF_SCHEMAS_KEYS = frozenset({
    'allOf', 'anyOf', 'oneOf', 'prefixItems',
})
_SCHEMA_SINGLE_SCHEMA_KEYS = frozenset({
    'additionalProperties', 'contains', 'contentSchema', 'else', 'if',
    'items', 'not', 'propertyNames', 'then', 'unevaluatedItems',
    'unevaluatedProperties',
})
_JSON_SCHEMA_INSTANCE_TYPES = frozenset({
    'array', 'boolean', 'integer', 'null', 'number', 'object', 'string',
})


def _schema_type_names(value: Any, *, path: str) -> tuple[tuple[str, ...], str]:
    """Return one validated JSON-Schema type union in declaration order."""
    if isinstance(value, str):
        candidates = (value,)
    elif isinstance(value, (list, tuple)) and value:
        candidates = tuple(value)
    else:
        return (), f'{path}.type must be a type name or non-empty type array'
    if any(not isinstance(name, str) for name in candidates):
        return (), f'{path}.type must contain only type names'
    invalid = [
        name for name in candidates if name not in _JSON_SCHEMA_INSTANCE_TYPES
    ]
    if invalid:
        return (), f'{path}.type contains unknown names: {", ".join(invalid)}'
    return tuple(dict.fromkeys(candidates)), ''


def _intersect_schema_types(
    parent_types: tuple[str, ...], branch_types: tuple[str, ...],
) -> tuple[str, ...]:
    """Intersect two JSON-Schema type unions, including integer ⊂ number."""
    parent = set(parent_types)
    branch = set(branch_types)
    result = [
        name for name in ('null', 'boolean', 'object', 'array', 'string')
        if name in parent and name in branch
    ]
    parent_accepts_number = 'number' in parent
    branch_accepts_number = 'number' in branch
    parent_accepts_integer = parent_accepts_number or 'integer' in parent
    branch_accepts_integer = branch_accepts_number or 'integer' in branch
    if parent_accepts_number and branch_accepts_number:
        result.append('number')
    elif parent_accepts_integer and branch_accepts_integer:
        result.append('integer')
    return tuple(result)


def _schema_allows_object(
    schema: Mapping[str, Any], *, path: str,
) -> tuple[bool, str]:
    """Return whether a schema can accept a JSON object."""
    if 'type' not in schema:
        return True, ''
    type_names, error = _schema_type_names(schema.get('type'), path=path)
    if error:
        return False, error
    return 'object' in type_names, ''


def _project_moonshot_root_object(
    schema: Mapping[str, Any], *, path: str,
) -> tuple[Mapping[str, Any], tuple[str, ...], str]:
    """Project a function-argument root into MFJS's mandatory object shape.

    Function arguments are JSON objects on every supported transport, but a
    full JSON Schema may express that object as a root ``anyOf``/``oneOf`` or
    ``allOf``. MFJS both requires ``parameters.type == 'object'`` and rejects a
    root ``type`` beside ``anyOf``. Those constraints make a strict root union
    unrepresentable. Preserve the canonical schema for execution validation
    and send a sound *relaxation* on the Kimi wire: the union of properties,
    plus only requirements that hold for every alternative. ``allOf`` keeps
    the union of its unconditional requirements. Any model call admitted by
    the canonical schema is therefore admitted by this projection; the
    request-owned ToolContract remains the final execution authority.
    """
    allows_object, error = _schema_allows_object(schema, path=path)
    if error:
        return schema, (), error
    if not allows_object:
        return schema, (), f'{path}.type must allow object for tool arguments'

    combinators = [
        key for key in ('anyOf', 'oneOf', 'allOf') if key in schema
    ]
    if len(combinators) > 1:
        # Multiple applicators are an intersection in full JSON Schema, but
        # MFJS cannot represent that root shape. Retaining the direct object
        # constraints and dropping every root applicator is a sound
        # relaxation; the canonical execution contract remains authoritative.
        projected = {'type': 'object'}
        projected.update({
            key: value for key, value in schema.items()
            if key != 'type' and key not in combinators
        })
        return projected, tuple(f'{path}.{key}' for key in combinators), ''

    if not combinators:
        if schema.get('type') == 'object':
            return schema, (), ''
        projected = {'type': 'object'}
        projected.update({key: value for key, value in schema.items()
                          if key != 'type'})
        return projected, (path,), ''

    combinator = combinators[0]
    branches = schema.get(combinator)
    if not isinstance(branches, list) or not branches:
        return (
            schema,
            (),
            f'{path}.{combinator} must be a non-empty schema array',
        )

    viable: list[Mapping[str, Any]] = []
    for index, branch in enumerate(branches):
        branch_path = f'{path}.{combinator}[{index}]'
        if branch is False:
            if combinator == 'allOf':
                projected = {'type': 'object'}
                projected.update({
                    key: value for key, value in schema.items()
                    if key not in {'type', combinator}
                })
                return projected, (f'{path}.{combinator}',), ''
            continue
        if branch is True:
            viable.append({})
            continue
        if not isinstance(branch, Mapping):
            return schema, (), f'{branch_path} must be an object schema'
        branch_allows_object, error = _schema_allows_object(
            branch, path=branch_path)
        if error:
            return schema, (), error
        if combinator == 'allOf' and not branch_allows_object:
            return (
                schema,
                (),
                f'{branch_path}.type is incompatible with object arguments',
            )
        if branch_allows_object:
            viable.append(branch)
    if not viable:
        return (
            schema,
            (),
            f'{path}.{combinator} has no object-compatible branch',
        )

    parent_properties = schema.get('properties')
    if parent_properties is not None and not isinstance(
            parent_properties, Mapping):
        return schema, (), f'{path}.properties must be an object'
    merged_properties: dict[str, Any] = dict(parent_properties or {})
    branch_property_candidates: dict[str, list[Any]] = {}
    branch_property_presence: dict[str, int] = {}
    for index, branch in enumerate(viable):
        properties = branch.get('properties')
        if properties is not None and not isinstance(properties, Mapping):
            return (
                schema,
                (),
                f'{path}.{combinator}[{index}].properties must be an object',
            )
        for name, child in (properties or {}).items():
            if name not in merged_properties:
                property_name = str(name)
                branch_property_candidates.setdefault(
                    property_name, []).append(child)
                branch_property_presence[property_name] = (
                    branch_property_presence.get(property_name, 0) + 1)
    for name, candidates in branch_property_candidates.items():
        unique: list[Any] = []
        for candidate in candidates:
            if not any(candidate == previous for previous in unique):
                unique.append(candidate)
        if combinator in ('anyOf', 'oneOf'):
            if branch_property_presence[name] < len(viable):
                # An alternative that omits this property may accept any
                # value for it. An unconstrained child keeps the projection a
                # true relaxation instead of accidentally narrowing it.
                merged_properties[name] = {}
            elif len(unique) > 1:
                merged_properties[name] = {'anyOf': unique}
            else:
                merged_properties[name] = unique[0]
        else:
            # For allOf, retaining any one conflicting constraint is a sound
            # relaxation: every canonical value had to satisfy that branch.
            merged_properties[name] = unique[0]

    parent_required = list(schema.get('required') or ())
    branch_required = [list(branch.get('required') or ()) for branch in viable]
    if combinator == 'allOf':
        unconditional = {
            name for names in branch_required for name in names
        }
    else:
        unconditional = set(branch_required[0])
        for names in branch_required[1:]:
            unconditional.intersection_update(names)
    merged_required = list(dict.fromkeys([
        *parent_required,
        *(name for names in branch_required for name in names
          if name in unconditional),
    ]))

    projected: dict[str, Any] = {'type': 'object'}
    for key, value in schema.items():
        if key not in {'type', combinator, 'properties', 'required'}:
            projected[key] = value
    if merged_properties or parent_properties is not None:
        projected['properties'] = merged_properties
    if merged_required:
        projected['required'] = merged_required
    if ('additionalProperties' not in schema
            and all(branch.get('additionalProperties') is False
                    for branch in viable)):
        projected['additionalProperties'] = False
    return projected, (f'{path}.{combinator}',), ''


def _normalize_provider_schema_anyof_types(
    schema: Mapping[str, Any], *, path: str,
) -> tuple[Mapping[str, Any], tuple[str, ...], str]:
    """Normalize recursive schemas into Moonshot's MFJS applicator shape.

    Standard JSON Schema treats ``type`` plus ``anyOf`` on one node as an
    intersection. Moonshot's function-tool dialect rejects that valid shape
    and requires the type on each branch instead. Distributing the parent type
    over every branch is logically equivalent. ``oneOf`` is relaxed to
    supported ``anyOf``; scalar ``const`` is represented by ``enum``; type
    arrays become typed ``anyOf`` branches. The canonical schema remains the
    execution authority, so the two deliberate relaxations cannot authorize a
    call. The rewrite is copy-on-write so clean schemas retain identity.
    """
    result: Mapping[str, Any] = schema
    repairs: list[str] = []

    if 'allOf' in schema:
        if 'anyOf' in schema or 'oneOf' in schema:
            # Full JSON Schema intersects sibling applicators. MFJS exposes
            # only anyOf, so keep that supported constraint (or the oneOf that
            # will become anyOf below) and remove allOf as a safe relaxation.
            result = {key: value for key, value in schema.items()
                      if key != 'allOf'}
            repairs.append(f'{path}.allOf')
        else:
            all_of = schema.get('allOf')
            if not isinstance(all_of, list) or not all_of:
                return schema, (), f'{path}.allOf must be a non-empty schema array'
            merged: dict[str, Any] = {
                key: value for key, value in schema.items() if key != 'allOf'
            }
            for index, branch in enumerate(all_of):
                if branch is True:
                    continue
                if branch is False:
                    merged = {
                        key: value for key, value in schema.items()
                        if key != 'allOf'
                    }
                    break
                if not isinstance(branch, Mapping):
                    return (
                        schema,
                        (),
                        f'{path}.allOf[{index}] must be an object schema',
                    )
                for key, value in branch.items():
                    if key not in merged:
                        merged[key] = value
                        continue
                    current = merged[key]
                    if current == value:
                        continue
                    if key == 'required' and isinstance(current, (list, tuple)) \
                            and isinstance(value, (list, tuple)):
                        merged[key] = list(dict.fromkeys([*current, *value]))
                    elif key == 'properties' and isinstance(current, Mapping) \
                            and isinstance(value, Mapping):
                        combined = dict(current)
                        for name, child in value.items():
                            combined.setdefault(name, child)
                        merged[key] = combined
                    elif key == 'type':
                        left, error = _schema_type_names(current, path=path)
                        if error:
                            return schema, tuple(repairs), error
                        right, error = _schema_type_names(value, path=path)
                        if error:
                            return schema, tuple(repairs), error
                        intersection = _intersect_schema_types(left, right)
                        if not intersection:
                            return (
                                schema,
                                tuple(repairs),
                                f'{path}.allOf has incompatible type constraints',
                            )
                        merged[key] = (
                            intersection[0] if len(intersection) == 1
                            else list(intersection)
                        )
                    elif key == 'additionalProperties' \
                            and (current is False or value is False):
                        merged[key] = False
                    # Other conflicting constraints retain the first one.
                    # Every canonical instance satisfied it, so this is a
                    # safe relaxation; execution still uses the original.
            result = merged
            repairs.append(f'{path}.allOf')

    if 'oneOf' in result:
        if 'anyOf' in result:
            result = {key: value for key, value in result.items()
                      if key != 'oneOf'}
        else:
            one_of = result.get('oneOf')
            if not isinstance(one_of, list) or not one_of:
                return schema, (), f'{path}.oneOf must be a non-empty schema array'
            result = {
                ('anyOf' if key == 'oneOf' else key): value
                for key, value in result.items()
            }
        repairs.append(f'{path}.oneOf')

    if 'const' in result:
        constant = result.get('const')
        converted = dict(result)
        converted.pop('const', None)
        if isinstance(constant, (str, int, float)) and not isinstance(
                constant, bool):
            converted.setdefault('enum', [constant])
        elif isinstance(constant, bool):
            converted.setdefault('type', 'boolean')
        elif constant is None:
            converted.setdefault('type', 'null')
        elif isinstance(constant, Mapping):
            converted.setdefault('type', 'object')
        elif isinstance(constant, list):
            converted.setdefault('type', 'array')
        result = converted
        repairs.append(f'{path}.const')

    if isinstance(result.get('type'), (list, tuple)) and 'anyOf' not in result:
        type_names, error = _schema_type_names(result.get('type'), path=path)
        if error:
            return schema, tuple(repairs), error
        converted = {
            key: value for key, value in result.items() if key != 'type'
        }
        converted['anyOf'] = [{'type': name} for name in type_names]
        result = converted
        repairs.append(f'{path}.type')

    if 'type' in result and 'anyOf' in result:
        any_of = result.get('anyOf')
        if not isinstance(any_of, list) or not any_of:
            return schema, (), f'{path}.anyOf must be a non-empty schema array'
        parent_types, error = _schema_type_names(
            result.get('type'), path=path)
        if error:
            return schema, (), error

        normalized_branches: list[Mapping[str, Any]] = []
        for index, branch in enumerate(any_of):
            branch_path = f'{path}.anyOf[{index}]'
            if branch is False:
                continue
            if branch is True:
                normalized_branches.append({
                    'type': copy.deepcopy(result.get('type')),
                })
                continue
            if not isinstance(branch, Mapping):
                return (
                    schema,
                    (),
                    f'{branch_path} must be an object schema',
                )
            if 'type' not in branch:
                normalized_branch = {
                    'type': copy.deepcopy(result.get('type')),
                    **branch,
                }
            else:
                branch_types, error = _schema_type_names(
                    branch.get('type'), path=branch_path)
                if error:
                    return schema, (), error
                intersection = _intersect_schema_types(
                    parent_types, branch_types)
                if not intersection:
                    # This branch was already impossible under the parent
                    # intersection. Omitting it preserves accepted instances.
                    continue
                normalized_type: Any = (
                    intersection[0] if len(intersection) == 1
                    else list(intersection)
                )
                if normalized_type == branch.get('type'):
                    normalized_branch = branch
                else:
                    normalized_branch = dict(branch)
                    normalized_branch['type'] = normalized_type
            normalized_branches.append(normalized_branch)
        if not normalized_branches:
            result = {
                key: value for key, value in result.items()
                if key != 'anyOf'
            }
            repairs.append(f'{path}.anyOf')
            return result, tuple(repairs), ''

        distributed: dict[str, Any] = {}
        for key, value in result.items():
            if key == 'type':
                continue
            distributed[key] = (
                normalized_branches if key == 'anyOf' else value
            )
        result = distributed
        repairs.append(path)

    if 'anyOf' in result:
        # MFJS also rejects an object applicator declared BOTH beside an
        # anyOf AND inside its branches ("conflicting keywords found in
        # anyOf with parent"). Standard JSON Schema treats parent-side
        # properties / required / additionalProperties as an intersection
        # with every branch, so folding them into each object branch — and
        # dropping them from the parent — is logically equivalent. Branches
        # that cannot be objects stay untouched: those keywords are vacuous
        # on non-object instances, so the fold is exact there too. On a
        # property-name conflict the branch's own constraint wins (a sound
        # relaxation; the canonical schema remains the execution
        # authority).
        sibling_keys = [
            key for key in ('properties', 'required', 'additionalProperties')
            if key in result
        ]
        any_of = result.get('anyOf')
        if sibling_keys and isinstance(any_of, list) and any_of \
                and all(isinstance(branch, Mapping) for branch in any_of):
            folded: list[Mapping[str, Any]] = []
            for branch in any_of:
                if 'type' in branch:
                    branch_types, error = _schema_type_names(
                        branch.get('type'), path=path)
                    if error:
                        return schema, tuple(repairs), error
                    if 'object' not in branch_types:
                        folded.append(branch)
                        continue
                merged_branch = dict(branch)
                parent_properties = result.get('properties')
                if isinstance(parent_properties, Mapping) \
                        and parent_properties:
                    combined = dict(parent_properties)
                    combined.update(merged_branch.get('properties') or {})
                    merged_branch['properties'] = combined
                parent_required = result.get('required')
                if isinstance(parent_required, (list, tuple)) \
                        and parent_required:
                    merged_branch['required'] = list(dict.fromkeys([
                        *parent_required,
                        *(merged_branch.get('required') or ()),
                    ]))
                parent_additional = result.get('additionalProperties')
                if parent_additional is False:
                    merged_branch['additionalProperties'] = False
                elif parent_additional is not None \
                        and 'additionalProperties' not in merged_branch:
                    merged_branch['additionalProperties'] = parent_additional
                folded.append(merged_branch)
            result = {
                key: (folded if key == 'anyOf' else value)
                for key, value in result.items()
                if key not in sibling_keys
            }
            repairs.append(path)

    def replace_child(key: str, value: Any) -> None:
        nonlocal result
        if result is schema:
            result = dict(schema)
        result[key] = value

    for key in _SCHEMA_MAP_OF_SCHEMAS_KEYS:
        children = result.get(key)
        if not isinstance(children, Mapping):
            continue
        normalized_children: Mapping[str, Any] = children
        for name, child in children.items():
            if not isinstance(child, Mapping):
                continue
            normalized_child, child_repairs, error = (
                _normalize_provider_schema_anyof_types(
                    child, path=f'{path}.{key}.{name}')
            )
            if error:
                return schema, tuple(repairs), error
            repairs.extend(child_repairs)
            if normalized_child is not child:
                if normalized_children is children:
                    normalized_children = dict(children)
                normalized_children[name] = normalized_child
        if normalized_children is not children:
            replace_child(key, normalized_children)

    for key in _SCHEMA_LIST_OF_SCHEMAS_KEYS:
        children = result.get(key)
        if not isinstance(children, list):
            continue
        normalized_children: list[Any] | None = None
        for index, child in enumerate(children):
            if not isinstance(child, Mapping):
                continue
            normalized_child, child_repairs, error = (
                _normalize_provider_schema_anyof_types(
                    child, path=f'{path}.{key}[{index}]')
            )
            if error:
                return schema, tuple(repairs), error
            repairs.extend(child_repairs)
            if normalized_child is not child:
                if normalized_children is None:
                    normalized_children = list(children)
                normalized_children[index] = normalized_child
        if normalized_children is not None:
            replace_child(key, normalized_children)

    for key in _SCHEMA_SINGLE_SCHEMA_KEYS:
        child = result.get(key)
        if isinstance(child, Mapping):
            normalized_child, child_repairs, error = (
                _normalize_provider_schema_anyof_types(
                    child, path=f'{path}.{key}')
            )
            if error:
                return schema, tuple(repairs), error
            repairs.extend(child_repairs)
            if normalized_child is not child:
                replace_child(key, normalized_child)
        elif isinstance(child, list):
            normalized_children: list[Any] | None = None
            for index, item in enumerate(child):
                if not isinstance(item, Mapping):
                    continue
                normalized_item, item_repairs, error = (
                    _normalize_provider_schema_anyof_types(
                        item, path=f'{path}.{key}[{index}]')
                )
                if error:
                    return schema, tuple(repairs), error
                repairs.extend(item_repairs)
                if normalized_item is not item:
                    if normalized_children is None:
                        normalized_children = list(child)
                    normalized_children[index] = normalized_item
            if normalized_children is not None:
                replace_child(key, normalized_children)

    return result, tuple(repairs), ''


def _normalize_provider_tool_schema(
    tool: Mapping[str, Any], *, model: str = '',
) -> tuple[Mapping[str, Any], tuple[str, ...], str]:
    """Return the model-specific provider projection for one tool schema."""
    from lib.model_info import is_kimi

    if not is_kimi(model):
        return tool, (), ''
    function = tool.get('function')
    if isinstance(function, Mapping):
        schema_key = 'parameters'
        parameters = function.get(schema_key)
        path = '$.function.parameters'
    else:
        function = None
        schema_key = 'input_schema'
        parameters = tool.get(schema_key)
        path = '$.input_schema'
        if parameters is None:
            schema_key = 'parameters'
            parameters = tool.get(schema_key)
            path = '$.parameters'
    if parameters is None and function is not None:
        normalized_tool = dict(tool)
        normalized_function = dict(function)
        normalized_function[schema_key] = {
            'type': 'object',
            'properties': {},
        }
        normalized_tool['function'] = normalized_function
        return normalized_tool, (path,), ''
    if parameters is True:
        normalized = {'type': 'object'}
        normalized_tool = dict(tool)
        if function is not None:
            normalized_function = dict(function)
            normalized_function[schema_key] = normalized
            normalized_tool['function'] = normalized_function
        else:
            normalized_tool[schema_key] = normalized
        return normalized_tool, (path,), ''
    if parameters is False:
        return tool, (), f'{path} does not permit object tool arguments'
    if not isinstance(parameters, Mapping):
        return tool, (), ''
    root, root_repairs, error = _project_moonshot_root_object(
        parameters, path=path)
    if error:
        return tool, root_repairs, error
    normalized, nested_repairs, error = (
        _normalize_provider_schema_anyof_types(root, path=path)
    )
    repairs = (*root_repairs, *nested_repairs)
    if error:
        return tool, repairs, error
    from lib.tools.moonshot_schema import project_mfjs_schema
    normalized, subset_repairs, error = project_mfjs_schema(
        normalized, path=path)
    repairs = (*repairs, *subset_repairs)
    if error or normalized is parameters:
        return tool, repairs, error
    normalized_tool = dict(tool)
    if function is not None:
        normalized_function = dict(function)
        normalized_function[schema_key] = normalized
        normalized_tool['function'] = normalized_function
    else:
        normalized_tool[schema_key] = normalized
    return normalized_tool, repairs, ''


def _without_json_schema_annotations(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Strip annotations only where keys are JSON-Schema keywords.

    Values below ``properties``/``$defs`` are *named schema maps*: their keys
    are argument/definition names, not annotation keywords.  A parameter may
    therefore legitimately be named ``description``, ``title`` or ``example``.
    Likewise, object-valued ``default``/``const``/``enum`` data is copied
    verbatim instead of being mistaken for a nested schema.
    """
    out: dict[str, Any] = {}
    for key, item in schema.items():
        if key in _SCHEMA_ANNOTATION_KEYS:
            continue
        if key in _SCHEMA_MAP_OF_SCHEMAS_KEYS and isinstance(item, Mapping):
            out[key] = {
                name: _without_json_schema_annotations(child)
                if isinstance(child, Mapping) else copy.deepcopy(child)
                for name, child in item.items()
            }
            continue
        if key in _SCHEMA_LIST_OF_SCHEMAS_KEYS and isinstance(item, list):
            out[key] = [
                _without_json_schema_annotations(child)
                if isinstance(child, Mapping) else copy.deepcopy(child)
                for child in item
            ]
            continue
        if key in _SCHEMA_SINGLE_SCHEMA_KEYS:
            if isinstance(item, Mapping):
                out[key] = _without_json_schema_annotations(item)
            elif isinstance(item, list):
                out[key] = [
                    _without_json_schema_annotations(child)
                    if isinstance(child, Mapping) else copy.deepcopy(child)
                    for child in item
                ]
            else:
                out[key] = copy.deepcopy(item)
            continue
        out[key] = copy.deepcopy(item)
    return out


def _without_schema_annotations(value: Any) -> Any:
    """Copy a schema without model-facing annotations.

    Validation keywords, property names, required fields, defaults, and all
    other execution semantics stay intact.  This is an emergency wire-size
    projection only; the task-owned executable contract remains unchanged.
    """
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)

    out = copy.deepcopy(dict(value))
    function = out.get('function')
    if isinstance(function, dict):
        for key in _SCHEMA_ANNOTATION_KEYS:
            function.pop(key, None)
        parameters = function.get('parameters')
        if isinstance(parameters, Mapping):
            function['parameters'] = _without_json_schema_annotations(
                parameters)
        return out

    # Anthropic/direct legacy shapes keep the schema at the top level.
    for key in _SCHEMA_ANNOTATION_KEYS:
        out.pop(key, None)
    for key in ('input_schema', 'parameters'):
        schema = out.get(key)
        if isinstance(schema, Mapping):
            out[key] = _without_json_schema_annotations(schema)
    return out


def _required_property_error(
    schema: Mapping[str, Any], *, path: str = '$',
) -> str:
    """Return the first Moonshot-strict required/properties violation.

    JSON Schema permits some cross-subschema constructions that Moonshot's
    function-tool dialect rejects. Its stable requirement is simpler: every
    name in an object schema's ``required`` array must be declared by that
    same schema's ``properties`` map. Enforce that provider-compatible subset
    recursively before a request leaves the process.
    """
    required = schema.get('required')
    if required is not None:
        if not isinstance(required, (list, tuple)) \
                or any(not isinstance(name, str) for name in required):
            return f'{path}.required must be an array of strings'
        properties = schema.get('properties')
        property_names = set(properties) if isinstance(properties, Mapping) \
            else set()
        missing = [name for name in required if name not in property_names]
        if missing:
            return (f'{path}.required references properties not declared at '
                    f'that level: {", ".join(missing)}')

    for key in _SCHEMA_MAP_OF_SCHEMAS_KEYS:
        children = schema.get(key)
        if not isinstance(children, Mapping):
            continue
        for name, child in children.items():
            if isinstance(child, Mapping):
                error = _required_property_error(
                    child, path=f'{path}.{key}.{name}')
                if error:
                    return error
    for key in _SCHEMA_LIST_OF_SCHEMAS_KEYS:
        children = schema.get(key)
        if not isinstance(children, list):
            continue
        for index, child in enumerate(children):
            if isinstance(child, Mapping):
                error = _required_property_error(
                    child, path=f'{path}.{key}[{index}]')
                if error:
                    return error
    for key in _SCHEMA_SINGLE_SCHEMA_KEYS:
        child = schema.get(key)
        if isinstance(child, Mapping):
            error = _required_property_error(child, path=f'{path}.{key}')
            if error:
                return error
        elif isinstance(child, list):
            for index, item in enumerate(child):
                if isinstance(item, Mapping):
                    error = _required_property_error(
                        item, path=f'{path}.{key}[{index}]')
                    if error:
                        return error
    return ''


def _moonshot_schema_shape_error(
    schema: Mapping[str, Any], *, path: str, root: bool = False,
) -> str:
    """Return an MFJS structural error that would reject a Kimi request."""
    if root:
        # The dedicated validator carries root-$defs context through the whole
        # document. Calling it independently for a child $ref would lose that
        # context and incorrectly reject a valid ``#/$defs/...`` reference.
        from lib.tools.moonshot_schema import mfjs_schema_error
        return mfjs_schema_error(schema, path=path, root=True)
    raw_type = schema.get('type')
    if isinstance(raw_type, (list, tuple)):
        return f'{path}.type must be one MFJS type string, not an array'
    if raw_type is not None:
        _types, error = _schema_type_names(raw_type, path=path)
        if error:
            return error
    if 'type' in schema and 'anyOf' in schema:
        return f'{path}.type must be declared inside anyOf branches'
    for unsupported in ('oneOf', 'allOf', 'const'):
        if unsupported in schema:
            return f'{path}.{unsupported} was not projected into MFJS'
    any_of = schema.get('anyOf')
    if any_of is not None and (not isinstance(any_of, list) or not any_of):
        return f'{path}.anyOf must be a non-empty schema array'
    if isinstance(any_of, list):
        for index, branch in enumerate(any_of):
            if not isinstance(branch, Mapping):
                return f'{path}.anyOf[{index}] must be an object schema'

    for key in _SCHEMA_MAP_OF_SCHEMAS_KEYS:
        children = schema.get(key)
        if not isinstance(children, Mapping):
            continue
        for name, child in children.items():
            if not isinstance(child, Mapping):
                return f'{path}.{key}.{name} must be an object schema'
            error = _moonshot_schema_shape_error(
                child, path=f'{path}.{key}.{name}')
            if error:
                return error
    for key in _SCHEMA_LIST_OF_SCHEMAS_KEYS:
        children = schema.get(key)
        if not isinstance(children, list):
            continue
        for index, child in enumerate(children):
            if isinstance(child, Mapping):
                error = _moonshot_schema_shape_error(
                    child, path=f'{path}.{key}[{index}]')
                if error:
                    return error
    for key in _SCHEMA_SINGLE_SCHEMA_KEYS:
        child = schema.get(key)
        if isinstance(child, Mapping):
            error = _moonshot_schema_shape_error(
                child, path=f'{path}.{key}')
            if error:
                return error
        elif isinstance(child, list):
            for index, item in enumerate(child):
                if isinstance(item, Mapping):
                    error = _moonshot_schema_shape_error(
                        item, path=f'{path}.{key}[{index}]')
                    if error:
                        return error
    return ''


def _wire_tool_schema_error(
    tool: Mapping[str, Any], *, model: str = '',
) -> str:
    """Return a provider-facing parameter-schema error, or ``''``."""
    function = tool.get('function')
    if isinstance(function, Mapping):
        parameters = function.get('parameters')
        path = '$.function.parameters'
    else:
        parameters = tool.get('input_schema')
        path = '$.input_schema'
        if parameters is None:
            parameters = tool.get('parameters')
            path = '$.parameters'
    # Parameter-less legacy tools remain valid and preserve old behavior.
    if parameters is None:
        return ''
    if not isinstance(parameters, Mapping):
        return f'{path} must be an object schema'
    error = _required_property_error(parameters, path=path)
    if error:
        return error
    if model:
        from lib.model_info import is_kimi
        if is_kimi(model):
            return _moonshot_schema_shape_error(
                parameters, path=path, root=True)
    return ''


def _source_wire_tool_schema_error(
    tool: Mapping[str, Any], *, model: str = '',
) -> str:
    """Validate source shape without applying Kimi-only wire restrictions.

    A canonical JSON Schema may legally require a property that it does not
    declare locally. MFJS rejects that shape, but the Kimi projection can add
    an unconstrained property declaration exactly. Other providers retain the
    historical strict preflight behavior.
    """
    from lib.model_info import is_kimi

    if not is_kimi(model):
        return _wire_tool_schema_error(tool)
    function = tool.get('function')
    if isinstance(function, Mapping):
        parameters = function.get('parameters')
        path = '$.function.parameters'
    else:
        parameters = tool.get('input_schema')
        path = '$.input_schema'
        if parameters is None:
            parameters = tool.get('parameters')
            path = '$.parameters'
    if parameters is None:
        return ''
    if parameters is True:
        return ''
    if parameters is False:
        return f'{path} does not permit object tool arguments'
    if not isinstance(parameters, Mapping):
        return f'{path} must be an object schema'
    return ''


def fit_tool_schema_budget(
    tools: list[dict[str, Any]] | None, *, budget_tokens: int,
    model: str = '', priority_names: set[str] | frozenset[str] = frozenset(),
    required_names: set[str] | frozenset[str] = frozenset(),
    on_tool_isolated: ToolIsolationReporter | None = None,
) -> list[dict[str, Any]]:
    """Fit optional schemas while retaining every valid required capability.

    ``budget_tokens`` is a soft, model-neutral cost target. Required schemas
    keep their full validation/help projection even when that means exceeding
    the target; correctness must not depend on registry order or schema size.
    """
    values: list[dict[str, Any]] = []
    for tool in tools or ():
        if not isinstance(tool, dict):
            _report_tool_isolation(
                on_tool_isolated,
                tool_name='',
                stage='budget_preflight',
                reason_code='non_object_schema',
                detail=f'expected object, got {type(tool).__name__}',
            )
            continue
        source_schema_error = _source_wire_tool_schema_error(
            tool, model=model)
        if source_schema_error:
            logger.error(
                '[ToolGateway] omitted invalid tool schema before budget fit: '
                'tool=%s error=%s',
                _schema_name(tool) or '?', source_schema_error)
            _report_tool_isolation(
                on_tool_isolated,
                tool_name=_schema_name(tool),
                stage='budget_preflight',
                reason_code='invalid_schema',
                detail=source_schema_error,
            )
            continue
        normalized_tool, repair_paths, normalization_error = (
            _normalize_provider_tool_schema(tool, model=model)
        )
        if normalization_error:
            logger.error(
                '[ToolGateway] omitted invalid tool schema before budget fit: '
                'tool=%s error=%s',
                _schema_name(tool) or '?', normalization_error)
            _report_tool_isolation(
                on_tool_isolated,
                tool_name=_schema_name(tool),
                stage='budget_preflight',
                reason_code='invalid_schema',
                detail=normalization_error,
            )
            continue
        if repair_paths:
            logger.warning(
                '[ToolGateway] projected provider-compatible MFJS schema '
                'before budget fit: tool=%s paths=%s',
                _schema_name(tool) or '?', ','.join(repair_paths[:8]))
        tool = normalized_tool
        schema_error = _wire_tool_schema_error(tool, model=model)
        if schema_error:
            logger.error(
                '[ToolGateway] omitted invalid tool schema before budget fit: '
                'tool=%s error=%s', _schema_name(tool) or '?', schema_error)
            _report_tool_isolation(
                on_tool_isolated,
                tool_name=_schema_name(tool),
                stage='budget_preflight',
                reason_code='invalid_schema',
                detail=schema_error,
            )
            continue
        values.append(tool)
    budget = max(0, int(budget_tokens or 0))
    gateway = [tool for tool in values
               if _schema_name(tool) in GATEWAY_TOOL_NAMES]
    gateway_tokens = tool_schema_tokens(gateway, model=model)
    if gateway and gateway_tokens > LOCAL_GATEWAY_MAX_TOKENS:
        # The target is an authoring contract guarded by tests, not a reason
        # to rewrite the pair at runtime: compacted gateway bytes broke the
        # provider prompt-cache prefix whenever PTC activation flipped.
        logger.warning(
            '[ToolGateway] gateway schemas exceed cost target: tokens=%d '
            'target=%d; request continues with byte-stable schemas',
            gateway_tokens, LOCAL_GATEWAY_MAX_TOKENS)
    if not budget or tool_schema_tokens(values, model=model) <= budget:
        return values
    direct = [(index, tool) for index, tool in enumerate(values)
              if _schema_name(tool) not in GATEWAY_TOOL_NAMES]
    required = {str(name) for name in required_names if str(name)}
    essential = {
        *CODE_CORE_DIRECT_TOOL_NAMES, 'web_search', 'fetch_url',
        'read_tool_artifact', 'search_tool_artifact',
    }
    ranked = sorted(
        (row for row in direct if _schema_name(row[1]) not in required),
        key=lambda row: (
            0 if _schema_name(row[1]) in essential else
            1 if _schema_name(row[1]) in priority_names else 2,
            row[0],
        ),
    )
    selected_tools: dict[int, dict[str, Any]] = {}
    compacted_names: list[str] = []
    selected = list(gateway)
    for index, tool in direct:
        if _schema_name(tool) not in required:
            continue
        selected.append(tool)
        selected_tools[index] = tool
    required_tokens = tool_schema_tokens(selected, model=model)
    if required_tokens > budget:
        logger.warning(
            '[ToolGateway] required direct schemas exceed cost target: '
            'tokens=%d target=%d required=%s; retaining functional floor',
            required_tokens, budget, ','.join(sorted(required)))
    for index, tool in ranked:
        candidate = tool
        if tool_schema_tokens([*selected, candidate], model=model) > budget:
            candidate = _without_schema_annotations(tool)
            schema_error = _wire_tool_schema_error(candidate)
            if schema_error:
                logger.error(
                    '[ToolGateway] annotation compaction violated the schema '
                    'contract; omitting tool=%s error=%s',
                    _schema_name(tool) or '?', schema_error)
                _report_tool_isolation(
                    on_tool_isolated,
                    tool_name=_schema_name(tool),
                    stage='annotation_compaction',
                    reason_code='invalid_schema_after_compaction',
                    detail=schema_error,
                )
                continue
            if tool_schema_tokens([*selected, candidate], model=model) > budget:
                continue
            compacted_names.append(_schema_name(tool))
        selected.append(candidate)
        selected_tools[index] = candidate
    result = [selected_tools[index] for index, _tool in direct
              if index in selected_tools]
    result.extend(gateway)
    dropped = [_schema_name(tool) for index, tool in direct
               if index not in selected_tools]
    if compacted_names or dropped:
        logger.info(
            '[ToolGateway] schema budget=%d selected=%d compacted=%d (%s) '
            'dropped=%d (%s)',
            budget, len(result), len(compacted_names),
            ','.join(compacted_names[:12]), len(dropped),
            ','.join(dropped[:12]))
    return result


def _schema_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ''
    fn = tool.get('function')
    if isinstance(fn, dict):
        return str(fn.get('name') or '')
    return str(tool.get('name') or '')


def _report_tool_isolation(
    reporter: ToolIsolationReporter | None,
    *,
    tool_name: str,
    stage: str,
    reason_code: str,
    detail: str,
) -> None:
    """Notify the task-owned observer without making diagnostics a dependency."""
    if not callable(reporter):
        return
    try:
        reporter({
            'toolName': str(tool_name or 'unknown tool')[:160],
            'stage': str(stage or 'wire_preflight')[:80],
            'reasonCode': str(reason_code or 'invalid_schema')[:160],
            'detail': str(detail or '')[:400],
            'action': 'omitted',
        })
    except Exception as exc:
        logger.warning(
            '[ToolGateway] tool-isolation observer failed (request continues): %s',
            exc,
        )


def sanitize_wire_tools(
    tools: Any,
    *,
    model: str = '',
    log_prefix: str = '',
    on_tool_isolated: ToolIsolationReporter | None = None,
) -> list[dict[str, Any]]:
    """Enforce the provider wire contract on a request's ``tools`` array.

    The Chat Completions wire requires every element to be an object; a
    function tool additionally requires ``type='function'`` (kimi rejects the
    request with "unknown tool type: " when it is absent) and any ``null``
    element hard-400s Gemini ("Expected a(n) 'tools' array element to be an
    object").  Producers are all validated upstream, but the array crosses
    many hands (registry assembly, conversation-latched catalogs, headless
    custom tools, rescue-body re-dispatch), so the LAST common boundary
    re-asserts the invariant instead of trusting every future producer:

    • non-dict, nameless, or structurally invalid entries are DROPPED — one
      bad apple must not 400 the whole request;
    • a function entry missing ``type`` is REPAIRED to ``'function'`` on a
      copy (the caller's canonical catalog is never mutated);
    • Kimi schemas are projected to MFJS on a copy: nested ``type`` +
      ``anyOf`` is distributed exactly, while an unrepresentable root object
      union becomes a sound relaxation and the canonical execution contract
      remains strict;
    • a clean array is returned AS THE SAME OBJECT so prompt-cache bytes and
      identity-based fast paths are untouched on the hot path.

    Unexpected producer defects are warnings with the offending index and
    shape. Expected provider-specific schema projection is a bounded debug
    diagnostic because it does not indicate a broken producer.
    """
    if not isinstance(tools, list):
        return []
    offenders: list[str] = []
    repaired_types: list[str] = []
    repaired_schemas: list[str] = []
    out: list[dict[str, Any]] | None = None
    for idx, tool in enumerate(tools):
        keep = tool
        drop = False
        if not isinstance(tool, dict):
            drop = True
            offenders.append(f'#{idx}:non-dict({type(tool).__name__})')
            _report_tool_isolation(
                on_tool_isolated,
                tool_name='',
                stage='wire_preflight',
                reason_code='non_object_schema',
                detail=f'expected object, got {type(tool).__name__}',
            )
        else:
            name = _schema_name(tool)
            if not name:
                drop = True
                offenders.append(
                    f'#{idx}:nameless(keys={sorted(map(str, tool))[:6]})')
                _report_tool_isolation(
                    on_tool_isolated,
                    tool_name='',
                    stage='wire_preflight',
                    reason_code='missing_tool_name',
                    detail='tool schema has no function/name field',
                )
            else:
                source_schema_error = _source_wire_tool_schema_error(
                    tool, model=model)
                if source_schema_error:
                    keep = tool
                    repair_paths = ()
                    normalization_error = source_schema_error
                else:
                    keep, repair_paths, normalization_error = (
                        _normalize_provider_tool_schema(tool, model=model)
                    )
                schema_error = normalization_error or _wire_tool_schema_error(
                    keep, model=model)
                if schema_error:
                    drop = True
                    offenders.append(
                        f'#{idx}:{name}:invalid-schema({schema_error})')
                    _report_tool_isolation(
                        on_tool_isolated,
                        tool_name=name,
                        stage='wire_preflight',
                        reason_code='invalid_schema',
                        detail=schema_error,
                    )
                elif repair_paths:
                    repaired_schemas.append(
                        f'#{idx}:{name}({",".join(repair_paths[:8])})')
            if not drop and isinstance(keep.get('function'), dict) \
                    and keep.get('type') != 'function':
                keep = {**keep, 'type': 'function'}
                repaired_types.append(f'#{idx}:{name}')
        if out is None and (drop or keep is not tool):
            out = list(tools[:idx])
        if out is not None and not drop:
            out.append(keep)
    if out is None:
        return tools
    if offenders or repaired_types:
        logger.warning(
            '%s[ToolGateway] wire tools invariant enforced: dropped=%s '
            'repaired_type=%s repaired_schema=%s — a producer emitted '
            'non-conforming entries; fix the producer, this boundary only '
            'contains the blast radius',
            log_prefix or '', offenders or '-', repaired_types or '-',
            repaired_schemas or '-')
    elif repaired_schemas:
        logger.debug(
            '%s[ToolGateway] projected %d provider-compatible tool schemas: %s',
            log_prefix or '', len(repaired_schemas), repaired_schemas[:4])
    return out


def preflight_wire_tool_body(
    body: dict[str, Any],
    *,
    log_prefix: str = '',
    on_tool_isolated: ToolIsolationReporter | None = None,
) -> None:
    """Apply the one tool-schema wire boundary shared by every transport."""
    if not isinstance(body.get('tools'), list):
        return
    body['tools'] = sanitize_wire_tools(
        body['tools'],
        model=str(body.get('model') or ''),
        log_prefix=log_prefix,
        on_tool_isolated=on_tool_isolated,
    )
    if not body['tools']:
        # Empty arrays and choices pointing at an isolated tool are rejected by
        # multiple OpenAI-compatible providers. Continue as an ordinary chat.
        body.pop('tools', None)
        body.pop('tool_choice', None)


def catalog_index(catalog: Any) -> dict[str, dict[str, Any]]:
    """Return the first server-owned schema for every registered name."""
    out: dict[str, dict[str, Any]] = {}
    for tool in catalog or ():
        name = _schema_name(tool)
        if name and isinstance(tool, dict):
            out.setdefault(name, tool)
    return out


def resolve_tool_search_backend(
    mode: str,
    *,
    protocol: str,
    model: str = '',
    responses_profile: str = '',
    base_url: str = '',
    oauth: str = '',
    capabilities: dict[str, Any] | None = None,
) -> str:
    """Resolve ``native_openai | native_anthropic | local | full``.

    A non-official endpoint is never promoted from a model-name guess.  It
    must carry a positive capability-probe result in ``capabilities``.
    """
    requested = str(mode or 'auto').strip().lower()
    if requested not in ('auto', 'native', 'local', 'off'):
        requested = 'auto'
    if requested == 'off':
        return 'full'
    if requested == 'local':
        return 'local'

    protocol = str(protocol or 'openai').strip().lower()
    model_id = str(model or '').strip().lower()
    host = (urlparse(str(base_url or '')).hostname or '').lower()
    caps = capabilities if isinstance(capabilities, dict) else {}

    from lib.model_info._openai_gpt56 import is_official_gpt56_model
    public_responses = (
        protocol == 'responses'
        and str(responses_profile or '').lower() == 'openai'
        and host in ('', 'api.openai.com')
        and is_official_gpt56_model(model_id)
        and str(oauth or '').lower() != 'codex'
    )
    if public_responses or caps.get('openai_native_tool_search') is True:
        return 'native_openai'

    # Tool Search is available on Claude 4.5+ (and the 5.x family).  Older
    # Claude endpoints must not receive the hosted-tool shape merely because
    # their model name begins with ``claude-``.
    native_claude_model = bool(re.search(
        r'(?:^|[-_])(?:opus|sonnet|haiku)[-_](?:4[-_.]?(?:5|6|7|8)|[5-9])(?:$|[-_.])',
        model_id))
    official_anthropic = (
        protocol == 'anthropic'
        and host in ('api.anthropic.com', '')
        and model_id.startswith('claude-')
        and native_claude_model
    )
    if official_anthropic or caps.get('anthropic_bm25_tool_search') is True:
        return 'native_anthropic'

    # ``native`` means "prefer native", not "send unverified vendor fields".
    # Unsupported/unverified providers fail over to the local gateway.
    return 'local'


def local_wire_tools(
    catalog: list[dict[str, Any]] | None,
    *,
    discovery_policy_by_name: dict[str, str] | None = None,
    discovery_catalog_size: int | None = None,
    searchable_count: int | None = None,
    include_search: bool = True,
    schema_budget_tokens: int = 0,
    model: str = '',
    priority_names: set[str] | frozenset[str] = frozenset(),
    required_names: set[str] | frozenset[str] = frozenset(),
    apply_schema_budget: bool = True,
    on_tool_isolated: ToolIsolationReporter | None = None,
) -> list[dict[str, Any]]:
    """Build a deterministic local surface from a stable schema projection.

    The definitions in ``catalog`` are the conversation-latched, model-visible
    projection.  The two optional counts describe the larger server-owned
    discovery catalog, allowing a small routed/MCP projection to retain
    ``search_tools`` without copying hidden schemas into the cached prefix.
    """
    tools = [tool for tool in (catalog or []) if isinstance(tool, dict)]
    policy = (discovery_policy_by_name
              if isinstance(discovery_policy_by_name, dict) else {})
    names = {_schema_name(tool) for tool in tools}
    gateways = [tool for tool in gateway_tool_schemas(
        include_search=include_search)
        if _schema_name(tool) not in names]

    try:
        total = max(len(tools), int(discovery_catalog_size)) \
            if discovery_catalog_size is not None else len(tools)
    except (TypeError, ValueError) as exc:
        logger.debug('[ToolGateway] invalid discovery catalog size: %s', exc)
        total = len(tools)
    visible_searchable = sum(
        policy.get(_schema_name(tool), 'eager') == 'searchable'
        for tool in tools)
    try:
        searchable = max(visible_searchable, int(searchable_count)) \
            if searchable_count is not None else visible_searchable
    except (TypeError, ValueError) as exc:
        logger.debug('[ToolGateway] invalid searchable tool count: %s', exc)
        searchable = visible_searchable

    def _fit(surface):
        if not apply_schema_budget:
            return list(surface)
        return fit_tool_schema_budget(
            list(surface), budget_tokens=schema_budget_tokens, model=model,
            priority_names=priority_names, required_names=required_names,
            on_tool_isolated=on_tool_isolated)

    # Small catalogs cost less than a discovery round. A large all-eager
    # catalog also has nothing to defer. If an explicit budget would omit any
    # schema, however, the gateway must remain present so every hidden member
    # of the executable catalog still has a live discovery path.
    if total < LOCAL_TOOL_SEARCH_MIN_FUNCTIONS or searchable <= 0:
        budget_pressure = bool(
            schema_budget_tokens
            and tool_schema_tokens(tools, model=model) > schema_budget_tokens)
        surface = tools
        if budget_pressure:
            surface = tools + [
                gateway for gateway in gateways
                if _schema_name(gateway) not in names]
        return _fit(surface)

    eager = [
        tool for tool in tools
        if policy.get(_schema_name(tool), 'eager') == 'eager'
    ]
    eager_names = {_schema_name(tool) for tool in eager}
    surface = eager + [g for g in gateways if _schema_name(g) not in eager_names]
    return _fit(surface)


def full_wire_tools(
    catalog: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Return the full original schema surface without a wrapper tool."""
    return [tool for tool in (catalog or []) if isinstance(tool, dict)]


def ptc_local_wire_tools(
    catalog: list[dict[str, Any]] | None,
    *,
    tier: str,
    eligible: list[str] | tuple | set,
    exposure: str = 'additive',
) -> list[dict[str, Any]]:
    """Project the PTC-local ``execute_tools`` surface for one round.

    Follows the Tool Search dual-backend precedent: the projection runs at
    the last common wire boundary and only shapes what the model sees —
    execution authority is unchanged. ``additive`` keeps the round's ordinary
    tools byte-stable and in order. ``gateway_only`` is a one-request adoption
    trial: after authoritative receipts prove a serial read chain, it exposes
    only ``execute_tools``. This removes command/skill read-around paths that
    real-model evaluation showed could make a sticky partial projection more
    expensive. The next request restores the full direct surface regardless of
    model behavior, so write, approval, and recovery availability are delayed
    by at most one model round and their execution authority never changes.
    Exactly one tier-shaped ``execute_tools`` schema carries the
    bounded read-only routing contract (``ptc_note``), so per-round tool names
    are never interpolated into cached schema text. An existing gateway schema
    (local Tool Search) is replaced rather than duplicated. Every model sees
    the free-form ``program`` parameter by default; only the explicit ``batch``
    override shape strips it.
    """
    include_program = str(tier or '').strip().lower() != 'batch'
    normalized_tier = 'program' if include_program else 'batch'
    execute = _stable_local_execute_tools_schema(tier=normalized_tier)
    # The fixed guidance is authored and tested inside the 600-token gateway
    # contract for supported tokenizers. Drift over that ceiling is reported
    # for maintainers, but the pair is never compacted at runtime: rewriting
    # gateway bytes between rounds breaks the provider prompt-cache prefix.
    projected_pair = [search_tools_schema(), execute]
    projected_tokens = tool_schema_tokens(projected_pair)
    if projected_tokens > LOCAL_GATEWAY_MAX_TOKENS:
        logger.warning(
            '[ToolGateway] fixed PTC gateway exceeds cost target: tokens=%d '
            'target=%d; request continues with byte-stable schemas',
            projected_tokens, LOCAL_GATEWAY_MAX_TOKENS)
    gateway_only = str(exposure or '').strip().lower() == 'gateway_only'
    out = [] if gateway_only else [
        tool for tool in (catalog or [])
        if isinstance(tool, dict)
        and _schema_name(tool) != EXECUTE_TOOLS_NAME
    ]
    out.append(execute)
    return out


_WORD_RE = re.compile(r'[a-z0-9_./:+-]+|[\u3400-\u9fff]+', re.I)

# A deliberately small, provider-neutral semantic layer.  Tool Search must be
# useful even when the user and a server-authored schema choose different
# everyday words (or different languages), but putting an embedding call on
# this path would add latency, cost and another availability dependency.  The
# canonical concepts below are stable code/data: they affect only the private
# index and never enter the cached tools array.
_SEARCH_CONCEPTS: dict[str, tuple[str, ...]] = {
    'search': (
        'search', 'find', 'locate', 'lookup', 'look up', 'discover',
        'retrieve', 'recall', '搜索', '搜一下', '查找', '找一下', '定位',
        '找回', '回忆', '想起', '记得', '上次'),
    'code_reference': (
        'grep', 'regex', 'regexp', 'contents', 'reference', 'references',
        'usage', 'usages', 'occurrence', 'occurrences', 'symbol', 'symbols',
        '引用', '调用', '使用位置', '出现位置', '出现', '函数', '变量'),
    'source_code': (
        'code', 'source', 'implementation', 'codebase',
        '代码', '源码', '实现'),
    'file': (
        'file', 'files', 'filename', 'filenames', 'path', 'paths',
        '文件', '文件名', '路径'),
    'configuration': (
        'config', 'configs', 'configuration', 'settings', 'setup', 'yaml',
        'toml', 'ini', '配置', '设置', '配置文件'),
    'edit': (
        'edit', 'update', 'change', 'modify', 'fix', 'revise', 'patch',
        'rewrite', 'adjust', 'tweak', '编辑', '更新', '修改', '修复', '改一下',
        '重写', '调整', '改动'),
    'screen': (
        'screen', 'display', 'monitor', 'screenshot', 'capture', 'desktop',
        '屏幕', '显示器', '截屏', '桌面', '电脑画面'),
    'schedule': (
        'schedule', 'scheduled', 'scheduler', 'recurring', 'reminder',
        'timed', 'cron', '定时', '日程', '计划任务', '提醒', '周期'),
    'cancel': (
        'cancel', 'stop', 'remove', 'delete', 'clear', 'disable',
        '取消', '停止', '删除', '不再', '别再', '关闭'),
    'claim': (
        'claim', 'ownership', 'own', 'assign', 'volunteer', 'take',
        '认领', '领取', '负责', '我来做', '交给我', '接手', '我来扛', '扛了'),
    'message': (
        'message', 'post', 'send', 'tell', 'notify', 'broadcast', 'chat',
        'channel', 'coworker', 'team', 'slack', '消息', '发消息', '通知',
        '群里', '群聊', '团队', '说一声'),
    'channel_chat': (
        'slack', 'chat', 'channel', 'workspace', 'group chat',
        '群里', '群聊', '工作群', '频道'),
    'pull_request': (
        'pull request', 'pull requests', 'pr', 'prs', 'code review',
        'code reviews', 'awaiting review', 'pending merge', 'merge request',
        '待合并', '拉取请求', '代码审查', '评审改动'),
    'documentation': (
        'documentation', 'docs', 'document', 'wiki', 'article', 'page',
        'knowledge base', '文档', '文章', '页面', '知识库', '维基'),
    'memory': (
        'memory', 'memories', 'recall', 'remember', 'remembered', 'past',
        'previous', 'earlier', 'decision', 'decisions', 'decide', 'decided',
        '记忆', '记住', '回忆', '之前', '上次', '决定', '拍板'),
    'calendar': (
        'calendar', 'meeting', 'appointment', 'event', 'book',
        '日历', '日程', '会议', '预约', '安排时间'),
    **CAPABILITY_SEARCH_CONCEPTS,
    'create': (
        'create', 'make', 'generate', 'add', 'new', 'build', 'produce',
        '创建', '新建', '生成', '制作', '添加', '做一份', '做一个', '做个',
        '做段', '做条', '画一张'),
    'authenticate': (
        'login', 'log in', 'sign in', 'signin', 'authenticate',
        'authentication', 'authorize', 'authorization', 'access approval',
        'not_logged_in', '登录', '登陆', '登入', '认证', '鉴权', '授权',
        '开通权限'),
    'download': (
        'download', 'save', 'copy', 'export', 'archive', 'zip', 'install',
        'unzip', 'staging', 'local', 'latest',
        '下载', '保存', '拷贝', '复制', '导出', '压缩包', '安装', '解压',
        '服务器', '本地', '最新版'),
    'list': (
        'list', 'show', 'open', 'what', 'which', 'see',
        '列出', '查看', '看看', '有哪些', '显示'),
}

_NAME_WEIGHTED_CONCEPTS = frozenset({
    '@search', '@edit', '@cancel', '@create', '@list', '@download',
    '@authenticate',
}) | frozenset(
    '@' + concept for concept in CAPABILITY_SEARCH_CONCEPTS
)


_TOKEN_CONCEPTS: dict[str, tuple[str, ...]] = {}
_PHRASE_CONCEPTS: tuple[tuple[str, str], ...]
_token_concept_sets: dict[str, set[str]] = {}
_phrase_concepts: list[tuple[str, str]] = []
for _concept, _aliases in _SEARCH_CONCEPTS.items():
    for _alias in _aliases:
        if ' ' in _alias or re.search(r'[\u3400-\u9fff]', _alias):
            _phrase_concepts.append((_alias, _concept))
        else:
            _token_concept_sets.setdefault(_alias, set()).add(_concept)
_TOKEN_CONCEPTS = {
    alias: tuple(concept for concept in _SEARCH_CONCEPTS
                 if concept in concepts)
    for alias, concepts in _token_concept_sets.items()
}
_PHRASE_CONCEPTS = tuple(_phrase_concepts)
del _token_concept_sets, _phrase_concepts, _concept, _aliases, _alias


def _cjk_ngrams(value: str) -> list[str]:
    """Keep short exact phrases; semantic matching is handled separately."""
    if not value:
        return []
    # Arbitrary character n-grams made common prose ("这个/一下/我来") outrank
    # the actual capability. Domain phrases still map through concepts below.
    return [value] if len(value) <= 12 else []


def _extract_terms(text: str) -> tuple[str, ...]:
    raw = _WORD_RE.findall(text)
    out: list[str] = []
    for word in raw:
        if re.fullmatch(r'[\u3400-\u9fff]+', word):
            out.extend(_cjk_ngrams(word))
        else:
            out.append(word)
            out.extend(p for p in re.split(r'[_./:+-]+', word)
                       if p and p != word)
    concepts: set[str] = set()
    for term in set(out):
        concepts.update(_TOKEN_CONCEPTS.get(term, ()))
    for phrase, concept in _PHRASE_CONCEPTS:
        if phrase in text:
            concepts.add(concept)
    out.extend('@' + concept for concept in _SEARCH_CONCEPTS
               if concept in concepts)
    return tuple(out)


@lru_cache(maxsize=tool_search_term_cache_capacity())
def _terms_cached(text: str) -> tuple[str, ...]:
    return _extract_terms(text)


def _terms(value: Any) -> list[str]:
    text = str(value or '').lower()
    if len(text) > LOCAL_TOOL_SEARCH_TERM_CACHE_MAX_INPUT_CHARS:
        return list(_extract_terms(text))
    return list(_terms_cached(text))


def _private_search_text(value: Any) -> str:
    """Flatten a server-owned aliases/intents sidecar, never a wire schema."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        value = value.values()
    if isinstance(value, (list, tuple, set)):
        return ' '.join(_private_search_text(item) for item in value)
    return ''


def _cursor_decode(cursor: Any) -> int:
    if not cursor:
        return 0
    try:
        encoded = str(cursor)
        if len(encoded) > LOCAL_TOOL_SEARCH_MAX_CURSOR_CHARS:
            raise ValueError('invalid_cursor')
        padded = encoded + '=' * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded.encode('ascii')).decode('ascii')
        return max(0, int(raw))
    except (ValueError, TypeError, UnicodeError):
        raise ValueError('invalid_cursor')


def _cursor_encode(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode('ascii')).decode(
        'ascii').rstrip('=')


# A snake_case query is almost always the model looking up ONE tool by its
# exact name.  When that name is absent, fuzzy part-matches are noise that
# otherwise invites an endless re-search loop (the catalog is task-fixed, so
# re-searching can never make a missing tool appear).
_NAME_SHAPED_RE = re.compile(r'[a-z][a-z0-9_]+')

_SEARCH_NOTICE = ("Call execute_tools with a result's exact name and "
                  'arguments matching arguments_schema.')


_DISCLOSED_OMITTED_HINT = (
    ' Tools already disclosed by earlier searches in this task are omitted '
    'from results — call them through execute_tools by exact name, or '
    'search the exact name to see the schema again.')


def _search_notice(
    missing_name: str,
    hint_ns: str,
    total: int,
    already_visible: str = '',
    already_disclosed: str = '',
) -> str:
    if already_visible:
        return (
            f"Tool '{already_visible}' is already available directly in this "
            'model request. Call it directly instead of searching for or '
            'routing it through execute_tools.')
    if already_disclosed:
        return (
            f"Tool '{already_disclosed}' was already disclosed by an earlier "
            'search in this task (schema unchanged); it is returned below '
            'for convenience. It is NOT in your direct tool list — call it '
            'through execute_tools with the exact name and the '
            'arguments_schema shown. Do not search for this name again.')
    if missing_name:
        return (
            f"No tool named '{missing_name}' exists in this task's catalog "
            '(exact-name lookup). The catalog is fixed for the whole task — '
            're-searching the same name cannot make it appear. The '
            f'{total} result(s) only matched PARTS of the name and are not '
            'substitutes. Do not search for this name again: pick a returned '
            'tool that genuinely fits, or treat the capability as unavailable '
            'this turn and proceed without it.')
    if hint_ns:
        return (
            f'A tool with this exact name exists under namespace {hint_ns!r} '
            '— re-run with that namespace (or omit the namespace filter). '
            + _SEARCH_NOTICE)
    return _SEARCH_NOTICE


def _bounded_summary(value: Any, limit: int = 240) -> str:
    """Return one whitespace-normalized, model-useful summary."""
    compact = ' '.join(str(value or '').split())
    if len(compact) <= limit:
        return compact
    return compact[:max(1, limit - 1)].rstrip() + '…'


_SEARCH_SCHEMA_KEYS = frozenset({
    'type', 'enum', 'const', 'default', 'required', 'additionalProperties',
    'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum',
    'minLength', 'maxLength', 'minItems', 'maxItems', 'uniqueItems',
    'format', 'pattern', 'nullable',
})


def _compact_arguments_schema(
    schema: Any,
    *,
    include_descriptions: bool = False,
    depth: int = 0,
) -> dict[str, Any]:
    """Project a callable JSON schema without duplicating contract prose."""
    if not isinstance(schema, dict) or depth > 8:
        return {'type': 'object', 'properties': {}} if depth == 0 else {}
    out = {
        key: copy.deepcopy(value)
        for key, value in schema.items()
        if key in _SEARCH_SCHEMA_KEYS
    }
    if include_descriptions and schema.get('description'):
        out['description'] = _bounded_summary(schema['description'], 180)
    properties = schema.get('properties')
    if isinstance(properties, dict):
        out['properties'] = {
            str(name): _compact_arguments_schema(
                value,
                include_descriptions=include_descriptions,
                depth=depth + 1,
            )
            for name, value in properties.items()
            if isinstance(value, dict)
        }
    items = schema.get('items')
    if isinstance(items, dict):
        out['items'] = _compact_arguments_schema(
            items,
            include_descriptions=include_descriptions,
            depth=depth + 1,
        )
    for choice_key in ('oneOf', 'anyOf', 'allOf'):
        choices = schema.get(choice_key)
        if isinstance(choices, list):
            out[choice_key] = [
                _compact_arguments_schema(
                    choice,
                    include_descriptions=include_descriptions,
                    depth=depth + 1,
                )
                for choice in choices[:16]
                if isinstance(choice, dict)
            ]
    if depth == 0:
        out.setdefault('type', 'object')
        out.setdefault('properties', {})
    return out


def _compact_contract_errors(value: Any) -> list[dict[str, str]]:
    """Expose error vocabulary on exact lookup without returning a manual."""
    rows = []
    for error in value if isinstance(value, list) else ():
        if not isinstance(error, dict):
            continue
        row = {
            key: _bounded_summary(error.get(key), 180)
            for key in ('code', 'message', 'retry_hint')
            if error.get(key)
        }
        if row:
            rows.append(row)
        if len(rows) >= 8:
            break
    return rows


def search_executable_catalog(
    catalog: list[dict[str, Any]] | None,
    query: Any,
    *,
    namespace: Any = '',
    limit: Any = LOCAL_TOOL_SEARCH_DEFAULT_LIMIT,
    cursor: Any = '',
    namespace_by_name: dict[str, str] | None = None,
    search_text_by_name: dict[str, Any] | None = None,
    contract_documents_by_name: dict[str, Any] | None = None,
    visible_names: set[str] | frozenset[str] | None = None,
    disclosed_names: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Rank hidden members of the immutable catalog without issuing authority.

    ``visible_names`` are directly callable on the provider wire;
    ``disclosed_names`` had their schema returned by an earlier search in
    this task and are callable only through ``execute_tools``. Both are
    excluded from broad results, but an exact-name lookup of a disclosed
    tool re-returns its schema: compaction may have dropped the earlier
    disclosure, and refusing to show it again leaves the model guessing
    argument names."""
    query_text = str(query or '').strip()
    if not query_text:
        return {'status': 'error', 'error': {
            'code': 'invalid_query', 'message': 'query must be non-empty'}}
    if len(query_text) > LOCAL_TOOL_SEARCH_MAX_QUERY_CHARS:
        return {'status': 'error', 'error': {
            'code': 'invalid_query',
            'message': 'query exceeds the Tool Search character limit',
            'max_chars': LOCAL_TOOL_SEARCH_MAX_QUERY_CHARS,
        }}
    try:
        wanted = int(limit)
    except (TypeError, ValueError) as exc:
        logger.debug('[ToolGateway] invalid search result limit %r: %s', limit, exc)
        wanted = LOCAL_TOOL_SEARCH_DEFAULT_LIMIT
    wanted = max(1, min(wanted, LOCAL_TOOL_SEARCH_MAX_LIMIT))
    try:
        offset = _cursor_decode(cursor)
    except ValueError as exc:
        logger.debug('[ToolGateway] invalid search cursor: %s', exc)
        return {'status': 'error', 'error': {
            'code': 'invalid_cursor', 'message': 'cursor is not valid'}}

    namespace_map = (namespace_by_name
                     if isinstance(namespace_by_name, dict) else {})
    search_text_map = (search_text_by_name
                       if isinstance(search_text_by_name, dict) else {})
    contract_map = (contract_documents_by_name
                    if isinstance(contract_documents_by_name, dict) else {})
    ns_filter = str(namespace or '').strip().lower()
    if len(ns_filter) > LOCAL_TOOL_SEARCH_MAX_NAMESPACE_CHARS:
        return {'status': 'error', 'error': {
            'code': 'invalid_namespace',
            'message': 'namespace exceeds the Tool Search character limit',
            'max_chars': LOCAL_TOOL_SEARCH_MAX_NAMESPACE_CHARS,
        }}
    qlower = query_text.lower()
    indexed_catalog = catalog_index(catalog)
    visible_casefold = {
        str(name).strip().lower() for name in (visible_names or ()) if name
    }
    visible_name_set = {
        name for name in indexed_catalog if name.lower() in visible_casefold
    }
    disclosed_casefold = {
        str(name).strip().lower() for name in (disclosed_names or ()) if name
    }
    disclosed_name_set = {
        name for name in indexed_catalog if name.lower() in disclosed_casefold
    } - visible_name_set
    hidden_name_set = visible_name_set | disclosed_name_set
    name_case = {name.lower(): name for name in indexed_catalog
                 if name not in GATEWAY_TOOL_NAMES}
    exact_lookup_name = name_case.get(qlower)
    legacy_lookup_name = ''
    if exact_lookup_name is None and '_' in qlower \
            and _NAME_SHAPED_RE.fullmatch(qlower):
        try:
            from lib.tool_input_repair import resolve_tool_name
            resolved, alias_kind = resolve_tool_name(
                query_text, known=set(name_case.values()))
            if alias_kind and resolved in set(name_case.values()):
                exact_lookup_name = resolved
                legacy_lookup_name = query_text
        except Exception as exc:
            logger.debug('[ToolGateway] exact alias lookup failed: %s', exc)
    already_visible_name = (
        exact_lookup_name if exact_lookup_name in visible_name_set else '')
    already_disclosed_name = (
        exact_lookup_name if exact_lookup_name in disclosed_name_set else '')
    docs: list[tuple[str, dict[str, Any], str, list[str]]] = []
    for name, tool in indexed_catalog.items():
        if name in GATEWAY_TOOL_NAMES or name in hidden_name_set:
            continue
        fn = tool.get('function') if isinstance(tool.get('function'), dict) \
            else tool
        ns = str(namespace_map.get(name) or 'general').lower()
        if ns_filter and ns != ns_filter:
            continue
        params = fn.get('parameters') if isinstance(fn, dict) else {}
        prop_names = ' '.join((params.get('properties') or {}).keys()) \
            if isinstance(params, dict) else ''
        # Field repetition is an intentionally simple weight that keeps the
        # BM25 implementation dependency-free. Private aliases/intents are
        # stronger than generic schema prose but never appear in the result.
        name_terms = _terms(name)
        description_terms = _terms(fn.get('description') or '')
        private_terms = _terms(_private_search_text(
            search_text_map.get(name)))
        property_terms = _terms(prop_names)
        terms = [
            *name_terms, *name_terms, *name_terms,
            *description_terms, *description_terms,
            *private_terms, *private_terms, *private_terms,
            *property_terms,
        ]
        docs.append((name, tool, ns, terms))

    # Repeating a word in a user sentence should not amplify it indefinitely.
    qterms = list(dict.fromkeys(_terms(query_text)))
    qconcepts = {term for term in qterms if term.startswith('@')}
    # Exact-name lookup honesty: a snake_case query matching NO catalog name
    # must be told the tool is absent — otherwise the fuzzy part-matches read
    # as "keep searching" and the model loops on the same keyword.
    missing_name = ''
    hint_ns = ''
    if '_' in qlower and _NAME_SHAPED_RE.fullmatch(qlower):
        exact = exact_lookup_name
        if exact is None:
            missing_name = query_text
        elif ns_filter and not already_disclosed_name \
                and all(row[0] != exact for row in docs):
            hint_ns = str(namespace_map.get(exact) or 'general').lower()
    if not docs and not already_disclosed_name:
        out: dict[str, Any] = {
            'status': 'ok', 'query': query_text, 'items': [],
            'execute_with': EXECUTE_TOOLS_NAME,
            'next_cursor': None, 'total': 0,
            'notice': _search_notice(
                missing_name, hint_ns, 0, already_visible_name),
        }
        if missing_name:
            out['missing_name'] = missing_name
        if already_visible_name:
            out['already_visible'] = already_visible_name
        if disclosed_name_set:
            out['notice'] += _DISCLOSED_OMITTED_HINT
        return out

    doc_freq = Counter()
    for _name, _tool, _ns, terms in docs:
        doc_freq.update(set(terms))
    avg_len = sum(len(row[3]) for row in docs) / max(1, len(docs))
    scored = []
    for position, (name, tool, ns, terms) in enumerate(docs):
        counts = Counter(terms)
        score = 0.0
        for term in qterms:
            freq = counts.get(term, 0)
            if not freq:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
            denom = freq + 1.2 * (0.25 + 0.75 * len(terms) / max(avg_len, 1))
            score += idf * (freq * 2.2) / denom
        # BM25 length normalization can make two nearly-identical action
        # candidates flip because one description is a few tokens shorter.
        # Reward each distinct semantic intent shared with the query once;
        # this lets "symbol references" beat generic find-files and lets
        # "recall" prefer memory_search over memory_write.
        score += 1.5 * len(qconcepts.intersection(terms))
        lname = name.lower()
        name_concepts = {term for term in _terms(name)
                         if term.startswith('@')}
        score += 3.0 * len(
            qconcepts.intersection(name_concepts).intersection(
                _NAME_WEIGHTED_CONCEPTS))
        if exact_lookup_name == name:
            score += 100.0
        elif qlower in lname:
            score += 12.0
        if ns_filter and ns == ns_filter:
            score += 2.0
        if score > 0:
            scored.append((-score, position, name, tool, ns))
    scored.sort()
    # Exact (including one-to-one maintained alias) lookup is a detail read,
    # not a broad recommendation query. Returning lexical neighbors here made
    # the model pay for unrelated schemas and occasionally choose a substitute
    # despite already naming the intended tool.
    if exact_lookup_name:
        scored = [row for row in scored if row[2] == exact_lookup_name]
    if already_disclosed_name and not any(
            row[2] == already_disclosed_name for row in scored):
        disclosed_tool = indexed_catalog.get(already_disclosed_name)
        if disclosed_tool is not None:
            scored.insert(0, (
                -100.0, -1, already_disclosed_name, disclosed_tool,
                str(namespace_map.get(already_disclosed_name)
                    or 'general').lower(),
            ))
    page = scored[offset:offset + wanted]
    items = []
    for negative, _pos, name, tool, ns in page:
        fn = tool.get('function') if isinstance(tool.get('function'), dict) \
            else tool
        contract = contract_map.get(name)
        contract = contract if isinstance(contract, dict) else {}
        detailed = name == exact_lookup_name
        summary = _bounded_summary(fn.get('description') or '')
        arguments_schema = (
            contract.get('arguments_schema') or fn.get('parameters') or {
                'type': 'object', 'properties': {}}
        )
        item = {
            'name': name,
            'namespace': ns,
            'summary': summary,
            # Retained for frontend/older model compatibility; now bounded.
            'description': summary,
            'arguments_schema': _compact_arguments_schema(
                arguments_schema, include_descriptions=detailed),
            'score': round(-negative, 6),
            'detail_level': 'exact' if detailed else 'compact',
        }
        if contract:
            item.update({
                'contract_version': contract.get('contractVersion'),
                'permission': contract.get('permission'),
                'idempotency': contract.get('idempotency'),
                'ptc_eligible': bool(contract.get('ptcEligible')),
            })
            if detailed:
                item['help'] = _bounded_summary(
                    contract.get('help') or item['description'], 1200)
                errors = _compact_contract_errors(contract.get('errors'))
                if errors:
                    item['errors'] = errors
        items.append(item)
    next_offset = offset + len(page)
    out = {
        'status': 'ok', 'query': query_text, 'namespace': ns_filter or None,
        'items': items,
        'execute_with': EXECUTE_TOOLS_NAME,
        'next_cursor': (_cursor_encode(next_offset)
                        if next_offset < len(scored) else None),
        'total': len(scored),
        'notice': _search_notice(
            missing_name, hint_ns, len(scored), already_visible_name,
            already_disclosed_name),
    }
    if missing_name:
        out['missing_name'] = missing_name
    if already_visible_name:
        out['already_visible'] = already_visible_name
    if already_disclosed_name:
        out['already_disclosed'] = already_disclosed_name
    if not scored and disclosed_name_set:
        out['notice'] += _DISCLOSED_OMITTED_HINT
    if legacy_lookup_name and exact_lookup_name:
        out['resolved_name'] = exact_lookup_name
        if not already_visible_name:
            out['notice'] = (
                f"Legacy tool name '{legacy_lookup_name}' resolves to canonical "
                f"'{exact_lookup_name}'. Use the canonical name with "
                'execute_tools. ' + _SEARCH_NOTICE)

    # Pagination is also a hard output budget: remove the lowest-ranked tail
    # until the complete JSON envelope fits, then point the cursor at the first
    # item not returned. Argument names/required fields are never truncated.
    out['items_returned'] = len(out['items'])
    original_notice = out['notice']
    while out['items'] and len(json.dumps(
            out, ensure_ascii=False, separators=(',', ':'))) \
            > LOCAL_TOOL_SEARCH_MAX_RESULT_CHARS:
        out['items'].pop()
        returned = len(out['items'])
        next_offset = offset + returned
        out['items_returned'] = returned
        out['next_cursor'] = (
            _cursor_encode(next_offset) if next_offset < len(scored) else None)
        out['truncated_for_budget'] = True
        out['notice'] = original_notice + (
            ' The result page hit its bounded output budget; continue with '
            'next_cursor if another candidate is needed.')
    return out


def _tool_parameters(tool: dict[str, Any]) -> dict[str, Any]:
    fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
    params = fn.get('parameters') if isinstance(fn, dict) else None
    return params if isinstance(params, dict) else {
        'type': 'object', 'properties': {}}


def _resolve_catalog_name_detail(
    raw_name: Any,
    catalog: list[dict[str, Any]] | None,
    *,
    namespace_by_name: dict[str, str] | None = None,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve a catalog name and describe any deterministic repair."""
    attempted = str(raw_name or '').strip()
    index = catalog_index(catalog)
    if attempted in index:
        return attempted, None, None
    if not attempted:
        return None, {
            'code': 'missing_tool_name',
            'message': 'Each call requires a tool name.',
            'retry_hint': 'Copy an exact name returned by search_tools.',
        }, None

    ns_map = namespace_by_name if isinstance(namespace_by_name, dict) else {}
    candidates: set[str] = set()
    for separator in ('::', '/', '.'):
        if separator in attempted:
            ns, tail = attempted.rsplit(separator, 1)
            candidates.update(
                name for name in index
                if name == tail and str(ns_map.get(name) or '').lower()
                == ns.lower())
    casefold = [name for name in index if name.lower() == attempted.lower()]
    candidates.update(casefold)
    if len(candidates) == 1:
        resolved = next(iter(candidates))
        kind = ('casefold_tool_name'
                if resolved in casefold else 'namespace_tool_name')
        return resolved, None, {
            'path': '$.name', 'kind': kind,
            'before': attempted, 'after': resolved,
        }
    if len(candidates) > 1:
        return None, {
            'code': 'ambiguous_tool_name',
            'message': f'Tool name {attempted!r} is ambiguous.',
            'candidates': sorted(candidates),
            'retry_hint': 'Retry with one exact candidate name.',
        }, None

    # Reuse the harness's curated aliases, but only when it yields one member
    # of this task's catalog. Fuzzy typo repair is handled by the stricter
    # confidence-and-margin gate below.
    try:
        from lib.tool_input_repair import resolve_tool_name
        resolved, kind = resolve_tool_name(attempted, known=set(index))
        if kind and resolved in index:
            return resolved, None, {
                'path': '$.name', 'kind': f'{kind}_tool_name',
                'before': attempted, 'after': resolved,
            }
    except Exception as exc:
        logger.debug('[ToolGateway] curated alias resolution failed: %s', exc)

    # A typo may be executed only when the winner is both absolutely strong
    # and clearly separated from the runner-up.  This applies to write tools
    # too, but it never bypasses the ordinary approval pipeline downstream.
    scored: list[tuple[str, float]] = []
    try:
        from lib.tool_input_repair import _name_similarity
        scored = sorted(
            ((name, float(_name_similarity(attempted, name)))
             for name in index),
            key=lambda row: (-row[1], row[0]),
        )
    except Exception as exc:
        logger.debug('[ToolGateway] fuzzy name scoring failed: %s', exc)
    top_score = scored[0][1] if scored else 0.0
    runner_up = scored[1][1] if len(scored) > 1 else 0.0
    margin = top_score - runner_up
    if scored and top_score >= 0.90 and margin >= 0.15:
        resolved = scored[0][0]
        return resolved, None, {
            'path': '$.name', 'kind': 'fuzzy_tool_name',
            'before': attempted, 'after': resolved,
            'confidence': round(top_score, 6),
            'margin': round(margin, 6),
        }
    suggestions = [
        {'name': name, 'score': round(score, 6)}
        for name, score in scored[:3] if score >= 0.45
    ]
    return None, {
        'code': 'tool_not_enabled',
        'message': f'Tool {attempted!r} is not enabled or not unambiguous.',
        'candidates': suggestions,
        'retry_hint': ('Retry with an exact candidate name, or call '
                       'search_tools with the intended capability.'),
    }, None


def resolve_catalog_name(
    raw_name: Any,
    catalog: list[dict[str, Any]] | None,
    *,
    namespace_by_name: dict[str, str] | None = None,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve exact, namespace, curated-alias, or high-confidence typo."""
    name, error, _repair = _resolve_catalog_name_detail(
        raw_name, catalog, namespace_by_name=namespace_by_name)
    return name, error


def _type_ok(value: Any, expected: str) -> bool:
    if expected == 'object':
        return isinstance(value, dict)
    if expected == 'array':
        return isinstance(value, list)
    if expected == 'string':
        return isinstance(value, str)
    if expected == 'boolean':
        return isinstance(value, bool)
    if expected == 'integer':
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == 'number':
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == 'null':
        return value is None
    return True


def _normalize_schema_value(value: Any, schema: Any, path: str,
                            repairs: list[dict[str, Any]]) -> Any:
    if not isinstance(schema, dict):
        return value
    expected = schema.get('type')
    types = list(expected) if isinstance(expected, list) else [expected]
    types = [str(t) for t in types if t]
    if types and not any(_type_ok(value, t) for t in types):
        repaired = value
        kind = ''
        if isinstance(value, str):
            raw = value.strip()
            if 'boolean' in types and raw.lower() in ('true', 'false'):
                repaired, kind = raw.lower() == 'true', 'string_to_boolean'
            elif 'integer' in types and re.fullmatch(r'[+-]?\d+', raw):
                repaired, kind = int(raw), 'string_to_integer'
            elif ('number' in types
                  and re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)', raw)):
                repaired, kind = float(raw), 'string_to_number'
            elif any(t in types for t in ('object', 'array')) \
                    and raw[:1] in ('{', '['):
                try:
                    candidate = json.loads(raw)
                except json.JSONDecodeError as exc:
                    logger.debug('[ToolGateway] schema JSON coercion failed: %s', exc)
                    candidate = value
                if any(_type_ok(candidate, t) for t in types):
                    repaired, kind = candidate, 'json_string_to_value'
            elif 'array' in types:
                repaired, kind = [value], 'scalar_to_array'
        elif 'array' in types and value is not None:
            repaired, kind = [value], 'scalar_to_array'
        if kind:
            repairs.append({'path': path, 'kind': kind,
                            'before': value, 'after': repaired})
            value = repaired
        if not any(_type_ok(value, t) for t in types):
            raise ValueError(json.dumps({
                'code': 'invalid_argument_type', 'path': path,
                'expected': types, 'actual': type(value).__name__,
                'message': (f'Invalid type at {path}: expected '
                            f'{" | ".join(types)}.'),
                'retry_hint': 'Match the returned arguments_schema and retry.',
            }, ensure_ascii=False))

    if isinstance(value, dict):
        props = schema.get('properties') or {}
        out = dict(value)
        for key, child_schema in props.items():
            if key not in out and isinstance(child_schema, dict) \
                    and 'default' in child_schema:
                default = copy.deepcopy(child_schema['default'])
                out[key] = default
                repairs.append({
                    'path': f'{path}.{key}', 'kind': 'schema_default',
                    'before': None, 'after': default,
                })
        required = schema.get('required') or []
        missing = [key for key in required
                   if key not in out or out.get(key) is None]
        if missing:
            raise ValueError(json.dumps({
                'code': 'missing_required_arguments', 'path': path,
                'missing': missing,
                'message': ('Missing required arguments: '
                            + ', '.join(str(key) for key in missing)),
                'retry_hint': 'Provide each missing argument and retry.',
            }, ensure_ascii=False))
        for key, child_schema in props.items():
            if key in out:
                out[key] = _normalize_schema_value(
                    out[key], child_schema, f'{path}.{key}', repairs)
        if schema.get('additionalProperties') is False:
            extras = sorted(set(out) - set(props))
            if extras:
                raise ValueError(json.dumps({
                    'code': 'unknown_arguments', 'path': path,
                    'arguments': extras,
                    'message': ('Unknown arguments: '
                                + ', '.join(str(key) for key in extras)),
                    'retry_hint': 'Remove unknown arguments and retry.',
                }, ensure_ascii=False))
        value = out
    elif isinstance(value, list) and isinstance(schema.get('items'), dict):
        value = [_normalize_schema_value(item, schema['items'],
                                         f'{path}[{i}]', repairs)
                 for i, item in enumerate(value)]

    if 'enum' in schema and value not in schema.get('enum', ()):
        allowed = schema.get('enum') or []
        casefold = [candidate for candidate in allowed
                    if isinstance(candidate, str) and isinstance(value, str)
                    and candidate.casefold() == value.casefold()]
        if len(casefold) == 1:
            repaired = casefold[0]
            repairs.append({
                'path': path, 'kind': 'casefold_enum',
                'before': value, 'after': repaired,
            })
            value = repaired
        else:
            raise ValueError(json.dumps({
                'code': 'invalid_argument_value', 'path': path,
                'allowed': allowed, 'actual': value,
                'message': f'Invalid value at {path}.',
                'retry_hint': 'Use one exact value from allowed.',
            }, ensure_ascii=False))
    return value


def normalize_gateway_call(
    raw_call: Any,
    *,
    catalog: list[dict[str, Any]] | None,
    namespace_by_name: dict[str, str] | None,
    gateway_call_id: str,
    index: int,
    source: str,
    contract_documents_by_name: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw_call, dict):
        return None, {'code': 'invalid_call', 'index': index,
                      'message': 'call must be an object'}
    function = raw_call.get('function')
    function = function if isinstance(function, dict) else {}
    raw_name = (raw_call.get('name') or raw_call.get('tool')
                or function.get('name'))
    name, error, name_repair = _resolve_catalog_name_detail(
        raw_name, catalog, namespace_by_name=namespace_by_name)
    if error:
        return None, {**error, 'index': index, 'attempted': raw_name}

    raw_args = (raw_call['arguments'] if 'arguments' in raw_call
                else raw_call.get('args', raw_call.get('input',
                                                       function.get('arguments', {}))))
    if raw_args is None:
        raw_args = {}
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args or '{}')
        except json.JSONDecodeError as exc:
            logger.debug('[ToolGateway] invalid call arguments JSON: %s', exc)
            return None, {'code': 'invalid_arguments_json', 'index': index,
                          'name': name, 'message': str(exc),
                          'retry_hint': ('Repair arguments as a JSON object '
                                         'matching arguments_schema.')}
    if not isinstance(raw_args, dict):
        return None, {'code': 'invalid_arguments', 'index': index,
                      'name': name, 'message': 'Arguments must be an object.',
                      'retry_hint': ('Provide arguments as a JSON object '
                                     'matching arguments_schema.')}
    repairs: list[dict[str, Any]] = []
    if name_repair:
        repairs.append(name_repair)
    # Reuse the ordinary harness's curated key aliases and guarded structural
    # transforms before validating against the task-owned schema.  Dynamic MCP
    # schemas that are absent from the global repair index pass through.
    try:
        from lib.tool_input_repair import validate_then_repair
        raw_args, shared_repairs = validate_then_repair(name, raw_args)
        repairs.extend({
            'path': str(path), 'kind': str(kind),
        } for path, kind in shared_repairs)
    except Exception as exc:
        logger.debug('[ToolGateway] shared argument repair failed: %s', exc)
    try:
        args = _normalize_schema_value(
            raw_args, _tool_parameters(catalog_index(catalog)[name]),
            '$.arguments', repairs)
    except ValueError as exc:
        try:
            detail = json.loads(str(exc))
        except json.JSONDecodeError as parse_exc:
            logger.debug('[ToolGateway] structured validation detail unavailable: %s',
                         parse_exc)
            detail = {'code': 'invalid_arguments', 'message': str(exc)}
        return None, {**detail, 'index': index, 'name': name}

    try:
        before_contract = args
        args = validate_tool_arguments_from_documents(
            contract_documents_by_name, name, args)
        if args != before_contract:
            added = sorted(set(args) - set(before_contract))
            repairs.extend({
                'path': f'$.arguments.{key}', 'kind': 'contract_default',
            } for key in added)
            if not added:
                repairs.append({
                    'path': '$.arguments', 'kind': 'contract_default'})
    except ToolContractError as exc:
        detail = exc.to_dict()
        detail['retry_hint'] = exc.next_action
        return None, {**detail, 'index': index, 'name': name}

    supplied_id = (raw_call.get('call_id') or raw_call.get('id')
                   or function.get('call_id'))
    if supplied_id:
        call_id = str(supplied_id)
    else:
        canonical = json.dumps([gateway_call_id, index, name, args],
                               sort_keys=True, ensure_ascii=False,
                               separators=(',', ':'))
        call_id = 'gw_' + hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:20]
    return {
        'id': call_id,
        'type': 'function',
        'source': source,
        'function': {
            'name': name,
            'arguments': json.dumps(args, ensure_ascii=False,
                                    separators=(',', ':')),
        },
        '_normalized_arguments': args,
        '_normalization_repairs': repairs,
    }, None


def normalize_execute_request(
    payload: Any,
    *,
    catalog: list[dict[str, Any]] | None,
    namespace_by_name: dict[str, str] | None,
    gateway_call_id: str,
    source: str = 'execute_calls',
    contract_documents_by_name: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {'calls': [], 'program': None, 'execution': 'auto',
                'warnings': [], 'errors': [{
                    'code': 'invalid_request',
                    'message': 'execute_tools arguments must be an object'}]}
    raw_program = payload.get('program')
    program = raw_program if isinstance(raw_program, str) else None
    warnings: list[dict[str, Any]] = []
    raw_calls = payload.get('calls')
    if raw_calls is None and any(
            key in payload for key in ('name', 'tool', 'function')):
        raw_calls = payload
        warnings.append({
            'code': 'wrapped_single_call',
            'message': 'A top-level tool call was treated as calls[0].',
        })
    if program is not None and raw_calls not in (None, [], {}):
        warnings.append({
            'code': 'program_preferred_over_calls',
            'message': ('Both program and calls were supplied; program was '
                        'executed and calls were ignored.'),
        })
        raw_calls = []
    if raw_calls is None:
        raw_calls = []
    elif isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    elif isinstance(raw_calls, str):
        try:
            raw_calls = json.loads(raw_calls)
            if isinstance(raw_calls, dict):
                raw_calls = [raw_calls]
        except json.JSONDecodeError as exc:
            logger.debug('[ToolGateway] calls payload JSON invalid: %s', exc)
            raw_calls = None
    if not isinstance(raw_calls, list):
        return {'calls': [], 'program': program, 'execution': 'auto',
                'warnings': warnings, 'errors': [{
                    'code': 'invalid_calls',
                    'message': 'calls must be an object or array'}]}
    execution = str(payload.get('execution') or 'auto').lower()
    errors: list[dict[str, Any]] = []
    if execution not in ('auto', 'sequential', 'parallel'):
        errors.append({'code': 'invalid_execution',
                       'message': 'execution must be auto, sequential, or parallel'})
        execution = 'auto'
    normalized = []
    if len(raw_calls) > 16:
        errors.append({'code': 'too_many_calls', 'limit': 16,
                       'actual': len(raw_calls)})
        raw_calls = raw_calls[:16]
    for position, raw_call in enumerate(raw_calls):
        call, error = normalize_gateway_call(
            raw_call, catalog=catalog,
            namespace_by_name=namespace_by_name,
            gateway_call_id=gateway_call_id, index=position, source=source,
            contract_documents_by_name=contract_documents_by_name)
        if error:
            errors.append(error)
        elif call:
            normalized.append(call)
    if program is None and not normalized and not errors:
        errors.append({'code': 'missing_work',
                       'message': 'provide calls or program'})
    return {'calls': normalized, 'program': program, 'execution': execution,
            'warnings': warnings, 'errors': errors}


__all__ = [
    'EXECUTE_TOOLS_NAME', 'GATEWAY_TOOL_NAMES', 'SEARCH_TOOLS_NAME',
    'CODE_CORE_DIRECT_TOOL_NAMES', 'LOCAL_GATEWAY_MAX_TOKENS',
    'LOCAL_TOOL_SEARCH_MAX_RESULT_CHARS',
    'catalog_index', 'full_wire_tools',
    'execute_tools_schema',
    'fit_tool_schema_budget', 'gateway_tool_schemas', 'local_wire_tools',
    'normalize_execute_request',
    'normalize_gateway_call', 'ptc_local_wire_tools', 'resolve_catalog_name',
    'resolve_tool_search_backend', 'search_executable_catalog',
    'search_tools_schema', 'tool_schema_tokens',
]
