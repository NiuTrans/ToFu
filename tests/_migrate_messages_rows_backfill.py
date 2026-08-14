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
import ctypes
import gc
import sys
import time

sys.path.insert(0, __file__.rsplit('/tests/', 1)[0])

from lib.database import DOMAIN_CHAT, _BACKEND, get_thread_db  # noqa: E402
from lib.database.messages_rows import (  # noqa: E402
    backfill_activity_projection, backfill_conv, backfill_light_projection,
    mark_conv_mirror_current, verify_conv_parity,
)
from lib.log import get_logger  # noqa: E402

logger = get_logger(__name__)


def _candidates(db, only_id, min_mb, limit, count_mismatch_only=False,
                include_empty=False):
    # Candidate discovery must not detoast/render the entire fleet.  On the live
    # 4.6 GB table, octet_length(messages::text)+ORDER BY took 87 seconds before
    # processing row one.  pg_column_size reads the stored datum size instead;
    # exact parity still compares the complete value one candidate at a time.
    size_expr = ('pg_column_size(c.messages)' if _BACKEND == 'pg'
                 else 'length(CAST(messages AS TEXT))')
    if only_id:
        rows = db.execute(
            f"SELECT c.id, {size_expr} AS n, c.msg_count "
            "FROM conversations c WHERE c.id=?", (only_id,)).fetchall()
    else:
        join = ''
        predicates = []
        if not include_empty:
            predicates.append('c.msg_count > 0')
        if count_mismatch_only:
            join = (' LEFT JOIN (SELECT conv_id, COUNT(*) AS row_count '
                    'FROM conversation_messages GROUP BY conv_id) cm '
                    'ON cm.conv_id=c.id ')
            predicates.append('c.msg_count<>COALESCE(cm.row_count,0)')
        predicates.append(f'{size_expr} >= ?')
        sql = (f"SELECT c.id, {size_expr} AS n, c.msg_count "
               "FROM conversations c " + join +
               "WHERE " + ' AND '.join(predicates) + ' ' +
               "ORDER BY n DESC")
        params = [int(min_mb * 1048576)]
        if limit:
            sql += ' LIMIT ?'
            params.append(int(limit))
        rows = db.execute(sql, tuple(params)).fetchall()
    out = [(r['id'], int(r['n'] or 0), int(r['msg_count'] or 0)) for r in rows]
    return out


def _trim_heap():
    gc.collect()
    try:
        ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass


def _light_candidates(db, only_id, limit):
    """Conversations missing any online read/report projection."""
    size_expr = ('pg_column_size(c.messages)' if _BACKEND == 'pg'
                 else 'length(CAST(c.messages AS TEXT))')
    where = ('WHERE cm.meta_light IS NULL OR cm.message_ts IS NULL '
             'OR cm.billing_meta IS NULL')
    params = []
    if only_id:
        where += ' AND c.id=?'
        params.append(only_id)
    sql = (
        f'SELECT c.id, MAX({size_expr}) AS n, '
        'SUM(CASE WHEN cm.meta_light IS NULL THEN 1 ELSE 0 END) AS missing_light, '
        'SUM(CASE WHEN cm.message_ts IS NULL THEN 1 ELSE 0 END) AS missing_activity, '
        'SUM(CASE WHEN cm.billing_meta IS NULL THEN 1 ELSE 0 END) AS missing_billing, '
        'SUM(CASE WHEN cm.message_ts IS NULL OR cm.billing_meta IS NULL '
        'THEN 1 ELSE 0 END) AS missing_projection '
        'FROM conversation_messages cm JOIN conversations c ON c.id=cm.conv_id '
        f'{where} GROUP BY c.id ORDER BY n DESC'
    )
    if limit:
        sql += ' LIMIT ?'
        params.append(int(limit))
    return [(r['id'], int(r['n'] or 0), int(r['missing_light'] or 0),
             int(r['missing_activity'] or 0), int(r['missing_billing'] or 0),
             int(r['missing_projection'] or 0))
            for r in db.execute(sql, tuple(params)).fetchall()]


def run_light(apply, only_id, limit, sleep_ms):
    """Online, resumable light/activity backfill (never rewrites authority)."""
    db = get_thread_db(DOMAIN_CHAT)
    candidates = _light_candidates(db, only_id, limit)
    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'\n  ═══ messages-light backfill [{mode}] — '
          f'{len(candidates)} conversation(s) ═══\n')
    light_filled = activity_filled = errored = 0
    t0 = time.time()
    for k, (cid, _nbytes, missing_light, _missing_activity,
            _missing_billing, missing_projection) in enumerate(candidates, 1):
        try:
            if apply:
                n = backfill_light_projection(db, cid, commit=False)
                m = backfill_activity_projection(db, cid, commit=False)
                db.commit()
                light_filled += n
                activity_filled += m
                # One activity pass fills both scalar timestamp and billing
                # metadata. Its rowcount is the UNION, not the sum, of NULLs.
                if n != missing_light or m != missing_projection:
                    logger.warning('[rows-light] conv=%s expected %d NULL rows, filled %d',
                                   cid, missing_light + missing_projection, n + m)
            else:
                db.rollback()
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            errored += 1
            logger.error('[rows-light] conv=%s failed: %s', cid, e, exc_info=True)
        finally:
            _trim_heap()
            if sleep_ms:
                time.sleep(sleep_ms / 1000.0)
            if k % 100 == 0:
                print(f'  … {k}/{len(candidates)} light={light_filled} '
                      f'activity={activity_filled} '
                      f'err={errored} ({time.time() - t0:.0f}s)', flush=True)
    print(f'\n  {mode}: candidates={len(candidates)} light_rows={light_filled} '
          f'activity_rows={activity_filled} '
          f'errors={errored} elapsed={time.time() - t0:.0f}s\n')
    return 1 if errored else 0


def _rebuild_and_verify(db, conv_id):
    """Lock one authority row, rebuild its mirror, verify, then commit."""
    lock_suffix = ' FOR UPDATE' if _BACKEND == 'pg' else ''
    try:
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=?' + lock_suffix,
            (conv_id,)).fetchone()
        if not row:
            db.rollback()
            return 0, {'ok': False, 'reason': 'missing'}
        n = backfill_conv(db, conv_id, row['messages'], commit=False)
        verdict = verify_conv_parity(db, conv_id)
        if not verdict['ok']:
            db.rollback()
            return n, verdict
        db.commit()
        return n, verdict
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise


def _mark_and_verify(db, conv_id):
    """Lock authority, re-verify exact content, then CAS-mark that revision."""
    lock_suffix = ' FOR UPDATE' if _BACKEND == 'pg' else ''
    try:
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=?' + lock_suffix,
            (conv_id,)).fetchone()
        if not row:
            db.rollback()
            return {'ok': False, 'reason': 'missing'}
        verdict = verify_conv_parity(db, conv_id)
        if not verdict['ok']:
            db.rollback()
            return verdict
        mark_conv_mirror_current(db, conv_id, row['messages'])
        db.commit()
        verdict['mirror_current'] = True
        return verdict
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise


def run(apply, only_id, min_mb, limit, sleep_ms,
        count_mismatch_only=False, mark_current=False):
    db = get_thread_db(DOMAIN_CHAT)
    candidates = _candidates(db, only_id, min_mb, limit,
                             count_mismatch_only=count_mismatch_only,
                             include_empty=mark_current)
    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'\n  ═══ messages-rows fleet backfill [{mode}] — '
          f'{len(candidates)} candidate conv(s), largest first ═══\n')
    if not candidates:
        print('  (no rows match)\n')
        return 0

    fresh = rebuilt = rebuilt_ok = mismatch = errored = 0
    marked = needs_mark = 0
    mismatches: list = []
    t0 = time.time()
    for k, (cid, nbytes, msg_count) in enumerate(candidates, 1):
        try:
            verdict = verify_conv_parity(db, cid)
            if verdict['ok']:
                if mark_current and not verdict.get('mirror_current'):
                    needs_mark += 1
                    if apply:
                        verdict = _mark_and_verify(db, cid)
                        if not verdict['ok']:
                            mismatch += 1
                            mismatches.append((cid, nbytes, verdict))
                            continue
                        marked += 1
                    else:
                        db.rollback()
                else:
                    # Release the read transaction before throttle.
                    db.rollback()
                fresh += 1
                continue
            if not apply:
                db.rollback()
                mismatch += 1
                mismatches.append((cid, nbytes, verdict))
                continue
            n, verdict2 = _rebuild_and_verify(db, cid)
            rebuilt += 1
            if verdict2['ok']:
                rebuilt_ok += 1
            else:
                mismatch += 1
                mismatches.append((cid, nbytes, verdict2))
                logger.warning('[rows-backfill] conv=%s parity STILL failing after '
                               'rebuild: %s', cid, verdict2)
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            errored += 1
            logger.error('[rows-backfill] conv=%s failed (%s): %s — skipped',
                         cid, type(e).__name__, e, exc_info=True)
        finally:
            _trim_heap()
            # Keep throttle/progress outside status-specific branches: the
            # former ``continue`` paths skipped both, turning a nominally
            # throttled all-fresh fleet verification into an unbounded scan.
            if sleep_ms:
                time.sleep(sleep_ms / 1000.0)
            if k % 25 == 0:
                print(f'  … {k}/{len(candidates)}  fresh={fresh} rebuilt={rebuilt} '
                      f'mismatch={mismatch} err={errored}  '
                      f'({time.time() - t0:.0f}s)', flush=True)

    print(f'\n  ─── {mode} summary ({time.time() - t0:.0f}s) ───')
    print(f'    already fresh (skip)      : {fresh}')
    if mark_current:
        print(f'    marker needed             : {needs_mark}')
        if apply:
            print(f'    marker committed          : {marked}')
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
    p.add_argument('--count-mismatch-only', action='store_true',
                   help='only scan conversations whose mirror row count differs')
    p.add_argument('--mark-current', action='store_true',
                   help='include empty convs and CAS-mark exact mirrors current; '
                        'writes only together with --apply')
    p.add_argument('--light-only', action='store_true',
                   help='materialize missing meta_light/message_ts/billing_meta projections; '
                        'online and resumable')
    args = p.parse_args()
    if args.light_only:
        sys.exit(run_light(args.apply, args.id or '', args.limit, args.sleep_ms))
    sys.exit(run(args.apply, args.id or '', args.min_mb, args.limit,
                 args.sleep_ms, args.count_mismatch_only, args.mark_current))


if __name__ == '__main__':
    main()
