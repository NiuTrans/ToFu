"""Join Responses PTC items to the ordinary nested-tool task timeline."""

from __future__ import annotations

from typing import Any

from lib.agent_core.events import EventType, build_event, now_ms
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event
from lib.tools.programmatic import (
    PROGRAMMATIC_MAX_CALLS,
    PROGRAMMATIC_MAX_CONCURRENT_CALLS,
    PROGRAMMATIC_MAX_CONTINUATIONS,
    PROGRAMMATIC_MAX_OUTPUT_BYTES,
    eligible_programmatic_tool_names,
    encode_programmatic_output,
)


_PROGRAM_ROUND_BASE = 8_800_000
_CONTENT_UNSET = object()
logger = get_logger(__name__)


def _append_program_event(task: dict[str, Any], event: dict[str, Any]) -> None:
    """Deliver a lifecycle event when the carrier has a task-stream identity.

    Standalone handler tests and offline gateway callers may deliberately use
    an anonymous task dict. They still receive programRuns/toolRounds state;
    there is simply no event stream to address.
    """
    try:
        append_event(task, event)
    except KeyError:
        if 'id' not in task:
            return
        raise


def _program_call_id(item: dict[str, Any]) -> str:
    # ``id`` is a provider item identity, while ``call_id`` correlates a
    # program/output pair. Older adapters exposed only ``id``, so retain that
    # fallback solely when the correlation field is absent. An explicitly
    # blank ``call_id`` is our fail-closed marker for an orphan/ambiguous
    # output and must never fall back to the unrelated item id.
    if 'call_id' in item:
        return str(item.get('call_id') or '')
    return str(item.get('id') or '')


def _next_round_num(rounds: list[dict[str, Any]]) -> int:
    used = {r.get('roundNum') for r in rounds if isinstance(r, dict)}
    candidate = _PROGRAM_ROUND_BASE + sum(
        1 for r in rounds if isinstance(r, dict) and r.get('_programSynthetic'))
    while candidate in used:
        candidate += 1
    return candidate


def _insert_before_children(rounds: list[dict[str, Any]], parent: dict[str, Any],
                            child_ids: list[str]) -> None:
    wanted = set(child_ids)
    for index, row in enumerate(rounds):
        if isinstance(row, dict) and row.get('toolCallId') in wanted:
            rounds.insert(index, parent)
            return
    rounds.append(parent)


def _program_runs(task: dict[str, Any]) -> list[dict[str, Any]]:
    runs = task.setdefault('programRuns', [])
    if isinstance(runs, list):
        return runs
    # Derived runtime metadata must remain writable even if a legacy/imported
    # snapshot carried the wrong shape. Do not append into a detached list.
    task['programRuns'] = []
    return task['programRuns']


def _is_native_program_run(run: Any) -> bool:
    return (isinstance(run, dict)
            and str(run.get('source') or 'openai_ptc') == 'openai_ptc')


def _fresh_program_call_id(task: dict[str, Any], preferred: str) -> str:
    """Return a task-unique canonical parent id for one new program."""
    used = {
        str(run.get('callId') or '')
        for run in _program_runs(task) if isinstance(run, dict)
    }
    preferred = str(preferred or '').strip()
    base = preferred[:80] or 'program'
    ordinal = len(used) + 1
    while True:
        candidate = f'{base}__tofu_ptc_{ordinal}'
        if candidate not in used:
            return candidate
        ordinal += 1


def _ensure_program_run(task: dict[str, Any], call_id: str, *,
                        llm_round: int | None = None,
                        provider_call_id: str = '') -> dict[str, Any]:
    for run in _program_runs(task):
        if (_is_native_program_run(run)
                and run.get('callId') == call_id):
            if llm_round is not None and run.get('llmRound') is None:
                run['llmRound'] = llm_round
            run.setdefault('source', 'openai_ptc')
            run.setdefault('providerCallId', provider_call_id or call_id)
            # Older checkpoints predate output-budget telemetry.  Deriving
            # defaults from measured children keeps replay idempotent.
            children = run.get('childCalls') or ()
            run.setdefault('rawOutputBytes', sum(
                int(child.get('rawOutputBytes') or 0)
                for child in children if isinstance(child, dict)))
            run.setdefault('outputBytes', sum(
                int(child.get('outputBytes') or 0)
                for child in children if isinstance(child, dict)))
            run.setdefault('outputTruncated', any(
                bool(child.get('outputTruncated'))
                for child in children if isinstance(child, dict)))
            run.setdefault('duplicateRejectedCallCount', 0)
            return run
    run = {
        'callId': call_id,
        'providerCallId': provider_call_id or call_id,
        'source': 'openai_ptc',
        'llmRound': llm_round,
        'code': '',
        'fingerprint': '',
        'status': 'running',
        'result': None,
        'childCalls': [],
        'admittedCallIds': [],
        'rejectedCallIds': [],
        'duplicateRejectedCallCount': 0,
        'continuationCount': 0,
        'rawOutputBytes': 0,
        'outputBytes': 0,
        'outputTruncated': False,
        'limits': {
            'maxCalls': PROGRAMMATIC_MAX_CALLS,
            'maxConcurrentCalls': PROGRAMMATIC_MAX_CONCURRENT_CALLS,
            'maxOutputBytes': PROGRAMMATIC_MAX_OUTPUT_BYTES,
            'maxContinuations': PROGRAMMATIC_MAX_CONTINUATIONS,
        },
        'tStart': now_ms(),
    }
    _program_runs(task).append(run)
    return run


def _native_runs_matching_provider_id(
    task: dict[str, Any], provider_call_id: str,
) -> list[dict[str, Any]]:
    return [
        run for run in _program_runs(task)
        if _is_native_program_run(run)
        and provider_call_id in {
            str(run.get('callId') or ''),
            str(run.get('providerCallId') or run.get('callId') or ''),
        }
    ]


def _canonicalize_programmatic_ids(
    task: dict[str, Any],
    assistant_msg: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    llm_round: int,
) -> None:
    """Make provider-recycled program ids task-unique before dispatch.

    A program ``call_id`` is an opaque correlation token. Some gateways reuse
    positional ids in later responses, so it cannot key execution budgets or
    durable UI parents globally. New program items claim a unique canonical
    id; outputs resolve to the newest matching active occurrence. A child
    caller must resolve to exactly one active occurrence or is made malformed
    so the authorization gate rejects it instead of guessing.
    """
    claimed_starts: set[str] = set()
    starts_by_provider_id: dict[str, list[str]] = {}

    for item in items:
        if item.get('type') != 'program':
            continue
        provider_call_id = _program_call_id(item)
        if not provider_call_id:
            continue
        replay_run = next((
            run for run in _program_runs(task)
            if _is_native_program_run(run)
            and str(run.get('callId') or '') == provider_call_id
            and run.get('llmRound') == llm_round
            and provider_call_id not in claimed_starts
        ), None)
        if replay_run is not None:
            canonical_id = provider_call_id
        else:
            canonical_id = _fresh_program_call_id(task, provider_call_id)
            _ensure_program_run(
                task, canonical_id, llm_round=llm_round,
                provider_call_id=provider_call_id)
        item['call_id'] = canonical_id
        claimed_starts.add(canonical_id)
        starts_by_provider_id.setdefault(provider_call_id, []).append(
            canonical_id)

    claimed_outputs: set[str] = set()
    for item in items:
        if item.get('type') != 'program_output':
            continue
        provider_call_id = _program_call_id(item)
        if not provider_call_id:
            continue
        all_matches = _native_runs_matching_provider_id(
            task, provider_call_id)
        matches = [
            run for run in all_matches
            if str(run.get('callId') or '') not in claimed_outputs
        ]
        active = [run for run in matches
                  if str(run.get('status') or 'running') == 'running']
        canonical_exact = [
            run for run in matches
            if str(run.get('callId') or '') == provider_call_id
            and str(run.get('providerCallId') or '') != provider_call_id
        ]
        selected = (
            canonical_exact[0] if len(canonical_exact) == 1
            else active[0] if len(active) == 1
            else matches[0] if len(matches) == 1
            else None
        )
        if selected is not None:
            canonical_id = str(selected.get('callId') or '')
            item['call_id'] = canonical_id
            claimed_outputs.add(canonical_id)
        else:
            # No output may manufacture its own program parent. This covers
            # genuine orphans, ambiguous recycled provider ids, and a second
            # output occurrence after the first already claimed the only
            # matching run. Blank the explicit correlation field so both the
            # reconciler and outbound replay fail closed; ``_program_call_id``
            # deliberately will not fall back to an item's unrelated ``id``.
            item['call_id'] = ''
            logger.warning(
                '[PTC] rejected unbound program output task=%s '
                'provider_call_id=%s candidates=%d available=%d active=%d',
                str(task.get('id') or '')[:8], provider_call_id,
                len(all_matches), len(matches), len(active))

    for tool_call in assistant_msg.get('tool_calls') or ():
        if not isinstance(tool_call, dict):
            continue
        caller = tool_call.get('caller')
        if not isinstance(caller, dict) or caller.get('type') != 'program':
            continue
        provider_call_id = str(caller.get('caller_id') or '')
        candidates = starts_by_provider_id.get(provider_call_id, [])
        if not candidates:
            candidates = [
                str(run.get('callId') or '')
                for run in _native_runs_matching_provider_id(
                    task, provider_call_id)
                if str(run.get('status') or 'running') == 'running'
            ]
        # Copy before rewriting: providers may reuse one caller mapping object
        # across several parsed calls.
        canonical_caller = dict(caller)
        canonical_caller['caller_id'] = (
            candidates[0] if len(candidates) == 1 else '')
        tool_call['caller'] = canonical_caller
        if len(candidates) != 1:
            logger.warning(
                '[PTC] rejected ambiguous program caller task=%s '
                'provider_call_id=%s candidates=%d',
                str(task.get('id') or '')[:8], provider_call_id,
                len(candidates))


def _upsert_child(run: dict[str, Any], call_id: str, name: str
                  ) -> dict[str, Any]:
    for child in run.setdefault('childCalls', []):
        if isinstance(child, dict) and child.get('id') == call_id:
            if name and not child.get('name'):
                child['name'] = name
            return child
    child = {
        'id': call_id,
        'name': name,
        'status': 'pending',
        'tStart': now_ms(),
    }
    run['childCalls'].append(child)
    return child


def _program_backend(source: str) -> str:
    return ('local_toolscript'
            if source in ('execute_program', 'local_toolscript')
            else 'native_openai')


def project_program_run(task: dict[str, Any], run: dict[str, Any], *,
                        llm_round: int | None = None,
                        terminal: bool | None = None) -> dict[str, Any]:
    """Project one native or local program run onto the shared UI timeline.

    This is the sole writer of ``_programSynthetic`` rows and program lifecycle
    events.  Hosted Responses reconciliation and local ToolScript execution
    both call it, which keeps reload and live-stream shapes backend-neutral.
    Repeated calls are idempotent at the projection level: one parent row, an
    optional start-upsert when locally learned children arrive, and at most one
    output event per distinct terminal state/result.
    """
    call_id = str(run.get('callId') or '')
    if not call_id:
        raise ValueError('program run requires callId')
    if llm_round is not None and run.get('llmRound') is None:
        run['llmRound'] = llm_round
    llm_round = (run.get('llmRound') if llm_round is None else llm_round)
    source = str(run.get('source') or 'openai_ptc')
    backend = _program_backend(source)
    children = [child for child in (run.get('childCalls') or ())
                if isinstance(child, dict)]
    child_ids = [str(child.get('id') or '') for child in children
                 if child.get('id')]
    child_tools = [str(child.get('name') or '') for child in children
                   if child.get('name')]

    # This is the first provider-neutral point at which a real program
    # trajectory exists.  Request projection alone is deliberately kept in a
    # separate evidence field and can never satisfy adoption.
    from lib.orchestration_adoption import record_orchestration_execution
    record_orchestration_execution(
        task, lane='programmatic', kind='program_run', backend=backend,
        call_id=call_id, status=str(run.get('status') or 'running'),
        child_call_count=len(children), round_index=llm_round)

    rounds = task.setdefault('toolRounds', [])
    parent = next((row for row in rounds
                   if isinstance(row, dict) and row.get('_programSynthetic')
                   and str(row.get('_programCallId') or '') == call_id), None)
    if parent is None:
        parent = {
            'roundNum': _next_round_num(rounds),
            'llmRound': llm_round,
            'status': 'searching',
            'attentionKind': 'important',
            '_programSynthetic': True,
            '_programCallId': call_id,
            'programCode': str(run.get('code') or ''),
            'programStatus': 'running',
            'programSource': source,
            'programBackend': backend,
            'childCallIds': child_ids,
            'childToolNames': child_tools,
            'programLimits': dict(run.get('limits') or {}),
            'tStart': run.get('tStart') or now_ms(),
        }
        _insert_before_children(rounds, parent, child_ids)
        _append_program_event(task, build_event(
            EventType.PROGRAM_START,
            roundNum=parent['roundNum'], llmRound=llm_round,
            programCallId=call_id, code=parent['programCode'],
            childCallIds=child_ids, childToolNames=child_tools,
            limits=dict(parent['programLimits']), status='running',
            source=source, backend=backend, tStart=parent['tStart']))
    else:
        prior_child_ids = list(parent.get('childCallIds') or [])
        prior_child_tools = list(parent.get('childToolNames') or [])
        if llm_round is not None and parent.get('llmRound') is None:
            parent['llmRound'] = llm_round
        if run.get('code'):
            parent['programCode'] = str(run['code'])
        parent['programSource'] = source
        parent['programBackend'] = backend
        if run.get('limits'):
            parent['programLimits'] = dict(run['limits'])
        if child_ids:
            parent['childCallIds'] = list(dict.fromkeys(
                (parent.get('childCallIds') or []) + child_ids))
        if child_tools:
            parent['childToolNames'] = list(dict.fromkeys(
                (parent.get('childToolNames') or []) + child_tools))
        structure_changed = (
            prior_child_ids != list(parent.get('childCallIds') or [])
            or prior_child_tools != list(parent.get('childToolNames') or []))
        if terminal and structure_changed:
            # Local ToolScript learns its children while the program runs.
            # Re-emitting the idempotent start/upsert shape once at terminal
            # updates a live card before the output frame; hosted PTC already
            # knows its child list when its first start projection is built.
            _append_program_event(task, build_event(
                EventType.PROGRAM_START,
                roundNum=parent['roundNum'], llmRound=parent.get('llmRound'),
                programCallId=call_id, code=parent.get('programCode') or '',
                childCallIds=list(parent.get('childCallIds') or []),
                childToolNames=list(parent.get('childToolNames') or []),
                limits=dict(parent.get('programLimits') or {}),
                status='running', source=source, backend=backend,
                tStart=parent.get('tStart')))

    if run.get('fingerprint'):
        parent['programFingerprint'] = run['fingerprint']
    status = str(run.get('status') or 'running')
    if terminal is None:
        terminal = status != 'running'
    if terminal:
        result = (run.get('result') if status == 'completed'
                  else run.get('error', run.get('result')))
        changed = (parent.get('programStatus') != status
                   or parent.get('programResult') != result)
        parent['programStatus'] = status
        parent['programResult'] = result
        parent['status'] = 'done' if status == 'completed' else 'error'
        if changed:
            run.setdefault('tEnd', now_ms())
            parent['tEnd'] = run['tEnd']
            _append_program_event(task, build_event(
                EventType.PROGRAM_OUTPUT,
                roundNum=parent['roundNum'],
                llmRound=parent.get('llmRound'), programCallId=call_id,
                result=result, status=status,
                childCallIds=list(parent.get('childCallIds') or []),
                childToolNames=list(parent.get('childToolNames') or []),
                source=source, backend=backend,
                tStart=parent.get('tStart'), tEnd=parent['tEnd']))
    return parent


def reject_programmatic_call(task: dict[str, Any], tc: dict[str, Any],
                             tool_name: str) -> tuple[str, dict] | None:
    """Admit one program child or return a model-facing rejection.

    This is the hard execution boundary.  The request schema is guidance for
    the hosted runtime; this check makes an upstream/proxy bug unable to invoke
    a direct-only or 17th client-owned tool from a program.
    """
    caller = tc.get('caller')
    if not isinstance(caller, dict) or caller.get('type') != 'program':
        return None
    raw_parent_id = caller.get('caller_id')
    parent_id = (
        raw_parent_id.strip()
        if isinstance(raw_parent_id, str) else ''
    )
    call_id = str(tc.get('id') or '')
    if not parent_id:
        return (
            '[SYSTEM: PROGRAM TOOL CALL DID NOT RUN]\n'
            'The program-issued tool call is missing a non-empty string '
            'caller_id. '
            'Return a structured failure; do not retry this malformed call.',
            {'kind': 'programmatic_invalid_caller',
             'attempted': tool_name, 'programCallId': ''},
        )
    if not call_id:
        return (
            '[SYSTEM: PROGRAM TOOL CALL DID NOT RUN]\n'
            'The program-issued tool call is missing its required call id. '
            'Return a structured failure; do not retry this malformed call.',
            {'kind': 'programmatic_invalid_call_id',
             'attempted': tool_name, 'programCallId': parent_id},
        )
    run = _ensure_program_run(task, parent_id)

    # A call id is an execution identity, not a reusable budget token.  Treat
    # any repeat as malformed instead of executing different arguments under
    # one admitted id and bypassing the 16-call ceiling.
    if call_id in run['admittedCallIds']:
        run['duplicateRejectedCallCount'] = int(
            run.get('duplicateRejectedCallCount') or 0) + 1
        return (
            '[SYSTEM: PROGRAM TOOL CALL DID NOT RUN]\n'
            f'Program {parent_id} reused child call id {call_id}. The repeated '
            'call was rejected and did not execute; return a structured '
            'failure instead of retrying it.',
            {'kind': 'programmatic_duplicate_call_id',
             'attempted': tool_name, 'programCallId': parent_id,
             'toolCallId': call_id},
        )
    if call_id in run['rejectedCallIds']:
        return (
            '[SYSTEM: PROGRAM TOOL CALL DID NOT RUN]\n'
            f'Tool call {call_id} was already rejected by the program budget.',
            {'kind': 'programmatic_budget', 'attempted': tool_name,
             'programCallId': parent_id, 'limit': PROGRAMMATIC_MAX_CALLS},
        )

    child = _upsert_child(run, call_id, tool_name)

    if tool_name not in eligible_programmatic_tool_names():
        child['status'] = 'rejected'
        child['rejection'] = 'direct_only'
        child['tEnd'] = now_ms()
        if call_id and call_id not in run['rejectedCallIds']:
            run['rejectedCallIds'].append(call_id)
        return (
            '[SYSTEM: PROGRAM TOOL CALL DID NOT RUN]\n'
            f'`{tool_name}` is direct-only and is not authorized for calls '
            f'from program {parent_id}. Re-plan using an eligible read-only '
            'tool or return a structured failure; do not retry this call from '
            'the same program.',
            {'kind': 'programmatic_direct_only', 'attempted': tool_name,
             'programCallId': parent_id, 'limit': PROGRAMMATIC_MAX_CALLS},
        )

    if len(run['admittedCallIds']) >= PROGRAMMATIC_MAX_CALLS:
        child['status'] = 'rejected'
        child['rejection'] = 'max_calls'
        child['tEnd'] = now_ms()
        if call_id:
            run['rejectedCallIds'].append(call_id)
        return (
            '[SYSTEM: PROGRAM TOOL CALL DID NOT RUN]\n'
            f'Program {parent_id} reached the hard limit of '
            f'{PROGRAMMATIC_MAX_CALLS} child tool calls. Return the best '
            'structured result available; do not issue more calls from this '
            'program.',
            {'kind': 'programmatic_budget', 'attempted': tool_name,
             'programCallId': parent_id, 'limit': PROGRAMMATIC_MAX_CALLS},
        )

    if call_id:
        run['admittedCallIds'].append(call_id)
    child['status'] = 'admitted'
    return None


def settle_programmatic_call(task: dict[str, Any], tc: dict[str, Any],
                             status: str,
                             content: Any = _CONTENT_UNSET) -> None:
    """Stamp one child result and its exact cumulative output-budget use.

    The dispatch pipeline calls this in the model's original call order, the
    same order used by Responses replay.  Measuring only once per child makes
    reconnects and cached settlement idempotent.
    """
    caller = tc.get('caller')
    if not isinstance(caller, dict) or caller.get('type') != 'program':
        return
    parent_id = str(caller.get('caller_id') or '')
    if not parent_id:
        return
    call_id = str(tc.get('id') or '')
    run = _ensure_program_run(task, parent_id)
    name = str((tc.get('function') or {}).get('name') or '')
    child = _upsert_child(run, call_id, name)
    child['status'] = status
    child['tEnd'] = now_ms()
    if content is _CONTENT_UNSET or 'rawOutputBytes' in child:
        return

    _raw_envelope, raw_bytes, _raw_truncated = encode_programmatic_output(
        content)
    remaining = max(
        0,
        PROGRAMMATIC_MAX_OUTPUT_BYTES - int(run.get('outputBytes') or 0),
    )
    _envelope, output_bytes, truncated = encode_programmatic_output(
        content, max_bytes=remaining)
    child['rawOutputBytes'] = raw_bytes
    child['outputBytes'] = output_bytes
    child['outputTruncated'] = truncated
    run['rawOutputBytes'] = int(run.get('rawOutputBytes') or 0) + raw_bytes
    run['outputBytes'] = int(run.get('outputBytes') or 0) + output_bytes
    run['outputTruncated'] = bool(run.get('outputTruncated')) or truncated


def admit_program_continuation(task: dict[str, Any], assistant_msg: Any
                              ) -> tuple[bool, int, int]:
    """Count protocol follow-ups per program, returning cap verdict."""
    items = (assistant_msg.get('_responses_items') or ()
             if isinstance(assistant_msg, dict) else ())
    call_ids = [
        _program_call_id(item) for item in items
        if isinstance(item, dict) and item.get('type') == 'program_output'
        and _program_call_id(item)
    ]
    if not call_ids:
        return False, 0, PROGRAMMATIC_MAX_CONTINUATIONS
    current = 0
    allowed = True
    for call_id in dict.fromkeys(call_ids):
        run = _ensure_program_run(task, call_id)
        run['continuationCount'] = int(run.get('continuationCount') or 0) + 1
        current = max(current, run['continuationCount'])
        allowed = allowed and current <= PROGRAMMATIC_MAX_CONTINUATIONS
    task['_programContinuationCount'] = current
    return allowed, current, PROGRAMMATIC_MAX_CONTINUATIONS


def reconcile_programmatic_items(task: dict[str, Any], assistant_msg: Any,
                                 *, llm_round: int) -> int:
    """Upsert display-only PTC parents and emit idempotent live events."""
    if not isinstance(assistant_msg, dict):
        return 0
    items = [item for item in assistant_msg.get('_responses_items') or ()
             if isinstance(item, dict)]
    _canonicalize_programmatic_ids(
        task, assistant_msg, items, llm_round=llm_round)
    programs = [item for item in items if item.get('type') == 'program']
    outputs = [item for item in items if item.get('type') == 'program_output']
    if not programs and not outputs:
        return 0

    children_by_program: dict[str, list[dict[str, str]]] = {}
    for tc in assistant_msg.get('tool_calls') or ():
        if not isinstance(tc, dict):
            continue
        caller = tc.get('caller')
        if not isinstance(caller, dict) or caller.get('type') != 'program':
            continue
        parent_id = str(caller.get('caller_id') or '')
        if not parent_id:
            continue
        fn = tc.get('function') or {}
        children_by_program.setdefault(parent_id, []).append({
            'id': str(tc.get('id') or ''),
            'name': str(fn.get('name') or ''),
        })

    by_id: dict[str, dict[str, Any]] = {}
    for item in programs + outputs:
        call_id = _program_call_id(item)
        if call_id:
            by_id.setdefault(call_id, {})[str(item.get('type'))] = item

    touched = 0
    for call_id, pair in by_id.items():
        program = pair.get('program') or {}
        output = pair.get('program_output')
        children = children_by_program.get(call_id, [])
        run = _ensure_program_run(task, call_id, llm_round=llm_round)
        if program.get('code'):
            run['code'] = str(program['code'])
        if program.get('fingerprint'):
            run['fingerprint'] = program['fingerprint']
        for child in children:
            _upsert_child(run, child['id'], child['name'])
        if isinstance(output, dict):
            raw_status = str(output.get('status') or 'completed')
            result = output.get('result')
            run['status'] = raw_status
            run['result'] = result
        project_program_run(
            task, run, llm_round=llm_round,
            terminal=isinstance(output, dict))
        touched += 1
    return touched


__all__ = [
    'admit_program_continuation',
    'project_program_run',
    'reconcile_programmatic_items',
    'reject_programmatic_call',
    'settle_programmatic_call',
]
