"""Private reconstructible Git workspaces and bounded crash reclamation.

Only marked jobs below this owner's private temporary directory are reclaimed.
PID creation times protect running requests across processes and PID reuse.
Historical upstream caches and caller upload sources never enter this directory.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import tempfile

import psutil

from .writes import WriteError, contract


def identity(pid):
    return {'pid': pid, 'created': psutil.Process(pid).create_time()}


def alive(value):
    try:
        if (not isinstance(value, dict) or type(value.get('pid')) is not int or value['pid'] <= 0
                or type(value.get('created')) not in (int, float)
                or not math.isfinite(value['created']) or value['created'] <= 0):
            return True
        process = psutil.Process(value['pid'])
        return process.create_time() == value['created'] and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, KeyError, TypeError, ValueError):
        return True  # Uncertain ownership cannot authorize reclamation.


def private_root(override=None):
    owner = str(os.getuid()) if hasattr(os, 'getuid') else hashlib.sha256(getpass.getuser().encode()).hexdigest()[:16]
    root = Path(override) if override else Path(tempfile.gettempdir()) / ('tofu-overleaf-git-' + owner)
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or (hasattr(os, 'getuid') and
            (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077)):
        raise WriteError('git_workspace_owner')
    return root


def sweep(root):
    count = 0
    remaining = 0
    maximum = contract()['x-resource-limits']['overleaf_git_workspace_jobs']
    with os.scandir(root) as entries:
        for entry in entries:
            count += 1
            remaining += 1
            if count > maximum * 2:
                raise WriteError('git_workspace_capacity')
            if not entry.name.startswith('job-') or not entry.is_dir(follow_symlinks=False):
                continue
            path = Path(entry.path)
            try:
                marker = path / 'owner.json'
                if marker.is_symlink() or marker.stat().st_size > 1024:
                    continue
                value = json.loads(marker.read_bytes())
                if not isinstance(value, dict):
                    continue
                if type(value.get('schema')) is not int or value['schema'] != 1 or (
                        value.get('retired') is not True and alive(value['parent'])):
                    continue
                worker = value.get('worker')
                if worker and alive(worker):
                    process = psutil.Process(worker['pid'])
                    # A matching PID lease and our exact private job argument
                    # are both required before terminating an orphan worker.
                    if process.cmdline()[-1:] != [str(path)]:
                        continue
                    children = process.children(recursive=True)
                    if os.name != 'nt' and os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        for child in children:
                            child.kill()
                        process.kill()
                    _, running = psutil.wait_procs([process, *children], timeout=1)
                    if any(p.status() != psutil.STATUS_ZOMBIE for p in running):
                        raise WriteError('git_workspace_cleanup')
                shutil.rmtree(path)
                remaining -= 1
            except (KeyError, ValueError, FileNotFoundError, psutil.NoSuchProcess):
                continue
    if remaining >= maximum:
        raise WriteError('git_workspace_capacity')


def create_workspace(override=None):
    base = private_root(override)
    sweep(base)
    root = Path(tempfile.mkdtemp(prefix='job-', dir=base))
    try:
        value = {'schema': 1, 'parent': identity(os.getpid())}
        (root / 'owner.json').write_text(json.dumps(value))
    except BaseException:
        shutil.rmtree(root)
        raise
    return root


def record_worker(root, pid):
    path = root / 'owner.json'
    value = json.loads(path.read_bytes())
    value['worker'] = identity(pid)
    replacement = root / 'owner.next'
    replacement.write_text(json.dumps(value))
    replacement.replace(path)


def retire_workspace(root):
    """Mark an exited job reclaimable even while its transport remains alive."""
    path = root / 'owner.json'
    value = json.loads(path.read_bytes())
    value['retired'] = True
    replacement = root / 'owner.next'
    replacement.write_text(json.dumps(value))
    replacement.replace(path)
