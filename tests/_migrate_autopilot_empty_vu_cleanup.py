"""tests/_migrate_autopilot_empty_vu_cleanup.py — one-shot cleanup of empty
VU shells persisted before the pt_be69e7cabef54676 double gate.

BACKGROUND
----------
Before the 2026-08-01 double gate (a7adb3eb), an aborted / degenerate VU
reply (text='') was treated as a valid "keep going": an EMPTY VU row was
appended to conversations.messages and a follow-up task spawned on top of it
— which the user usually stopped immediately, leaving a SECOND ghost: an
empty assistant row (content='', finishReason='aborted', no toolRounds)
directly after the empty VU row. Neither row carries any information; the
empty VU row additionally goes UPSTREAM on every later turn as
{'role': 'user', 'content': ''} (strict providers hard-400 on it), and the
frontend renders it as a permanent empty "Autopilot" bubble.

WHAT THIS DELETES (two classes, both provably content-free)
-----------------------------------------------------------
  Class A — ``role='user'`` + ``_isVirtualUser=True`` + empty/whitespace
    ``content``. A VU row is machine-authored; an empty one is always the
    ghost (a real VU instruction is never empty by construction — the gate
    now refuses to persist it).
  Class B — ``role='assistant'`` + empty ``content`` + empty ``thinking`` +
    ``finishReason='aborted'`` + no ``toolRounds`` DIRECTLY ADJACENT AFTER a
    Class-A row. An assistant answering an EMPTY user turn can only be the
    ghost follow-up the user had to stop. A human-stopped turn's aborted
    assistant follows a REAL user message (content non-empty) and is kept.

Everything else is kept byte-identical, INCLUDING real aborted-empty
assistant rows after human turns (a legitimate "you stopped this turn"
record) and non-empty VU rows.

SAFETY
------
  * DRY-RUN BY DEFAULT — prints the per-conv account; writes nothing.
  * ``--apply`` first dumps every affected conv's FULL messages to
    data/migration_backups/autopilot_empty_vu_cleanup_<ts>.json.
  * Per-conv rev-CAS (same pattern as _append_vu_message_to_conv): skip +
    report a conv whose rev moved under us, never blind-overwrite.
  * msg_count / search_text / updated_at are rebuilt; the Phase-5 mirror is
    refreshed best-effort (inert when the flag is off).

Usage:
    python tests/_migrate_autopilot_empty_vu_cleanup.py          # dry run
    python tests/_migrate_autopilot_empty_vu_cleanup.py --apply  # write
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger

logger = get_logger(__name__)


def classify_rows(messages: list) -> tuple[list, list]:
    """Split a conv's message list into (keep, drop) by the Class-A/B rules.

    Pure function — the unit tests pin this, the runner below only does I/O.
    """
    drop_idx: set = set()

    def _empty(text) -> bool:
        return not (text or '').strip()

    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        if (m.get('role') == 'user' and m.get('_isVirtualUser')
                and _empty(m.get('content'))):
            drop_idx.add(i)

    for i, m in enumerate(messages):
        if i in drop_idx or not isinstance(m, dict):
            continue
        if (m.get('role') == 'assistant'
                and _empty(m.get('content'))
                and _empty(m.get('thinking'))
                and m.get('finishReason') == 'aborted'
                and not m.get('toolRounds')
                and (i - 1) in drop_idx):
            drop_idx.add(i)

    keep = [m for i, m in enumerate(messages) if i not in drop_idx]
    drop = [m for i, m in enumerate(messages) if i in drop_idx]
    return keep, drop


def _iter_affected_convs(db):
    """Yield (conv_id, messages, rev) for every conv carrying a Class-A row."""
    rows = db.execute(
        "SELECT id, messages, rev FROM conversations WHERE user_id=1"
    ).fetchall()
    from lib.tasks_pkg.persistence_store import _row_fields
    for row in rows:
        conv_id, raw, rev_raw = _row_fields(row, 'id', 'messages', 'rev')
        try:
            messages = json.loads(raw or '[]')
        except (json.JSONDecodeError, TypeError):
            continue
        _, drop = classify_rows(messages)
        if drop:
            yield conv_id, messages, int(rev_raw or 0), drop


def run(apply: bool = False) -> dict:
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg

    db = get_thread_db(DOMAIN_CHAT)
    account = {'convs': 0, 'dropped': 0, 'skipped_race': [], 'details': []}
    backup: dict = {}

    for conv_id, messages, rev, drop in _iter_affected_convs(db):
        account['convs'] += 1
        account['dropped'] += len(drop)
        account['details'].append((conv_id, len(messages), len(drop)))
        backup[conv_id] = messages

    for conv_id, n_before, n_drop in account['details']:
        logger.info('[Migrate] conv=%s messages=%d → drop %d ghost row(s)%s',
                    conv_id[:8], n_before, n_drop, '' if apply else ' (dry-run)')

    if not apply:
        return account

    import pathlib
    ts = int(time.time())
    out = pathlib.Path('data/migration_backups') / \
        f'autopilot_empty_vu_cleanup_{ts}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(backup, ensure_ascii=False))
    logger.info('[Migrate] backup written: %s (%d convs)', out, len(backup))

    from lib.conversations import build_search_text
    from lib.tasks_pkg.persistence_store import _row_fields
    for conv_id, n_before, n_drop in account['details']:
        row = db.execute(
            'SELECT messages, rev FROM conversations WHERE id=? AND user_id=1',
            (conv_id,),
        ).fetchone()
        if not row:
            continue
        raw, rev_raw = _row_fields(row, 'messages', 'rev')
        cur_rev = int(rev_raw or 0)
        try:
            messages = json.loads(raw or '[]')
        except (json.JSONDecodeError, TypeError):
            continue
        keep, drop = classify_rows(messages)
        if not drop:
            continue
        now_ms = int(time.time() * 1000)
        cur = db.execute(
            '''UPDATE conversations
                  SET messages=?, updated_at=?, msg_count=?, search_text=?
                  WHERE id=? AND user_id=1 AND rev=?''',
            (json_dumps_pg(keep), now_ms, len(keep),
             build_search_text(keep), conv_id, cur_rev),
        )
        db.commit()
        if not getattr(cur, 'rowcount', 0):
            account['skipped_race'].append(conv_id)
            logger.warning('[Migrate] conv=%s lost the rev=%s race — SKIPPED '
                           '(re-run to retry)', conv_id[:8], cur_rev)
            continue
        try:
            from lib.database.messages_rows import mirror_write_and_commit
            mirror_write_and_commit(db, conv_id, keep, now_ms=now_ms)
        except Exception as e:
            logger.warning('[Migrate] conv=%s mirror refresh failed '
                           '(non-fatal, authoritative write already durable): '
                           '%s', conv_id[:8], e)
        logger.info('[Migrate] conv=%s ✅ dropped %d ghost row(s) (rev %s→%s)',
                    conv_id[:8], len(drop), cur_rev, cur_rev + 1)

    return account


if __name__ == '__main__':
    account = run(apply='--apply' in sys.argv)
    print(json.dumps({
        'mode': 'apply' if '--apply' in sys.argv else 'dry-run',
        'convs_affected': account['convs'],
        'rows_dropped': account['dropped'],
        'skipped_race': account['skipped_race'],
        'details': [
            {'conv': c, 'messages_before': n, 'ghost_rows': d}
            for c, n, d in account['details']
        ],
    }, ensure_ascii=False, indent=2))
