"""Time- or steer-triggered handoff for a locally running ``run_command`` call.

The model-facing protocol stays synchronous for short calls. After 60 seconds
of actual subprocess time, or if an operator steer arrives, the already-running
command stays on its worker and the current tool call returns a provisional
result. The authoritative completion follows the standard injection-lane
contract (durable authority + volatile twin):

  * the durable ``message_queue`` row is the authority — active-turn injection
    or one collective completion turn delivers it after all siblings settle;
  * a conversation-keyed ``agent_inbox`` twin (``mode='background-command'``)
    is the fast path — a still-running turn drains it at its next round
    boundary and the deferred-confirm flush deletes the durable row, so the
    result lands MID-TURN instead of waiting for the whole steered turn;
  * both races collapse by queue identity (forward: round-boundary drain +
    post-LLM row dedup; reverse: a later turn rehydrates the surviving row).

The handed-off process is queryable and stoppable by the unified poll/kill
tools for this server lifetime; it cannot gain the restart record reserved for
commands launched with background=true.
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
# Leave room for the next provider request before a typical five-minute
# prompt-cache expiry. Measured from process spawn, not handler admission.
AUTO_BACKGROUND_AFTER_S = 60.0


class CommandHandoffControl:
    """One live tool call's owner-requested handoff, scoped by round and call ID.

    The request is only a signal to the existing foreground worker; that worker
    alone adopts its pipes and creates the background handle. The control never
    claims success until the worker actually registers the process.
    """

    def __init__(self, task: dict[str, Any], round_num: int, tool_call_id: str):
        self.task = task
        self.round_num = round_num
        self.tool_call_id = tool_call_id
        self.pid: int | None = None
        self._lock = threading.Lock()
        self._resolved = threading.Event()
        self._requested = False

        self._requested_pid: int | None = None
        self._finished = False
        self._result: dict[str, Any] | None = None

    def note_pid(self, pid: int | None) -> None:
        with self._lock:
            self.pid = pid
            if (self._requested and self._result is None
                    and pid != self._requested_pid):
                self._resolve_locked(False, 'no_active_command')

    def _resolve_locked(self, backgrounded: bool, reason: str = '') -> None:
        self._result = {'backgrounded': backgrounded}
        if reason:
            self._result['reason'] = reason
        self._resolved.set()

    def request(self, *, round_num: int, tool_call_id: str) -> dict[str, Any]:
        with self._lock:
            if round_num != self.round_num or tool_call_id != self.tool_call_id:
                return {'backgrounded': False, 'reason': 'command_changed'}
            if self._finished or not self.pid or self.task.get('_subprocess_pid') != self.pid:
                return {'backgrounded': False, 'reason': 'no_active_command'}
            abort_event = self.task.get('abort_event')
            if (self.task.get('aborted') or self.task.get('status') != 'running'
                    or (abort_event is not None and abort_event.is_set())):
                return {'backgrounded': False, 'reason': 'task_not_running'}
            if isinstance(self.task.get('_cmd_interrupt'), dict) and self.task['_cmd_interrupt'].get('pid') == self.pid:
                return {'backgrounded': False, 'reason': 'command_interrupting'}
            if any(isinstance(item, dict) and item.get('status') == 'awaiting_stdin'
                   for item in self.task.get('toolRounds', ())):
                return {'backgrounded': False, 'reason': 'awaiting_stdin'}
            if self._result is not None:
                if self._result.get('reason') != 'awaiting_stdin':
                    return dict(self._result)
                self._result = None
                self._resolved.clear()
            self._requested = True

            self._requested_pid = self.pid
        # The handoff owner checks at 100 ms intervals. The bounded wait gives
        # an honest refusal if Stop/completion wins the race.
        self._resolved.wait(2)
        with self._lock:
            return dict(self._result or {'backgrounded': False, 'reason': 'handoff_pending'})

    def requested_for(self, pid: int | None) -> bool:
        with self._lock:
            return bool(self._requested and self._result is None
                        and not self._finished and pid == self._requested_pid
                        and pid == self.pid)

    def resolve(self, backgrounded: bool, reason: str = '') -> None:
        with self._lock:
            if self._result is None:
                self._resolve_locked(backgrounded, reason)

    def finish(self) -> None:
        with self._lock:
            self._finished = True
            if self._result is None:
                self._resolve_locked(False, 'no_active_command')


class DetachableCommandTask(dict):
    """A task snapshot that mirrors process control until handoff.

    Before detachment the route-level interrupt/Stop paths still see the live
    PID on the parent task. After detachment, PID cleanup and per-command
    interrupts are isolated from subsequent foreground commands. Whole-task
    Stop remains authoritative and is still observed by the background owner.
    """

    _PROCESS_KEYS = frozenset({'_subprocess_pid', '_subprocess_pgid'})

    def __init__(self, parent: dict[str, Any], handoff: CommandHandoffControl | None = None):
        super().__init__(parent)
        self._parent = parent
        self._handoff = handoff
        self._handoff_lock = threading.RLock()
        self._detached = False
        self._spawned = False
        self._spawned_at: float | None = None

    @property
    def detached(self) -> bool:
        with self._handoff_lock:
            return self._detached

    @property
    def has_spawned(self) -> bool:
        with self._handoff_lock:
            return self._spawned

    @property
    def spawned_at(self) -> float | None:
        with self._handoff_lock:
            return self._spawned_at

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
                return bool(self._parent.get(key, False)
                            or dict.get(self, key, False))
            if key == '_cmd_interrupt' and not self._detached:
                return self._parent.get(key, default)
            return dict.get(self, key, default)

    def __setitem__(self, key, value):
        with self._handoff_lock:
            dict.__setitem__(self, key, value)
            if key == '_subprocess_pid' and value is not None:
                self._spawned = True
                if self._spawned_at is None:
                    self._spawned_at = time.monotonic()
                if self._handoff is not None:
                    self._handoff.note_pid(value)
            if key in self._PROCESS_KEYS and not self._detached:
                self._parent[key] = value

    def pop(self, key, default=None):
        with self._handoff_lock:
            value = dict.pop(self, key, default)
            if key == '_subprocess_pid' and self._handoff is not None and value is not None:
                self._handoff.note_pid(None)
            if key in self._PROCESS_KEYS and not self._detached:
                if self._parent.get(key) == value:
                    self._parent.pop(key, None)
            elif key == '_cmd_interrupt' and not self._detached:
                self._parent.pop(key, None)
            return value


def is_background_command_result(value: Any) -> bool:
    return BACKGROUND_COMMAND_MARKER in str(value or '')


def _provisional_result(command: str, command_id: str, *,
                        task: dict[str, Any]) -> str:
    from lib.tasks_pkg.background_commands import background_command_result

    return background_command_result(
        command, command_id, task=task,
        has_conversation=bool(task.get('convId') and task.get('_userId')),
    )


def _completion_payload(command_id: str, command: str, result: str) -> str:
    return (
        f'<background-command id="{command_id}" status="completed">\n'
        'A background command has finished. '
        'This is its authoritative result; do not rerun it merely to obtain '
        'the exit status.\n\n'
        f'{result or f"$ {command}\\n\\n[no result returned]"}\n'
        '</background-command>'
    )


def _delivery_config(config: dict | None) -> dict:
    from lib.background_command_delivery import completion_delivery_config
    return completion_delivery_config(config)


def _queue_completion(*, task: dict[str, Any], config: dict | None,
                      command_id: str, command: str, result: str) -> bool:
    """Durably publish a passive receipt and its live-turn inbox twin."""
    conv_id = str(task.get('convId') or '')
    if not conv_id:
        logger.warning(
            '[BackgroundCommand:%s] no conversation; completion retained only '
            'in logs', command_id)
        return False
    try:
        from lib.message_queue import (
            KIND_BACKGROUND_RECEIPT,
            enqueue_message,
        )
        from lib.tasks_pkg.manager import task_user_id

        owner_user_id = int(task_user_id(task))
        text = _completion_payload(command_id, command, result)
        from lib.background_command_projection import command_result_status
        completion = {
            'commandId': command_id[:128],
            'status': 'completed',
            'command': command[:2000],
            'resultPreview': result[:2000],
            'resultTruncated': len(result) > 2000,

            **command_result_status(result),
        }
        from lib.background_command_delivery import publish_completion_envelope
        from lib.tasks_pkg.background_commands import (
            freeze_completion_envelope, release_completion_envelope,
        )
        envelope = freeze_completion_envelope(
            command_id=command_id, conv_id=conv_id, user_id=owner_user_id,
            message={
                'text': text,
                '_backgroundCommand': command_id,
                '_backgroundCommandCompletion': completion,
                'timestamp': int(time.time() * 1000),
            },
            config=_delivery_config(config),
        )
        queued = publish_completion_envelope(
            enqueue_message, conv_id, envelope,
            kind=KIND_BACKGROUND_RECEIPT,
            user_id=owner_user_id,
        )
        text = envelope['message']['text']
        completion = envelope['message']['_backgroundCommandCompletion']
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
                    'backgroundCommand': completion,
                },
            )
        except Exception as exc:
            logger.debug(
                '[BackgroundCommand:%s] inbox twin enqueue failed (durable row '
                'unaffected): %s', command_id, exc)
        release_completion_envelope(command_id=command_id, conv_id=conv_id,
                                    user_id=owner_user_id)
        # One completion is evidence, not a turn instruction. The registry
        # wakes the conversation only after every sibling has published.
        return True
    except Exception as exc:
        logger.error(
            '[BackgroundCommand:%s] durable completion delivery failed: %s',
            command_id, exc, exc_info=True)
        return False


def run_with_steer_handoff(
    *,
    task: dict[str, Any],
    config: dict | None,
    command: str,
    execute: Callable[[dict[str, Any]], str],
    on_detach: Callable[[], Any] | None = None,

    on_handoff: Callable[[str], Any] | None = None,
    round_num: int | None = None,
    tool_call_id: str = '',
) -> str:
    """Run synchronously until a steer or elapsed process time hands it off.

    ``execute`` receives a dict-compatible task proxy. The worker owns the
    command from the outset, so a handoff never leaves stdout/stderr pipes
    without a reader and project-level postprocessing can finish normally.
    """
    conv_id = str(task.get('convId') or '')
    if task.get('_unattended') or not conv_id or not task.get('_userId'):
        return execute(task)

    command_id = f'bg_{uuid.uuid4().hex[:12]}'
    handoff = (CommandHandoffControl(task, round_num, tool_call_id)
               if round_num is not None and tool_call_id else None)
    if handoff is not None:
        task['_cmd_handoff_control'] = handoff
    command_task = DetachableCommandTask(task, handoff)
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
            logger.debug('background command handoff worker failed: %s',
                         type(exc).__name__)
            result = (
                f'$ {command}\n\nBackground execution failed with '
                f'{type(exc).__name__}: {exc}\n[exit code: -1]'
            )
            error = exc
        else:
            error = None
        with state_lock:
            # Completion cannot resolve a request between registry adoption
            # and detach. Both decisions serialize on the same lock.
            if handoff is not None:
                handoff.finish()
            state['result'] = result
            state['error'] = error
            state['finished'] = True
            detached = bool(state['detached'])
        if detached:
            from lib.tasks_pkg.background_commands import finish_handed_off_command
            finish_handed_off_command(
                command_id=command_id, result=str(result or ''),
                error=str(error) if error is not None else None)
            published = _queue_completion(
                task=task,
                config=config,
                command_id=command_id,
                command=command,
                result=str(result or ''),
            )
            from lib.tasks_pkg.background_commands import set_handed_off_delivery
            set_handed_off_delivery(command_id, published)
            if published:
                from lib.tasks_pkg.background_commands import wake_completed_background_batch
                wake_completed_background_batch(conv_id, task.get('_userId'))
        else:
            done.set()

    worker = threading.Thread(
        target=lambda: copied_context.run(_worker),
        name=f'tofu-background-command-{command_id[3:]}',
        daemon=True,
    )
    worker.start()

    from lib.agent_inbox import has_pending

    while not done.wait(0.1):
        if task.get('aborted'):
            if handoff is not None:
                handoff.resolve(False, 'task_not_running')
            continue
        spawned_at = command_task.spawned_at
        timed_handoff = (spawned_at is not None and
                         time.monotonic() - spawned_at >= AUTO_BACKGROUND_AFTER_S)
        steered = has_pending(conv_id, modes=['user-steer'])
        manually_requested = (handoff is not None and
                              handoff.requested_for(command_task.get('_subprocess_pid')))
        if not timed_handoff and not steered and not manually_requested:
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
            if manually_requested:
                handoff.resolve(False, 'awaiting_stdin')
            continue
        with state_lock:
            if state['finished']:
                break
            # The worker clears its PID while holding this lock. A request
            # observed before cleanup must not adopt a finished subprocess or
            # drift onto a later subprocess in the same tool call.
            with command_task._handoff_lock:
                pid = command_task.get('_subprocess_pid')
                if not pid or task.get('_subprocess_pid') != pid:
                    if manually_requested:
                        handoff.resolve(False, 'no_active_command')
                    continue
                abort_event = task.get('abort_event')
                if (task.get('aborted') or task.get('status', 'running') != 'running'
                        or (abort_event is not None and abort_event.is_set())):
                    if manually_requested:
                        handoff.resolve(False, 'task_not_running')
                    continue
                if isinstance(task.get('_cmd_interrupt'), dict) and task['_cmd_interrupt'].get('pid') == pid:
                    if manually_requested:
                        handoff.resolve(False, 'command_interrupting')
                    continue
                if (manually_requested and handoff is not None
                        and not handoff.requested_for(pid)):
                    continue
                from lib.tasks_pkg.background_commands import (
                    register_handed_off_command, unregister_handed_off_command,
                )
                register_handed_off_command(
                    command_id=command_id, task=command_task,
                    command=command, done=done)
                # Stop can be signalled while controls are being registered.
                # If it won before detach, remove the control entry. A Stop
                # after detach still cancels the already adopted subprocess.
                if (task.get('aborted') or task.get('status', 'running') != 'running'
                        or (abort_event is not None and abort_event.is_set())):
                    unregister_handed_off_command(command_id=command_id, task=command_task)
                    if manually_requested:
                        handoff.resolve(False, 'task_not_running')
                    continue
                state['detached'] = True
                command_task.detach()
                if handoff is not None:
                    handoff.resolve(True)
        if on_detach is not None:
            try:
                on_detach()
            except Exception as exc:
                logger.debug(
                    '[BackgroundCommand:%s] presentation detach failed: %s',
                    command_id, exc)
        logger.info(
            '[BackgroundCommand:%s] %s handed off PID=%s conv=%s',
            command_id, ('operator request' if manually_requested else
                         'elapsed-time' if timed_handoff else 'operator steer'),
            command_task.get('_subprocess_pid'), conv_id[:8])
        if handoff is not None and task.get('_cmd_handoff_control') is handoff:
            task.pop('_cmd_handoff_control', None)
        if on_handoff is not None:
            on_handoff(command_id)
        return _provisional_result(command, command_id, task=task)

    if handoff is not None and task.get('_cmd_handoff_control') is handoff:
        task.pop('_cmd_handoff_control', None)
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
