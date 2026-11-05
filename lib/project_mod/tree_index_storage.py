"""Placement and evict-only disk retention for reconstructible project indexes.

Entry points: index_directory chooses a process-stable private namespace using
serverctl_pkg.local_placement; prepare_write reclaims only complete .tfix blobs.
The tree-index owner still validates freshness and atomically publishes blobs.
No project contents, undo state, database authority or running task is evicted.
Missing/oversized/unwritable caches fall back to the existing in-memory index
and live scans. Dependencies: stdlib, runtime_guards and shared local placement.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import time

from runtime_guards import resolve_resource_budget
from serverctl_pkg.local_placement import (
    ensure_private_directory, has_free_space, index_cache_placement,
)

_BLOB_NAME = re.compile(r'[0-9a-f]{20}\.tfix')
_STAGING_NAME = re.compile(r'[0-9a-f]{20}\.tfix\.tmp\.([1-9][0-9]*)\.[0-9]+\.[0-9]+')
_TTL_SECONDS = 7 * 24 * 60 * 60
_ORPHAN_AGE_SECONDS = 24 * 60 * 60


def _process_is_gone(pid: int) -> bool:
    # Signal zero is a liveness probe on POSIX only. Reused/inaccessible PIDs
    # are conservatively live; no cache cleanup may signal or kill a writer.
    if os.name != 'posix':
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        pass
    return False


def index_directory(sessions_directory: str) -> str:
    """Select local cache storage, keeping the legacy path when inapplicable."""
    placement = index_cache_placement(sessions_directory)
    if not placement.selected:
        return os.path.join(sessions_directory, 'project_indexes')
    identity = hashlib.sha256(os.fsencode(os.path.realpath(sessions_directory))).hexdigest()
    base = Path(placement.root)
    # Each level we own is private; reject a namespace symlink before creation.
    for path in (base, base / 'project-indexes', base / 'project-indexes' / identity):
        ensure_private_directory(path)
    return str(path)


def prepare_write(final: str, incoming_bytes: int, *, now: float | None = None) -> bool:
    """Best-effort TTL/LRU reclamation before one atomic cache publication.

    Only complete, privately owned regular blobs are candidates. Open readers
    retain their descriptors; live staged writes and unrelated files survive.
    A staged blob older than a day is removed only when its writer PID is gone.
    Capacity is a cache retention policy, never a file/search/task limit.
    """
    target = Path(final)
    maximum = resolve_resource_budget(
        'TOFU_TREE_INDEX_DISK_MAX_MIB', minimum=1, maximum=4096) * 1024 * 1024
    if incoming_bytes > maximum:
        return False
    current = time.time() if now is None else now
    entries = []
    total = 0
    owner = os.getuid() if hasattr(os, 'getuid') else None
    try:
        with os.scandir(target.parent) as candidates:
            for entry in candidates:
                complete = _BLOB_NAME.fullmatch(entry.name)
                staging = _STAGING_NAME.fullmatch(entry.name)
                if not complete and not staging:
                    continue
                info = entry.stat(follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode)
                        or (owner is not None and info.st_uid != owner)
                        or info.st_nlink != 1):
                    continue
                if staging:
                    if (current - info.st_mtime > _ORPHAN_AGE_SECONDS
                            and _process_is_gone(int(staging.group(1)))):
                        Path(entry.path).unlink()
                    continue
                size = info.st_size
                total += size
                entries.append((info.st_mtime, entry.name, size))
        replacement = next((size for _, name, size in entries if name == target.name), 0)
        for modified, name, size in sorted(entries):
            if name == target.name:
                continue
            if (current - modified <= _TTL_SECONDS
                    and total - replacement + incoming_bytes <= maximum
                    and has_free_space(target.parent, incoming_bytes)):
                break
            try:
                (target.parent / name).unlink()
                total -= size
            except FileNotFoundError:
                total -= size
        return (total - replacement + incoming_bytes <= maximum
                and has_free_space(target.parent, incoming_bytes))
    except OSError:
        return False
