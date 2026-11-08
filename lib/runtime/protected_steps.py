"""Protected steps: process-lifetime markers that a server restart must not interrupt.

A protected step changes or publishes the checkout the server runs from:
``scripts/git_land.py`` landing a branch and ``export.py`` runs. Such a run may
be a foreground command of a conversation, a background command, a terminal
or another agent, so conversation activity alone cannot see it.
``protected_step(kind)`` keeps one marker under ``<data root>/protected-steps/``
while the step runs; ``active_protected_steps()`` reports the live ones to the
restart-protection signal (``lib.tasks_pkg.manager.list_running_tasks``), which
Settings restart, automatic restart, deferred-restart quiescence and the Global
Router's ``restart_server`` share.

A local marker counts while its process (PID and start time) is alive, so a
crashed run never blocks restarts. A marker written on another host counts
until its writer removes it: its liveness cannot be observed here, and a
restart must not boot from a half-landed checkout (fail closed; the restart
refusal names the marker so a stale one can be removed by hand).

Entry points: protected_step, active_protected_steps, protected_steps_root.
Dependencies: lib.runtime.runtime_paths (data root), runtime_guards (liveness).
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import secrets
import socket
import time

_KINDS = frozenset({'git_land', 'export'})


def protected_steps_root() -> str:
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), 'protected-steps')


def _start_time(pid: int) -> str | None:
    from runtime_guards import proc_stat_fields
    fields = proc_stat_fields(pid)
    return fields[19] if fields is not None and len(fields) > 19 else None


@contextmanager
def protected_step(kind: str, *, detail: str = ''):
    """Mark this process as inside a protected step until the block exits."""
    if kind not in _KINDS:
        raise ValueError(f'unknown protected step: {kind!r}')
    root = protected_steps_root()
    os.makedirs(root, exist_ok=True)
    host, pid = socket.gethostname(), os.getpid()
    identity = f'{kind}-{host}-{pid}-{secrets.token_hex(4)}'
    path = os.path.join(root, identity + '.json')
    marker = {'version': 1, 'id': identity, 'kind': kind, 'host': host, 'pid': pid,
              'startTime': _start_time(pid), 'startedAt': time.time(), 'detail': str(detail)[:300]}
    staged = path + '.tmp'
    with open(staged, 'w', encoding='utf-8') as handle:
        json.dump(marker, handle)
    os.replace(staged, path)
    try:
        yield marker
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _alive(marker: dict) -> bool:
    if marker.get('host') != socket.gethostname():
        return True
    from runtime_guards import pid_is_alive
    pid = marker.get('pid')
    if type(pid) is not int or not pid_is_alive(pid):
        return False
    expected = marker.get('startTime')
    return expected is None or _start_time(pid) in (None, expected)


def active_protected_steps() -> list[dict]:
    """Live protected steps, oldest first; an unreadable marker counts as live."""
    root = protected_steps_root()
    try:
        names = sorted(name for name in os.listdir(root) if name.endswith('.json'))
    except FileNotFoundError:
        return []
    steps = []
    for name in names:
        path = os.path.join(root, name)
        try:
            with open(path, encoding='utf-8') as handle:
                marker = json.load(handle)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            marker = None
        if not isinstance(marker, dict) or not isinstance(marker.get('startedAt'), (int, float)):
            marker = {'id': name[:-5], 'kind': 'unknown', 'host': '', 'pid': None,
                      'startedAt': 0.0, 'unreadable': True}
        elif not _alive(marker):
            continue
        steps.append({**marker, 'path': path})
    return sorted(steps, key=lambda step: step['startedAt'])


__all__ = ['active_protected_steps', 'protected_step', 'protected_steps_root']
