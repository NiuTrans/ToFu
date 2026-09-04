"""Handlers for local Tool Search and the stable execute adapter."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from typing import Any

from lib.agent_core.events import now_ms
from lib.log import audit_log, get_logger
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from lib.tasks_pkg.tool_dispatch._flags import (
    _call_id_signature,
    _execute_gateway_delegation_scope,
)
from lib.tool_history_pairing import adjacent_tool_call_result_pairs
from lib.weak_lock_pool import WeakLockPool
from lib.tools.gateway import (
    EXECUTE_TOOLS_NAME,
    SEARCH_TOOLS_NAME,
    normalize_execute_request,
    normalize_gateway_call,
    search_executable_catalog,
)

logger = get_logger(__name__)

_MAX_EXECUTE_GATEWAY_RECEIPTS = 256
_TOOLSCRIPT_BATCH_FALLBACK_AFTER = 2
_TOOLSCRIPT_AUTHORING_ERRORS = frozenset({
    'syntax_error', 'unsafe_call', 'unknown_name', 'json_error', 'type_error',
})


def _gateway_llm_round(round_entry, fallback_round_num) -> int:
    """Return a non-negative live round or reject ambiguous receipt identity."""
    row = round_entry if isinstance(round_entry, Mapping) else {}
    raw_round = row.get('llmRound')
    if raw_round is None:
        raw_round = fallback_round_num
    if (not isinstance(raw_round, int) or isinstance(raw_round, bool)
            or raw_round < 0):
        raise ValueError(
            'execute_tools requires a non-negative integer llmRound')
    return raw_round


def _execute_gateway_receipt_key(
        task, tc_id, fn_args, round_entry, *, fallback_round_num=0):
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
    llm_round = _gateway_llm_round(round_entry, fallback_round_num)
    attempt_id = str(
        task.get('_attemptId') or task.get('attemptId') or task.get('id') or '')
    world_version = str(
        task.get('_worldVersion') or task.get('worldVersion') or '')
    return (f'{attempt_id}\x00{tc_id}\x00{llm_round}\x00'
            f'{world_version}\x00{digest}')


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
# task snapshot. Active gateway callers retain the per-task lock; completed
# task identities disappear automatically, avoiding both ID reuse and a
# process-lifetime id(task) registry.
_GATEWAY_LOCKS = WeakLockPool(threading.RLock)


def _gateway_lock(task: dict[str, Any]) -> threading.RLock:
    return _GATEWAY_LOCKS.lock_for(id(task))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                      default=str)


def _executable_catalog(task: dict[str, Any], fallback=None) -> list[dict]:
    """Return task authority without confusing it with wire exposure."""
    if '_executable_tool_catalog' in task:
        return list(task.get('_executable_tool_catalog') or [])
    return list(fallback or [])


def _schema_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ''
    fn = tool.get('function')
    fn = fn if isinstance(fn, dict) else tool
    return str(fn.get('name') or '').strip()


def _model_visible_tool_names(task: dict[str, Any]) -> frozenset[str]:
    """Return the exact final provider surface, with assembly as fallback.

    ``_tool_schema`` is the pre-transport proposal and can be wider than the
    provider request after schema-budget fitting. The latest bounded
    ``tool_wire_projection`` event records what the model actually received.
    Standalone runners that do not emit request diagnostics fall back to their
    latched wire schema.
    """
    events = task.get('events')
    if isinstance(events, (list, tuple)):
        for event in reversed(events):
            if not isinstance(event, dict) \
                    or event.get('type') != 'tool_wire_projection':
                continue
            names = event.get('toolNames')
            if isinstance(names, (list, tuple)):
                return frozenset(
                    str(name).strip() for name in names if str(name).strip())
    schemas = task.get('_tool_schema')
    if not isinstance(schemas, (list, tuple)):
        return frozenset()
    return frozenset(name for name in map(_schema_name, schemas) if name)


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
    from lib.tools.disclosure_state import disclosed_names_for_catalog
    visible_names = (
        _model_visible_tool_names(task)
        | disclosed_names_for_catalog(task, catalog)
    )
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
                task.get('_toolContractDocumentsByName') or {}),
            visible_names=_model_visible_tool_names(task),
            disclosed_names=disclosed_names_for_catalog(task, catalog))
    except Exception as exc:
        # Local retrieval must fail open. Keep native schemas server-owned,
        # mark subsequent rounds to send the complete catalog, and give this
        # round a compact hidden-tool directory so it can recover immediately.
        logger.warning('[ToolSearch] local catalog search failed: %s; '
                       'failing open to hidden executable catalog', exc,
                       exc_info=True)
        task['_tool_search_fail_open'] = True
        items = []
        for tool in catalog:
            fn = tool.get('function') if isinstance(tool, dict) else None
            if not isinstance(fn, dict) or not fn.get('name'):
                continue
            if str(fn['name']) in visible_names:
                continue
            params = fn.get('parameters') or {}
            properties = params.get('properties') \
                if isinstance(params, dict) else {}
            items.append({
                'name': str(fn['name']),
                'description': ' '.join(
                    str(fn.get('description') or '').split())[:240],
                'arguments_schema': {
                    'type': 'object',
                    'properties': {
                        str(name): {
                            'type': str((schema or {}).get('type') or 'any')
                        }
                        for name, schema in (properties or {}).items()
                        if isinstance(schema, dict)
                    },
                    'required': list(params.get('required') or [])
                    if isinstance(params, dict) else [],
                },
            })
            if len(items) >= 20:
                break
        result = {
            'status': 'ok', 'query': str(fn_args.get('query') or ''),
            'items': items, 'total': len(items), 'next_cursor': None,
            'execute_with': EXECUTE_TOOLS_NAME,
            'fail_open': True,
            'directory_truncated': (
                len(items) < sum(
                    _schema_name(tool) not in visible_names
                    for tool in catalog if _schema_name(tool))),
            'warning': (
                'Local search failed; hidden executable catalog restored.'),
            'notice': ("Call execute_tools with a result's exact name and "
                       'arguments matching arguments_schema.'),
        }
    if result.get('status') == 'ok':
        repeat = _note_search_query(task, fn_args)
        # Search result schemas are model disclosures, not imports. Remember
        # only the page actually returned; pagination can still disclose later
        # candidates, while an unchanged schema cannot reappear next round.
        from lib.tools.disclosure_state import record_search_items
        record_search_items(task, result.get('items'), catalog=catalog)
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
    existing = []
    raw_rounds = task.get('toolRounds')
    for row in raw_rounds if isinstance(raw_rounds, list) else []:
        if not isinstance(row, dict):
            continue
        round_number = row.get('roundNum')
        if (isinstance(round_number, int) and not isinstance(round_number, bool)
                and 8_700_000 <= round_number < 8_800_000):
            existing.append(round_number)
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
                        output: Any, started: float,
                        internal_delivery: dict[str, Any] | None = None,
                        ) -> dict[str, Any]:
    """Build the ordinary receipt for a child that executed here."""
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
    delivered_output = (
        internal_delivery.get('content')
        if isinstance(internal_delivery, dict) else output)
    if status in ('error', 'rejected', 'aborted'):
        receipt['error'] = delivered_output
    else:
        receipt['output'] = delivered_output
    if isinstance(internal_delivery, dict):
        receipt['output_truncated'] = bool(
            internal_delivery.get('truncated'))
        receipt['raw_output_bytes'] = max(
            0, int(internal_delivery.get('rawBytes') or 0))
        receipt['output_bytes'] = max(
            0, int(internal_delivery.get('outputBytes') or 0))
    repairs = call.get('_normalization_repairs') or []
    if repairs:
        receipt['normalization'] = {'repairs': repairs}
    return receipt


def _delegated_child_receipt(
    call: dict[str, Any], direct_call_id: str,
) -> dict[str, Any]:
    """Truthfully point at the direct sibling that owns this occurrence."""
    name = str((call.get('function') or {}).get('name') or '')
    reason = (
        'Not executed here: this occurrence is identical to direct sibling '
        f'{direct_call_id} in the same assistant response. The direct {name} '
        'call is authoritative; use its separate tool result.')
    return {
        'call_id': str(call.get('id') or ''),
        'name': name,
        'status': 'delegated',
        'delegation': {
            'kind': 'same_response_direct_call',
            'direct_call_id': str(direct_call_id),
        },
        'approval': {'required': False, 'status': 'not_required'},
        'duration': 0,
        'source': str(call.get('source') or 'execute_calls'),
        'output': reason,
    }


def _partition_delegated_gateway_children(
    task: dict[str, Any], calls: list[dict[str, Any]], llm_round: int,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[int, dict[str, Any]]]:
    """Pair calls[] children with surviving direct siblings, FIFO and 1:1."""
    state = task.get('_execute_gateway_direct_siblings')
    if not isinstance(state, dict):
        return list(enumerate(calls)), {}
    scope = _execute_gateway_delegation_scope(task, llm_round)
    if state.get('scope') != scope:
        return list(enumerate(calls)), {}
    registry = state.get('by_signature')
    if not isinstance(registry, dict):
        return list(enumerate(calls)), {}

    executing: list[tuple[int, dict[str, Any]]] = []
    delegated: dict[int, dict[str, Any]] = {}
    for index, call in enumerate(calls):
        name = str((call.get('function') or {}).get('name') or '')
        signature = _call_id_signature(
            name, call.get('_normalized_arguments') or {})
        direct_ids = registry.get(signature)
        if not isinstance(direct_ids, list) or not direct_ids:
            executing.append((index, call))
            continue
        direct_call_id = str(direct_ids.pop(0))
        if not direct_ids:
            registry.pop(signature, None)
        delegated[index] = _delegated_child_receipt(call, direct_call_id)
        audit_log(
            'execute_gateway_child_delegated',
            task_id=str(task.get('id') or ''),
            model=str(task.get('model') or ''),
            tool=name,
            direct_call_id=direct_call_id,
            gateway_child_call_id=str(call.get('id') or ''),
            llm_round=llm_round,
        )
        logger.info(
            '[ToolGateway] delegated calls[] child %s to direct call %s '
            '(tool=%s round=%d); duplicate execution suppressed',
            str(call.get('id') or ''), direct_call_id, name, llm_round)
    return executing, delegated


def _execute_calls_with_direct_delegation(
    task: dict[str, Any], calls: list[dict[str, Any]], execution: str, *,
    cfg: dict[str, Any], project_path: str | None, project_enabled: bool,
    model: str, llm_round: int,
) -> list[dict[str, Any]]:
    """Execute unmatched children and restore receipts to calls[] order."""
    executing, delegated = _partition_delegated_gateway_children(
        task, calls, llm_round)
    executed = _execute_normalized(
        task, [call for _index, call in executing], execution,
        cfg=cfg, project_path=project_path,
        project_enabled=project_enabled, model=model, llm_round=llm_round)
    if len(executed) != len(executing):
        raise RuntimeError(
            'execute_tools child receipt cardinality mismatch: '
            f'expected {len(executing)}, received {len(executed)}')
    receipts: list[dict[str, Any] | None] = [None] * len(calls)
    for index, receipt in delegated.items():
        receipts[index] = receipt
    for (index, _call), receipt in zip(executing, executed):
        receipts[index] = receipt
    if any(receipt is None for receipt in receipts):
        raise RuntimeError('execute_tools child receipt cardinality mismatch')
    return [receipt for receipt in receipts if receipt is not None]


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
    program_result_budget: Any = None,
) -> list[dict[str, Any]]:
    if not calls:
        return []
    from lib.tasks_pkg.tool_dispatch.api import (
        execute_tool_pipeline,
        parse_tool_calls,
    )

    # Do not add a gateway-child call-id cache here. The shared execution
    # pipeline remints every completed/recycled child id and executes the new
    # response occurrence. Its separate read cache may satisfy an idempotent
    # read after freshness checks, but a receipt never substitutes for a new
    # model action merely because name and arguments are equal.
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
        row_by_call_object = {
            id(parsed_call): parsed_row
            for parsed_call, _name, _call_id, _args, _round_num, parsed_row,
            _parse_error in parsed
        }
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
        internal_result_sink = (
            program_result_budget.begin_batch(
                [parsed_call[2] for parsed_call in parsed])
            if program_result_budget is not None else None)
        try:
            execute_tool_pipeline(
                task, parsed, cfg, project_path, project_enabled,
                _executable_catalog(task), nested_messages,
                [], llm_round, model,
                internal_result_sink=internal_result_sink,
                publish_direct_gateway_siblings=False)
        finally:
            if internal_result_sink is not None:
                internal_result_sink.finish()
        # Pair by the owning call OBJECT inside this adjacent run. Even though
        # the shared pipeline remints duplicate/recycled ids, a future repair
        # regression must not let an id-keyed dict overwrite one sibling's
        # output with another's.
        output_by_call_object = {
            id(parsed_call): result_message.get('content')
            for parsed_call, result_message
            in adjacent_tool_call_result_pairs(nested_messages)
        }
        receipts = []
        for call in fresh:
            receipts.append(_receipt_from_round(
                call,
                row_by_call_object.get(id(call)),
                output_by_call_object.get(id(call), ''),
                started[id(call)],
                (internal_result_sink.result(call.get('id'))
                 if internal_result_sink is not None else None)))
        return receipts
    return []


def _execute_normalized(
    task: dict[str, Any], calls: list[dict[str, Any]], execution: str, *,
    cfg: dict[str, Any], project_path: str | None, project_enabled: bool,
    model: str, llm_round: int,
    program_result_budget: Any = None,
) -> list[dict[str, Any]]:
    if execution == 'sequential':
        out = []
        for call in calls:
            out.extend(_execute_call_batch(
                task, [call], cfg=cfg, project_path=project_path,
                project_enabled=project_enabled, model=model,
                llm_round=llm_round,
                program_result_budget=program_result_budget))
        return out
    # ``calls`` is one semantic gateway batch. Keep every ordered occurrence
    # intact; equal name+argument payloads at different positions are distinct
    # model actions. The pipeline caps gateway/program children at eight
    # workers while retaining all logical receipts and serializing
    # writes/approval tools itself.
    return _execute_call_batch(
        task, calls, cfg=cfg, project_path=project_path,
        project_enabled=project_enabled, model=model, llm_round=llm_round,
        program_result_budget=program_result_budget)


def _program_run(task: dict[str, Any], call_id: str, source: str) -> dict[str, Any]:
    from lib.tools.toolscript import (
        MAX_AST_NODES, MAX_CONCURRENT_CALLS, MAX_NESTING, MAX_OUTPUT_BYTES,
        MAX_SOURCE_BYTES, MAX_STEPS, MAX_SYNTAX_REPAIRS, MAX_TOOL_CALLS,
    )
    runs = task.setdefault('programRuns', [])
    if not isinstance(runs, list):
        runs = []
        task['programRuns'] = runs
    provider_call_id = call_id
    for run in runs:
        if (isinstance(run, dict)
                and str(run.get('source') or '') == source
                and (run.get('callId') == call_id
                     or run.get('gatewayCallId') == call_id)):
            run.setdefault('gatewayCallId', provider_call_id)
            return run
    used_ids = {
        str(run.get('callId') or '')
        for run in runs if isinstance(run, dict)
    }
    if call_id in used_ids:
        ordinal = len(used_ids) + 1
        base = str(call_id or 'program')[:80]
        while f'{base}__tofu_local_{ordinal}' in used_ids:
            ordinal += 1
        call_id = f'{base}__tofu_local_{ordinal}'
    run = {
        'callId': call_id, 'gatewayCallId': provider_call_id,
        'source': source, 'code': '', 'status': 'running',
        'result': None, 'childCalls': [], 'tStart': now_ms(),
        'limits': {
            'maxSourceBytes': MAX_SOURCE_BYTES, 'maxAstNodes': MAX_AST_NODES,
            'maxSteps': MAX_STEPS, 'maxCalls': MAX_TOOL_CALLS,
            'maxConcurrentCalls': MAX_CONCURRENT_CALLS,
            'maxOutputBytes': MAX_OUTPUT_BYTES, 'maxNesting': MAX_NESTING,
            'maxSyntaxRepairs': MAX_SYNTAX_REPAIRS,
        },
    }
    runs.append(run)
    return run


def _execute_program(
    task: dict[str, Any], source: str, gateway_call_id: str, *,
    cfg: dict[str, Any], project_path: str | None, project_enabled: bool,
    model: str, llm_round: int,
) -> dict[str, Any]:
    from lib.tools.toolscript import (
        MAX_SYNTAX_REPAIRS, ToolScriptError, execute_toolscript,
    )
    from lib.tools.programmatic import ProgrammaticResultBudget

    catalog = _executable_catalog(task)
    namespaces = task.get('_executableToolNamespaceByName') or {}
    search_text = task.get('_executableToolSearchTextByName') or {}
    run = _program_run(task, gateway_call_id, 'execute_program')
    program_result_budget = ProgrammaticResultBudget()
    run['code'] = source
    from lib.tasks_pkg.orchestrator._programmatic import project_program_run
    project_program_run(task, run, llm_round=llm_round, terminal=False)
    child_counter = 0
    program_started = time.monotonic()
    logger.info('[PTC] local program start task=%s model=%s source=%.200s',
                task.get('id'), model, source)

    # ``task['_ptc_local']`` selects the local wire tier; it is not a second
    # execution authority. ToolScript children use the same immutable task
    # catalog, request-owned ToolContract validation, approval checks, and
    # execution pipeline as ordinary ``execute_tools.calls``. In particular,
    # discovery and the hosted-PTC eligible list never grant or remove local
    # authority: a model that already knows a valid name/schema may call it.
    latch = task.get('_ptc_local')
    if isinstance(latch, dict) and latch:
        run['ptcLocal'] = {
            'tier': str(latch.get('tier') or ''),
        }

    def search(query, namespace='', limit=8, cursor=''):
        from lib.tools.disclosure_state import disclosed_names_for_catalog
        return search_executable_catalog(
            catalog, query, namespace=namespace, limit=limit, cursor=cursor,
            namespace_by_name=namespaces, search_text_by_name=search_text,
            contract_documents_by_name=(
                task.get('_toolContractDocumentsByName') or {}),
            visible_names=_model_visible_tool_names(task),
            disclosed_names=disclosed_names_for_catalog(task, catalog))

    def call(name, arguments=None, call_id=None):
        nonlocal child_counter
        # Only absence means an empty argument object. Truthiness coercion
        # turned explicit invalid values such as ``[]``, ``""``, or ``0``
        # into a valid no-arg call and could authorize unintended execution.
        raw = {
            'name': name,
            'arguments': {} if arguments is None else arguments,
        }
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
        normalized['_presentationParentToolCallId'] = run['callId']
        _audit_gateway_repairs(task, normalized)
        result = _execute_normalized(
            task, [normalized], 'sequential', cfg=cfg,
            project_path=project_path, project_enabled=project_enabled,
            model=model, llm_round=llm_round,
            program_result_budget=program_result_budget)[0]
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
                call_row['_presentationParentToolCallId'] = run['callId']
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
            model=model, llm_round=llm_round,
            program_result_budget=program_result_budget)
        run['childCalls'].extend({
            'id': result['call_id'], 'name': result['name'],
            'status': result['status']} for result in results)
        return results

    def remember_syntax_repairs(document: Any) -> list[dict[str, Any]]:
        raw_repairs = (document.get('syntax_repairs')
                       if isinstance(document, dict) else None)
        if not isinstance(raw_repairs, list):
            raw_repairs = []
        repairs = [{
            'kind': str(item.get('kind') or ''),
            'offset': max(0, int(item.get('offset') or 0)),
        } for item in raw_repairs[:MAX_SYNTAX_REPAIRS]
            if isinstance(item, dict)]
        if not repairs:
            return []
        run['syntaxRepairs'] = repairs
        audit_log(
            'toolscript_syntax_repaired', task_id=task.get('id'),
            model=model, repair_count=len(repairs),
            kinds=[item['kind'] for item in repairs],
            offsets=[item['offset'] for item in repairs])
        return repairs

    try:
        value, stats = execute_toolscript(
            source, search=search, call=call, call_many=call_many,
            aborted=lambda: bool(task.get('aborted')))
        run['status'] = 'completed'
        run['result'] = value
        task['_toolScriptConsecutiveAuthoringFailures'] = 0
        stats['result_delivery'] = program_result_budget.stats()
        run['stats'] = stats
        syntax_repairs = remember_syntax_repairs(stats)
        elapsed_ms = int((time.monotonic() - program_started) * 1000)
        logger.info(
            '[PTC] local program completed task=%s model=%s children=%d '
            'steps=%d tool_calls=%d syntax_repairs=%d elapsed_ms=%d',
            task.get('id'), model, len(run['childCalls']),
            int(stats.get('steps') or 0), int(stats.get('tool_calls') or 0),
            len(syntax_repairs), elapsed_ms)
        audit_log('ptc_local_program', task_id=task.get('id'), model=model,
                  status='completed', children=len(run['childCalls']),
                  elapsed_ms=elapsed_ms)
        return {'status': 'ok', 'result': value, 'stats': stats}
    except ToolScriptError as exc:
        logger.debug('[ToolGateway] ToolScript rejected: %s', exc)
        run['status'] = 'error'
        run['error'] = exc.as_dict()
        syntax_repairs = remember_syntax_repairs(run['error'])
        error_code = str(run['error'].get('code') or '')
        if not run['childCalls'] and error_code in _TOOLSCRIPT_AUTHORING_ERRORS:
            failure_count = min(
                _TOOLSCRIPT_BATCH_FALLBACK_AFTER,
                max(0, int(task.get(
                    '_toolScriptConsecutiveAuthoringFailures') or 0)) + 1)
            task['_toolScriptConsecutiveAuthoringFailures'] = failure_count
            if failure_count >= _TOOLSCRIPT_BATCH_FALLBACK_AFTER:
                task['_toolScriptBatchFallback'] = True
                if isinstance(task.get('_ptc_local'), dict):
                    task['_ptc_local']['tier'] = 'batch'
                logger.info(
                    '[PTC] local ToolScript authoring fallback task=%s '
                    'model=%s failures=%d next_tier=batch',
                    task.get('id'), model, failure_count)
        else:
            task['_toolScriptConsecutiveAuthoringFailures'] = 0
        logger.info(
            '[PTC] local program rejected task=%s model=%s kind=%s '
            'children=%d syntax_repairs=%d elapsed_ms=%d',
            task.get('id'), model,
            str(run['error'].get('code') or run['error'].get('kind') or ''),
            len(run['childCalls']), len(syntax_repairs),
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
        run['internalResultBudget'] = program_result_budget.stats()
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
        task, tc_id, fn_args, round_entry, fallback_round_num=rn)
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
            local_latch = task.get('_ptc_local')
            program_disabled = (
                task.get('_toolScriptBatchFallback')
                or (isinstance(local_latch, dict)
                    and local_latch.get('tier') == 'batch'))
            if program_disabled:
                payload['program'] = {
                    'status': 'error',
                    'error': {
                        'code': 'toolscript_batch_fallback',
                        'message': (
                            'ToolScript is disabled for the remainder of this '
                            'task after repeated authoring failures.'),
                        'retry_hint': (
                            'Use execute_tools calls[] with '
                            'execution="parallel" or "sequential".'),
                    },
                }
            else:
                payload['program'] = _execute_program(
                    task, normalized['program'], tc_id, cfg=cfg,
                    project_path=project_path, project_enabled=project_enabled,
                    model=str(task.get('model') or ''),
                    llm_round=_gateway_llm_round(round_entry, rn))
            payload['status'] = payload['program']['status']
        else:
            _llm_round = _gateway_llm_round(round_entry, rn)
            payload['results'] = _execute_calls_with_direct_delegation(
                task, normalized['calls'], normalized['execution'], cfg=cfg,
                project_path=project_path, project_enabled=project_enabled,
                model=str(task.get('model') or ''), llm_round=_llm_round)
            _delegated_count = sum(
                result.get('status') == 'delegated'
                for result in payload['results'])
            if _delegated_count:
                round_entry['_delegatedChildren'] = _delegated_count
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
