"""Steer-aware handoff for a locally running ``run_command`` call.

The model-facing protocol stays synchronous in the common case. If an
operator steer becomes durable while the handler is waiting, ownership of the
already-started command moves to a daemon worker and the current tool call
returns a provisional result. The authoritative completion follows the
standard injection-lane contract (durable authority + volatile twin):

  * the durable ``message_queue`` row is the authority — settlement drain,
    dispatch, and startup orphan-redispatch all guarantee delivery from it;
  * a conversation-keyed ``agent_inbox`` twin (``mode='background-command'``)
    is the fast path — a still-running turn drains it at its next round
    boundary and the deferred-confirm flush deletes the durable row, so the
    result lands MID-TURN instead of waiting for the whole steered turn;
  * both races collapse to exactly-once (forward: round-boundary drain +
    post-LLM row dedup; reverse: dispatch pops the row and consumes the twin).

No polling/session tool is exposed to the model.
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

BACKGROUND_COMMAND_MARKER = '[Command moved to background:'


class DetachableCommandTask(dict):
    """A task snapshot that mirrors process control until handoff.

    Before detachment the route-level interrupt/Stop paths still see the live
    PID on the parent task. After detachment, PID cleanup and per-command
    interrupts are isolated from subsequent foreground commands. Whole-task
    Stop remains authoritative and is still observed by the background owner.
    """

    _PROCESS_KEYS = frozenset({'_subprocess_pid', '_subprocess_pgid'})

    def __init__(self, parent: dict[str, Any]):
        super().__init__(parent)
        self._parent = parent
        self._handoff_lock = threading.RLock()
        self._detached = False
        self._spawned = False

    @property
    def detached(self) -> bool:
        with self._handoff_lock:
            return self._detached

    @property
    def has_spawned(self) -> bool:
        with self._handoff_lock:
            return self._spawned

    def detach(self) -> None:
        with self._handoff_lock:
            if self._detached:
                return
            self._detached = True
            dict.__setitem__(self, '_background_detached', True)
            for key in self._PROCESS_KEYS:
                local_value = dict.get(self, key)
                if self._parent.get(key) == local_value:
                    self._parent.pop(key, None)

    def get(self, key, default=None):
        with self._handoff_lock:
            if key == 'aborted':
                return self._parent.get(key, default)
            if key == '_cmd_interrupt' and not self._detached:
                return self._parent.get(key, default)
            return dict.get(self, key, default)

    def __setitem__(self, key, value):
        with self._handoff_lock:
            dict.__setitem__(self, key, value)
            if key == '_subprocess_pid' and value is not None:
                self._spawned = True
            if key in self._PROCESS_KEYS and not self._detached:
                self._parent[key] = value

    def pop(self, key, default=None):
        with self._handoff_lock:
            value = dict.pop(self, key, default)
            if key in self._PROCESS_KEYS and not self._detached:
                if self._parent.get(key) == value:
                    self._parent.pop(key, None)
            elif key == '_cmd_interrupt' and not self._detached:
                self._parent.pop(key, None)
            return value


def is_background_command_result(value: Any) -> bool:
    return BACKGROUND_COMMAND_MARKER in str(value or '')


def _provisional_result(command: str, command_id: str) -> str:
    return (
        f'$ {command}\n\n'
        f'{BACKGROUND_COMMAND_MARKER} {command_id}. The command is still '
        'running; its completion will be injected automatically as soon as it '
        'finishes — do not poll for it.]'
    )


def _completion_payload(command_id: str, command: str, result: str) -> str:
    return (
        f'<background-command id="{command_id}" status="completed">\n'
        'A command that was moved aside for an operator message has finished. '
        'This is its authoritative result; do not rerun it merely to obtain '
        'the exit status.\n\n'
        f'{result or f"$ {command}\\n\\n[no result returned]"}\n'
        '</background-command>'
    )


def _delivery_config(config: dict | None) -> dict:
    clean = dict(config or {})
    for key in (
        '_turnId', '_attemptId', '_turnActor', '_turnKind',
        'assistantMsgId', '_assistantMsgId',
    ):
        clean.pop(key, None)
    return clean


def _queue_completion(*, task: dict[str, Any], config: dict | None,
                      command_id: str, command: str, result: str) -> None:
    """Durably enqueue, then opportunistically wake an idle conversation."""
    conv_id = str(task.get('convId') or '')
    if not conv_id:
        logger.warning(
            '[BackgroundCommand:%s] no conversation; completion retained only '
            'in logs', command_id)
        return
    try:
        from lib.message_queue import (
            KIND_WORKFLOW,
            dispatch_next_queued,
            enqueue_message,
        )
        from lib.tasks_pkg.manager import task_user_id
        from lib.turn_initiation import INITIATOR_PROACTIVE, stamp_initiator

        owner_user_id = int(task_user_id(task))
        text = _completion_payload(command_id, command, result)
        user_msg = stamp_initiator({
            'role': 'user',
            'content': text,
            'timestamp': int(time.time() * 1000),
        }, INITIATOR_PROACTIVE)
        queued = enqueue_message(
            conv_id,
            {
                'text': text,
                '_user_msg': user_msg,
                '_backgroundCommand': command_id,
                'timestamp': int(time.time() * 1000),
            },
            _delivery_config(config),
            kind=KIND_WORKFLOW,
            user_id=owner_user_id,
        )
        queue_id = str(queued.get('queueId') or '')
        logger.info(
            '[BackgroundCommand:%s] completion queued conv=%s row=%s',
            command_id, conv_id[:8], queue_id[:8])
        # Fast-path twin: a still-running turn drains this at its next round
        # boundary (deferred-confirm then deletes the durable row). Loss or
        # refusal (tombstoned slot) is harmless — the row above remains the
        # authority and is dispatched on settlement / startup redispatch.
        try:
            from lib.agent_inbox import enqueue as inbox_enqueue

            inbox_enqueue(
                conv_id,
                text,
                priority='next',
                mode='background-command',
                extra={
                    'queueId': queue_id,
                    'commandId': command_id,
                    'command': command,
                },
            )
        except Exception as exc:
            logger.debug(
                '[BackgroundCommand:%s] inbox twin enqueue failed (durable row '
                'unaffected): %s', command_id, exc)
        # If the steered task is still live this is a cheap no-op; its normal
        # settlement drain owns the row. If it already ended, this closes the
        # enqueue-after-finalize race and starts the completion turn now.
        dispatch_next_queued(conv_id, user_id=owner_user_id)
    except Exception as exc:
        logger.error(
            '[BackgroundCommand:%s] durable completion delivery failed: %s',
            command_id, exc, exc_info=True)


def run_with_steer_handoff(
    *,
    task: dict[str, Any],
    config: dict | None,
    command: str,
    execute: Callable[[dict[str, Any]], str],
    on_detach: Callable[[], Any] | None = None,
) -> str:
    """Run synchronously unless a durable ``user-steer`` asks to take over.

    ``execute`` receives a dict-compatible task proxy. The worker owns the
    command from the outset, so a handoff never leaves stdout/stderr pipes
    without a reader and project-level postprocessing can finish normally.
    """
    conv_id = str(task.get('convId') or '')
    if task.get('_unattended') or not conv_id or not task.get('_userId'):
        return execute(task)

    command_id = f'bg_{uuid.uuid4().hex[:12]}'
    command_task = DetachableCommandTask(task)
    done = threading.Event()
    state_lock = threading.Lock()
    state: dict[str, Any] = {
        'detached': False,
        'result': None,
        'error': None,
        'finished': False,
    }
    copied_context = contextvars.copy_context()

    def _worker() -> None:
        try:
            result = execute(command_task)
        except BaseException as exc:  # handed back or reported, never orphaned
            result = (
                f'$ {command}\n\nBackground execution failed with '
                f'{type(exc).__name__}: {exc}\n[exit code: -1]'
            )
            error = exc
        else:
            error = None
        with state_lock:
            state['result'] = result
            state['error'] = error
            state['finished'] = True
            detached = bool(state['detached'])
        done.set()
        if detached:
            _queue_completion(
                task=task,
                config=config,
                command_id=command_id,
                command=command,
                result=str(result or ''),
            )

    worker = threading.Thread(
        target=lambda: copied_context.run(_worker),
        name=f'tofu-background-command-{command_id[3:]}',
        daemon=True,
    )
    worker.start()

    from lib.agent_inbox import has_pending

    while not done.wait(0.1):
        if not has_pending(conv_id, modes=['user-steer']):
            continue
        # Finish cheap validation/fast paths synchronously. Handoff begins
        # only after a real subprocess existed, so restriction context,
        # command rewriting, and pre-execution display callbacks have already
        # run on the worker before the original round can settle.
        if not command_task.has_spawned:
            continue
        # An explicit stdin prompt remains foreground-owned. Its response has
        # a separate endpoint and detaching it would strand that prompt.
        if task.get('toolRounds') and any(
            item.get('status') == 'awaiting_stdin'
            for item in task.get('toolRounds', ())
            if isinstance(item, dict)
        ):
            continue
        with state_lock:
            if state['finished']:
                break
            state['detached'] = True
            command_task.detach()
        if on_detach is not None:
            try:
                on_detach()
            except Exception as exc:
                logger.debug(
                    '[BackgroundCommand:%s] presentation detach failed: %s',
                    command_id, exc)
        logger.info(
            '[BackgroundCommand:%s] operator steer handed off PID=%s conv=%s',
            command_id, command_task.get('_subprocess_pid'), conv_id[:8])
        return _provisional_result(command, command_id)

    error = state.get('error')
    if error is not None:
        raise error
    return str(state.get('result') or '')


__all__ = [
    'BACKGROUND_COMMAND_MARKER',
    'DetachableCommandTask',
    'is_background_command_result',
    'run_with_steer_handoff',
]
