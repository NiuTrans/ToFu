"""Lifecycle-owned, expiring reader reservations in the artifact cache.

The cache's cross-process guard owns every call. No task, process-local timer,
or open file descriptor is retained between HTTP requests. Metadata is cache
state; the build receipt remains immutable. Wall-clock discontinuities expire
reservations rather than extending them beyond the configured lifetime.
"""
from __future__ import annotations

import json
import contextlib
import os
import re
import time
from pathlib import Path

from .contract import limit
from .errors import BuildError

FILES = frozenset({'.viewers.json', '.viewers.pending'})


def active_viewers(directory: Path, now: int | None = None) -> dict[str, int]:
    now = int(time.time()) if now is None else now
    path = directory / '.viewers.json'
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise BuildError('cache_busy', 'Paper reader reservations are temporarily unavailable') from exc
    lifetime = limit('viewer_lease_seconds')
    try:
        if path.is_symlink():
            raise ValueError('invalid reader metadata')
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError('invalid reader count')
        if any(not re.fullmatch(r'[0-9a-f]{32}', key) or type(deadline) is not int
               for key, deadline in value.items()):
            raise ValueError('invalid reader identity or expiry')
        return {key: deadline for key, deadline in value.items()
                if now < deadline <= now + lifetime}
    except (ValueError, OSError) as exc:
        # A failed metadata read cannot silently evict a potentially live PDF.
        # The mtime bound also expires corrupt cache metadata after a crash.
        if now - stat.st_mtime >= lifetime or stat.st_mtime > now + lifetime:
            return {}
        raise BuildError('cache_busy', 'Paper reader reservations are temporarily unavailable') from exc


def write_viewers(directory: Path, viewers: dict[str, int]) -> None:
    path = directory / '.viewers.json'
    if not viewers:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise BuildError('cache_busy', 'Cannot release the paper reader reservation; retry') from exc
        return
    encoded = json.dumps(viewers, separators=(',', ':')).encode()
    pending = directory / '.viewers.pending'
    try:
        pending.write_bytes(encoded)
        os.replace(pending, path)
    except OSError as exc:
        raise BuildError('cache_busy', 'Cannot update the paper reader reservation; retry') from exc
    finally:
        with contextlib.suppress(OSError):
            pending.unlink(missing_ok=True)
