"""Handlers for local Tool Search and the stable execute adapter."""

from __future__ import annotations

import hashlib
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
    search_executable_catalog,
)

logger = get_logger(__name__)

_MAX_EXECUTE_GATEWAY_RECEIPTS = 256


def _execute_gateway_receipt_key(task, tc_id, fn_args, round_entry):
    """Key replay protection by call identity, round/world, and arguments.

    Kimi commonly recycles positional IDs such as ``execute_tools_0`` on every
    assistant message. An ID-only receipt therefore returns a stale result for
    a different later call. Canonical argument hashing still deduplicates an
    exact duplicate frame within the same round, while a new round or world
    version executes afresh.
    """
    try:
        canonical = json.dumps(
            fn_args, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), default=str)
    except Exception as exc:
        logger.debug(
            '[ToolGateway] canonical argument serialization failed; using repr: %s',
            exc,
        )
        canonical = repr(fn_args)
    digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    llm_round = int((round_entry or {}).get('llmRound') or 0)
    world_version = str(
        task.get('_worldVersion') or task.get('worldVersion') or '')
    return f'{tc_id}\x00{llm_round}\x00{world_version}\x00{digest}'


def _remember_execute_gateway_receipt(receipts, key, content, *, ok):
    receipts[key] = {'content': content, 'ok': bool(ok)}
    overflow = len(receipts) - _MAX_EXECUTE_GATEWAY_RECEIPTS
    if overflow > 0:
        for stale_key in list(receipts)[:overflow]:
            receipts.pop(stale_key, None)


def _replay_execute_gateway_receipt(task, receipts, receipt_key, tc_id, rn,
                                    round_entry, fn_name):
    receipt = receipts[receipt_key]
    if not isinstance(receipt, dict):
        raise ValueError('execute gateway receipt must be an object')
    content = str(receipt.get('content') or '')
    ok = bool(receipt.get('ok'))
    _finalize(task, rn, round_entry, fn_name, content, ok=ok)
    return tc_id, content, False

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
    # The verdict goes THROUGH the finalize seam so it is stamped before the
    # tool_result event is built and rides that frame — stamping it after the
    # emit (the old shape) shipped a verdict-less frame the client settled as
    # 'done', so a failed gateway call rendered ✓ until the next reload.
    _finalize_tool_round(
        task, rn, round_entry, display_results,
        query_override=query_override or name,
        extra_event_fields=extra_event_fields,
        status='done' if ok else 'error')


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




def _note_search_query(task: dict[str, Any], fn_args: Any) -> int:
    """Count identical (namespace, query) searches within one task.

    The catalog is fixed for the task's lifetime, so a repeated search can
    never return anything new — the count drives the loop-breaking notice.
    """
    if not isinstance(fn_args, dict):
        return 0
    key = '{}\x00{}'.format(
        str(fn_args.get('namespace') or '').strip().lower(),
        str(fn_args.get('query') or '').strip().lower())
    counts = task.setdefault('_tool_search_query_counts', {})
    if not isinstance(counts, dict):
        counts = {}
        task['_tool_search_query_counts'] = counts
    if key not in counts and len(counts) >= 128:
        return 1
    counts[key] = int(counts.get(key) or 0) + 1
    return counts[key]
@tool_registry.handler(SEARCH_TOOLS_NAME, category='tools',
                       description='Search the executable task tool catalog')
def handle_search_tools(
    task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
    project_path, project_enabled, all_tools=None,
):
    catalog = _executable_catalog(task, all_tools)
    try:
        result = search_executable_catalog(
            catalog, fn_args.get('query'),
            namespace=fn_args.get('namespace', ''),
            limit=fn_args.get('limit', 8), cursor=fn_args.get('cursor', ''),
            namespace_by_name=(
                task.get('_executableToolNamespaceByName') or {}),
            search_text_by_name=(
                task.get('_executableToolSearchTextByName') or {}),
            contract_documents_by_name=(
                task.get('_toolContractDocumentsByName') or {}))
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
    if result.get('status') == 'ok':
        repeat = _note_search_query(task, fn_args)
        if repeat >= 2:
            result['repeated_query'] = repeat
            result['notice'] = (
                f'This exact search has already run {repeat} times in this '
                'task against an unchanged catalog — the outcome cannot '
                'change. Break the loop: act on the results above, or treat '
                'the capability as unavailable this turn and proceed without '
                're-searching.')
            logger.warning(
                '[ToolSearch] repeated query x%d task=%s query=%.80r — '
                're-searching a stable catalog cannot change the outcome',
                repeat, task.get('id'), fn_args.get('query'))
        if result.get('missing_name'):
            logger.info(
                '[ToolSearch] exact-name miss task=%s query=%.80r total=%s',
                task.get('id'), fn_args.get('query'), result.get('total'))
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
    from lib.tasks_pkg.tool_dispatch.api import (
        execute_tool_pipeline,
        parse_tool_calls,
    )

    results_by_id: dict[str, dict[str, Any]] = {}
    # Do not add a gateway-local call-id cache here.  The shared execution
    # pipeline owns the durable name+arguments signature receipts for native,
    # gateway and ToolScript calls alike.  A cache keyed by ID alone would
    # return an old result for a DIFFERENT call that recycled the id — the
    # pipeline replays only an exact name+args match and executes a recycled
    # id with new args as a fresh call (positional-id models like kimi-k3
    # reuse ``{tool}_{index}`` every message; rejecting that reused id locked
    # the tool out for the rest of the task — conv mswu06rpir1hwv).
    fresh = list(calls)
    if fresh:
        assistant = {'role': 'assistant', 'content': '', 'tool_calls': fresh}
        base = _gateway_round_base(task)
        # Key the start-clock map by OBJECT identity, never by call id: the
        #   shared pipeline REMINTS any call id that already has a completed
        #   receipt in this task (positional-id recycle / exact re-emit),
        #   mutating ``call['id']`` in place — an id-keyed ``started`` then
        #   raised KeyError for every reminted gateway child (latent since the
        #   remint root fix; reachable by any recycled-id program call).
        started = {id(call): time.monotonic() for call in fresh}
        parsed, _ = parse_tool_calls(
            assistant, task, llm_round, base, project_enabled,
            early_announced=None)
        # ``execute_tool_pipeline`` appends role=tool results to the message
        # list it receives.  Seed that local transcript with the assistant
        # carrier that owns those calls, exactly like the top-level
        # orchestrator does.  Passing an empty list produced a structurally
        # impossible transcript (tool results with no preceding tool_calls):
        # the attended post-tool snapshot then had to delete every result as
        # an orphan, emitting a warning after each ``execute_tools`` child.
        # Keeping the pair together also makes any future consumer of this
        # local transcript inherit a valid OpenAI/Anthropic tool protocol.
        nested_messages: list[dict[str, Any]] = [assistant]
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
                started[id(call)])
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
    namespaces = task.get('_executableToolNamespaceByName') or {}
    search_text = task.get('_executableToolSearchTextByName') or {}
    run = _program_run(task, gateway_call_id, 'execute_program')
    run['code'] = source
    from lib.tasks_pkg.orchestrator._programmatic import project_program_run
    project_program_run(task, run, llm_round=llm_round, terminal=False)
    child_counter = 0
    program_started = time.monotonic()
    logger.info('[PTC] local program start task=%s model=%s source=%.200s',
                task.get('id'), model, source)

    # PTC-local read-only contract, refreshed every round by the orchestrator
    # (``task['_ptc_local']``).  While a programmatic round is active, program
    # child calls may only reach the reviewed read-only tools plus
    # ``search_tools`` discovery; anything else is a typed rejection so the
    # model re-issues it as an ordinary direct call with normal admission and
    # approval.  An absent latch leaves the generic Tool Search program path
    # fully open.
    latch = task.get('_ptc_local')
    gate_active = isinstance(latch, dict) and bool(latch)
    program_allowed = (
        {str(name) for name in (latch.get('eligible') or ())}
        if gate_active else set())
    if gate_active:
        run['ptcLocal'] = {
            'tier': str(latch.get('tier') or ''),
            'eligible': sorted(program_allowed),
        }

    def _assert_program_eligible(name: str) -> None:
        if not gate_active:
            return
        if name in program_allowed or name == SEARCH_TOOLS_NAME:
            return
        raise ToolScriptError(
            'tool_not_program_eligible',
            f'Tool {name!r} is not eligible for this programmatic round.',
            tool=name, eligible=sorted(program_allowed),
            retry_hint=(
                'Programs may only call the reviewed read-only tools and '
                'search_tools in this round. Issue writes, approvals, and '
                'other tools as ordinary direct calls instead.'))

    def search(query, namespace='', limit=8, cursor=''):
        return search_executable_catalog(
            catalog, query, namespace=namespace, limit=limit, cursor=cursor,
            namespace_by_name=namespaces, search_text_by_name=search_text,
            contract_documents_by_name=(
                task.get('_toolContractDocumentsByName') or {}))

    def call(name, arguments=None, call_id=None):
        nonlocal child_counter
        raw = {'name': name, 'arguments': arguments or {}}
        if call_id:
            raw['call_id'] = call_id
        normalized, error = normalize_gateway_call(
            raw, catalog=catalog, namespace_by_name=namespaces,
            gateway_call_id=gateway_call_id, index=child_counter,
            source='execute_program', contract_documents_by_name=(
                task.get('_toolContractDocumentsByName')
                if '_toolContractDocumentsByName' in task else None))
        child_counter += 1
        if error:
            return {'call_id': str(call_id or ''), 'name': str(name or ''),
                    'status': 'error', 'error': error,
                    'approval': {'required': False, 'status': 'not_required'},
                    'duration': 0, 'source': 'execute_program'}
        _assert_program_eligible(
            str((normalized.get('function') or {}).get('name') or ''))
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
                source='execute_program', contract_documents_by_name=(
                    task.get('_toolContractDocumentsByName')
                    if '_toolContractDocumentsByName' in task else None))
            child_counter += 1
            if error: errors.append(error)
            elif call_row:
                _assert_program_eligible(
                    str((call_row.get('function') or {}).get('name') or ''))
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
        elapsed_ms = int((time.monotonic() - program_started) * 1000)
        logger.info(
            '[PTC] local program completed task=%s model=%s children=%d '
            'steps=%d tool_calls=%d elapsed_ms=%d',
            task.get('id'), model, len(run['childCalls']),
            int(stats.get('steps') or 0), int(stats.get('tool_calls') or 0),
            elapsed_ms)
        audit_log('ptc_local_program', task_id=task.get('id'), model=model,
                  status='completed', children=len(run['childCalls']),
                  elapsed_ms=elapsed_ms)
        return {'status': 'ok', 'result': value, 'stats': stats}
    except ToolScriptError as exc:
        logger.debug('[ToolGateway] ToolScript rejected: %s', exc)
        run['status'] = 'error'
        run['error'] = exc.as_dict()
        logger.info(
            '[PTC] local program rejected task=%s model=%s kind=%s '
            'children=%d elapsed_ms=%d',
            task.get('id'), model, str(exc.as_dict().get('kind') or ''),
            len(run['childCalls']),
            int((time.monotonic() - program_started) * 1000))
        return {'status': 'error', 'error': exc.as_dict()}
    except Exception as exc:
        run['status'] = 'error'
        run['error'] = {
            'kind': 'program_internal_error',
            'message': f'{type(exc).__name__}: {exc}',
        }
        raise
    finally:
        run['tEnd'] = now_ms()
        project_program_run(task, run, llm_round=llm_round, terminal=True)


@tool_registry.handler(EXECUTE_TOOLS_NAME, category='tools',
                       description='Execute task-available tools or ToolScript')
def handle_execute_tools(
    task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
    project_path, project_enabled, all_tools=None,
):
    receipts = task.setdefault('_execute_gateway_receipts', {})
    receipt_key = _execute_gateway_receipt_key(
        task, tc_id, fn_args, round_entry)
    if receipt_key in receipts:
        return _replay_execute_gateway_receipt(
            task, receipts, receipt_key, tc_id, rn, round_entry, fn_name)

    with _gateway_lock(task):
        # The first check above is the fast path. This second check is the
        # race-closing path: two streamed/replayed copies can both miss before
        # one acquires the lock, and a write must still execute only once.
        if receipt_key in receipts:
            return _replay_execute_gateway_receipt(
                task, receipts, receipt_key, tc_id, rn, round_entry, fn_name)
        normalized = normalize_execute_request(
            fn_args, catalog=_executable_catalog(task, all_tools),
            namespace_by_name=(
                task.get('_executableToolNamespaceByName') or {}),
            gateway_call_id=tc_id, source='execute_calls',
            contract_documents_by_name=(
                task.get('_toolContractDocumentsByName')
                if '_toolContractDocumentsByName' in task else None))
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
        # A mixed batch still exposes each successful child in the payload, but
        # its outer settlement is a failure verdict. Never paint a failed child
        # batch as a green/done gateway round merely because some work landed.
        ok = payload['status'] == 'ok'
        _remember_execute_gateway_receipt(
            receipts, receipt_key, content, ok=ok)
        _finalize(task, rn, round_entry, fn_name, content, ok=ok)
        return tc_id, content, False


__all__ = ['handle_execute_tools', 'handle_search_tools']
