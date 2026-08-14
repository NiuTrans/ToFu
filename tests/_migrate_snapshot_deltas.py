#!/usr/bin/env python3
"""One-shot migration: compress legacy full ``messages_snapshot`` rows to deltas.

Design: ``docs/DEBUG_PANEL_REDESIGN.md`` §11 (owner-ratified 2026-07-25).

Why
===
Every round used to persist the WHOLE ``messages`` array plus a byte-identical
``tools`` array. Measured on live data: snapshots were 92.4% of ``task_events``
bytes; one 167-round task alone was 123.2 MB. New writes are already projected
to delta form (lib/tasks_pkg/snapshot_delta.py); this script converts the
BACKLOG.

Safety contract (§11) — the reason this is not a one-liner UPDATE
=================================================================
Per task, and inside ONE transaction:

  1. read every snapshot row (ordered by event_id),
  2. project them to delta form in memory,
  3. **rebuild from the projection and compare to the ORIGINAL byte-for-byte**
     (canonical JSON of messages + tools),
  4. only if EVERY round matches, write the delta rows,
  5. any mismatch / exception → ROLLBACK that task, leave its rows untouched,
     log an error, and continue with the next task.

So a task is either fully migrated or fully untouched — never half-written,
and old rows are never deleted on unverified data.

Idempotent: rows already in delta form (carrying ``prefixLen``) are skipped, so
the script can be re-run and can be interrupted safely.

Usage
=====
    python3 tests/_migrate_snapshot_deltas.py --dry-run      # report only
    python3 tests/_migrate_snapshot_deltas.py                # migrate all
    python3 tests/_migrate_snapshot_deltas.py --limit 20     # first 20 tasks
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import (  # noqa: E402
    DOMAIN_CHAT, _BACKEND, get_thread_db, json_dumps_pg,
)
from lib.log import get_logger  # noqa: E402
from lib.tasks_pkg.snapshot_delta import (  # noqa: E402
    DELTA_MARKER,
    SnapshotProjector,
    rebuild_snapshots,
)

# Keep reconstruction of the EXISTING authoritative chain separate from the
# verification seam below.  The corruption-control test deliberately neuters
# ``rebuild_snapshots`` to prove verify-before-write is load-bearing; letting
# that neuter fabricate the input baseline too would make the projector see
# non-snapshot objects and invalidate the control rather than the verifier.
_rebuild_existing_snapshots = rebuild_snapshots

logger = get_logger(__name__)

SNAPSHOT = 'messages_snapshot'


def _canon(o) -> str:
    return json.dumps(o, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _payload(raw):
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or '{}')
    except (TypeError, ValueError):
        return {}


def _snapshot_bytes(db) -> int:
    """Total stored bytes of snapshot payloads (the owner's acceptance metric)."""
    try:
        row = db.execute(
            "SELECT SUM(pg_column_size(payload)) FROM task_events "
            "WHERE type='messages_snapshot'").fetchone()
        return int(row[0] or 0)
    except Exception:
        # SQLite has no pg_column_size — fall back to the JSON length.
        row = db.execute(
            "SELECT SUM(LENGTH(payload)) FROM task_events "
            "WHERE type='messages_snapshot'").fetchone()
        return int(row[0] or 0)


def _tasks_with_full_rows(db, limit=0, *, comprehensive=False) -> list:
    """Task ids that still have at least one FULL snapshot row.

    The original probe inspected only the first snapshot.  A mixed deployment
    can write delta rows first and legacy full rows later (measured: 23 tasks /
    797 full rows hidden this way), so that probe falsely declared the fleet
    complete.  The maintenance daemon uses cheap first+latest probes; mode
    transitions are chronological and this catches both old->new and new->old
    deployments without detoasting 100k payloads every 15 minutes.  The
    one-shot migration requests ``comprehensive=True`` and scans the marker
    explicitly, so even a delta->full->delta sandwich cannot be missed.
    """
    if comprehensive:
        if _BACKEND == 'pg':
            marker_missing = "NOT jsonb_exists(payload, 'prefixLen')"
        else:
            marker_missing = "json_extract(payload, '$.prefixLen') IS NULL"
        sql = ("SELECT DISTINCT task_id FROM task_events "
               "WHERE type='messages_snapshot' AND " + marker_missing +
               " ORDER BY task_id")
        params = ()
        if limit:
            sql += ' LIMIT ?'
            params = (int(limit),)
        return [r[0] for r in db.execute(sql, params).fetchall()]

    rows = db.execute(
        "SELECT DISTINCT task_id FROM task_events WHERE type='messages_snapshot'"
    ).fetchall()
    out = []
    for r in rows:
        tid = r[0]
        probes = db.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND type='messages_snapshot' "
            "ORDER BY event_id ASC LIMIT 1", (tid,)).fetchall()
        probes += db.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND type='messages_snapshot' "
            "ORDER BY event_id DESC LIMIT 1", (tid,)).fetchall()
        if any(DELTA_MARKER not in _payload(probe[0]) for probe in probes):
            out.append(tid)
        if limit and len(out) >= limit:
            break
    return out


def migrate_task(db, task_id: str, *, dry_run=False) -> dict:
    """Migrate ONE task. Returns a per-task report dict.

    Verified-then-written, all inside one transaction (see module docstring).
    """
    rows = db.execute(
        'SELECT event_id, payload FROM task_events '
        'WHERE task_id=? AND type=? ORDER BY event_id ASC',
        (task_id, SNAPSHOT)).fetchall()
    if not rows:
        return {'task': task_id, 'status': 'empty', 'rounds': 0}

    stored = [(int(r[0]), _payload(r[1])) for r in rows]
    if all(DELTA_MARKER in p for _, p in stored):
        return {'task': task_id, 'status': 'already-delta', 'rounds': len(stored)}

    # Mixed tables are legal and the read path already knows how to rebuild
    # them.  Establish one FULL authoritative baseline for every row before
    # re-projecting; otherwise an already-delta prefix is compared as though it
    # were a full payload (``messages`` absent) and a healthy mixed task is
    # falsely refused.  Refuse honestly if the existing chain itself is
    # degraded — never manufacture a new baseline from incomplete data.
    originals = _rebuild_existing_snapshots(
        [{'type': SNAPSHOT, 'payload': p} for _, p in stored])
    if len(originals) != len(stored):
        return {'task': task_id, 'status': 'FAILED',
                'reason': ('existing mixed chain rebuilt %d of %d rounds'
                           % (len(originals), len(stored))),
                'rounds': len(stored)}
    for (eid, _), full in zip(stored, originals):
        if full.get('degraded'):
            return {'task': task_id, 'status': 'FAILED',
                    'reason': ('existing event %d is degraded: %s'
                               % (eid, full.get('degradedReason'))),
                    'rounds': len(stored)}

    before = sum(len(_canon(p)) for _, p in stored)

    # ── project ──
    proj = SnapshotProjector()
    projected = []
    for (eid, _stored_payload), p in zip(stored, originals):
        projected.append((eid, proj.project(task_id, p)))
    after = sum(len(_canon(p)) for _, p in projected)

    # ── VERIFY: rebuild and compare byte-for-byte before writing anything ──
    rebuilt = rebuild_snapshots(
        [{'type': SNAPSHOT, 'payload': p} for _, p in projected])
    if len(rebuilt) != len(originals):
        return {'task': task_id, 'status': 'FAILED',
                'reason': f'rebuild produced {len(rebuilt)} of {len(originals)} rounds',
                'rounds': len(originals)}
    for (eid, _), orig, got in zip(stored, originals, rebuilt):
        if got.get('degraded'):
            return {'task': task_id, 'status': 'FAILED', 'rounds': len(originals),
                    'reason': f'event {eid} rebuilt degraded: {got.get("degradedReason")}'}
        if _canon(got.get('messages')) != _canon(orig.get('messages') or []):
            return {'task': task_id, 'status': 'FAILED', 'rounds': len(originals),
                    'reason': f'event {eid} messages diverged'}
        if _canon(got.get('tools') or []) != _canon(orig.get('tools') or []):
            return {'task': task_id, 'status': 'FAILED', 'rounds': len(originals),
                    'reason': f'event {eid} tools diverged'}

    report = {'task': task_id, 'status': 'ok', 'rounds': len(originals),
              'before': before, 'after': after,
              'ratio': (before / after) if after else 0}
    if dry_run:
        report['status'] = 'dry-run'
        return report

    # ── write (single transaction; any failure rolls the whole task back) ──
    try:
        for eid, p in projected:
            db.execute(
                'UPDATE task_events SET payload=? WHERE task_id=? AND event_id=?',
                (json_dumps_pg(p), task_id, eid))
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception as re:
            logger.debug('[SnapshotMigrate] rollback failed: %s', re)
        logger.error('[SnapshotMigrate] task=%s write failed, rolled back: %s',
                     task_id, e, exc_info=True)
        return {'task': task_id, 'status': 'FAILED', 'rounds': len(originals),
                'reason': f'write failed (rolled back): {e}'}
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='verify + report, write nothing')
    ap.add_argument('--limit', type=int, default=0,
                    help='migrate at most N tasks (0 = all)')
    args = ap.parse_args()

    db = get_thread_db(DOMAIN_CHAT)
    total_before_bytes = _snapshot_bytes(db)
    tasks = _tasks_with_full_rows(
        db, limit=args.limit, comprehensive=True)
    print(f'snapshot bytes BEFORE : {total_before_bytes/1e6:.1f} MB')
    print(f'tasks with full rows  : {len(tasks)}')
    if not tasks:
        print('nothing to migrate.')
        return 0

    t0 = time.time()
    ok = failed = skipped = 0
    sum_before = sum_after = 0
    for i, tid in enumerate(tasks, 1):
        rep = migrate_task(db, tid, dry_run=args.dry_run)
        st = rep['status']
        if st in ('ok', 'dry-run'):
            ok += 1
            sum_before += rep.get('before', 0)
            sum_after += rep.get('after', 0)
            print(f'  [{i}/{len(tasks)}] {str(tid)[:12]} rounds={rep["rounds"]:3d} '
                  f'{rep["before"]/1e6:6.1f}MB → {rep["after"]/1e6:5.2f}MB '
                  f'({rep["ratio"]:.1f}x)')
        elif st == 'FAILED':
            failed += 1
            print(f'  [{i}/{len(tasks)}] {str(tid)[:12]} FAILED — {rep.get("reason")}')
        else:
            skipped += 1

    elapsed = time.time() - t0
    print()
    print(f'ok={ok} failed={failed} skipped={skipped} in {elapsed:.1f}s')
    if sum_after:
        print(f'verified payload: {sum_before/1e6:.1f} MB → {sum_after/1e6:.1f} MB '
              f'({sum_before/sum_after:.1f}x)')
    if not args.dry_run:
        print(f'snapshot bytes AFTER  : {_snapshot_bytes(db)/1e6:.1f} MB')
        print('NOTE: PG does not return dead-tuple space until VACUUM runs; the '
              'byte figure above may lag until autovacuum passes over the table.')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
