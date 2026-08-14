#!/usr/bin/env python3
"""Trim historical ``_wire_*`` diagnostics from durable task events.

New writes use ``event_log._project_usage_diagnostics_for_storage``.  This
one-shot backfill applies that exact projection to legacy ``round_usage`` and
terminal events.  It is dry-run by default, key-bounded, batch-committed, and
append-only event rows are addressed by their immutable composite primary key.

Usage::

    python tests/_migrate_trim_task_event_usage.py
    python tests/_migrate_trim_task_event_usage.py --apply
    python tests/_migrate_trim_task_event_usage.py --apply --limit 1000
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
from lib.tasks_pkg.event_log import (  # noqa: E402
    _project_usage_diagnostics_for_storage,
)

logger = get_logger(__name__)

_EVENT_TYPES = (
    'round_usage', 'done', 'error', 'aborted', 'interrupted',
    'autopilot_vu_start', 'autopilot_vu_done', 'autopilot_vu_event',
)


def _payload(raw):
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or '{}')
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _candidate_keys(db, limit=0):
    placeholders = ','.join('?' for _ in _EVENT_TYPES)
    size_expr = ('pg_column_size(payload)' if _BACKEND == 'pg'
                 else 'length(CAST(payload AS TEXT))')
    if _BACKEND == 'pg':
        wire_pred = ("((payload->'usage')::text LIKE ? OR "
                     " (payload->'apiRounds')::text LIKE ? OR "
                     " (payload->'committedMessage')::text LIKE ? OR "
                     " (payload->'parentMessage')::text LIKE ?)")
    else:
        wire_pred = ("(json_extract(payload, '$.usage') LIKE ? OR "
                     " json_extract(payload, '$.apiRounds') LIKE ? OR "
                     " json_extract(payload, '$.committedMessage') LIKE ? OR "
                     " json_extract(payload, '$.parentMessage') LIKE ?)")
    sql = (f'SELECT task_id,event_id,type,{size_expr} AS n FROM task_events '
           f'WHERE type IN ({placeholders}) AND {wire_pred} '
           'ORDER BY task_id,event_id')
    params = [*_EVENT_TYPES, *(['%"_wire_%'] * 4)]
    if limit:
        sql += ' LIMIT ?'
        params.append(int(limit))
    rows = db.execute(sql, tuple(params)).fetchall()
    return [(r['task_id'], int(r['event_id']), r['type'], int(r['n'] or 0))
            for r in rows]


def _process_batch(db, keys, *, apply=False):
    report = {'changed': 0, 'noop': 0, 'missing': 0, 'cas_lost': 0,
              'before': 0, 'after': 0}
    try:
        for task_id, event_id, event_type, _stored_n in keys:
            row = db.execute(
                'SELECT payload,type FROM task_events '
                'WHERE task_id=? AND event_id=?',
                (task_id, event_id)).fetchone()
            if not row:
                report['missing'] += 1
                continue
            event = _payload(row['payload'])
            if event is None:
                report['missing'] += 1
                continue
            clean = _project_usage_diagnostics_for_storage(event)
            if clean is event:
                report['noop'] += 1
                continue
            before_text = (json.dumps(event, ensure_ascii=False,
                                      separators=(',', ':')))
            after_text = json_dumps_pg(clean)
            before = len(before_text.encode('utf-8'))
            after = len(after_text.encode('utf-8'))
            if after >= before:
                report['noop'] += 1
                continue
            report['before'] += before
            report['after'] += after
            if apply:
                cur = db.execute(
                    'UPDATE task_events SET payload=? '
                    'WHERE task_id=? AND event_id=? AND type=?',
                    (after_text, task_id, event_id, event_type))
                if int(getattr(cur, 'rowcount', 0) or 0) != 1:
                    report['cas_lost'] += 1
                    continue
            report['changed'] += 1
        if apply:
            db.commit()
        else:
            db.rollback()
        return report
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise


def _trim_heap():
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass


def run(*, apply=False, limit=0, batch_size=200, sleep_ms=20):
    db = get_thread_db(DOMAIN_CHAT)
    keys = _candidate_keys(db, limit=limit)
    db.rollback()
    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'\n  === trim-task-event-usage [{mode}] {len(keys)} candidate(s) ===\n')
    total = {'changed': 0, 'noop': 0, 'missing': 0, 'cas_lost': 0,
             'before': 0, 'after': 0, 'errors': 0}
    started = time.time()
    for offset in range(0, len(keys), max(1, batch_size)):
        batch = keys[offset:offset + max(1, batch_size)]
        try:
            report = _process_batch(db, batch, apply=apply)
            for key, value in report.items():
                total[key] += value
        except Exception as exc:
            total['errors'] += len(batch)
            logger.error('[event-usage-trim] batch %d..%d failed: %s',
                         offset, offset + len(batch), exc, exc_info=True)
        finally:
            _trim_heap()
            if sleep_ms:
                time.sleep(sleep_ms / 1000.0)
        done = offset + len(batch)
        if done % 2000 == 0 or done == len(keys):
            print(f'  ... {done}/{len(keys)} processed '
                  f'({time.time() - started:.0f}s)', flush=True)

    reclaimed = total['before'] - total['after']
    print(f'\n  changed={total["changed"]} noop={total["noop"]} '
          f'missing={total["missing"]} cas_lost={total["cas_lost"]} '
          f'errors={total["errors"]}')
    print(f'  logical bytes: {total["before"] / 1048576:.2f} MiB -> '
          f'{total["after"] / 1048576:.2f} MiB '
          f'(reclaimed {reclaimed / 1048576:.2f} MiB)\n')
    return 1 if total['errors'] else 0


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--batch-size', type=int, default=200)
    parser.add_argument('--sleep-ms', type=int, default=20)
    args = parser.parse_args()
    return run(apply=args.apply, limit=args.limit, batch_size=args.batch_size,
               sleep_ms=args.sleep_ms)


if __name__ == '__main__':
    sys.exit(main())
