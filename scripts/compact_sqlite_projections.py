#!/usr/bin/env python3
"""Compact historical SQLite first-paint projections without touching authority.

Old ``conversation_messages.meta_light`` rows can still carry backend-only
``_wire_*`` diagnostics even though current writes and HTTP responses remove
them.  Those values make a windowed read transfer megabytes from SQLite into
Python before the HTTP sanitizer can help.

Safety contract:

* dry-run is the default and opens SQLite with ``mode=ro`` + ``query_only``;
* ``--apply`` updates only the derived ``meta_light`` column;
* the lossless ``meta`` and authoritative ``conversations.messages`` columns
  are never selected for rewrite;
* bounded keyset batches avoid loading the fleet into memory;
* each write uses an exact old-value CAS, so a concurrent server update wins;
* short transactions, busy retries, and optional pacing keep the personal
  server responsive;
* no ``VACUUM`` or journal-mode mutation is performed.

Examples::

    python scripts/compact_sqlite_projections.py
    python scripts/compact_sqlite_projections.py --limit 100
    python scripts/compact_sqlite_projections.py --apply --batch-size 8
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import gc
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Pure by contract: importing this module must not discover/start a database.
from lib.storage_projection import project_message_for_window  # noqa: E402


_TOOLING_PATH = ROOT / 'lib' / 'database' / 'sqlite_tooling.py'
_TOOLING_SPEC = importlib.util.spec_from_file_location(
    '_tofu_projection_sqlite_tooling', _TOOLING_PATH)
if _TOOLING_SPEC is None or _TOOLING_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError(f'cannot load SQLite tooling module: {_TOOLING_PATH}')
_SQLITE_TOOLING = importlib.util.module_from_spec(_TOOLING_SPEC)
_TOOLING_SPEC.loader.exec_module(_SQLITE_TOOLING)


def _connect(path: Path, *, writable: bool):
    return _SQLITE_TOOLING.open_sqlite_tool_connection(
        path, writable=writable)


def _validate_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row['name']) for row in conn.execute(
            'PRAGMA table_info(conversation_messages)')
    }
    required = {'conv_id', 'seq', 'meta', 'meta_light'}
    missing = required - columns
    if missing:
        raise RuntimeError(
            'conversation_messages schema is missing: ' + ', '.join(sorted(missing)))


def _candidate_batch(conn: sqlite3.Connection, after_conv: str,
                     after_seq: int, batch_size: int):
    return conn.execute(
        "SELECT conv_id, seq, meta_light FROM conversation_messages "
        "WHERE meta_light IS NOT NULL "
        "AND instr(CAST(meta_light AS TEXT), '\"_wire_') > 0 "
        "AND (conv_id > ? OR (conv_id=? AND seq>?)) "
        "ORDER BY conv_id, seq LIMIT ?",
        (after_conv, after_conv, after_seq, batch_size),
    ).fetchall()


def _compact_value(raw):
    """Return ``(replacement, before_bytes, after_bytes, changed)``."""
    if isinstance(raw, bytes):
        text = raw.decode('utf-8', 'replace')
    elif isinstance(raw, str):
        text = raw
    else:
        text = json.dumps(raw, ensure_ascii=False, separators=(',', ':'))
    value = json.loads(text)
    projected = project_message_for_window(value)
    if projected is value:
        return text, len(text.encode('utf-8')), len(text.encode('utf-8')), False
    replacement = json.dumps(
        projected, ensure_ascii=False, separators=(',', ':'))
    return (replacement, len(text.encode('utf-8')),
            len(replacement.encode('utf-8')), True)


@contextmanager
def _apply_lock(path: Path):
    lock_path = path.with_name(f'.{path.name}.projection-compact.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open('a+', encoding='utf-8')
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError, OSError) as exc:
            raise RuntimeError(
                f'another projection compactor is active (lock={lock_path})') from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            'pid': os.getpid(), 'db': str(path.resolve()),
            'started_at': int(time.time()),
        }, sort_keys=True))
        handle.flush()
        os.fsync(handle.fileno())
        yield
    finally:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        handle.close()


def _write_batch(conn, updates, *, path: Path, retries: int = 5):
    """Apply exact-old-value CAS updates in one short, retryable transaction."""
    def _apply(db):
        applied = cas_lost = 0
        for replacement, conv_id, seq, old_value in updates:
            cur = db.execute(
                'UPDATE conversation_messages SET meta_light=? '
                'WHERE conv_id=? AND seq=? AND meta_light=?',
                (replacement, conv_id, seq, old_value),
            )
            if cur.rowcount == 1:
                applied += 1
            else:
                cas_lost += 1
        return applied, cas_lost

    return _SQLITE_TOOLING.run_sqlite_tool_write(
        conn,
        db_path=path,
        canonical_path=ROOT / 'data' / 'tofu.db',
        purpose='compact conversation message projections',
        operation=_apply,
        retries=retries,
    )


def run(path: Path, *, apply: bool = False, limit: int = 0,
        batch_size: int = 8, sleep_ms: int = 25, progress_every: int = 100):
    """Scan/compact projections and return a machine-readable report."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    batch_size = max(1, min(int(batch_size), 256))
    limit = max(0, int(limit))
    sleep_ms = max(0, min(int(sleep_ms), 10_000))

    scan = _connect(path, writable=False)
    writer = _connect(path, writable=True) if apply else None
    _validate_schema(scan)
    report = {
        'mode': 'apply' if apply else 'dry-run',
        'database': str(path),
        'candidates': 0,
        'compactable': 0,
        'applied': 0,
        'cas_lost': 0,
        'malformed': 0,
        'logical_bytes_before': 0,
        'logical_bytes_after': 0,
        'logical_bytes_reclaimed': 0,
        'vacuum_performed': False,
    }
    started = time.monotonic()
    after_conv, after_seq = '', -1
    lock = _apply_lock(path) if apply else _null_context()
    try:
        with lock:
            while True:
                remaining = limit - report['candidates'] if limit else batch_size
                if limit and remaining <= 0:
                    break
                rows = _candidate_batch(
                    scan, after_conv, after_seq,
                    min(batch_size, remaining) if limit else batch_size)
                if not rows:
                    break
                after_conv = str(rows[-1]['conv_id'])
                after_seq = int(rows[-1]['seq'])
                updates = []
                for row in rows:
                    report['candidates'] += 1
                    raw = row['meta_light']
                    try:
                        replacement, before, after, changed = _compact_value(raw)
                    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                        report['malformed'] += 1
                        continue
                    if not changed or after >= before:
                        continue
                    report['compactable'] += 1
                    report['logical_bytes_before'] += before
                    report['logical_bytes_after'] += after
                    updates.append((replacement, str(row['conv_id']),
                                    int(row['seq']), raw))
                if apply and updates:
                    applied, cas_lost = _write_batch(
                        writer, updates, path=path)
                    report['applied'] += applied
                    report['cas_lost'] += cas_lost
                if (progress_every and report['candidates'] % progress_every
                        < len(rows)):
                    print(json.dumps({
                        'progress': report['candidates'],
                        'compactable': report['compactable'],
                        'applied': report['applied'],
                    }, sort_keys=True), flush=True)
                updates.clear()
                rows = None
                gc.collect()
                if apply and sleep_ms:
                    time.sleep(sleep_ms / 1000.0)
    finally:
        scan.close()
        if writer is not None:
            writer.close()

    report['logical_bytes_reclaimed'] = (
        report['logical_bytes_before'] - report['logical_bytes_after'])
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return report


@contextmanager
def _null_context():
    yield


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path,
                        default=ROOT / 'data' / 'tofu.db')
    parser.add_argument('--apply', action='store_true',
                        help='write compact derived projections (default: read-only)')
    parser.add_argument('--limit', type=int, default=0,
                        help='inspect at most N candidates (0 = all)')
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--sleep-ms', type=int, default=25,
                        help='pace apply batches; ignored in dry-run')
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    report = run(
        args.database, apply=args.apply, limit=args.limit,
        batch_size=args.batch_size, sleep_ms=args.sleep_ms,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 2 if report['malformed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
