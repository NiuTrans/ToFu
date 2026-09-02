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


logger = get_logger(__name__)

SEARCH_TOOLS_NAME = 'search_tools'
EXECUTE_TOOLS_NAME = 'execute_tools'
GATEWAY_TOOL_NAMES = frozenset({SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME})

LOCAL_TOOL_SEARCH_MIN_FUNCTIONS = 12
LOCAL_TOOL_SEARCH_DEFAULT_LIMIT = 8
LOCAL_TOOL_SEARCH_MAX_LIMIT = 20
LOCAL_GATEWAY_MAX_TOKENS = 500
CODE_CORE_DIRECT_TOOL_NAMES = frozenset({
    'read_files', 'grep_search', 'find_files', 'edit_file', 'run_command',
})

ToolIsolationReporter = Callable[[dict[str, Any]], None]


def search_tools_schema(*, compact: bool = False) -> dict[str, Any]:
    description = (
        'Find tools; run a result with execute_tools.' if compact else
        'Find task-available tools by capability. This only finds '
        'tools. To run a result, call execute_tools; copy its exact '
        'name and provide arguments matching arguments_schema.')
    return {
        'type': 'function',
        'function': {
            'name': SEARCH_TOOLS_NAME,
            'description': description,
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'minLength': 1},
                    'namespace': {'type': 'string'},
                    'limit': {'type': 'integer', 'minimum': 1,
                              'maximum': LOCAL_TOOL_SEARCH_MAX_LIMIT,
                              'default': LOCAL_TOOL_SEARCH_DEFAULT_LIMIT},
                    'cursor': {'type': 'string'},
                },
                'required': ['query'],
                'additionalProperties': False,
            },
        },
    }


def execute_tools_schema(*, include_program: bool = True,
                         ptc_note: str = '',
                         compact: bool = False) -> dict[str, Any]:
    """Return the ``execute_tools`` gateway schema.

    Default output is byte-identical to the historical shape and exposes the
    full ToolScript surface to every model.  ``include_program=False`` is the
    explicit ``TOFU_PTC_TIER=batch`` operator/benchmark override shape (the
    model batches parallel ``calls`` instead of authoring ToolScript), and
    ``ptc_note`` (the bounded read-only routing contract) is spliced into the
    description so the policy travels with the schema the model sees.
    """
    if compact:
        description = (
            'Run exact tool names from search_tools with calls; use program '
            'for dependent reads.' if include_program else
            'Run exact tool names from search_tools with calls.')
    else:
        description = (
            'Run task-available tools found with search_tools. Provide '
            'calls or program. Use calls for ordinary or independent '
            'work. Use program only when later calls depend on earlier '
            'results. ToolScript supports '
            'catalog.search(query, namespace?, limit?, cursor?), '
            'tools.call(name, arguments, callId?), '
            'tools.callMany(calls, execution?), and tools.parallel(calls). '
            'It has no eval, imports, filesystem, process, or network '
            'access except through those tools.')
    if not include_program and not compact:
        description = (
            'Run task-available tools found with search_tools. Provide calls '
            'for one or more independent tool invocations; prefer a single '
            'batched call with execution=parallel over issuing tools one per '
            'turn.')
    if ptc_note and not compact:
        description = f'{description} {ptc_note}'
    calls_property: dict[str, Any] = {
        'type': 'array', 'maxItems': 16,
    }
    if not compact:
        calls_property['description'] = (
            'Tool calls. Copy each exact name from search_tools '
            'and match its arguments_schema.')
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
        }
        if not compact:
            properties['program']['description'] = (
                'JavaScript-like bounded ToolScript for '
                'data-dependent search, calls, filtering, and '
                'aggregation. Calls are synchronous; do not use '
                'await.')
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


def _wire_tool_schema_error(tool: Mapping[str, Any]) -> str:
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
    return _required_property_error(parameters, path=path)


def _compact_gateway_schemas(
    tools: list[dict[str, Any]], *, model: str = '',
) -> list[dict[str, Any]]:
    """Return one concise canonical schema for each gateway name present."""
    compacted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in tools:
        name = _schema_name(tool)
        if name in seen or name not in GATEWAY_TOOL_NAMES:
            continue
        seen.add(name)
        if name == SEARCH_TOOLS_NAME:
            compacted.append(search_tools_schema(compact=True))
            continue
        fn = tool.get('function') if isinstance(tool, dict) else None
        parameters = fn.get('parameters') if isinstance(fn, dict) else None
        properties = (parameters.get('properties')
                      if isinstance(parameters, dict) else None)
        include_program = (
            isinstance(properties, dict) and 'program' in properties)
        compacted.append(execute_tools_schema(
            include_program=include_program, compact=True))
    compact_tokens = tool_schema_tokens(compacted, model=model)
    if compact_tokens > LOCAL_GATEWAY_MAX_TOKENS:
        # The limit is an application cost budget, not a provider validity
        # rule.  A future tokenizer/schema change must not turn it into an
        # availability failure; send the smallest functional pair and report
        # the budget drift for maintainers.
        logger.warning(
            '[ToolGateway] compact gateway still exceeds target: tokens=%d '
            'target=%d; continuing with functional schemas',
            compact_tokens, LOCAL_GATEWAY_MAX_TOKENS)
    return compacted


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
        schema_error = _wire_tool_schema_error(tool)
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
        gateway = _compact_gateway_schemas(gateway, model=model)
        values = [tool for tool in values
                  if _schema_name(tool) not in GATEWAY_TOOL_NAMES]
        values.extend(gateway)
        logger.warning(
            '[ToolGateway] compacted oversized gateway schemas: tokens=%d '
            '-> %d target=%d; request continues',
            gateway_tokens, tool_schema_tokens(gateway, model=model),
            LOCAL_GATEWAY_MAX_TOKENS)
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
    • a clean array is returned AS THE SAME OBJECT so prompt-cache bytes and
      identity-based fast paths are untouched on the hot path.

    Every intervention is logged loudly with the offending index and shape
    so the regressing producer is named in the logs.
    """
    if not isinstance(tools, list):
        return []
    offenders: list[str] = []
    repaired: list[str] = []
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
                schema_error = _wire_tool_schema_error(tool)
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
            if not drop and isinstance(tool.get('function'), dict) \
                    and tool.get('type') != 'function':
                keep = {**tool, 'type': 'function'}
                repaired.append(f'#{idx}:{name}')
        if out is None and (drop or keep is not tool):
            out = list(tools[:idx])
        if out is not None and not drop:
            out.append(keep)
    if out is None:
        return tools
    if offenders or repaired:
        logger.warning(
            '%s[ToolGateway] wire tools invariant enforced: dropped=%s '
            'repaired_type=%s — a producer emitted non-conforming entries; '
            'fix the producer, this boundary only contains the blast radius',
            log_prefix or '', offenders or '-', repaired or '-')
    return out


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
) -> list[dict[str, Any]]:
    """Project the PTC-local ``execute_tools`` surface for one round.

    Follows the Tool Search dual-backend precedent: the projection runs at
    the last common wire boundary and only shapes what the model sees —
    execution authority is unchanged.  The round's ordinary tools stay
    byte-stable and in order; exactly one tier-shaped ``execute_tools``
    schema carries the bounded read-only routing contract (``ptc_note``), so
    the policy travels with the tool the model actually calls.  Per-round
    observations (including eligible-name visibility and the serial-read-chain
    adoption signal) are not interpolated: for a fixed tier this gateway schema
    is a byte fixpoint, preserving the provider prompt-cache prefix.  An existing
    gateway schema (local Tool Search) is replaced rather than duplicated.
    Every model sees the free-form ``program`` parameter by default; only the
    explicit ``batch`` override shape strips it.
    """
    include_program = str(tier or '').strip().lower() != 'batch'
    normalized_tier = 'program' if include_program else 'batch'
    execute = _stable_local_execute_tools_schema(tier=normalized_tier)
    # PTC guidance is advisory; it must never make the fixed local gateway
    # pair unusable.  Leave headroom for model-tokenizer variance, and if an
    # unusually long future stable guidance edit consumes it, retain the
    # complete execution contract while dropping advisory text. Execution
    # authority still comes from the task-owned catalog in either shape.
    projected_pair = [search_tools_schema(), execute]
    guidance_target = max(0, LOCAL_GATEWAY_MAX_TOKENS - 50)
    if tool_schema_tokens(projected_pair) > guidance_target:
        execute = _stable_local_execute_tools_schema(tier=normalized_tier)
        logger.info(
            '[ToolGateway] compacted PTC guidance to preserve the %d-token '
            'gateway contract', LOCAL_GATEWAY_MAX_TOKENS)
    projected_pair = [search_tools_schema(), execute]
    if tool_schema_tokens(projected_pair) > LOCAL_GATEWAY_MAX_TOKENS:
        compacted_pair = _compact_gateway_schemas(projected_pair)
        execute = next(
            tool for tool in compacted_pair
            if _schema_name(tool) == EXECUTE_TOOLS_NAME)
        logger.warning(
            '[ToolGateway] compacted the fixed PTC gateway; request continues')
    out = [tool for tool in (catalog or [])
           if isinstance(tool, dict)
           and _schema_name(tool) != EXECUTE_TOOLS_NAME]
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
    'slides': (
        'slides', 'slide', 'deck', 'presentation', 'powerpoint', 'ppt',
        'keynote', '幻灯片', '演示文稿', '演示', 'ppt'),
    'create': (
        'create', 'make', 'generate', 'add', 'new', 'build', 'produce',
        '创建', '新建', '生成', '制作', '添加', '做一份'),
    'list': (
        'list', 'show', 'open', 'what', 'which', 'see',
        '列出', '查看', '看看', '有哪些', '显示'),
}


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


@lru_cache(maxsize=16_384)
def _terms_cached(text: str) -> tuple[str, ...]:
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


def _terms(value: Any) -> list[str]:
    return list(_terms_cached(str(value or '').lower()))


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
        padded = str(cursor) + '=' * (-len(str(cursor)) % 4)
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


def _search_notice(missing_name: str, hint_ns: str, total: int) -> str:
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
) -> dict[str, Any]:
    """BM25-rank the immutable task catalog without issuing authority."""
    query_text = str(query or '').strip()
    if not query_text:
        return {'status': 'error', 'error': {
            'code': 'invalid_query', 'message': 'query must be non-empty'}}
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
    qlower = query_text.lower()
    name_case = {name.lower(): name for name in catalog_index(catalog)
                 if name not in GATEWAY_TOOL_NAMES}
    docs: list[tuple[str, dict[str, Any], str, list[str]]] = []
    for name, tool in catalog_index(catalog).items():
        if name in GATEWAY_TOOL_NAMES:
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
        exact = name_case.get(qlower)
        if exact is None:
            missing_name = query_text
        elif ns_filter and all(row[0] != exact for row in docs):
            hint_ns = str(namespace_map.get(exact) or 'general').lower()
    if not docs:
        out: dict[str, Any] = {
            'status': 'ok', 'query': query_text, 'items': [],
            'execute_with': EXECUTE_TOOLS_NAME,
            'next_cursor': None, 'total': 0,
            'notice': _search_notice(missing_name, hint_ns, 0),
        }
        if missing_name:
            out['missing_name'] = missing_name
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
        action_concepts = {
            '@search', '@edit', '@cancel', '@create', '@list',
        }
        score += 3.0 * len(
            qconcepts.intersection(name_concepts).intersection(
                action_concepts))
        if qlower == lname:
            score += 100.0
        elif qlower in lname:
            score += 12.0
        if ns_filter and ns == ns_filter:
            score += 2.0
        if score > 0:
            scored.append((-score, position, name, tool, ns))
    scored.sort()
    page = scored[offset:offset + wanted]
    items = []
    for negative, _pos, name, tool, ns in page:
        fn = tool.get('function') if isinstance(tool.get('function'), dict) \
            else tool
        contract = contract_map.get(name)
        contract = contract if isinstance(contract, dict) else {}
        item = {
            'name': name,
            'namespace': ns,
            'description': str(fn.get('description') or ''),
            'arguments_schema': contract.get('arguments_schema') or fn.get('parameters') or {
                'type': 'object', 'properties': {}},
            'score': round(-negative, 6),
        }
        if contract:
            item.update({
                'contract_version': contract.get('contractVersion'),
                'help': contract.get('help') or item['description'],
                'permission': contract.get('permission'),
                'idempotency': contract.get('idempotency'),
                'ptc_eligible': bool(contract.get('ptcEligible')),
                'errors': contract.get('errors') or [],
            })
        items.append(item)
    next_offset = offset + len(page)
    out = {
        'status': 'ok', 'query': query_text, 'namespace': ns_filter or None,
        'items': items,
        'execute_with': EXECUTE_TOOLS_NAME,
        'next_cursor': (_cursor_encode(next_offset)
                        if next_offset < len(scored) else None),
        'total': len(scored),
        'notice': _search_notice(missing_name, hint_ns, len(scored)),
    }
    if missing_name:
        out['missing_name'] = missing_name
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
    'catalog_index', 'full_wire_tools',
    'execute_tools_schema',
    'fit_tool_schema_budget', 'gateway_tool_schemas', 'local_wire_tools',
    'normalize_execute_request',
    'normalize_gateway_call', 'ptc_local_wire_tools', 'resolve_catalog_name',
    'resolve_tool_search_backend', 'search_executable_catalog',
    'search_tools_schema', 'tool_schema_tokens',
]
