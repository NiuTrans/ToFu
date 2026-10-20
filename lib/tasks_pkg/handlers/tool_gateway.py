"""Handlers for local Tool Search and the stable execute adapter."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from typing import Any

from lib.agent_core.execution_clock import task_execution_time_ms
from lib.log import audit_log, get_logger
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from lib.tasks_pkg.tool_dispatch._flags import (
    _call_id_signature,
    _execute_gateway_delegation_scope,
)
from lib.tasks_pkg.tool_search_outcome import audit_search_outcome
from lib.tasks_pkg.tool_search_projection import (
    tool_search_display_results,
    tool_search_metadata,
)
from lib.tool_results.tool_history_pairing import adjacent_tool_call_result_pairs
from lib.weak_lock_pool import WeakLockPool
from lib.tools.gateway import (
    EXECUTE_TOOLS_NAME,
    EXECUTE_PROGRAM_NAME,
    SEARCH_TOOLS_NAME,
    normalize_execute_request,
    normalize_gateway_call,
    search_executable_catalog,
    search_execution_notice,
)

from lib.tools.program_contract import MAX_OUTPUT_BYTES

logger = get_logger(__name__)

_MAX_EXECUTE_GATEWAY_RECEIPTS = 256
_PROGRAM_BATCH_FALLBACK_AFTER = 2
_PROGRAM_AUTHORING_ERRORS = frozenset({
    'syntax_error', 'javascript_error', 'unawaited_tools', 'unresolved_promise',
    'type_error', 'invalid_call', 'missing_program_result',
    'unhandled_promise_rejection',
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
    content = receipt.get('content') or ''
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


def _search_execution_gateway(task):
    names = _model_visible_tool_names(task)
    return (EXECUTE_PROGRAM_NAME if EXECUTE_PROGRAM_NAME in names
            and EXECUTE_TOOLS_NAME not in names else EXECUTE_TOOLS_NAME)


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




def _note_search_query(task: dict[str, Any], fn_args: Any, catalog) -> int:
    """Break repeated searches only while the current catalog is unchanged."""
    if not isinstance(fn_args, dict):
        return 0
    fingerprint = hashlib.sha256('\x00'.join(
        sorted(_schema_name(tool) for tool in catalog)).encode()).hexdigest()
    key = '{}\x00{}\x00{}'.format(
        fingerprint,
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


def _with_browser_availability(result, task, catalog, fn_args):
    """Keep direct and programmatic discovery on the same owner evidence."""
    if result.get('status') == 'ok':
        from lib.browser.availability import browser_search_availability
        # Owner identity is optional only in offline schema tests. Never probe
        # the operator/global client view when the task has no authenticated owner.
        availability = browser_search_availability(
            int(task.get('_userId') or 0), str(fn_args.get('query') or ''),
            str(fn_args.get('namespace') or ''),
            (_schema_name(tool) for tool in catalog))
        if availability:
            result['capability_availability'] = [availability]
            if availability.get('tool_name'):
                result.pop('missing_name', None)
            result['notice'] = (
                (result['notice'] + ' ' if result.get('missing_name') else '')
                + availability['message'] + ' ' + availability['recovery_hint'])
    return result


@tool_registry.handler(SEARCH_TOOLS_NAME, category='tools',
                       description='Search the executable task tool catalog')
def handle_search_tools(
    task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
    project_path, project_enabled, all_tools=None,
):
    catalog = _executable_catalog(task, all_tools)
    from lib.tools.disclosure_state import disclosed_names_for_catalog
    search_visible = _model_visible_tool_names(task)
    search_disclosed = disclosed_names_for_catalog(task, catalog)
    visible_names = search_visible | search_disclosed
    search_started = time.monotonic()
    search_exception = None
    try:
        result = search_executable_catalog(
            catalog, fn_args.get('query'),
            execution_gateway=_search_execution_gateway(task),
            namespace=fn_args.get('namespace', ''),
            limit=fn_args.get('limit', 8), cursor=fn_args.get('cursor', ''),
            namespace_by_name=(
                task.get('_executableToolNamespaceByName') or {}),
            search_text_by_name=(
                task.get('_executableToolSearchTextByName') or {}),
            contract_documents_by_name=(
                task.get('_toolContractDocumentsByName') or {}),
            visible_names=search_visible,
            disclosed_names=search_disclosed)
    except Exception as exc:
        search_exception = exc
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
            'execute_with': _search_execution_gateway(task),
            'fail_open': True,
            'directory_truncated': (
                len(items) < sum(
                    _schema_name(tool) not in visible_names
                    for tool in catalog if _schema_name(tool))),
            'warning': (
                'Local search failed; hidden executable catalog restored.'),
            'notice': search_execution_notice(_search_execution_gateway(task)),
        }
    if result.get('status') == 'ok':
        result = _with_browser_availability(result, task, catalog, fn_args)
        repeat = _note_search_query(task, fn_args, catalog)
        # Search result schemas are model disclosures, not imports. Remember
        # only the page actually returned; pagination can still disclose later
        # candidates, while an unchanged schema cannot reappear next round.
        from lib.tools.disclosure_state import record_search_items
        record_search_items(task, result.get('items'), catalog=catalog)
        if repeat >= 2:
            result['repeated_query'] = repeat
            exact_recovery = bool(
                result.get('already_disclosed')
                or result.get('already_visible')
                or result.get('resolved_name'))
            if exact_recovery:
                # Re-reading one schema after compaction dropped it is a
                # legitimate recovery, not a re-search loop: keep the
                # recovery notice that says how to call the tool.
                logger.info(
                    '[ToolSearch] repeated exact-name recovery x%d task=%s '
                    'query=%.80r', repeat, task.get('id'),
                    fn_args.get('query'))
            elif not result.get('capability_availability'):
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
    audit_search_outcome(
        task, fn_args, result, source='search_tools', call_id=tc_id,
        llm_round=round_entry.get('llmRound', rn),
        catalog_names=(_schema_name(tool) for tool in catalog),
        visible_names=search_visible,
        disclosed_names=search_disclosed,
        exception=search_exception,
        duration_ms=(time.monotonic() - search_started) * 1000)
    content = _json(result)
    search_meta = tool_search_metadata(result)
    round_entry.update(search_meta)
    _finalize(task, rn, round_entry, fn_name, content,
              ok=result.get('status') == 'ok',
              results=(tool_search_display_results(result)
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
    from lib.tools.result_value import CanonicalReceipt
    receipt = CanonicalReceipt({
        'call_id': str(call.get('id') or ''), 'name': name,
        'status': status, 'approval': _approval_summary(row),
        'duration': max(0, int((time.monotonic() - started) * 1000)),
        'source': str(call.get('source') or 'execute_calls'),
    }, canonical=getattr(internal_delivery, 'canonical', None))
    delivered_output = (
        internal_delivery.get('content')
        if isinstance(internal_delivery, dict) and 'content' in internal_delivery else output)
    if status in ('error', 'rejected', 'aborted'):
        receipt['error'] = delivered_output
    else:
        receipt['output'] = delivered_output
    if isinstance(internal_delivery, dict):
        receipt.update({key: internal_delivery[key] for key in
                        ('canonicalRef', 'resultId', 'retentionError') if key in internal_delivery})
        receipt['output_truncated'] = bool(
            internal_delivery.get('truncated') if 'content' in internal_delivery
            else ((row or {}).get('toolResultEvidence') or {}).get('truncated'))
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
        from lib.tools.result_value import CanonicalBatchSink
        from lib.tasks_pkg.manager import task_user_id
        internal_result_sink = CanonicalBatchSink(task_user_id(task),
            projection_sink=(program_result_budget.begin_batch(
                [parsed_call[2] for parsed_call in parsed])
                if program_result_budget is not None else None))
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
        # Multimodal children ride the frozen ``toolModelContent`` projection
        # (owner-resolvable ``tofu-media:`` refs), never the live nested
        # message: that one carries inline base64, which used to bloat the
        # gateway payload (~275KB for one screenshot) until the v2 budget
        # spilled it into a bare artifact pointer and blinded the model.
        output_by_call_object = {}
        for parsed_call, result_message in adjacent_tool_call_result_pairs(
                nested_messages):
            content = result_message.get('content')
            parsed_row = row_by_call_object.get(id(parsed_call))
            projected = (parsed_row.get('toolModelContent')
                         if isinstance(parsed_row, dict) else None)
            if isinstance(projected, list):
                content = projected
            output_by_call_object[id(parsed_call)] = content
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
        if program_result_budget is None:
            from lib.tools.programmatic import ProgrammaticResultBudget
            program_result_budget = ProgrammaticResultBudget()
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
    from lib.tools.program_contract import (
        MAX_OUTPUT_BYTES, MAX_SOURCE_BYTES, MAX_TOOL_CALLS,
    )
    runs = task.setdefault('programRuns', [])
    if not isinstance(runs, list):
        runs = []
        task['programRuns'] = runs
    provider_call_id = call_id
    # Exact duplicate frames are handled by the outer round/world/argument
    # receipt key before reaching execution. A new execution needs a new
    # ledger occurrence even when the provider recycles its call ID.
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
        'result': None, 'childCalls': [], 'tStart': task_execution_time_ms(task),
        'limits': {
            'maxSourceBytes': MAX_SOURCE_BYTES, 'maxCalls': MAX_TOOL_CALLS,
            'maxOutputBytes': MAX_OUTPUT_BYTES,
        },
    }
    runs.append(run)
    return run


def _publish_visible_todo_receipts(value: Any, candidates: dict) -> None:
    """Witness only actual returned child objects, never reconstructed program text.

    childResults normally contains retention metadata, which proves execution but
    not model visibility. Copy the bounded successful todo projection there only
    when its unmodified receipt occurs in the structured return. JSON copies,
    output strings and summaries deliberately do not inherit this provenance.
    """
    if not candidates:
        return
    remaining = MAX_OUTPUT_BYTES
    pending = [value]
    seen = set()
    while pending:
        value = pending.pop()
        if not isinstance(value, (dict, list)) or id(value) in seen:
            continue
        seen.add(id(value))
        candidate = candidates.get(id(value))
        if candidate is not None:
            original, canonical, witness = candidate
            if value is original and all(value.get(k) == v for k, v in witness.items()):
                size = len(_json(witness).encode('utf-8'))
                if size <= remaining:
                    canonical.update(witness)
                    remaining -= size
            continue
        pending.extend(value.values() if isinstance(value, dict) else value)


def _program_input_shapes(receipts):
    """Bounded observed structure for recovery; never reveal child bodies.

    Inspect only complete, inline, post-hook evidence already owned by this
    program. A first array item is a sample, not an inferred result schema.
    No repository read, tool execution or new retained cache occurs here.
    """
    def shape(value):
        if isinstance(value, dict):
            keys = list(value)
            return {'type': 'object', 'keys': [key[:64] for key in keys[:8]],
                    'omittedKeys': max(0, len(keys) - 8),
                    'shortenedKeys': any(len(key) > 64 for key in keys[:8])}
        if isinstance(value, list):
            result = {'type': 'array', 'length': len(value)}
            if value and not isinstance(value[0], list):
                result['firstItem'] = shape(value[0])
            return result
        return {'type': ('null' if value is None else
                         'boolean' if isinstance(value, bool) else
                         'number' if isinstance(value, (int, float)) else 'string')}

    inputs = []
    for receipt in receipts:
        canonical = getattr(receipt, 'canonical', None)
        row = {'tool': receipt['name'], 'callId': receipt['call_id']}
        if canonical is not None:
            row['executionStatus'] = canonical.execution_status
            if (canonical.retention_coverage == 'complete'
                    and not canonical.requires_reference()):
                source = canonical.text()
                try:
                    row['shape'] = shape(json.loads(source))
                except (ValueError, TypeError, RecursionError):
                    row['shape'] = {'type': 'text', 'chars': len(source)}
        if receipt.get('canonicalRef'):
            row['canonicalRef'] = receipt['canonicalRef']
        if receipt.get('retentionError'):
            row['retentionError'] = receipt['retentionError']
        inputs.append(row)
    return inputs


def _execute_program(
    task: dict[str, Any], source: str, gateway_call_id: str, *,
    cfg: dict[str, Any], project_path: str | None, project_enabled: bool,
    model: str, llm_round: int,
) -> dict[str, Any]:
    from lib.tools.program_contract import ProgramExecutionError
    from lib.tools.javascript import execute_javascript_program, javascript_limits
    from lib.tools.programmatic import ProgrammaticResultBudget
    from lib.tools.program_results import list_program_inputs

    catalog = _executable_catalog(task)
    namespaces = task.get('_executableToolNamespaceByName') or {}
    search_text = task.get('_executableToolSearchTextByName') or {}
    run = _program_run(task, gateway_call_id, 'execute_program')
    program_result_budget = ProgrammaticResultBudget()
    run['code'] = source
    run['backend'] = 'local_javascript'
    execution_call_id = str(run['callId'])
    limits = javascript_limits()
    run['limits'].update({
        'maxHeapBytes': limits.heap_bytes, 'maxWasmFuel': limits.fuel,
        'maxGuestExecutionMs': round(limits.execution_seconds * 1000),
        'maxLinearMemoryBytes': limits.heap_bytes * 2 + 16 * 1024 * 1024,
        'maxOutputBytes': limits.heap_bytes, 'maxWireBytes': limits.heap_bytes,
    })
    from lib.tasks_pkg.orchestrator._programmatic import project_program_run
    project_program_run(task, run, llm_round=llm_round, terminal=False)
    child_counter = 0
    program_started = time.monotonic()
    canonical_receipts = []
    retained_inputs = []
    todo_visibility_candidates = {}

    def remember_children(results):
        for result in results:
            from lib.tools.result_value import CanonicalReceipt
            canonical = CanonicalReceipt({key: result[key] for key in
                ('name', 'call_id', 'canonicalRef', 'resultId', 'retentionError') if key in result},
                canonical=getattr(result, 'canonical', None))
            canonical_receipts.append(canonical)
            if (result.get('name') in ('todo_write', 'todo_list')
                    and result.get('status') == 'done'
                    and result.get('output_truncated') is False
                    and not result.get('retentionError')
                    and isinstance(result.get('output'), str)):
                # Snapshot only immutable receipt fields before program code can
                # use the object; keep the object alive for identity verification.
                witness = {key: result[key] for key in (
                    'name', 'call_id', 'status', 'output', 'output_truncated')}
                todo_visibility_candidates[id(result)] = (result, canonical, witness)
    logger.info('[PTC] local program start task=%s model=%s source=%.200s',
                task.get('id'), model, source)

    # ``task['_ptc_local']`` selects the local wire tier; it is not a second
    # execution authority. JavaScript children use the same immutable task
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
        arguments = {'query': query, 'namespace': namespace, 'cursor': cursor}
        visible = _model_visible_tool_names(task)
        disclosed = disclosed_names_for_catalog(task, catalog)
        search_started = time.monotonic()
        result = None
        search_exception = None
        try:
            result = search_executable_catalog(
                catalog, query, namespace=namespace, limit=limit, cursor=cursor,
                execution_gateway=_search_execution_gateway(task),
                namespace_by_name=namespaces, search_text_by_name=search_text,
                contract_documents_by_name=(
                    task.get('_toolContractDocumentsByName') or {}),
                visible_names=visible, disclosed_names=disclosed)
            result = _with_browser_availability(result, task, catalog, arguments)
            return result
        except Exception as exc:
            search_exception = exc
            raise
        finally:
            audit_search_outcome(
                task, arguments, result, source='catalog.search',
                call_id=execution_call_id, llm_round=llm_round,
                catalog_names=(_schema_name(tool) for tool in catalog),
                visible_names=visible, disclosed_names=disclosed,
                exception=search_exception,
                duration_ms=(time.monotonic() - search_started) * 1000)

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
            gateway_call_id=execution_call_id, index=child_counter,
            source='execute_program', contract_documents_by_name=(
                task.get('_toolContractDocumentsByName')
                if '_toolContractDocumentsByName' in task else None))
        child_counter += 1
        if error:
            from lib.tasks_pkg.tool_call_outcome import (
                audit_gateway_rejected_call)
            audit_gateway_rejected_call(task, error, source='execute_program')
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
        remember_children([result])
        run['childCalls'].append({
            'id': result['call_id'], 'name': result['name'],
            'status': result['status']})
        return result

    def call_many(raw_calls, execution='auto'):
        nonlocal child_counter
        normalized = []
        errors = []
        prepared = []
        for raw in raw_calls:
            call_row, error = normalize_gateway_call(
                raw, catalog=catalog, namespace_by_name=namespaces,
                gateway_call_id=execution_call_id, index=child_counter,
                source='execute_program', contract_documents_by_name=(
                    task.get('_toolContractDocumentsByName')
                    if '_toolContractDocumentsByName' in task else None))
            child_counter += 1
            prepared.append((raw, call_row, error))
            if error:
                errors.append(error)
                from lib.tasks_pkg.tool_call_outcome import (
                    audit_gateway_rejected_call)
                audit_gateway_rejected_call(
                    task, error, source='execute_program')
            elif call_row:
                call_row['_presentationParentToolCallId'] = run['callId']
                _audit_gateway_repairs(task, call_row)
                normalized.append(call_row)
        if errors:
            # Preflight remains all-or-nothing. Return one ordered receipt
            # per attempted call so the VM can hand off the actual rejection;
            # dropping valid siblings makes that rejection look like a broken
            # callback and obscures the fact that nothing was dispatched.
            receipts = []
            for raw, call_row, error in prepared:
                identity = call_row or (raw if isinstance(raw, dict) else {})
                function = identity.get('function')
                name = function.get('name') if isinstance(function, dict) else identity.get('name')
                receipts.append({
                    'call_id': str(identity.get('id') or identity.get('call_id') or ''),
                    'name': str(name or ''), 'status': 'error',
                    'error': error or {
                        'code': 'batch_rejected',
                        'message': 'Batch rejected before execution because a sibling call is invalid',
                        'cause': errors[0],
                    }, 'approval': {'required': False, 'status': 'not_required'},
                    'duration': 0, 'source': 'execute_program'})
            return receipts
        results = _execute_normalized(
            task, normalized, execution if execution in (
                'auto', 'parallel', 'sequential') else 'auto', cfg=cfg,
            project_path=project_path, project_enabled=project_enabled,
            model=model, llm_round=llm_round,
            program_result_budget=program_result_budget)
        remember_children(results)
        run['childCalls'].extend({
            'id': result['call_id'], 'name': result['name'],
            'status': result['status']} for result in results)
        return results

    try:
        from lib.tools.program_results import read_program_result, keep_program_value
        from lib.tasks_pkg.manager import task_user_id
        def read_retained(name, arguments):
            receipt = read_program_result(task, name, arguments,
                user_id=task_user_id(task), budget=program_result_budget,
                catalog_names={_schema_name(tool) for tool in catalog})
            if receipt.get('status') == 'done':
                retained_inputs.append(receipt)
            return receipt
        value, stats = execute_javascript_program(
            source, search=search, call=call, call_many=call_many,
            result_reader=read_retained,
            child_settled=lambda: project_program_run(
                task, run, llm_round=llm_round, terminal=False, progress=True),
            value_keeper=lambda value: keep_program_value(task, value,
                user_id=task_user_id(task), call_id=execution_call_id,
                catalog_names={_schema_name(tool) for tool in catalog}),
            input_lister=lambda query: list_program_inputs(
                task, query, user_id=task_user_id(task),
                catalog_names={_schema_name(tool) for tool in catalog}),
            aborted=lambda: bool(task.get('aborted')), limits=limits)
        run['status'] = 'completed'
        # Retain the same JSON-safe evidence for live and reload projection.
        run['result'] = json.loads(_json(value))
        task['_toolScriptConsecutiveAuthoringFailures'] = 0
        stats['result_delivery'] = program_result_budget.stats()
        # Observed byte counts, not counterfactual token or cost savings.
        stats['result_bytes'] = len(_json(run['result']).encode('utf-8'))
        run['stats'] = stats
        elapsed_ms = int((time.monotonic() - program_started) * 1000)
        logger.info(
            '[PTC] local program completed task=%s model=%s children=%d '
            'engine=%s tool_calls=%d elapsed_ms=%d',
            task.get('id'), model, len(run['childCalls']),
            stats.get('engine'), int(stats.get('tool_calls') or 0), elapsed_ms)
        audit_log('ptc_local_program', task_id=task.get('id'), model=model,
                  status='completed', children=len(run['childCalls']),
                  elapsed_ms=elapsed_ms)
        _publish_visible_todo_receipts(value, todo_visibility_candidates)
        return {'status': 'ok', 'result': value, 'stats': stats,
                'childResults': canonical_receipts}
    except ProgramExecutionError as exc:
        logger.debug('[ToolGateway] JavaScript program rejected: %s', exc)
        run['status'] = 'error'
        run['error'] = exc.as_dict()
        error_code = str(run['error'].get('code') or '')
        if error_code in _PROGRAM_AUTHORING_ERRORS:
            fallback = 'Correct the reported code error or use other available tools.'
        elif error_code in {'output_limit', 'memory_limit', 'execution_limit'}:
            fallback = 'Process or page smaller inputs and return only the needed findings.'
        elif error_code in {'runtime_unavailable', 'runtime_integrity'}:
            fallback = ('Use available tools directly or execute_tools calls[]. '
                        'Repair the reported runtime problem before using JavaScript again.')
        elif error_code == 'cancelled':
            fallback = 'Respect cancellation; do not resume the cancelled operation.'
        else:
            fallback = ('Inspect the reported cause before retrying; use other '
                        'available tools for independent work.')
        # A later authoring error does not undo earlier children. Put this
        # before the specific hint so the model's 300-character view keeps it.
        recovery = (
            'Do not repeat completed child operations; inspect uncertain outcomes. '
            'Reuse retained data via tools.inputs and tools.result. '
            if canonical_receipts or run['childCalls'] or retained_inputs else '')
        if error_code == 'cancelled':
            recovery = ''
        run['error']['retry_hint'] = (recovery + str(
            run['error'].get('retry_hint') or fallback))[:300]
        native_stats = getattr(exc, 'execution_stats', None)
        if isinstance(native_stats, dict):
            run['stats'] = {**native_stats,
                'result_delivery': program_result_budget.stats(),
                'result_bytes': len(_json(run['error']).encode('utf-8'))}
        if not run['childCalls'] and error_code in _PROGRAM_AUTHORING_ERRORS:
            failure_count = min(
                _PROGRAM_BATCH_FALLBACK_AFTER,
                max(0, int(task.get(
                    '_toolScriptConsecutiveAuthoringFailures') or 0)) + 1)
            task['_toolScriptConsecutiveAuthoringFailures'] = failure_count
            if failure_count >= _PROGRAM_BATCH_FALLBACK_AFTER:
                task['_toolScriptBatchFallback'] = True
                if isinstance(task.get('_ptc_local'), dict):
                    task['_ptc_local']['tier'] = 'batch'
                logger.info(
                    '[PTC] local JavaScript authoring fallback task=%s '
                    'model=%s failures=%d next_tier=batch',
                    task.get('id'), model, failure_count)
        else:
            task['_toolScriptConsecutiveAuthoringFailures'] = 0
        logger.info(
            '[PTC] local program rejected task=%s model=%s kind=%s '
            'children=%d elapsed_ms=%d',
            task.get('id'), model,
            str(run['error'].get('code') or run['error'].get('kind') or ''),
            len(run['childCalls']),
            int((time.monotonic() - program_started) * 1000))
        return {'status': 'error', 'error': run['error'],
                **({'stats': run['stats']} if 'stats' in run else {}),
                'inputShapes': _program_input_shapes(canonical_receipts + retained_inputs),
                'childResults': canonical_receipts}
    except Exception as exc:
        run['status'] = 'error'
        run['error'] = {
            'kind': 'program_internal_error',
            'message': f'{type(exc).__name__}: {exc}',
        }
        raise
    finally:
        run['internalResultBudget'] = program_result_budget.stats()
        run['tEnd'] = task_execution_time_ms(task)
        project_program_run(task, run, llm_round=llm_round, terminal=True)


def _nonretryable_batch_error(results):
    """Classify only a nonempty batch of explicit settled terminal failures."""
    from dataclasses import replace
    from lib.tools.result_envelope import tool_result_error, typed_tool_error

    if not results:
        return None
    codes = []
    for result in results:
        if result.get('status') not in ('error', 'rejected'):
            return None
        error = tool_result_error(result.get('error'))
        if error is None or error.retryable:
            return None
        codes.append(error.code)
    failure = typed_tool_error(
        'tool_batch_failed', retryable=False,
        message='Every child in this batch failed and cannot be retried unchanged.',
        next_action=(
            'Inspect each child error and use corrected arguments or another authorized tool. '
            'Do not replay the unchanged batch or uncertain effects; independent work may continue.'))
    return replace(failure, items=({'failureCodes': codes},))


@tool_registry.handler(EXECUTE_PROGRAM_NAME, category='tools',
                       description='Execute JavaScript over task tool data')
def handle_execute_program(
    task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
    project_path, project_enabled, all_tools=None,
):
    """Code action delegates admission, replay, effects and settlement once."""
    return handle_execute_tools(
        task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
        project_path, project_enabled, all_tools, program_entry=True)


@tool_registry.handler(EXECUTE_TOOLS_NAME, category='tools',
                       description='Execute task-available tools or JavaScript')
def handle_execute_tools(
    task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
    project_path, project_enabled, all_tools=None, *, program_entry=False,
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
                if '_toolContractDocumentsByName' in task else None),
            program_entry=program_entry)
        for call in normalized['calls']:
            _audit_gateway_repairs(task, call)
        payload: dict[str, Any] = {
            'status': 'ok', 'results': [],
            'warnings': normalized['warnings'],
        }
        if normalized['errors']:
            payload['status'] = 'error'
            payload['errors'] = normalized['errors']
            from lib.tasks_pkg.tool_call_outcome import (
                audit_gateway_rejected_call)
            for _norm_error in normalized['errors']:
                audit_gateway_rejected_call(
                    task, _norm_error, source='execute_calls')
        elif normalized['program'] is not None:
            local_latch = task.get('_ptc_local')
            # Retain existing task-latch keys so in-flight authoring recovery
            # survives this runtime cutover; these names do not select a VM.
            authoring_fallback = bool(task.get('_toolScriptBatchFallback'))
            from lib.context_experiment_flags import normalize_context_experiment_flags
            from lib.tools.programmatic import programmatic_tier
            mode = normalize_context_experiment_flags(cfg)['tools']['programmaticCalling']
            mode_off = mode == 'off'
            program_disabled = (
                mode_off or authoring_fallback
                or programmatic_tier(requested=mode) == 'batch'
                or (isinstance(local_latch, dict)
                    and local_latch.get('tier') == 'batch'))
            if program_disabled:
                payload['program'] = {
                    'status': 'error',
                    'error': {
                        'code': 'program_disabled',
                        'reason': ('mode_off' if mode_off else
                                   'authoring_failures' if authoring_fallback
                                   else 'batch_tier'),
                        'message': (
                            'Program execution is disabled by programmaticCalling=off.'
                            if mode_off else
                            'Program execution is disabled for this task '
                            'after repeated authoring failures.'
                            if authoring_fallback else
                            'Program execution is disabled by the configured '
                            'batch tier.'),
                        'retry_hint': (
                            'Use ordinary tools directly, or execute_tools '
                            'calls[] with '
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
        from lib.tools.result_value import compose_result
        program = payload.get('program')
        model_value = payload
        canonical_options = {}
        if program is not None:
            if program['status'] == 'ok':
                model_value = program['result']
            else:
                model_value = {'status': 'error', 'error': program['error']}
                if program.get('inputShapes'):
                    model_value['inputs'] = program['inputShapes']
            canonical_options = {'program_value': json.loads(_json(model_value)),
                                 'private_children': True}
        content = compose_result(_json(model_value if program_entry else payload),
                                 payload.get('results') or
                                 (program or {}).get('childResults') or [],
                                 **canonical_options,
                                 execution_status='ok' if payload['status'] == 'ok' else 'error')
        if program_entry and program is not None and program['status'] != 'ok':
            # Project one typed failure while retaining canonical child evidence.
            from dataclasses import replace
            from lib.tools.result_envelope import typed_tool_error
            from lib.tools.result_value import TrustedResultText
            error = program['error']
            failure = typed_tool_error(str(error.get('code') or 'program_error'),
                retryable=str(error.get('code') or '') in (_PROGRAM_AUTHORING_ERRORS
                          | {'output_limit', 'memory_limit', 'execution_limit'}),
                message=str(error.get('message') or ''),
                next_action=str(error.get('retry_hint') or
                    'Completed child calls remain settled. Use tools.inputs to reuse retained data; '
                    'process or page large results and return the needed findings. '
                    'Do not repeat completed tool effects.'))
            diagnostics = {'detail': {key: value for key, value in error.items()
                                      if key not in {'code', 'message', 'retry_hint'}}}
            if program.get('inputShapes'):
                diagnostics['inputs'] = program['inputShapes']
            failure = replace(failure, items=(diagnostics,)).with_visible_bytes()
            content = TrustedResultText(failure.to_envelope_text(),
                                        canonical=content.canonical, envelope=True)
        elif program is None and payload['status'] == 'partial_failure':
            failure = _nonretryable_batch_error(payload['results'])
            if failure is not None:
                # Keep exact child evidence behind the same canonical result.
                # Reuse the ordinary model view instead of echoing receipt
                # transport controls inside this typed recovery envelope.
                from dataclasses import replace
                from lib.tools.result_surfaces import project_model_source
                from lib.tools.result_value import TrustedResultText
                failure = replace(failure, summary=project_model_source(
                    EXECUTE_TOOLS_NAME, content)).with_visible_bytes()
                content = TrustedResultText(
                    failure.to_envelope_text(), canonical=content.canonical,
                    envelope=True)
        # A mixed batch still exposes each successful child in the payload, but
        # its outer settlement is a failure verdict. Never paint a failed child
        # batch as a green/done gateway round merely because some work landed.
        ok = payload['status'] == 'ok'
        _remember_execute_gateway_receipt(
            receipts, receipt_key, content, ok=ok)
        _finalize(task, rn, round_entry, fn_name, content, ok=ok)
        return tc_id, content, False


__all__ = ['handle_execute_program', 'handle_execute_tools', 'handle_search_tools']
