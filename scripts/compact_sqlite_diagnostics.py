#!/usr/bin/env python3
"""Compact private wire diagnostics from historical SQLite task storage.

Current writes remove backend-only ``_wire_*`` data before persisting task
events and terminal-result metadata. Historical rows predate that projection
and can retain hundreds of megabytes that recovery and the UI never read.

Safety contract:

* dry-run is the default and opens SQLite with ``mode=ro`` + ``query_only``;
* only ``task_events.payload`` and terminal ``task_results.metadata`` change;
* public usage/cost/dispatch fields and every other column are retained;
* exact old-value CAS makes a concurrent producer/finalizer win;
* small ``BEGIN IMMEDIATE`` batches, busy retries, and pacing bound lock time;
* no application/database bootstrap import, ``VACUUM``, or journal mutation.

Examples::

    python scripts/compact_sqlite_diagnostics.py
    python scripts/compact_sqlite_diagnostics.py --apply --batch-size 8
    python scripts/compact_sqlite_diagnostics.py --target events --limit 100
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

from lib.storage_projection import (  # noqa: E402
    project_event_usage_for_storage,
    project_task_result_metadata_for_storage,
)


_TOOLING_PATH = ROOT / 'lib' / 'database' / 'sqlite_tooling.py'
_TOOLING_SPEC = importlib.util.spec_from_file_location(
    '_tofu_diagnostics_sqlite_tooling', _TOOLING_PATH)
if _TOOLING_SPEC is None or _TOOLING_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError(f'cannot load SQLite tooling module: {_TOOLING_PATH}')
_SQLITE_TOOLING = importlib.util.module_from_spec(_TOOLING_SPEC)
_TOOLING_SPEC.loader.exec_module(_SQLITE_TOOLING)


_TERMINAL = ('done', 'error', 'aborted', 'interrupted')


def _connect(path: Path, *, writable: bool):
    return _SQLITE_TOOLING.open_sqlite_tool_connection(
        path, writable=writable)


def _validate_schema(conn: sqlite3.Connection) -> None:
    expected = {
        'task_events': {'task_id', 'event_id', 'type', 'payload'},
        'task_results': {
            'task_id', 'status', 'completed_at', 'metadata',
        },
    }
    for table, required in expected.items():
        columns = {
            str(row['name']) for row in conn.execute(
                f'PRAGMA table_info({table})')
        }
        missing = required - columns
        if missing:
            raise RuntimeError(
                f'{table} schema is missing: ' + ', '.join(sorted(missing)))


def _json_projection(raw, projector):
    text = raw.decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)
    value = json.loads(text)
    clean = projector(value)
    before = len(text.encode('utf-8'))
    if clean is value:
        return text, before, before, False
    replacement = json.dumps(clean, ensure_ascii=False, separators=(',', ':'))
    after = len(replacement.encode('utf-8'))
    return replacement, before, after, after < before


def _event_batch(conn, after_task: str, after_event: int, size: int):
    return conn.execute(
        "SELECT task_id,event_id,type,payload FROM task_events "
        "WHERE instr(CAST(payload AS TEXT), '\"_wire_') > 0 "
        "AND (task_id > ? OR (task_id=? AND event_id>?)) "
        "ORDER BY task_id,event_id LIMIT ?",
        (after_task, after_task, after_event, size),
    ).fetchall()


def _result_batch(conn, after_task: str, size: int):
    placeholders = ','.join('?' for _ in _TERMINAL)
    return conn.execute(
        f"SELECT task_id,status,completed_at,metadata FROM task_results "
        f"WHERE status IN ({placeholders}) AND completed_at IS NOT NULL "
        "AND metadata IS NOT NULL "
        "AND instr(CAST(metadata AS TEXT), '\"_wire_') > 0 "
        "AND task_id > ? ORDER BY task_id LIMIT ?",
        (*_TERMINAL, after_task, size),
    ).fetchall()


def _write_batch(conn, sql: str, updates, *, path: Path, retries: int = 5):
    def _apply(db):
        applied = cas_lost = 0
        for params in updates:
            cur = db.execute(sql, params)
            if cur.rowcount == 1:
                applied += 1
            else:
                cas_lost += 1
        return applied, cas_lost

    return _SQLITE_TOOLING.run_sqlite_tool_write(
        conn,
        db_path=path,
        canonical_path=ROOT / 'data' / 'tofu.db',
        purpose='compact task storage diagnostics',
        operation=_apply,
        retries=retries,
    )


@contextmanager
def _apply_lock(path: Path):
    lock_path = path.with_name(f'.{path.name}.diagnostics-compact.lock')
    handle = lock_path.open('a+', encoding='utf-8')
    try:
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (ImportError, BlockingIOError, OSError) as exc:
            raise RuntimeError(
                f'another diagnostics compactor is active (lock={lock_path})') from exc
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({'pid': os.getpid(), 'started_at': int(time.time())}))
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


@contextmanager
def _null_context():
    yield


def _empty_report():
    return {
        'candidates': 0, 'compactable': 0, 'applied': 0, 'cas_lost': 0,
        'malformed': 0, 'logical_bytes_before': 0,
        'logical_bytes_after': 0, 'logical_bytes_reclaimed': 0,
    }


def _accumulate(report, before, after):
    report['compactable'] += 1
    report['logical_bytes_before'] += before
    report['logical_bytes_after'] += after


def run(path: Path, *, apply: bool = False, target: str = 'all',
        limit: int = 0, batch_size: int = 8, sleep_ms: int = 25):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    if target not in ('all', 'events', 'results'):
        raise ValueError(f'invalid target: {target}')
    limit = max(0, int(limit))
    batch_size = max(1, min(int(batch_size), 256))
    sleep_ms = max(0, min(int(sleep_ms), 10_000))
    scan = _connect(path, writable=False)
    writer = _connect(path, writable=True) if apply else None
    _validate_schema(scan)
    report = {
        'mode': 'apply' if apply else 'dry-run', 'database': str(path),
        'target': target, 'events': _empty_report(), 'results': _empty_report(),
        'vacuum_performed': False,
    }
    started = time.monotonic()
    lock = _apply_lock(path) if apply else _null_context()
    try:
        with lock:
            if target in ('all', 'events'):
                after_task, after_event = '', -1
                while True:
                    remaining = limit - report['events']['candidates'] if limit else batch_size
                    if limit and remaining <= 0:
                        break
                    rows = _event_batch(scan, after_task, after_event,
                                        min(batch_size, remaining) if limit else batch_size)
                    if not rows:
                        break
                    after_task = str(rows[-1]['task_id'])
                    after_event = int(rows[-1]['event_id'])
                    updates = []
                    for row in rows:
                        part = report['events']
                        part['candidates'] += 1
                        try:
                            replacement, before, after, changed = _json_projection(
                                row['payload'], project_event_usage_for_storage)
                        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                            part['malformed'] += 1
                            continue
                        if not changed:
                            continue
                        _accumulate(part, before, after)
                        updates.append((replacement, row['task_id'], row['event_id'],
                                        row['type'], row['payload']))
                    if apply and updates:
                        applied, lost = _write_batch(
                            writer,
                            'UPDATE task_events SET payload=? WHERE task_id=? '
                            'AND event_id=? AND type=? AND payload=?', updates,
                            path=path)
                        report['events']['applied'] += applied
                        report['events']['cas_lost'] += lost
                    rows = updates = None
                    gc.collect()
                    if apply and sleep_ms:
                        time.sleep(sleep_ms / 1000.0)

            if target in ('all', 'results'):
                after_task = ''
                while True:
                    remaining = limit - report['results']['candidates'] if limit else batch_size
                    if limit and remaining <= 0:
                        break
                    rows = _result_batch(
                        scan, after_task,
                        min(batch_size, remaining) if limit else batch_size)
                    if not rows:
                        break
                    after_task = str(rows[-1]['task_id'])
                    updates = []
                    for row in rows:
                        part = report['results']
                        part['candidates'] += 1
                        try:
                            replacement, before, after, changed = _json_projection(
                                row['metadata'],
                                project_task_result_metadata_for_storage)
                        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
                            part['malformed'] += 1
                            continue
                        if not changed:
                            continue
                        _accumulate(part, before, after)
                        updates.append((replacement, row['task_id'], row['status'],
                                        row['completed_at'], row['metadata']))
                    if apply and updates:
                        applied, lost = _write_batch(
                            writer,
                            'UPDATE task_results SET metadata=? WHERE task_id=? '
                            'AND status=? AND completed_at=? AND metadata=?', updates,
                            path=path)
                        report['results']['applied'] += applied
                        report['results']['cas_lost'] += lost
                    rows = updates = None
                    gc.collect()
                    if apply and sleep_ms:
                        time.sleep(sleep_ms / 1000.0)
    finally:
        scan.close()
        if writer is not None:
            writer.close()

    for part in (report['events'], report['results']):
        part['logical_bytes_reclaimed'] = (
            part['logical_bytes_before'] - part['logical_bytes_after'])
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'data' / 'tofu.db')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--target', choices=('all', 'events', 'results'), default='all')
    parser.add_argument('--limit', type=int, default=0,
                        help='maximum candidates per selected table (0 = all)')
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--sleep-ms', type=int, default=25)
    args = parser.parse_args(argv)
    report = run(args.database, apply=args.apply, target=args.target,
                 limit=args.limit, batch_size=args.batch_size,
                 sleep_ms=args.sleep_ms)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    malformed = report['events']['malformed'] + report['results']['malformed']
    return 2 if malformed else 0


if __name__ == '__main__':
    raise SystemExit(main())
