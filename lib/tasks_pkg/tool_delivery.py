"""Explicit tool-supplied final replies through the ordinary root terminal sink.

Applications bind an ephemeral request after committing their delivery intent.
The root consumes it only after a successful sole-call receipt and a verified
provider finish. The application verifier owns durable authority. This port
does not persist, restart, accept results, settle usage, or call a model.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Callable

from lib.agent_core.execution_limits import execution_limit_reason
from lib.llm.stream_result import ProviderStreamResult

EXIT_REASON = 'explicit_tool_delivery'
_REQUEST = '_explicitToolDeliveryRequest'
_COMMIT = '_explicitToolDeliveryCommit'


def arguments_digest(arguments: dict) -> str:
    return hashlib.sha256(json.dumps(arguments, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def sole_call(message: dict, call_id: str, name: str, digest: str) -> bool:
    if not isinstance(message, dict):
        return False
    calls = message.get('tool_calls') or []
    if not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict):
        return False
    call = calls[0]
    function = call.get('function') or {}
    if not isinstance(function, dict):
        return False
    arguments = function.get('arguments')
    try:
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        return (call.get('id') == call_id and function.get('name') == name
                and isinstance(arguments, dict) and arguments_digest(arguments) == digest)
    except (TypeError, ValueError):
        return False


def require_current_sole_call(task: dict, call_id: str, name: str, arguments: dict) -> None:
    """Refuse before application writes when the active batch has sibling tools."""
    messages = task.get('_compact_messages') or []
    message = next((m for m in reversed(messages) if m.get('role') == 'assistant'), {})
    if not sole_call(message, call_id, name, arguments_digest(arguments)):
        raise ValueError('explicit delivery must be the sole tool call in the current round')


@dataclass(frozen=True)
class _Delivery:
    task_id: str
    owner: int
    call_id: str
    name: str
    round_num: int
    arguments_sha256: str
    content: str
    acknowledgement: str
    verify: Callable[[], bool]


def bind_delivery(task: dict, *, call_id: str, name: str, round_num: int,
                  arguments: dict, content: str, acknowledgement: str,
                  verify: Callable[[], bool]) -> None:
    require_current_sole_call(task, call_id, name, arguments)
    request = _Delivery(task['id'], task['_userId'], call_id, name, round_num,
        arguments_digest(arguments), content, acknowledgement, verify)
    if task.get(_REQUEST) is not None:
        raise ValueError('explicit delivery already bound')
    task[_REQUEST] = request


def _active(task: dict, request: _Delivery) -> bool:
    abort_event = task.get('abort_event')
    budget = task.get('_executionBudget')
    return (task.get('id') == request.task_id and task.get('_userId') == request.owner
        and not task.get('aborted') and not task.get('error')
        and not (abort_event is not None and abort_event.is_set())
        and not execution_limit_reason(task)
        and (budget is None or budget.actuals_complete())
        and task.get('_executionActualsComplete') is not False
        and request.verify())


def consume_delivery(task: dict, state, *, round_num: int, message: dict,
                     messages: list, note: dict | None) -> bool:
    request = task.pop(_REQUEST, None)
    if not isinstance(request, _Delivery):
        return False
    result = state.last_stream_result
    if (request.round_num != round_num or (note or {}).get('timed_out')
            or not isinstance(result, ProviderStreamResult) or not result.is_verified_complete
            or result.provider_finish_reason not in ('tool_calls', 'tool_use')
            or state.last_finish_reason != result.provider_finish_reason
            or not sole_call(message, request.call_id, request.name, request.arguments_sha256)
            or not sole_call(result.message, request.call_id, request.name, request.arguments_sha256)):
        return False
    receipt = (task.get('_tool_call_id_receipts') or {}).get(request.call_id) or {}
    tool_result = next((m for m in reversed(messages)
        if m.get('role') == 'tool' and m.get('tool_call_id') == request.call_id), {})
    if (receipt.get('status') != 'done' or receipt.get('name') != request.name
            or request.acknowledgement not in str(tool_result.get('content') or '')
            or not _active(task, request)):
        return False
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager import append_event, reset_task_text
    epoch = reset_task_text(task, content=request.content)
    append_event(task, build_event(EventType.DELTA_RESET, roundNum=round_num,
        discard=True, contentEpoch=epoch))
    append_event(task, build_event(EventType.DELTA, content=request.content))
    state.assistant_msg = {'role': 'assistant', 'content': request.content}
    task[_COMMIT] = request
    state.exit_reason = EXIT_REASON
    return True


def has_committed_delivery(task: dict, finish_reason, loop_exit_reason) -> bool:
    """Keep provider tool finish intact; cancellation/error/late limits still win."""
    request = task.get(_COMMIT)
    return (isinstance(request, _Delivery) and loop_exit_reason == EXIT_REASON
        and finish_reason in ('tool_calls', 'tool_use')
        and task.get('content') == request.content and _active(task, request))


def delivery_round_closed(task: dict, round_num: int) -> bool:
    """The root closes an explicitly delivered tool round before post-loop work."""
    request = task.get(_COMMIT)
    return isinstance(request, _Delivery) and request.round_num == round_num
