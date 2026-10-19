"""lib/client_report_store.py — durable intake store for native client reports.

Responsibility: append bounded, operator-facing support reports sent by the
Android shell (``POST /api/v1/mobile/client-report``) to a JSONL log and read
them back newest-first. Entry points: :func:`append_report`,
:func:`read_recent`, :func:`store_dir`. Dependencies: ``lib.runtime.runtime_paths`` and
``lib.log`` only; never imports ``routes``.

The report is diagnostic evidence a user chose to send, not durable domain
state, so it lives beside the other operator artifacts under ``data_root()``
rather than in the storage authority. The store is deliberately bounded: a
single file is capped and rotates into timestamped segments, and only the
newest few segments are retained, so a chatty client can never grow the tree
without limit. ``TOFU_CLIENT_REPORTS_DIR`` relocates the whole store.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from lib.log import get_logger
from lib.runtime.runtime_paths import data_root

logger = get_logger(__name__)

CURRENT_NAME = 'client-reports.jsonl'

# One segment holds up to 4 MiB of reports before rotating. Reports are capped
# well below this at the route boundary, so a segment holds many of them.
_MAX_SEGMENT_BYTES = 4 * 1024 * 1024
# Keep the newest N segments (current file + rotated) and prune the rest.
_MAX_SEGMENTS = 8

# A single process-wide lock is enough: appends are small and rare, and this
# store has no cross-process writer (only the serving process receives POSTs).
_LOCK = threading.Lock()


def store_dir() -> Path:
    """Absolute directory holding the report segments (created on demand)."""
    override = os.environ.get('TOFU_CLIENT_REPORTS_DIR', '').strip()
    path = Path(override) if override else Path(data_root()) / 'client_reports'
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_path() -> Path:
    """Path of the segment new reports are appended to."""
    return store_dir() / CURRENT_NAME


def _serialize(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(',', ':'))


def _prune_segments(directory: Path) -> None:
    try:
        segments = sorted(directory.glob('client-reports.*.jsonl'))
    except OSError:
        return
    if len(segments) <= _MAX_SEGMENTS:
        return
    for stale in segments[:-_MAX_SEGMENTS]:
        try:
            stale.unlink()
        except OSError:
            pass


def _rotate_if_needed(path: Path, incoming_bytes: int) -> None:
    try:
        current = path.stat().st_size
    except OSError:
        return
    if current + incoming_bytes <= _MAX_SEGMENT_BYTES:
        return
    segment = path.with_name(f'client-reports.{int(time.time() * 1000)}.jsonl')
    try:
        path.rename(segment)
    except OSError as exc:
        # A failed rotation must not lose the incoming report; fall through and
        # let the append grow the current file rather than raising.
        logger.warning('[client_reports] rotation failed: %s', exc)
        return
    _prune_segments(path.parent)


def append_report(record: dict) -> int:
    """Append one report; returns the number of bytes written to the store."""
    line = _serialize(record) + '\n'
    data = line.encode('utf-8')
    with _LOCK:
        path = store_dir() / CURRENT_NAME
        _rotate_if_needed(path, len(data))
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(line)
    return len(data)


def read_recent(limit: int = 20) -> list[dict]:
    """Return up to [limit] newest reports, newest first (current segment only)."""
    limit = max(1, min(int(limit), 200))
    path = current_path()
    if not path.is_file():
        return []
    records: list[dict] = []
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    records.append(parsed)
    except OSError as exc:
        logger.warning('[client_reports] read failed: %s', exc)
        return []
    return list(reversed(records[-limit:]))


__all__ = ['append_report', 'read_recent', 'store_dir', 'current_path',
           'CURRENT_NAME']
