#!/usr/bin/env python3
"""One-shot backfill: trim the transient/diagnostic bloat out of ALREADY-STORED
conversation rows, so conversations that are fat in the DB right now become
openable without exhausting the browser tab.

WHY
---
The write-path fix (lib/tasks_pkg/manager.py _sanitize_* + the frontend PUT /
IndexedDB strips) only trims on the NEXT persist. A finished or interrupted
conversation may never be persisted again, so a row that is 114 MB today
(mr80gsd8rywph9) stays 114 MB and still OOMs the tab on click. This migration
rewrites those rows in place through the EXACT SAME server sanitizers, once.

Three fields are trimmed (see the manager.py helpers for the full rationale):
  • ``usage._wire_fp`` / ``_wire_static`` — backend-only SSE cache-miss
    diagnostics (~226 KB/round), read by no render path. Stripped from the
    top-level ``usage``, every ``apiRounds[].usage``, and the frontend-only
    ``_liveLastRoundUsage.usage``.
  • ``toolRounds[]._partialOutput`` on a ``done`` round — the transient
    run_command terminal buffer (authoritative output is in toolContent).
  • Inline base64 ``imageDataUris[].uri`` is NOT touched here: it is the
    render source and stays server-side; the browser IndexedDB cache strip
    (static/js/idb-cache.js) handles it locally. So base64-heavy rows only
    partially shrink here — expected.

SINGLE SOURCE OF TRUTH
----------------------
This script imports and reuses the manager.py helpers verbatim
(``_sanitize_usage_for_persist`` / ``_sanitize_api_rounds_for_persist`` /
``_trim_round_for_persist``). It does NOT re-implement the trim logic — a
divergent copy would drift from the write path.

SAFETY
------
  • Idempotent: a row is UPDATEd only when trimming actually shrinks it. Running
    twice is a no-op on already-trimmed rows (they no longer carry the fields).
  • Dry-run by default: prints per-conv before/after bytes + total reclaimed,
    writes nothing. Pass ``--apply`` to write.
  • Per-row isolation: each row is wrapped so one bad row logs + is skipped
    without aborting the batch.
  • Single UPDATE per row (messages + recomputed msg_count), via the project's
    dual-backend wrapper (``async_execute``; ``?`` → ``%s`` + JSONB coercion is
    handled by the wrapper, same path the conversation upserts use).

Usage:
    python tests/_migrate_trim_oversized_conversations.py                 # dry-run, all rows
    python tests/_migrate_trim_oversized_conversations.py --min-mb 5      # dry-run rows >= 5 MB
    python tests/_migrate_trim_oversized_conversations.py --id mr80gsd8rywph9   # one row
    python tests/_migrate_trim_oversized_conversations.py --apply         # WRITE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import async_execute, async_fetchall, async_fetchone  # noqa: E402
from lib.database._wrappers import json_dumps_pg  # noqa: E402
from lib.log import audit_log, get_logger  # noqa: E402
# Reuse the EXACT write-path sanitizers — single source of truth.
from lib.tasks_pkg.manager import (  # noqa: E402
    _sanitize_api_rounds_for_persist,
    _sanitize_usage_for_persist,
    _trim_round_for_persist,
)

logger = get_logger(__name__)


def trim_messages(messages):
    """Apply the write-path sanitizers to a full messages list.

    Returns a NEW list (never mutates the input). Mirrors exactly what the
    server persist path does: build_result_meta sanitizes the final ``usage``
    + ``apiRounds``, and _merge_tool_rounds runs _trim_round_for_persist on
    each round. We additionally cover the frontend-only ``_liveLastRoundUsage``
    (which the browser writes and which carries the same raw usage dict), so a
    backfilled row matches what a fresh client PUT would now store.
    """
    if not isinstance(messages, list):
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        m2 = dict(m)
        if isinstance(m2.get('usage'), dict):
            m2['usage'] = _sanitize_usage_for_persist(m2['usage'])
        if isinstance(m2.get('apiRounds'), list):
            m2['apiRounds'] = _sanitize_api_rounds_for_persist(m2['apiRounds'])
        live = m2.get('_liveLastRoundUsage')
        if isinstance(live, dict) and isinstance(live.get('usage'), dict):
            m2['_liveLastRoundUsage'] = {**live, 'usage': _sanitize_usage_for_persist(live['usage'])}
        if isinstance(m2.get('toolRounds'), list):
            m2['toolRounds'] = [
                _trim_round_for_persist(r) if isinstance(r, dict) else r
                for r in m2['toolRounds']
            ]
        out.append(m2)
    return out


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


async def _candidate_ids(min_bytes, only_id):
    """Return (id, byte_len) for rows to consider, largest first.

    ``length(messages::text)`` casts the JSONB column to text (the same probe
    used to find the fat convs). SQLite stores messages as TEXT so ``::text``
    is a harmless no-op there via the dialect bridge.
    """
    if only_id:
        row = await async_fetchone(
            'SELECT id, length(messages::text) AS n FROM conversations WHERE id=?',
            (only_id,))
        return [(row['id'], row['n']) for row in ([row] if row else [])]
    rows = await async_fetchall(
        'SELECT id, length(messages::text) AS n FROM conversations '
        'WHERE length(messages::text) >= ? ORDER BY n DESC',
        (min_bytes,))
    return [(r['id'], r['n']) for r in rows]


async def run(apply, min_mb, only_id, limit):
    min_bytes = int(min_mb * 1024 * 1024)
    candidates = await _candidate_ids(min_bytes, only_id)
    if limit:
        candidates = candidates[:limit]

    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'\n  ═══ trim-oversized-conversations [{mode}] — '
          f'{len(candidates)} candidate row(s) >= {min_mb} MB ═══\n')
    if not candidates:
        print('  (no rows match)\n')
        return

    total_before = 0
    total_after = 0
    shrunk = 0
    skipped_noop = 0
    errored = 0

    for cid, raw_len in candidates:
        try:
            row = await async_fetchone(
                'SELECT messages, msg_count FROM conversations WHERE id=?', (cid,))
            if not row:
                continue
            messages = _as_list(row['messages'])
            if messages is None:
                print(f'  {cid:18s}  SKIP (unparseable messages)')
                errored += 1
                continue

            before = len(json_dumps_pg(messages))
            trimmed = trim_messages(messages)
            after_text = json_dumps_pg(trimmed)
            after = len(after_text)

            total_before += before
            if after >= before:
                # Idempotent: nothing shrank → already trimmed (or nothing to
                # trim). Do NOT write.
                total_after += before
                skipped_noop += 1
                continue

            total_after += after
            shrunk += 1
            reclaimed = before - after
            new_count = len(trimmed)
            old_count = row['msg_count']
            count_note = '' if old_count in (None, new_count) else f'  msg_count {old_count}→{new_count}'
            print(f'  {cid:18s}  {before/1048576:8.2f} MB → {after/1048576:7.2f} MB  '
                  f'(reclaim {reclaimed/1048576:7.2f} MB, {100*reclaimed//before:3d}%){count_note}')

            if apply:
                await async_execute(
                    'UPDATE conversations SET messages=?, msg_count=? WHERE id=?',
                    (after_text, new_count, cid))
                logger.info('[trim-migration] conv=%s trimmed %d→%d bytes (reclaimed %d)',
                            cid, before, after, reclaimed)
                try:
                    audit_log('conversation_trim_backfill', conv_id=cid,
                              before_bytes=before, after_bytes=after, reclaimed=reclaimed)
                except Exception as e:
                    logger.debug('[trim-migration] audit_log failed (non-fatal): %s', e)
        except Exception as e:
            errored += 1
            logger.error('[trim-migration] row %s failed (%s): %s — skipped',
                         cid, type(e).__name__, e, exc_info=True)
            print(f'  {cid:18s}  ERROR ({type(e).__name__}: {e}) — skipped')

    print(f'\n  ─── {mode} summary ───')
    print(f'    rows that shrink : {shrunk}')
    print(f'    rows no-op (idempotent skip) : {skipped_noop}')
    print(f'    rows errored (skipped) : {errored}')
    print(f'    total before : {total_before/1048576:.2f} MB')
    print(f'    total after  : {total_after/1048576:.2f} MB')
    print(f'    reclaimed    : {(total_before-total_after)/1048576:.2f} MB')
    if not apply and shrunk:
        print('\n  (dry-run — no rows written. Re-run with --apply to write.)')
    print()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--apply', action='store_true', help='write changes (default: dry-run)')
    p.add_argument('--min-mb', type=float, default=1.0,
                   help='only consider rows whose messages JSON >= this many MB (default 1)')
    p.add_argument('--id', default='', help='restrict to a single conversation id')
    p.add_argument('--limit', type=int, default=0, help='cap number of rows processed (0 = all)')
    args = p.parse_args()
    asyncio.run(run(args.apply, args.min_mb, args.id or '', args.limit))


if __name__ == '__main__':
    main()
