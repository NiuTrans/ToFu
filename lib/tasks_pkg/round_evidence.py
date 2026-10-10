"""Shared root/child Inspector evidence construction; persistence stays with callers.

The existing event-log delta writer owns size bounds and incremental storage.
A request-specific turn token makes the existing authorized payload API exact,
including restarted invocations with the same displayed round number.
"""
from __future__ import annotations

from uuid import uuid4

from lib.agent_core.events import EventType, build_event


def begin_round_evidence(task: dict, task_id: str, round_num: int,
                         phase: str = '') -> dict:
    previous = task.get('_inspectorRef') or {}
    turn = previous.get('turn') if (
        previous.get('taskId') == task_id
        and round_num > previous.get('roundNum', 0)) else None
    ref = {'taskId': task_id, 'roundNum': round_num,
           'turn': turn or f'{phase or "request"}:{uuid4().hex}'}
    task['_inspectorRef'] = ref
    return dict(ref)


def round_evidence_turn(task: dict, round_num: int | None) -> str:
    """Join diagnostics only to the current task's exact round reference.

    Legacy callers without a matching reference retain their phase identity.
    Never borrow a stale round or another task's Inspector reference.
    """
    ref = task.get('_inspectorRef') or {}
    task_id = task.get('_inspectorTaskId') or task.get('id')
    if (round_num is not None and ref.get('roundNum') == round_num
            and ref.get('taskId') == task_id and ref.get('turn')):
        return str(ref['turn'])
    return str(task.get('_flow_phase') or '')


def build_round_evidence(task: dict, messages: list, *, ref: dict,
                         kind: str, model: str, prepared_messages=None,
                         params=None, tools=None) -> dict:
    from lib.tasks_pkg.manager._events import _strip_base64_for_snapshot
    from lib.tasks_pkg.wire_messages import apply_wire_sanitize
    canonical = isinstance(prepared_messages, list)
    wire = prepared_messages if canonical else apply_wire_sanitize(
        messages, conv_id=task.get('convId', ''),
        provider_id=task.get('provider_id') or '',
        user_id=task.get('_userId') or None)
    snapshot = _strip_base64_for_snapshot(wire)
    label = '请求前' if kind == 'request' else '工具结果后'
    event = build_event(
        EventType.MESSAGES_SNAPSHOT, kind=kind, model=model,
        roundNum=ref['roundNum'], turn=ref['turn'], inspectorRef=dict(ref),
        label=f'Round {ref["roundNum"]} {label} · {len(snapshot)}条',
        messages=snapshot, contextManifest=list(task.get('_contextManifest') or []))
    if params is not None:
        event['params'] = params
    if tools:
        event['tools'] = tools
    if kind == 'request' and not canonical:
        event.update(degraded=True, degradedReason='request-build-fallback')
    return event
