"""When a running agent is wedged: one silence judgement for root and child agents.

Entry points: ``stall_threshold_seconds``, ``judge``, ``is_wedged`` and
``silence_origin``. Dependencies: ``explained_wait`` (who owns a silence),
``lib.fs_keepalive`` (host filesystem recovery) and the per-task foreground
command registry. This module decides; actors apply verdicts elsewhere:

- the stuck-task reaper (``manager._maintenance.reap_stuck_running_tasks``)
  interrupts a silent command, finalizes a wedged root turn from outside, and
  delivers a wedged child's typed stall through its own invocation owner;
- the Swarm beacon (``lib.swarm.liveness``) reports bound agents and backstops
  a swarm with the same verdicts;
- the running-work projection (``manager._registry.list_running_tasks``) hides
  wedged tasks.

A child agent is an ordinary agent running beside its parent: the same clocks,
explained waits, command ladder and threshold decide it. Only the delivery of a
verdict follows the settlement owner (conversation Turn for a root, the
invocation reporting to its parent for a child).

Clocks (wall time, both must be stale): ``_t_last_event`` advances on real
progress events (deltas, tool results and output, retry/wait phases; tool
heartbeat self-ticks never count) and ``_dispatch_heartbeat`` on live provider
dispatch, model waits, ratified human waits and each observed advance of work a
call owns (a completed Coordinator status poll, an advancing worker-root
preparation).

Silence never counts before ``silence_origin``: the end of the task's last
explained wait (the reaper stamps ``EXPLAINED_AT_KEY`` while it observes one) or
the last host filesystem recovery. I/O blocked on a stale mount cannot be
interrupted, so a verdict issued during a freeze could only destroy work at the
moment it resumes.

Ladder: a live foreground command whose own output has been silent past the
threshold is interrupted (partial output returns to the model and the agent
continues) while the dispatch heartbeat is stale; only an interrupt left
unconsumed past its grace, with both clocks stale, makes the agent wedged.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

from lib.log import get_logger
from lib.tasks_pkg.explained_wait import explained_wait

logger = get_logger(__name__)

EXPLAINED_AT_KEY = '_livenessExplainedAt'
# Comfortably longer than any legitimate silent gap (queueing, long tool prep).
_DEFAULT_STALL_THRESHOLD_SECONDS = 1800
# How long a just-issued command interrupt may wait for the run_command read
# loop (which polls every ~0.2s) before that loop itself counts as wedged.
_DEFAULT_COMMAND_INTERRUPT_GRACE_SECONDS = 120


def _env_seconds(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '') or default)
    except (ValueError, TypeError) as error:
        logger.debug('[Liveness] %s parse failed, using %ds: %s', name, default, error)
        return default


def stall_threshold_seconds() -> int:
    """Unexplained silence after which a running agent is wedged; ``<= 0`` disables."""
    return _env_seconds('TOFU_STUCK_TASK_MAX_SILENT_SECS', _DEFAULT_STALL_THRESHOLD_SECONDS)


def command_interrupt_grace_seconds() -> int:
    return _env_seconds('TOFU_CMD_INTERRUPT_GRACE_SECS',
                        _DEFAULT_COMMAND_INTERRUPT_GRACE_SECONDS)


@dataclass(frozen=True, slots=True)
class Verdict:
    """``state``: ``explained``, ``alive``, ``interrupt`` (command ``pid``) or ``wedged``."""

    state: str
    reason: str = ''
    silent_seconds: float = 0.0
    pid: int | None = None
    command_silence: float = 0.0


def silence_origin(task: dict) -> float:
    """Wall instant before which this task's silence is forgiven."""
    from lib.fs_keepalive import filesystem_resumed_at
    try:
        explained_at = float(task.get(EXPLAINED_AT_KEY) or 0.0)
    except (TypeError, ValueError):
        explained_at = 0.0
    return max(explained_at, filesystem_resumed_at()[1])


def judge(task: dict, now: float, threshold: float, *, commands=None) -> Verdict:
    """Judge one running task's silence at wall time ``now``; never mutates it.

    ``commands`` are ``(pid, own silence)`` per live foreground command; omitted,
    they are read from the task's command registry.
    """
    reason = explained_wait(task, now)
    if reason:
        return Verdict('explained', reason)
    origin = silence_origin(task)
    created = task.get('created_at', now)
    event_silent = now - max(task.get('_t_last_event', created), origin)
    dispatch_silent = now - max(task.get('_dispatch_heartbeat', created), origin)
    silent = max(0.0, min(event_silent, dispatch_silent))
    if commands is None:
        from lib.project_mod.foreground_command_slots import foreground_command_silences
        commands = foreground_command_silences(task, now)
    if commands:
        commands = [(pid, min(silence, now - origin)) for pid, silence in commands]
        return _command_ladder(task, commands, now, threshold,
                               event_silent, dispatch_silent, silent)
    if event_silent < threshold or dispatch_silent < threshold:
        return Verdict('alive', silent_seconds=silent)
    return Verdict('wedged', silent_seconds=silent)


def _command_ladder(task, commands, now, threshold, event_silent, dispatch_silent,
                    silent) -> Verdict:
    """Judge per command: concurrent rounds can run several in one task.

    Each command keeps its own output clock, so a chatty sibling cannot hide a
    silent one and the interrupt names the silent pid
    (docs/contracts/TOOL_EXECUTION_POLICY.md, "Concurrent tool rounds").
    """
    pending = task.get('_cmd_interrupt')
    if isinstance(pending, dict) and (
            pending.get('pid') is None
            or pending.get('pid') in {pid for pid, _silence in commands}):
        issued_at = pending.get('ts', now)
        try:
            issued_at = float(issued_at)
        except (ValueError, TypeError) as error:
            logger.debug('[Liveness] bad _cmd_interrupt ts (%s) — treating as fresh', error)
            issued_at = now
        if (now - issued_at >= command_interrupt_grace_seconds()
                and event_silent >= threshold and dispatch_silent >= threshold):
            return Verdict('wedged', silent_seconds=silent)
        return Verdict('alive', silent_seconds=silent)
    if dispatch_silent < threshold:
        return Verdict('alive', silent_seconds=silent)
    overdue = [(silence, pid) for pid, silence in commands if silence >= threshold]
    if not overdue:
        return Verdict('alive', silent_seconds=silent)
    silence, pid = max(overdue, key=lambda row: row[0])
    return Verdict('interrupt', silent_seconds=silent, pid=pid, command_silence=silence)


def is_wedged(task: dict, *, now: float | None = None,
              threshold: float | None = None) -> bool:
    """Whether the stuck-task reaper would stop this running task now."""
    if threshold is None:
        threshold = stall_threshold_seconds()
    if threshold <= 0:
        return False
    return judge(task, time.time() if now is None else now, threshold).state == 'wedged'


__all__ = ['EXPLAINED_AT_KEY', 'Verdict', 'command_interrupt_grace_seconds', 'is_wedged',
           'judge', 'silence_origin', 'stall_threshold_seconds']
