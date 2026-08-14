"""One-time heal: tombstone the persisted unanswered-engine-tail adjacencies.

Why this exists (owner log-audit, 2026-08-05): the llm_sanitize same-role
merge WARNING storm (1,203 hits in ~2.5 days) was root-fixed for NEW rows
(settle_unanswered_engine_tail + the double-dispatch guard, commit
56406f93) — but the EXISTING adjacencies stayed in the DB: measured 20 of
199 last-7-day conversations still carry a persisted [engine-user, user]
pair, and every request on those conversations rebuilds the wire from the
store and warns again. This module backfills the same
``build_engine_no_reply_tombstone`` row between each pair — once,
idempotently, through the normal rev/notify channel.

Discipline (owner-mandated):
  * Idempotent — a healed pair is [user, assistant-tombstone, user], which
    is no longer an adjacency, so a rerun finds nothing; per-row failures
    skip without affecting other rows.
  * Rev/notify — writes CAS on ``rev`` (the trigger bumps it), read the new
    rev back, and ``notify_conv_changed`` so open tabs refetch instead of
    keeping the pre-heal copy.
  * Single source — the adjacency predicate is
    ``lib.chat.messages.is_engine_user_msg`` and the row is
    ``build_engine_no_reply_tombstone``; nothing is re-implemented here.
  * Dry-run by default: ``python -m lib.conversations.engine_tail_heal``
    prints; ``--apply`` writes.
"""

from __future__ import annotations

import json
import time

from lib.chat.messages import (
    build_engine_no_reply_tombstone, is_engine_user_msg)
from lib.log import get_logger

logger = get_logger(__name__)


def find_engine_tail_adjacencies(messages) -> list[int]:
    """Return the indices i where [i-1] is an unanswered ENGINE user row and
    [i] is a user row — i.e. where a tombstone must be inserted.

    Inherently idempotent: a healed pair has an assistant tombstone between
    the two user rows, so it never matches again.
    """
    out = []
    for i in range(1, len(messages)):
        if (is_engine_user_msg(messages[i - 1])
                and isinstance(messages[i], dict)
                and messages[i].get('role') == 'user'):
            out.append(i)
    return out


def heal_messages(messages, now_ms=None):
    """Return (healed_messages, n_inserted). Pure — no DB, no notify."""
    pairs = find_engine_tail_adjacencies(messages)
    if not pairs:
        return messages, 0
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    out = list(messages)
    for i in reversed(pairs):  # descending — earlier indices stay valid
        out.insert(i, build_engine_no_reply_tombstone(now_ms))
    return out, len(pairs)


def heal_engine_tail_adjacencies(*, dry_run: bool = True,
                                 updated_since_ms: int | None = None,
                                 progress=print) -> dict:
    """Scan ``conversations`` and tombstone every unanswered-engine-tail
    adjacency. Per-row failures are logged and skipped.

    Returns a stats dict: {scanned, rows_with_pairs, pairs, written,
    skipped_errors, notified}.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db

    db = get_thread_db(DOMAIN_CHAT)
    from lib.database.conversation_repository import list_conversation_snapshots
    invalid_rows = []
    rows = list_conversation_snapshots(
        db, user_id=None, updated_at_gt=updated_since_ms,
        metadata_columns=('updated_at',),
        on_invalid=lambda conv_id, exc: invalid_rows.append((conv_id, exc)))

    for conv_id, exc in invalid_rows:
        logger.warning('[EngineTailHeal] conv=%s invalid transcript — skipped: %s',
                       str(conv_id)[:8], exc)
    stats = {'scanned': len(rows) + len(invalid_rows), 'rows_with_pairs': 0,
             'pairs': 0, 'written': 0,
             'skipped_errors': len(invalid_rows), 'notified': 0}
    for row in rows:
        conv_id = row['id']
        try:
            messages = row.messages
            healed, n = heal_messages(messages)
            if not n:
                continue
            stats['rows_with_pairs'] += 1
            stats['pairs'] += n
            progress(f'{conv_id}: {n} pair(s) '
                     f'{"[dry-run]" if dry_run else "[healed]"}')
            if dry_run:
                continue
            now_ms = int(time.time() * 1000)
            from lib.database.conversation_repository import replace_messages
            result = replace_messages(
                db, conv_id, healed, expected_rev=row['rev'],
                metadata={'updated_at': now_ms, 'msg_count': len(healed)},
                full=True)
            if not result.applied:
                # A concurrent writer bumped rev mid-heal — skip (a rerun
                # heals it); never clobber.
                logger.warning('[EngineTailHeal] conv=%s CAS miss — skipped '
                               '(rerun heals)', conv_id[:8])
                stats['skipped_errors'] += 1
                continue
            stats['written'] += 1
            try:
                from lib.conversations import notify_conv_changed
                notify_conv_changed(conv_id, rev=result.rev)
                stats['notified'] += 1
            except Exception as e:
                logger.warning('[EngineTailHeal] conv=%s notify failed '
                               '(write landed): %s', conv_id[:8], e)
        except Exception as e:
            logger.warning('[EngineTailHeal] conv=%s heal failed: %s — skipped',
                           conv_id[:8], e)
            stats['skipped_errors'] += 1
    return stats


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description='Tombstone unanswered engine-tail '
                                      'user,user adjacencies (one-time heal).')
    p.add_argument('--apply', action='store_true',
                   help='write (default is dry-run: print only)')
    p.add_argument('--days', type=int, default=0,
                   help='only scan conversations updated in the last N days '
                        '(default: all)')
    args = p.parse_args(argv)
    since = int((time.time() - args.days * 86400) * 1000) if args.days else None
    stats = heal_engine_tail_adjacencies(
        dry_run=not args.apply, updated_since_ms=since)
    print(('DRY-RUN ' if not args.apply else 'APPLIED ') + json.dumps(stats))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
