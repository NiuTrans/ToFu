#!/usr/bin/env python3
"""One-shot fleet backfill: rebuild the conversation_messages row store.

pt_59140ecd step ③. The row store has been frozen since 2026-07-26 (the
``TOFU_MESSAGES_ROWS`` write flag is OFF, so nothing dual-writes): measured
2026-07-27 — 3,696 convs / 26,950 rows, with 484 PARTIAL convs (0 < rows <
msg_count, the charter "killer shape") and 477 EMPTY convs, and 9 of the 10
largest blobs (the most expensive conversations to rewrite) carrying ZERO
rows. The write-path flip needs a fresh, parity-verified mirror of the whole
fleet BEFORE the owner flips the flag.

WHAT IT DOES (per conversation, largest blob first)
---------------------------------------------------
  1. ``verify_conv_parity`` — already fresh (search_text byte-identical
     between the JSONB blob and the rows)? Skip (idempotent resume).
  2. Otherwise ``backfill_conv`` — DELETE + re-insert every row from the
     authoritative blob (per-conv committed), then re-verify.

SAFETY
------
  • Dry-run by default: prints the full fresh/mismatch inventory, writes
    nothing. Pass ``--apply`` to write.
  • Idempotent: step 1 makes re-runs converge; only stale/missing convs are
    ever rebuilt.
  • Per-conv isolation: one bad conv logs + is skipped, the fleet continues.
  • The rows table is a MIRROR — the authoritative blob is never touched.
  • Throttled (``--sleep-ms``, default 20 ms/conv) so a fleet run on the
    FUSE-backed PG does not starve the live server.

Usage:
    python tests/_migrate_messages_rows_backfill.py                 # dry-run inventory
    python tests/_migrate_messages_rows_backfill.py --limit 10      # dry-run top 10
    python tests/_migrate_messages_rows_backfill.py --apply         # WRITE (fleet)
    python tests/_migrate_messages_rows_backfill.py --apply --id mrxinirv0t6n6v
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, __file__.rsplit('/tests/', 1)[0])

from lib.database import DOMAIN_CHAT, get_thread_db  # noqa: E402
from lib.database.messages_rows import (  # noqa: E402
    backfill_conv, verify_conv_parity,
)
from lib.log import get_logger  # noqa: E402

logger = get_logger(__name__)


def _candidates(db, only_id, min_mb, limit):
    if only_id:
        rows = db.execute(
            "SELECT id, octet_length(messages::text) AS n, msg_count "
            "FROM conversations WHERE id=?", (only_id,)).fetchall()
    else:
        rows = db.execute(
            "SELECT id, octet_length(messages::text) AS n, msg_count "
            "FROM conversations WHERE msg_count > 0 "
            "AND octet_length(messages::text) >= ? "
            "ORDER BY n DESC", (int(min_mb * 1048576),)).fetchall()
    out = [(r['id'], int(r['n'] or 0), int(r['msg_count'] or 0)) for r in rows]
    return out[:limit] if limit else out


def run(apply, only_id, min_mb, limit, sleep_ms):
    db = get_thread_db(DOMAIN_CHAT)
    candidates = _candidates(db, only_id, min_mb, limit)
    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'\n  ═══ messages-rows fleet backfill [{mode}] — '
          f'{len(candidates)} candidate conv(s), largest first ═══\n')
    if not candidates:
        print('  (no rows match)\n')
        return 0

    fresh = rebuilt = rebuilt_ok = mismatch = errored = 0
    mismatches: list = []
    t0 = time.time()
    for k, (cid, nbytes, msg_count) in enumerate(candidates, 1):
        try:
            verdict = verify_conv_parity(db, cid)
            if verdict['ok']:
                fresh += 1
                continue
            if not apply:
                mismatch += 1
                mismatches.append((cid, nbytes, verdict))
                continue
            row = db.execute('SELECT messages FROM conversations WHERE id=?',
                             (cid,)).fetchone()
            if not row:
                errored += 1
                continue
            n = backfill_conv(db, cid, row['messages'])
            verdict2 = verify_conv_parity(db, cid)
            rebuilt += 1
            if verdict2['ok']:
                rebuilt_ok += 1
            else:
                mismatch += 1
                mismatches.append((cid, nbytes, verdict2))
                logger.warning('[rows-backfill] conv=%s parity STILL failing after '
                               'rebuild: %s', cid, verdict2)
            if k % 25 == 0:
                print(f'  … {k}/{len(candidates)}  fresh={fresh} rebuilt={rebuilt} '
                      f'mismatch={mismatch} err={errored}  '
                      f'({time.time() - t0:.0f}s)')
        except Exception as e:
            errored += 1
            logger.error('[rows-backfill] conv=%s failed (%s): %s — skipped',
                         cid, type(e).__name__, e, exc_info=True)
        if sleep_ms:
            time.sleep(sleep_ms / 1000.0)

    print(f'\n  ─── {mode} summary ({time.time() - t0:.0f}s) ───')
    print(f'    already fresh (skip)      : {fresh}')
    if apply:
        print(f'    rebuilt                   : {rebuilt} (parity OK: {rebuilt_ok})')
    print(f'    mismatch remaining        : {mismatch}')
    print(f'    errored (skipped)         : {errored}')
    if mismatches and not apply:
        print('\n  worst mismatches (largest first):')
        for cid, nbytes, v in mismatches[:10]:
            print(f'    {cid:20s} {nbytes / 1048576:7.1f} MB  '
                  f'jsonb_msgs={v["jsonb_msgs"]} rows_msgs={v["rows_msgs"]}')
    if not apply and mismatch:
        print('\n  (dry-run — nothing written. Re-run with --apply to rebuild.)')
    print()
    return 1 if (mismatch and apply) else 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--apply', action='store_true',
                   help='write rebuilds (default: dry-run inventory)')
    p.add_argument('--id', default='', help='restrict to one conversation id')
    p.add_argument('--min-mb', type=float, default=0.0,
                   help='only convs whose messages JSON >= this many MB (default 0)')
    p.add_argument('--limit', type=int, default=0, help='cap convs processed (0=all)')
    p.add_argument('--sleep-ms', type=int, default=20,
                   help='throttle between convs in ms (default 20; 0=none)')
    args = p.parse_args()
    sys.exit(run(args.apply, args.id or '', args.min_mb, args.limit, args.sleep_ms))


if __name__ == '__main__':
    main()
