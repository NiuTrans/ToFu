#!/usr/bin/env python3
"""One-shot online cleanup for historical ``task_results.metadata`` bloat.

The current result write path removes every private ``_wire_*`` usage key
before persisting ``usage`` and ``apiRounds``.  Rows written by older builds
still contain those cache-diagnostic payloads, however.  On the measured
personal installation 4,633 terminal rows retained about 1.19 GiB of
compressed metadata; one result expanded to 99 MiB, 98 MiB of which was
``_wire_field_bytes`` / ``_wire_bytes``.  No recovery or frontend render path
reads that private namespace.

This migration reuses the exact live-write sanitizer.  It is dry-run by
default, processes one large value at a time, updates terminal rows only, and
uses ``status + completed_at`` as an optimistic CAS.  Every normal result
write changes ``completed_at``, so a late checkpoint/finalizer wins rather
than being overwritten by maintenance.

Usage::

    python tests/_migrate_trim_task_result_metadata.py
    python tests/_migrate_trim_task_result_metadata.py --min-kb 256
    python tests/_migrate_trim_task_result_metadata.py --apply
    python tests/_migrate_trim_task_result_metadata.py --apply --id TASK_ID
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import DOMAIN_CHAT, _BACKEND, get_thread_db  # noqa: E402
from lib.database._wrappers import json_dumps_pg  # noqa: E402
from lib.log import get_logger  # noqa: E402
from lib.storage_projection import (  # noqa: E402
    _sanitize_api_rounds_for_persist,
)

logger = get_logger(__name__)

_TERMINAL = "('done','error','aborted','interrupted')"


def trim_metadata(meta):
    """Return a non-mutating metadata copy with api-round wire data removed."""
    if not isinstance(meta, dict):
        return meta
    rounds = meta.get('apiRounds')
    if not isinstance(rounds, list):
        return meta
    clean = _sanitize_api_rounds_for_persist(rounds)
    if clean is rounds:
        return meta
    return {**meta, 'apiRounds': clean}


def _as_dict(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None
    return None


def _candidate_ids(db, only_id='', min_kb=0.0, limit=0):
    """Find ids without returning/detaining every multi-MiB metadata value."""
    size_expr = ('pg_column_size(metadata)' if _BACKEND == 'pg'
                 else 'length(CAST(metadata AS TEXT))')
    where = [f'status IN {_TERMINAL}', 'completed_at IS NOT NULL',
             'metadata IS NOT NULL', 'metadata LIKE ?']
    params = ['%"_wire_%']
    if only_id:
        where.append('task_id=?')
        params.append(only_id)
    if min_kb > 0:
        where.append(f'{size_expr}>=?')
        params.append(int(min_kb * 1024))
    sql = (f'SELECT task_id, {size_expr} AS n FROM task_results WHERE '
           + ' AND '.join(where) + ' ORDER BY task_id')
    if limit:
        sql += ' LIMIT ?'
        params.append(int(limit))
    rows = db.execute(sql, tuple(params)).fetchall()
    return [(r['task_id'], int(r['n'] or 0)) for r in rows]


def _process_one(db, task_id, apply=False):
    row = db.execute(
        'SELECT metadata, status, completed_at FROM task_results '
        'WHERE task_id=?', (task_id,)).fetchone()
    if not row:
        db.rollback()
        return {'status': 'missing'}
    if row['status'] not in ('done', 'error', 'aborted', 'interrupted'):
        db.rollback()
        return {'status': 'nonterminal'}
    meta = _as_dict(row['metadata'])
    if meta is None:
        db.rollback()
        return {'status': 'unparseable'}

    before_text = row['metadata'] if isinstance(row['metadata'], str) \
        else json_dumps_pg(meta)
    clean = trim_metadata(meta)
    after_text = json_dumps_pg(clean)
    before = len(before_text.encode('utf-8'))
    after = len(after_text.encode('utf-8'))
    result = {'status': 'noop' if after >= before else 'shrunk',
              'before': before, 'after': min(before, after)}
    if after >= before or not apply:
        db.rollback()
        return result

    cur = db.execute(
        'UPDATE task_results SET metadata=? '
        'WHERE task_id=? AND status=? AND completed_at=?',
        (after_text, task_id, row['status'], row['completed_at']))
    if int(getattr(cur, 'rowcount', 0) or 0) != 1:
        db.rollback()
        result['status'] = 'cas_lost'
        return result
    db.commit()
    result['status'] = 'applied'
    return result


def _trim_heap():
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass


def run(*, apply=False, only_id='', min_kb=0.0, limit=0, sleep_ms=10):
    db = get_thread_db(DOMAIN_CHAT)
    candidates = _candidate_ids(db, only_id=only_id, min_kb=min_kb,
                                limit=limit)
    db.rollback()  # release the inventory snapshot before per-row work
    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'\n  === trim-task-result-metadata [{mode}] '
          f'{len(candidates)} candidate(s) ===\n')
    totals = {'applied': 0, 'shrunk': 0, 'noop': 0, 'cas_lost': 0,
              'error': 0, 'before': 0, 'after': 0}
    started = time.time()
    for pos, (task_id, _stored_n) in enumerate(candidates, 1):
        try:
            result = _process_one(db, task_id, apply=apply)
            status = result['status']
            if status in ('shrunk', 'applied', 'cas_lost'):
                totals['before'] += result['before']
                totals['after'] += result['after']
                totals[status] += 1
            elif status == 'noop':
                totals['noop'] += 1
            else:
                totals['error'] += 1
                logger.warning('[result-meta-trim] task=%s skipped: %s',
                               task_id, status)
        except Exception as exc:  # one bad historical row cannot abort fleet
            try:
                db.rollback()
            except Exception:
                pass
            totals['error'] += 1
            logger.error('[result-meta-trim] task=%s failed: %s', task_id,
                         exc, exc_info=True)
        finally:
            _trim_heap()
            if sleep_ms:
                time.sleep(sleep_ms / 1000.0)
        if pos % 100 == 0:
            print(f'  ... {pos}/{len(candidates)} processed '
                  f'({time.time() - started:.0f}s)', flush=True)

    reclaimed = totals['before'] - totals['after']
    changed = totals['applied'] if apply else totals['shrunk']
    print(f'\n  changed={changed} noop={totals["noop"]} '
          f'cas_lost={totals["cas_lost"]} error={totals["error"]}')
    print(f'  logical bytes: {totals["before"] / 1048576:.2f} MiB -> '
          f'{totals["after"] / 1048576:.2f} MiB '
          f'(reclaimed {reclaimed / 1048576:.2f} MiB)')
    if not apply and changed:
        print('  dry-run only; pass --apply to write')
    print()
    return 1 if totals['error'] else 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--id', default='')
    parser.add_argument('--min-kb', type=float, default=0.0)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--sleep-ms', type=int, default=10)
    args = parser.parse_args()
    return run(apply=args.apply, only_id=args.id, min_kb=args.min_kb,
               limit=args.limit, sleep_ms=args.sleep_ms)


if __name__ == '__main__':
    sys.exit(main())
