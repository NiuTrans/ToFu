"""Automatic episode settlement on task-engine terminal transitions.

Responsibility: listen to the task engine's one settlement path and settle
bound evolution episodes. Entry point: ``install`` (idempotent), called once
from the API surface at registration. Dependencies: the terminal-listener
seam, the bridge, the checkers, and the episodes/goals stores.

A task binds an episode through ``task['config']['evolution']`` =
``{'episode_id': ..., 'goal_id': ...}``, written at launch. Settlement here
is fail-soft and never blocks or retries the engine's own path: error/aborted
tasks settle ``aborted`` with no verdict; done tasks settle only when the
checkers can evaluate every criterion, otherwise the episode stays running
for an explicit human verdict through the API.
"""
from __future__ import annotations

import threading
from collections import deque

from lib.identity import require_user_id
from lib.log import get_logger

from .bridge import actuals_complete, terminal_actuals, settle_from_task, settle_terminal_receipt
from .checkers import evaluate_goal
from .contracts import EvolutionStateError
from .episodes import get_episode
from .goals import get_goal

logger = get_logger(__name__)
_installed = False


def episode_binding(task: dict) -> dict | None:
    config = (task or {}).get('config') or {}
    binding = config.get('evolution')
    if isinstance(binding, dict) and (
            binding.get('episode_id') or binding.get('goal_id')):
        return binding
    return None


def on_task_terminal(task: dict, *, actuals=None, cancel_event=None) -> None:
    user_id = int(task.get('_userId') or 0)
    if user_id < 1:
        return
    binding = episode_binding(task)
    if binding is None:
        # Tasks with no evolution binding never touch the stores — the
        # listener stays silent on the engine's unrelated terminal traffic.
        return
    if binding.get('episode_id'):
        episode_id = str(binding['episode_id'])
    else:
        # Launched tasks carry goal/plan in config; the durable link is the
        # episode's task_id. Match it instead of mutating a live task config.
        from .episodes import find_running_episode_for_task
        task_id = str(task.get('id') or '')
        match = find_running_episode_for_task(task_id=task_id, user_id=user_id)
        episode_id = match['episode_id'] if match else ''
    if not episode_id:
        return
    episode = get_episode(episode_id, user_id=user_id)
    if episode is None or episode.get('status') != 'running':
        return
    if episode.get('task_id') != str(task.get('id') or ''):
        logger.warning('[Evolution:Automation] terminal task does not match episode binding')
        return
    status = str(task.get('status') or '')
    verdict = None
    if status == 'done':
        goal = get_goal(episode.get('goal_id') or '', user_id=user_id)
        verdict = evaluate_goal(goal or {}, user_id=user_id, cancel_event=cancel_event)
        if verdict is None:
            logger.info('[Evolution:Automation] episode %s awaits a human '
                        'verdict (criteria not machine-checkable)', episode_id)
            return
    if cancel_event is not None and cancel_event.is_set():
        return
    try:
        if actuals is None:
            settled = settle_from_task(episode_id, task, user_id=user_id, verdict=verdict)
        else:
            settled = settle_terminal_receipt(episode_id, user_id=user_id,
                task_status=status, actuals=actuals, verdict=verdict,
                actuals_complete=actuals_complete(task))
        logger.info('[Evolution:Automation] episode %s settled %s from task %s',
                    episode_id, settled['status'], str(task.get('id'))[:8])
    except EvolutionStateError as exc:
        logger.warning('[Evolution:Automation] episode %s settlement refused: '
                       '%s', episode_id, exc)


_IDLE_SECONDS = 30.0
_pending = deque()
_worker = None
_worker_condition = threading.Condition()
_worker_stop = threading.Event()


def _start_terminal_worker_locked():
    global _worker
    _worker = threading.Thread(target=_consume_terminals,
                               name='evolution-terminal', daemon=True)
    try:
        _worker.start()
    except BaseException:
        _worker = None
        raise


def _check_terminal_receipt(task, actuals):
    if _worker_stop.is_set():
        return
    try:
        on_task_terminal(task, actuals=actuals, cancel_event=_worker_stop)
    except Exception as exc:
        logger.warning('[Evolution:Automation] terminal check failed: %s',
                       type(exc).__name__)
    finally:
        with _worker_condition:
            _worker_condition.notify_all()


def _consume_terminals():
    global _worker
    owner = threading.current_thread()
    try:
        from lib.production.concurrency import production_executor
        with production_executor(name='evolution-terminal-check') as executor:
            while True:
                with _worker_condition:
                    if not _pending and not _worker_stop.is_set():
                        _worker_condition.wait(timeout=_IDLE_SECONDS)
                    if _worker_stop.is_set():
                        return
                    if not _pending:
                        snapshot = executor.resource_snapshot()
                        if snapshot['used'] or snapshot['waiting']:
                            continue
                        return
                    task, actuals = _pending.popleft()
                try:
                    executor.submit(_check_terminal_receipt, task, actuals)
                except Exception as exc:
                    logger.warning('[Evolution:Automation] terminal worker start failed: %s',
                                   type(exc).__name__)
    finally:
        # Keep the physical owner published until its executor has joined.
        # Enqueue during idle close stays owned and starts a fresh generation.
        with _worker_condition:
            if _worker is owner:
                _worker = None
                if _pending and not _worker_stop.is_set():
                    try:
                        _start_terminal_worker_locked()
                    except Exception as exc:
                        logger.warning('[Evolution:Automation] terminal owner start failed: %s',
                                       type(exc).__name__)


def enqueue_task_terminal(task: dict) -> bool:
    """Own every accepted identity/consumption receipt, never task history."""
    global _worker
    binding = episode_binding(task)
    if binding is None:
        return False
    if binding.get('run_id'):
        from lib.agent_core.terminal_delivery import TerminalDelivery, deliver_terminal
        if isinstance(task.get('_terminalDelivery'), TerminalDelivery):
            # The required receiver owns acknowledgement and retry debt.
            # Repeated observers delegate to its same idempotent port.
            deliver_terminal(task)
            return True
        # Durable domain tasks without a receiver use this committed observer.
        if binding.get('stage') == 'coordinator':
            from .coordinator import settle
            settle(task)
        else:
            from .runs import advance_run
            advance_run(binding['run_id'], user_id=task['_userId'], terminal_task=task)
        return True
    owner = require_user_id(task.get('_userId'), context='evolution terminal owner')
    identifiers = {key: str(binding.get(key) or '') for key in ('episode_id', 'goal_id')}
    task_id = str(task.get('id') or '')
    if not task_id or len(task_id) > 256 or any(len(value) > 256 for value in identifiers.values()):
        raise ValueError('evolution terminal identity exceeds receipt budget')
    receipt = {'id': task_id, '_userId': owner, 'status': str(task.get('status') or ''),
               'config': {'evolution': identifiers}}
    actuals, complete = terminal_actuals(task)
    if not complete:
        receipt['_executionActualsComplete'] = False
    with _worker_condition:
        if _worker_stop.is_set():
            logger.warning('[Evolution:Automation] terminal checks are stopped')
            return False
        _pending.append((receipt, actuals))
        if _worker is None:
            try:
                _start_terminal_worker_locked()
            except BaseException:
                _pending.pop()
                raise
        _worker_condition.notify()
    return True


def start_terminal_checks() -> None:
    """Enable lazy receipt delivery; importing/starting launches no thread."""
    with _worker_condition:
        if _worker is None:
            _worker_stop.clear()


def stop_terminal_checks(*, timeout: float = 2.0) -> bool:
    """Cancel active checks and discard only reconstructible notifications."""
    _worker_stop.set()
    with _worker_condition:
        pending = len(_pending)
        _pending.clear()
        worker = _worker
        _worker_condition.notify_all()
    if pending:
        logger.info('[Evolution:Automation] %d pending episodes await explicit settlement', pending)
    if worker is not None:
        worker.join(timeout=max(0.0, timeout))
    return worker is None or not worker.is_alive()


def launch_plan(plan: dict, goal: dict, options: dict, *, user_id) -> dict:
    """Legacy manual launch shares the commit-before-spawn execution boundary."""
    from lib.tasks_pkg.entry import build_chat_config
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.manager._terminal import reject_unstarted_chat_task
    from lib.tasks_pkg.spawn import spawn_task
    from .episodes import open_episode, get_episode
    cfg = build_chat_config(str(options.get('model') or ''))
    cfg['evolution'] = {'goal_id': goal['goal_id'], 'plan_id': plan['plan_id']}
    task = create_task('', [{'role': 'user', 'content': str(options.get('message')
                       or f"Goal: {goal['statement']}")}], cfg,
                       user_id=user_id, supersede=False, transient=True)
    try:
        episode = open_episode(goal_id=goal['goal_id'], plan_id=plan['plan_id'],
                               selected_path_id=plan['selected_path_id'],
                               task_id=task['id'], user_id=user_id)
        task['config']['evolution']['episode_id'] = episode['episode_id']
    except Exception:
        reject_unstarted_chat_task(task, 'episode binding was not committed',
                                   cause='evolution_launch_failed')
        raise
    from lib.agent_core.terminal_delivery import bind_terminal_delivery
    bind_terminal_delivery(task, enqueue_task_terminal)
    spawn_task(task)
    return {'episode': get_episode(episode['episode_id'], user_id=user_id),
            'task_id': task['id']}


def install() -> bool:
    """Register the listener once on the engine's settlement path."""
    global _installed
    if _installed:
        return False
    from lib.tasks_pkg.manager._terminal import register_terminal_listener
    register_terminal_listener(enqueue_task_terminal)
    _installed = True
    return True
