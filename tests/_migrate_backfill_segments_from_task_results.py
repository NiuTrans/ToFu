#!/usr/bin/env python3
"""One-shot backfill: PERSIST the segment-timeline into ALREADY-STORED
conversation rows whose assistant messages lost their ``segments``.

WHY
---
Segment-timeline delivery (epic pt_cb8f98b0cb9b47fb) has two write paths:

  • The save_conv preserve-merge keeps ``segments`` in the ``messages`` column
    for turns that re-sync AFTER the fix (durable, forward-looking).
  • The GET-path backstop rehydrates ``segments`` from ``task_results`` for
    turns persisted BEFORE the fix — but DISPLAY-ONLY, never written back.

The backstop's recovery is therefore only as durable as the ``task_results``
row it reads from, and those rows are subject to cleanup/TTL. Once a pre-fix
conversation's task rows are reaped it has NEITHER column-segments NOR a
``task_results`` source → it renders the three grouped blocks forever, and no
client PUT can fix it (the client strips ``segments`` on the wire). This is the
"a write-path fix is incomplete without a backfill" lesson from the conv-OOM
work: this migration walks existing conversations ONCE and splices the
authoritative ``task_results.segments`` into the ``messages`` column, making
the recovery permanent and independent of ``task_results`` retention.

SINGLE SOURCE OF TRUTH
----------------------
Reuses the EXACT fill core the GET-path backstop uses
(``lib.conversations.segments_backfill`` — ``collect_taskids_needing_segments``
/ ``fill_messages_with_segments``). It does NOT re-implement "which message
needs filling" or the segment-shape guard — a divergent copy would drift from
the route.

REV-CAS NEUTRALITY (the important difference from the save_conv merge)
----------------------------------------------------------------------
The save_conv preserve-merge re-materializes bytes the row ALREADY held, so the
``conversations_rev_bump_trg`` (fires on a genuine ``messages`` change) does not
fire. This backfill is DIFFERENT: it genuinely ADDS a ``segments`` field to a
column that lacked it, so ``messages`` really changes and the trigger WOULD bump
``rev``. A gratuitous rev bump would make every open client's compare-and-swap
PUT eat a spurious 409 rebase. So the write is done in a transaction that
(1) UPDATEs ``messages`` guarded on the rev we read (skip on a concurrent
writer), then (2) resets ``rev`` back to that value — the rev-only reset does
NOT re-fire the ``OF messages``-scoped trigger. Net effect: segments land in the
column with ``rev`` unchanged (and ``updated_at`` untouched → no sidebar
re-sort). Idempotent: a row already carrying segments needs no fill → no write.

SAFETY
------
  • Dry-run by default: prints per-conv counts, writes nothing. ``--apply`` writes.
  • Idempotent: a row is UPDATEd only when a message actually gained segments.
  • Per-row isolation: one bad row logs + is skipped without aborting the batch.
  • Enrich-only: never removes a message, never truncates; only adds ``segments``
    to a message that lacked it. msg_count is unchanged (same message list).

Usage:
    python tests/_migrate_backfill_segments_from_task_results.py                 # dry-run, all rows
    python tests/_migrate_backfill_segments_from_task_results.py --id mrakfxr... # one row
    python tests/_migrate_backfill_segments_from_task_results.py --limit 50      # first 50 rows
    python tests/_migrate_backfill_segments_from_task_results.py --apply         # WRITE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.conversations.segments_backfill import (  # noqa: E402
    collect_taskids_needing_segments,
    fill_messages_with_segments,
)
from lib.database import async_fetchall, async_fetchone  # noqa: E402
from lib.database.aio import async_transaction  # noqa: E402
from lib.database._wrappers import json_dumps_pg  # noqa: E402
from lib.log import audit_log, get_logger  # noqa: E402

logger = get_logger(__name__)


def _as_list(raw):
    """Coerce the stored ``messages`` column (jsonb → list, or JSON text) to a list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw or '[]')
        except (json.JSONDecodeError, TypeError):
            return None
    return None


async def _candidate_ids(only_id, limit):
    """Return conversation ids to consider (newest first)."""
    if only_id:
        row = await async_fetchone(
            'SELECT id FROM conversations WHERE id=?', (only_id,))
        return [row['id']] if row else []
    rows = await async_fetchall(
        'SELECT id FROM conversations WHERE msg_count > 0 ORDER BY updated_at DESC')
    ids = [r['id'] for r in rows]
    if limit:
        ids = ids[:limit]
    return ids


async def _fetch_segments_for(taskids):
    """Fetch ``task_id -> segments`` for the given task ids from task_results."""
    if not taskids:
        return {}
    placeholders = ','.join('?' for _ in taskids)
    rows = await async_fetchall(
        'SELECT task_id, segments FROM task_results WHERE task_id IN (%s)'
        % placeholders, tuple(taskids))
    return {r['task_id']: r['segments'] for r in rows}


async def _rev_neutral_write(cid, messages_json, expected_rev):
    """Persist the enriched messages while keeping ``rev`` unchanged.

    The messages UPDATE fires ``conversations_rev_bump_trg`` (rev → rev+1); we
    reset rev to ``expected_rev`` in the SAME transaction. The rev-only reset
    does not touch ``messages`` so it does not re-fire the ``OF messages``-scoped
    trigger. Guarded on ``expected_rev`` so a concurrent writer's change is not
    clobbered (its message UPDATE would have moved rev; ours no-ops).

    Returns True if the row was written, False if a concurrent writer moved rev
    (skip — the next run picks it up).
    """
    async with async_transaction() as conn:
        cur = await conn.execute(
            'UPDATE conversations SET messages=? WHERE id=? AND rev=?',
            (messages_json, cid, expected_rev))
        if getattr(cur, 'rowcount', 0) == 0:
            return False
        # Undo the trigger's rev bump — keep the version stable so open clients
        # do not eat a spurious CAS 409. rev-only UPDATE: no messages change, so
        # the messages-scoped bump trigger does not re-fire.
        await conn.execute(
            'UPDATE conversations SET rev=? WHERE id=?', (expected_rev, cid))
    return True


async def run(apply, only_id, limit):
    candidates = await _candidate_ids(only_id, limit)
    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'\n  ═══ backfill-segments-from-task_results [{mode}] — '
          f'{len(candidates)} candidate conversation(s) ═══\n')
    if not candidates:
        print('  (no rows match)\n')
        return

    convs_touched = 0
    msgs_filled_total = 0
    skipped_concurrent = 0
    errored = 0

    for cid in candidates:
        try:
            row = await async_fetchone(
                'SELECT messages, rev FROM conversations WHERE id=?', (cid,))
            if not row:
                continue
            messages = _as_list(row['messages'])
            if messages is None:
                print(f'  {cid:20s}  SKIP (unparseable messages)')
                errored += 1
                continue

            need = collect_taskids_needing_segments(messages)
            if not need:
                continue  # every assistant msg already has segments (or none apply)

            segs_by_tid = await _fetch_segments_for(list(need.keys()))
            filled = fill_messages_with_segments(need, segs_by_tid)
            if filled == 0:
                continue  # no task_results source for any missing message

            expected_rev = 0
            try:
                expected_rev = int(row['rev'] or 0)
            except (TypeError, ValueError):
                expected_rev = 0

            print(f'  {cid:20s}  +segments on {filled:3d} message(s)  '
                  f'(rev stays {expected_rev})')
            convs_touched += 1
            msgs_filled_total += filled

            if apply:
                messages_json = json_dumps_pg(messages)
                wrote = await _rev_neutral_write(cid, messages_json, expected_rev)
                if not wrote:
                    skipped_concurrent += 1
                    print(f'  {cid:20s}  (skipped — concurrent writer moved rev; '
                          f'next run retries)')
                    convs_touched -= 1
                    msgs_filled_total -= filled
                    continue
                logger.info('[segments-backfill] conv=%s filled %d message(s) '
                            '(rev held at %d)', cid, filled, expected_rev)
                try:
                    audit_log('conversation_segments_backfill', conv_id=cid,
                              messages_filled=filled, rev=expected_rev)
                except Exception as e:
                    logger.debug('[segments-backfill] audit_log failed (non-fatal): %s', e)
        except Exception as e:
            errored += 1
            logger.error('[segments-backfill] row %s failed (%s): %s — skipped',
                         cid, type(e).__name__, e, exc_info=True)
            print(f'  {cid:20s}  ERROR ({type(e).__name__}: {e}) — skipped')

    print(f'\n  ─── {mode} summary ───')
    print(f'    conversations enriched : {convs_touched}')
    print(f'    messages given segments : {msgs_filled_total}')
    print(f'    skipped (concurrent rev move) : {skipped_concurrent}')
    print(f'    rows errored (skipped) : {errored}')
    if not apply and convs_touched:
        print('\n  (dry-run — no rows written. Re-run with --apply to write.)')
    print()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--apply', action='store_true', help='write changes (default: dry-run)')
    p.add_argument('--id', default='', help='restrict to a single conversation id')
    p.add_argument('--limit', type=int, default=0, help='cap number of rows processed (0 = all)')
    args = p.parse_args()
    asyncio.run(run(args.apply, args.id or '', args.limit))


if __name__ == '__main__':
    main()
