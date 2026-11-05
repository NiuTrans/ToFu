"""Process-group records for foreground commands, retired after owner death.

Responsibility: a foreground ``run_command`` subprocess owns a POSIX process
group whose output only its tool call can read. Graceful shutdown retires it,
but a crash, the shutdown hard deadline or an in-place ``execv`` restart can
leave it running unobserved, still holding inherited workspace leases, while a
resumed agent may start the same work again. Each spawn writes one small
record; settlement removes it. At boot, a dead owner's records are retired
through ``process_group``: a live leader must match its recorded start time,
and with the leader gone the group id is still referenced only by survivors
of the recorded group. Background commands keep their own durable records and
reattach after a restart; they are never recorded here.

Entry points: ``record_foreground_command`` / ``release_foreground_command``
(command runners), ``retire_orphaned_foreground_commands`` (boot), and
``surviving_foreground_commands`` (swarm restart recovery).
Dependencies: ``lib.boot_identity``, ``lib.process_owner``,
``lib.project_mod.process_group``, ``lib.runtime.runtime_paths``.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

_RECORD_SCHEMA = 1
_COMMAND_DISPLAY_CHARS = 200


def _records_root() -> str:
    from lib.runtime.runtime_paths import data_root
    return os.path.join(data_root(), 'foreground_commands')


def _current_owner() -> dict | None:
    """Host/boot/namespace/PID identity plus the per-exec instance id.

    ``execv`` keeps PID and start time, so only ``BOOT_ID`` distinguishes a
    restarted server from the incarnation that spawned a recorded group.
    """
    from lib.boot_identity import BOOT_ID
    from lib.process_owner import process_owner
    owner = process_owner()
    return None if owner is None else {**owner, 'instance': BOOT_ID}


def _start_time_token(pid: int) -> str | None:
    from lib.project_mod.process_group import ProcessGroupRetirementError, read_process_fields
    try:
        return read_process_fields(pid)[19].decode('ascii')
    except (OSError, IndexError, UnicodeError, ProcessGroupRetirementError):
        return None


def record_foreground_command(proc: Any, *, task: dict | None = None,
                              runtime_context: Any = None, command: str = '') -> str | None:
    """Persist the spawned group before callbacks run; best effort, never blocks.

    Returns the record path, or None when the platform, background ownership
    or an I/O failure leaves nothing to record.
    """
    if os.name != 'posix' or (task is not None and task.get('_background_detached')):
        return None
    pgid = getattr(proc, '_tofu_owned_pgid', None)
    owner = _current_owner()
    token = _start_time_token(proc.pid) if pgid else None
    if not pgid or owner is None or token is None:
        return None
    record = {
        'schema': _RECORD_SCHEMA, 'owner': owner,
        'pid': proc.pid, 'pgid': pgid, 'pid_starttime': token,
        'conv_id': str((task or {}).get('convId') or ''),
        'tool_call_id': str(getattr(runtime_context, 'tool_call_id', '') or ''),
        'command': str(command or '')[:_COMMAND_DISPLAY_CHARS],
        'started_at': time.time(),
    }
    root = _records_root()
    path = os.path.join(root, f"{owner['instance']}-{pgid}.json")
    try:
        os.makedirs(root, exist_ok=True)
        with open(path + '.tmp', 'w', encoding='utf-8') as handle:
            json.dump(record, handle)
        os.replace(path + '.tmp', path)
    except OSError as exc:
        logger.warning('[ForegroundCommand] record for pgid=%s unavailable: %s', pgid, exc)
        return None
    return path


def release_foreground_command(path: str | None) -> None:
    """Drop a record once its group retired or exited; a retained one is swept."""
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning('[ForegroundCommand] record %s not released: %s', path, exc)


def _read_record(path: str) -> dict | None:
    try:
        with open(path, encoding='utf-8') as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and isinstance(record.get('owner'), dict) else None


def _record_paths() -> list[str]:
    """Record files, including a crash-interrupted ``.tmp`` publication."""
    root = _records_root()
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    return [os.path.join(root, name) for name in names
            if name.endswith('.json') or name.endswith('.json.tmp')]


def _owner_state(owner: dict, current: dict) -> str:
    """``dead``, ``alive``, ``unknown``, ``previous_boot`` or ``foreign``."""
    if (owner.get('host') != current['host']
            or owner.get('pid_namespace') != current['pid_namespace']):
        return 'foreign'
    if owner.get('boot') != current['boot']:
        return 'previous_boot'
    if owner.get('pid') == current['pid'] and owner.get('started') == current['started']:
        return 'dead'  # This process before execv; the instance id differs.
    from lib.process_owner import owner_state
    return owner_state(owner)


def _leader_state(pid: int, token: str) -> str:
    try:
        from lib.project_mod.process_group import ProcessGroupRetirementError, read_process_fields
        fields = read_process_fields(pid)
    except (FileNotFoundError, ProcessLookupError):
        return 'gone'
    except (OSError, ProcessGroupRetirementError):
        return 'unknown'
    return 'same' if fields[19].decode('ascii', 'replace') == token else 'other'


def _retire_record_group(record: dict) -> str:
    """``retired``, ``gone`` (nothing of ours is left), ``invalid`` or ``failed``."""
    from lib.project_mod.process_group import (
        ProcessGroupRetirementError, group_has_live_members, retire_process_group,
    )
    try:
        pid, pgid, token = int(record['pid']), int(record['pgid']), str(record['pid_starttime'])
    except (KeyError, TypeError, ValueError):
        return 'invalid'
    if pid != pgid or pgid <= 1 or pgid == os.getpgrp() or not token.isdecimal():
        return 'invalid'
    leader = _leader_state(pid, token)
    if leader == 'other':
        return 'gone'  # The number was reused, so the recorded group has ended.
    if leader == 'unknown':
        return 'failed'
    try:
        # A dead leader's group id stays allocated while any member lives, so
        # surviving members can only belong to the recorded group.
        if leader == 'gone' and not group_has_live_members(pgid):
            return 'gone'
        retire_process_group(pgid, leader_alive=(
            (lambda: _leader_state(pid, token) == 'same') if leader == 'same' else None))
    except (OSError, ProcessGroupRetirementError) as exc:
        logger.warning('[ForegroundCommand] orphaned group pgid=%d not retired: %s', pgid, exc)
        return 'failed'
    try:
        os.waitpid(pid, os.WNOHANG)  # After execv the old leader is our own child.
    except ChildProcessError:
        pass
    return 'retired'


def retire_orphaned_foreground_commands() -> dict[str, int]:
    """Boot sweep: retire command groups whose owning server process is gone.

    Live owners (another process) and other hosts are left alone; records from
    an earlier OS boot are dropped because their processes cannot survive it.
    A record whose group could not be proven retired stays for swarm recovery
    to see and for the next sweep. Returns counts per outcome.
    """
    counts = {'retired': 0, 'gone': 0, 'kept': 0}
    current = _current_owner()
    if current is None:
        return counts
    for path in _record_paths():
        record = _read_record(path)
        if record is None:
            if not os.path.basename(path).startswith(current['instance']):
                release_foreground_command(path)  # Unreadable and not ours: no authority.
            continue
        owner = record['owner']
        if owner.get('instance') == current['instance']:
            continue
        state = _owner_state(owner, current)
        if state == 'previous_boot':
            release_foreground_command(path)
            counts['gone'] += 1
            continue
        if state != 'dead':
            counts['kept'] += 1
            continue
        outcome = _retire_record_group(record)
        if outcome == 'failed':
            counts['kept'] += 1
            continue
        release_foreground_command(path)
        counts['retired' if outcome == 'retired' else 'gone'] += 1
        if outcome == 'retired':
            logger.warning('[ForegroundCommand] retired orphaned command group pgid=%s '
                           'left by a previous server process: %r',
                           record.get('pgid'), record.get('command'))
    return counts


def surviving_foreground_commands(*, conv_id: str, tool_call_ids: list[str]) -> list[dict]:
    """Records naming these tool calls; after the boot sweep each may still run.

    Raises ``OSError`` when the records cannot be listed: absence of evidence
    is not proof that no command survives.
    """
    wanted = {str(call_id) for call_id in tool_call_ids if call_id}
    if not wanted:
        return []
    found = []
    for path in _record_paths():
        record = _read_record(path)
        if (record is not None and record.get('tool_call_id') in wanted
                and (not conv_id or not record.get('conv_id') or record['conv_id'] == conv_id)):
            found.append(record)
    return found


__all__ = [
    'record_foreground_command', 'release_foreground_command',
    'retire_orphaned_foreground_commands', 'surviving_foreground_commands',
]
