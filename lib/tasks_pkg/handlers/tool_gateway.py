"""Handlers for local Tool Search and the stable execute adapter."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from lib.agent_core.events import now_ms
from lib.log import audit_log, get_logger
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from lib.tools.gateway import (
    EXECUTE_TOOLS_NAME,
    SEARCH_TOOLS_NAME,
    normalize_execute_request,
    normalize_gateway_call,
    search_enabled_catalog,
)

logger = get_logger(__name__)

# Locks are runtime coordination objects and must never enter the persisted
# task snapshot.  Keep them process-local, keyed by the live task identity.
_GATEWAY_LOCKS: dict[int, threading.RLock] = {}
_GATEWAY_LOCKS_GUARD = threading.Lock()


def _gateway_lock(task: dict[str, Any]) -> threading.RLock:
    key = id(task)
    with _GATEWAY_LOCKS_GUARD:
        lock = _GATEWAY_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _GATEWAY_LOCKS[key] = lock
        return lock


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                      default=str)


def _executable_catalog(task: dict[str, Any], fallback=None) -> list[dict]:
    """Return task authority without confusing it with wire exposure."""
    if '_executable_tool_catalog' in task:
        return list(task.get('_executable_tool_catalog') or [])
    if '_enabled_tool_catalog' in task:
        return list(task.get('_enabled_tool_catalog') or [])
    return list(fallback or [])


def _finalize(task: dict[str, Any], rn: int, round_entry: dict[str, Any],
              name: str, content: str, *, ok: bool = True,
              results: list[dict[str, Any]] | None = None,
              query_override: str = '',
              extra_event_fields: dict[str, Any] | None = None) -> None:
    badge = 'tool gateway' if ok else 'tool gateway error'
    display_results = results if results is not None else [{
        'type': 'tool_gateway', 'toolName': name, 'title': name,
        'content': content, 'badge': badge,
    }]
    _finalize_tool_round(
        task, rn, round_entry, display_results,
        query_override=query_override or name,
        extra_event_fields=extra_event_fields)
    if not ok:
        round_entry['status'] = 'error'


def _schema_type_label(schema: Any) -> str:
    if not isinstance(schema, dict):
        return 'value'
    raw_type = schema.get('type')
    if isinstance(raw_type, list):
        types = [str(value) for value in raw_type if value != 'null']
        return ' | '.join(types) or 'value'
    if raw_type:
        return str(raw_type)
    if 'enum' in schema:
        return 'enum'
    if 'oneOf' in schema or 'anyOf' in schema:
        return 'variant'
    return 'value'


def _search_display_results(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Project model-facing search JSON into compact, safe UI card data."""
    rows: list[dict[str, Any]] = []
    for item in result.get('items') or []:
        if not isinstance(item, dict) or not item.get('name'):
            continue
        schema = item.get('arguments_schema')
        schema = schema if isinstance(schema, dict) else {}
        properties = schema.get('properties')
        properties = properties if isinstance(properties, dict) else {}
        required = {str(value) for value in (schema.get('required') or [])}
        arguments = []
        for arg_name, arg_schema in properties.items():
            arguments.append({
                'name': str(arg_name),
                'type': _schema_type_label(arg_schema),
                'required': str(arg_name) in required,
            })
        rows.append({
            'type': 'tool_catalog_match',
            'toolName': str(item['name']),
            'title': str(item['name']),
            'namespace': str(item.get('namespace') or 'general'),
            'snippet': str(item.get('description') or ''),
            'arguments': arguments,
            'score': item.get('score'),
        })
    return rows


@tool_registry.handler(SEARCH_TOOLS_NAME, category='tools',
                       description='Search the executable task tool catalog')
def handle_search_tools(
    task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
    project_path, project_enabled, all_tools=None,
):
    catalog = _executable_catalog(task, all_tools)
    try:
        result = search_enabled_catalog(
            catalog, fn_args.get('query'),
            namespace=fn_args.get('namespace', ''),
            limit=fn_args.get('limit', 8), cursor=fn_args.get('cursor', ''),
            namespace_by_name=task.get('_enabledToolNamespaceByName') or {},
            search_text_by_name=(
                task.get('_enabledToolSearchTextByName') or {}))
    except Exception as exc:
        # Local retrieval must fail open. Keep the full native schemas server-
        # owned, mark subsequent rounds to send the complete catalog, and give
        # this round a compact directory so the model can recover immediately.
        logger.warning('[ToolSearch] local catalog search failed: %s; '
                       'failing open to full executable catalog', exc,
                       exc_info=True)
        task['_tool_search_fail_open'] = True
        items = []
        for tool in catalog:
            fn = tool.get('function') if isinstance(tool, dict) else None
            if not isinstance(fn, dict) or not fn.get('name'):
                continue
            items.append({
                'name': str(fn['name']),
                'description': str(fn.get('description') or ''),
                'arguments_schema': fn.get('parameters') or {
                    'type': 'object', 'properties': {}},
            })
        result = {
            'status': 'ok', 'query': str(fn_args.get('query') or ''),
            'items': items, 'total': len(items), 'next_cursor': None,
            'execute_with': EXECUTE_TOOLS_NAME,
            'fail_open': True,
            'warning': 'Local search failed; full executable catalog restored.',
            'notice': ("Call execute_tools with a result's exact name and "
                       'arguments matching arguments_schema.'),
        }
    content = _json(result)
    search_meta = {
        'toolSearchTotal': int(result.get('total') or 0),
        'toolSearchNextCursor': result.get('next_cursor'),
        'toolSearchFailOpen': bool(result.get('fail_open')),
    }
    round_entry.update(search_meta)
    _finalize(task, rn, round_entry, fn_name, content,
              ok=result.get('status') == 'ok',
              results=(_search_display_results(result)
                       if result.get('status') == 'ok' else None),
              query_override=str(fn_args.get('query') or fn_name),
              extra_event_fields=search_meta)
    return tc_id, content, False


def _gateway_round_base(task: dict[str, Any]) -> int:
    existing = [int(row.get('roundNum') or 0)
                for row in task.get('toolRounds', [])
                if isinstance(row, dict)
                and 8_700_000 <= int(row.get('roundNum') or 0) < 8_800_000]
    return max(existing, default=8_699_999) + 1


def _approval_summary(round_entry: dict[str, Any] | None) -> dict[str, Any]:
    row = round_entry or {}
    required = bool(row.get('approvalId') or row.get('approvalMeta'))
    if not required:
        return {'required': False, 'status': 'not_required'}
    return {'required': True,
            'status': ('rejected' if row.get('status') == 'rejected'
                       else 'approved')}


def _receipt_from_round(call: dict[str, Any], row: dict[str, Any] | None,
                        output: Any, started: float) -> dict[str, Any]:
    name = str((call.get('function') or {}).get('name') or '')
    status = str((row or {}).get('status') or 'done')
    if status in ('searching', 'executing', 'pending_approval'):
        status = 'done'
    receipt = {
        'call_id': str(call.get('id') or ''), 'name': name,
        'status': status, 'approval': _approval_summary(row),
        'duration': max(0, int((time.monotonic() - started) * 1000)),
        'source': str(call.get('source') or 'execute_calls'),
    }
    if status in ('error', 'rejected', 'aborted'):
        receipt['error'] = output
    else:
        receipt['output'] = output
    repairs = call.get('_normalization_repairs') or []
    if repairs:
        receipt['normalization'] = {'repairs': repairs}
    return receipt


def _audit_gateway_repairs(task: dict[str, Any], call: dict[str, Any]) -> None:
    name = str((call.get('function') or {}).get('name') or '')
    for repair in call.get('_normalization_repairs') or ():
        if not isinstance(repair, dict):
            continue
        detail = {
            'tool': name, 'model': str(task.get('model') or ''),
            'path': str(repair.get('path') or ''),
            'kind': str(repair.get('kind') or ''),
            'confidence': repair.get('confidence'),
        }
        # Tool names are public catalog metadata. Argument values may be
        # sensitive, so their before/after forms stay out of audit logs.
        if repair.get('path') == '$.name':
            detail['attempted'] = repair.get('before')
            detail['resolved'] = repair.get('after')
        audit_log('tool_gateway_repaired', **detail)


def _execute_call_batch(
    task: dict[str, Any], calls: list[dict[str, Any]], *,
    cfg: dict[str, Any], project_path: str | None, project_enabled: bool,
    model: str, llm_round: int,
) -> list[dict[str, Any]]:
    if not calls:
        return []
    from lib.tasks_pkg.tool_dispatch import execute_tool_pipeline, parse_tool_calls

    results_by_id: dict[str, dict[str, Any]] = {}
    # Do not add a gateway-local call-id cache here.  The shared execution
    # pipeline owns the durable name+arguments signature receipts for native,
    # gateway and ToolScript calls alike.  A cache keyed by ID alone would
    # return an old result before the pipeline can reject a conflicting reuse
    # of that ID with different arguments.
    fresh = list(calls)
    if fresh:
        assistant = {'role': 'assistant', 'content': '', 'tool_calls': fresh}
        base = _gateway_round_base(task)
        started = {str(call.get('id') or ''): time.monotonic()
                   for call in fresh}
        parsed, _ = parse_tool_calls(
            assistant, task, llm_round, base, project_enabled,
            early_announced=None)
        nested_messages: list[dict[str, Any]] = []
        execute_tool_pipeline(
            task, parsed, cfg, project_path, project_enabled,
            _executable_catalog(task), nested_messages,
            [], llm_round, model)
        outputs = {str(message.get('tool_call_id') or ''): message.get('content')
                   for message in nested_messages
                   if isinstance(message, dict) and message.get('role') == 'tool'}
        rows = {str(row.get('toolCallId') or ''): row
                for row in task.get('toolRounds', []) if isinstance(row, dict)}
        for call in fresh:
            call_id = str(call.get('id') or '')
            receipt = _receipt_from_round(
                call, rows.get(call_id), outputs.get(call_id, ''),
                started[call_id])
            results_by_id[call_id] = receipt
    return [results_by_id[str(call.get('id') or '')] for call in calls]


def _execute_normalized(
    task: dict[str, Any], calls: list[dict[str, Any]], execution: str, *,
    cfg: dict[str, Any], project_path: str | None, project_enabled: bool,
    model: str, llm_round: int,
) -> list[dict[str, Any]]:
    if execution == 'sequential':
        out = []
        for call in calls:
            out.extend(_execute_call_batch(
                task, [call], cfg=cfg, project_path=project_path,
                project_enabled=project_enabled, model=model,
                llm_round=llm_round))
        return out
    # auto and parallel both enter the existing pipeline as one batch.  That
    # pipeline still serializes writes and approval-requiring tools. Bound
    # each wave to eight even when an operator configured the global executor
    # wider; ToolScript/gateway concurrency has its own hard contract.
    out = []
    for start in range(0, len(calls), 8):
        out.extend(_execute_call_batch(
            task, calls[start:start + 8], cfg=cfg, project_path=project_path,
            project_enabled=project_enabled, model=model,
            llm_round=llm_round))
    return out


def _program_run(task: dict[str, Any], call_id: str, source: str) -> dict[str, Any]:
    from lib.tools.toolscript import (
        MAX_AST_NODES, MAX_CONCURRENT_CALLS, MAX_NESTING, MAX_OUTPUT_BYTES,
        MAX_SOURCE_BYTES, MAX_STEPS, MAX_TOOL_CALLS,
    )
    for run in task.setdefault('programRuns', []):
        if isinstance(run, dict) and run.get('callId') == call_id:
            return run
    run = {
        'callId': call_id, 'source': source, 'code': '', 'status': 'running',
        'result': None, 'childCalls': [], 'tStart': now_ms(),
        'limits': {
            'maxSourceBytes': MAX_SOURCE_BYTES, 'maxAstNodes': MAX_AST_NODES,
            'maxSteps': MAX_STEPS, 'maxCalls': MAX_TOOL_CALLS,
            'maxConcurrentCalls': MAX_CONCURRENT_CALLS,
            'maxOutputBytes': MAX_OUTPUT_BYTES, 'maxNesting': MAX_NESTING,
        },
    }
    task['programRuns'].append(run)
    return run


def _execute_program(
    task: dict[str, Any], source: str, gateway_call_id: str, *,
    cfg: dict[str, Any], project_path: str | None, project_enabled: bool,
    model: str, llm_round: int,
) -> dict[str, Any]:
    from lib.tools.toolscript import ToolScriptError, execute_toolscript

    catalog = _executable_catalog(task)
    namespaces = task.get('_enabledToolNamespaceByName') or {}
    search_text = task.get('_enabledToolSearchTextByName') or {}
    run = _program_run(task, gateway_call_id, 'execute_program')
    run['code'] = source
    child_counter = 0

    def search(query, namespace='', limit=8, cursor=''):
        return search_enabled_catalog(
            catalog, query, namespace=namespace, limit=limit, cursor=cursor,
            namespace_by_name=namespaces, search_text_by_name=search_text)

    def call(name, arguments=None, call_id=None):
        nonlocal child_counter
        raw = {'name': name, 'arguments': arguments or {}}
        if call_id:
            raw['call_id'] = call_id
        normalized, error = normalize_gateway_call(
            raw, catalog=catalog, namespace_by_name=namespaces,
            gateway_call_id=gateway_call_id, index=child_counter,
            source='execute_program')
        child_counter += 1
        if error:
            return {'call_id': str(call_id or ''), 'name': str(name or ''),
                    'status': 'error', 'error': error,
                    'approval': {'required': False, 'status': 'not_required'},
                    'duration': 0, 'source': 'execute_program'}
        _audit_gateway_repairs(task, normalized)
        result = _execute_normalized(
            task, [normalized], 'sequential', cfg=cfg,
            project_path=project_path, project_enabled=project_enabled,
            model=model, llm_round=llm_round)[0]
        run['childCalls'].append({
            'id': result['call_id'], 'name': result['name'],
            'status': result['status']})
        return result

    def call_many(raw_calls, execution='auto'):
        nonlocal child_counter
        normalized = []
        errors = []
        for raw in raw_calls:
            call_row, error = normalize_gateway_call(
                raw, catalog=catalog, namespace_by_name=namespaces,
                gateway_call_id=gateway_call_id, index=child_counter,
                source='execute_program')
            child_counter += 1
            if error: errors.append(error)
            elif call_row:
                _audit_gateway_repairs(task, call_row)
                normalized.append(call_row)
        if errors:
            return [{'call_id': '', 'name': '', 'status': 'error',
                     'error': error, 'approval': {'required': False,
                                                 'status': 'not_required'},
                     'duration': 0, 'source': 'execute_program'}
                    for error in errors]
        results = _execute_normalized(
            task, normalized, execution if execution in (
                'auto', 'parallel', 'sequential') else 'auto', cfg=cfg,
            project_path=project_path, project_enabled=project_enabled,
            model=model, llm_round=llm_round)
        run['childCalls'].extend({
            'id': result['call_id'], 'name': result['name'],
            'status': result['status']} for result in results)
        return results

    try:
        value, stats = execute_toolscript(
            source, search=search, call=call, call_many=call_many,
            aborted=lambda: bool(task.get('aborted')))
        run['status'] = 'completed'
        run['result'] = value
        run['stats'] = stats
        return {'status': 'ok', 'result': value, 'stats': stats}
    except ToolScriptError as exc:
        logger.debug('[ToolGateway] ToolScript rejected: %s', exc)
        run['status'] = 'error'
        run['error'] = exc.as_dict()
        return {'status': 'error', 'error': exc.as_dict()}
    finally:
        run['tEnd'] = now_ms()


@tool_registry.handler(EXECUTE_TOOLS_NAME, category='tools',
                       description='Execute task-available tools or ToolScript')
def handle_execute_tools(
    task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
    project_path, project_enabled, all_tools=None,
):
    receipts = task.setdefault('_execute_gateway_receipts', {})
    if tc_id in receipts:
        content = receipts[tc_id]
        _finalize(task, rn, round_entry, fn_name, content, ok=True)
        return tc_id, content, False

    with _gateway_lock(task):
        # The first check above is the fast path. This second check is the
        # race-closing path: two streamed/replayed copies can both miss before
        # one acquires the lock, and a write must still execute only once.
        if tc_id in receipts:
            content = receipts[tc_id]
            _finalize(task, rn, round_entry, fn_name, content, ok=True)
            return tc_id, content, False
        normalized = normalize_execute_request(
            fn_args, catalog=_executable_catalog(task, all_tools),
            namespace_by_name=task.get('_enabledToolNamespaceByName') or {},
            gateway_call_id=tc_id, source='execute_calls')
        for call in normalized['calls']:
            _audit_gateway_repairs(task, call)
        payload: dict[str, Any] = {
            'status': 'ok', 'results': [],
            'warnings': normalized['warnings'],
        }
        if normalized['errors']:
            payload['status'] = 'error'
            payload['errors'] = normalized['errors']
        elif normalized['program'] is not None:
            payload['program'] = _execute_program(
                task, normalized['program'], tc_id, cfg=cfg,
                project_path=project_path, project_enabled=project_enabled,
                model=str(task.get('model') or ''),
                llm_round=int(round_entry.get('llmRound') or 0))
            payload['status'] = payload['program']['status']
        else:
            payload['results'] = _execute_normalized(
                task, normalized['calls'], normalized['execution'], cfg=cfg,
                project_path=project_path, project_enabled=project_enabled,
                model=str(task.get('model') or ''),
                llm_round=int(round_entry.get('llmRound') or 0))
            if any(result.get('status') in ('error', 'rejected', 'aborted')
                   for result in payload['results']):
                payload['status'] = 'partial_failure'
        content = _json(payload)
        receipts[tc_id] = content
        _finalize(task, rn, round_entry, fn_name, content,
                  ok=payload['status'] in ('ok', 'partial_failure'))
        return tc_id, content, False


__all__ = ['handle_execute_tools', 'handle_search_tools']
