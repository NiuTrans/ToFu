#!/usr/bin/env python3
"""One-shot, idempotent, dry-run-safe migration: clean the FROZEN evidence of
the terminal-state race in conversation history (pt_50c0ee26faac44fc, follow-up
to the code fix pt_bf93496e98b9441e / commit 3bb877f4).

The code fix made NEW terminal writes converge; it does not rewrite history.
16 messages in the production library still carry the race's frozen outcome:

  CLASS P  (provable provenance violation): a message whose task COMPLETED
           (finishReason='stop', full answer, usage/cost) wearing another
           task's reaper error — the reaper's error bubble was appended one
           turn late and the next task's sync filled it. A completed turn
           cannot be wedged-dead, so the error is foreign and is REMOVED.
  CLASS R  (mislabeled reap verdict): a message carrying the reaper's own
           envelope (context='stuck-task-reaper') but finishReason='aborted'
           — the late finalize collapsed the system kill into the user-Stop
           value. finishReason is restamped to 'error', matching the
           message's own envelope.
  CLASS OK (correct tombstone): finishReason='error' already — untouched.
  UNKNOWN  (any other finishReason, incl. None): reported, never touched.

Writes (per changed message, atomically per conversation):
  * ``conversations.messages`` (the JSON array);
  * the ``conversation_messages`` mirror row (its ``meta`` JSON);
  * CLASS R only: the owning task's ``task_results.metadata.finishReason``
    (the poll-fallback path reads it — leaving it 'aborted' would
    re-materialize the mislabel on recovery);
  * the ``settings`` shell's lastFinishReason/lastMsgError when the changed
    message is the conversation's tail (sidebar reads those).

``conversations.updated_at`` is deliberately NOT bumped (a metadata repair
must not reorder the user's conversation list); the rev trigger fires on the
messages change as usual so clients refetch the body once. ``search_text``
needs no rebuild — it only indexes content/thinking, which never change here.

Usage:
    python tests/_migrate_reaper_terminal_cleanup.py             # dry-run (default)
    python tests/_migrate_reaper_terminal_cleanup.py --apply     # write + backup
    python tests/_migrate_reaper_terminal_cleanup.py --apply --backup /path/bak.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REAPER_CONTEXT = 'stuck-task-reaper'


def classify_message(msg):
    """Return ('P'|'R'|'OK'|'UNKNOWN', reason) for a message carrying a
    reaper-context error envelope. Pure — trivially unit-testable."""
    err = msg.get('error')
    if not isinstance(err, dict) or err.get('context') != REAPER_CONTEXT:
        return None, None
    fr = msg.get('finishReason')
    if fr == 'stop':
        return 'P', ('completed turn (finishReason=stop) cannot be wedged-dead '
                     '— the reaper error is foreign and is removed')
    if fr == 'aborted':
        return 'R', ("reaper envelope present but finishReason='aborted' — "
                     "restamp to 'error' (the race's collapsed verdict)")
    if fr == 'error':
        return 'OK', 'correct tombstone'
    return 'UNKNOWN', f'finishReason={fr!r} — reported, never touched'


def plan_changes(db):
    """Scan the whole library and return the full change plan (no writes).

    Returns a list of dicts:
      {conv_id, idx, msg_id, cls, task_id, before, after, is_tail}
    where before/after carry ONLY the changed fields.
    """
    rows = db.execute(
        "SELECT id, CAST(messages AS TEXT), CAST(settings AS TEXT) FROM conversations "
        "WHERE CAST(messages AS TEXT) LIKE '%' || ? || '%'", (REAPER_CONTEXT,)).fetchall()
    changes = []
    for row in rows:
        conv_id = row[0]
        try:
            messages = json.loads(row[1] or '[]')
        except (json.JSONDecodeError, TypeError):
            continue
        for i, m in enumerate(messages):
            cls, reason = classify_message(m)
            if cls in (None, 'OK'):
                continue
            changes.append({
                'conv_id': conv_id, 'idx': i,
                'msg_id': m.get('_msgId'), 'cls': cls,
                'task_id': m.get('_taskId'), 'reason': reason,
                'before': {'finishReason': m.get('finishReason'),
                           'error': m.get('error')},
                'after': ({'finishReason': m.get('finishReason'), 'error': None}
                          if cls == 'P' else
                          ({'finishReason': 'error', 'error': m.get('error')}
                           if cls == 'R' else None)),
                'is_tail': i == len(messages) - 1,
                'content_len': len(m.get('content') or ''),
            })
    return changes


def _mutate_message(msg, cls):
    """Apply the class mutation to a message dict IN PLACE."""
    if cls == 'P':
        msg.pop('error', None)
    elif cls == 'R':
        msg['finishReason'] = 'error'


def _apply_to_conv(db, conv_id, cls_by_idx, apply):
    """Mutate conversations.messages (+ settings sidecar) for one conv.

    Returns (changed_fields_summary, before_row) — before_row is None in
    dry-run mode beyond what was read.
    """
    from lib.database import json_dumps_pg
    row = db.execute(
        'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
        (conv_id,)).fetchone()
    if not row:
        return None, None
    messages = json.loads(row[0] or '[]')
    settings = json.loads(row[1] or '{}') if row[1] else {}
    changed = []
    settings_changed = False
    for idx, cls in cls_by_idx.items():
        if idx >= len(messages):
            continue
        m = messages[idx]
        _mutate_message(m, cls)
        changed.append((idx, cls))
        if idx == len(messages) - 1:
            # Sidebar shell facts (mirror _sync.py's recompute rule).
            if settings.get('lastFinishReason') != m.get('finishReason'):
                settings['lastFinishReason'] = m.get('finishReason')
                settings_changed = True
            if settings.get('lastMsgError') != bool(m.get('error')):
                settings['lastMsgError'] = bool(m.get('error'))
                settings_changed = True
    if not changed:
        return None, None
    if apply:
        if settings_changed:
            db.execute(
                'UPDATE conversations SET messages=?, msg_count=?, settings=? '
                'WHERE id=? AND user_id=1',
                (json_dumps_pg(messages), len(messages),
                 json.dumps(settings, ensure_ascii=False), conv_id))
        else:
            db.execute(
                'UPDATE conversations SET messages=?, msg_count=? '
                'WHERE id=? AND user_id=1',
                (json_dumps_pg(messages), len(messages), conv_id))
    return changed, row


def _apply_to_mirror(db, conv_id, cls_by_idx, apply):
    """Mutate the conversation_messages mirror rows' meta JSON."""
    changed = []
    for idx, cls in cls_by_idx.items():
        row = db.execute(
            'SELECT meta FROM conversation_messages WHERE conv_id=? AND seq=?',
            (conv_id, str(idx))).fetchone()
        if not row or not row[0]:
            continue
        try:
            meta = json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            continue
        if cls == 'P' and 'error' not in meta:
            continue
        if cls == 'R' and meta.get('finishReason') == 'error':
            continue
        _mutate_message(meta, cls)
        changed.append(idx)
        if apply:
            db.execute(
                'UPDATE conversation_messages SET meta=?, updated_at=? '
                'WHERE conv_id=? AND seq=?',
                (json.dumps(meta, ensure_ascii=False),
                 int(time.time() * 1000), conv_id, str(idx)))
    return changed


def _apply_to_task_results(db, task_ids, apply):
    """CLASS R consistency: restamp task_results.metadata.finishReason so the
    poll-fallback path agrees with the cleaned message."""
    changed = []
    for tid in task_ids:
        if not tid:
            continue
        row = db.execute(
            'SELECT metadata, error FROM task_results WHERE task_id=?',
            (tid,)).fetchone()
        if not row or not row[0]:
            continue
        try:
            meta = json.loads(row[0])
            err = json.loads(row[1]) if row[1] else None
        except (json.JSONDecodeError, TypeError):
            continue
        if meta.get('finishReason') != 'aborted':
            continue
        if not (isinstance(err, dict) and err.get('context') == REAPER_CONTEXT):
            continue
        meta['finishReason'] = 'error'
        changed.append(tid)
        if apply:
            db.execute(
                'UPDATE task_results SET metadata=? WHERE task_id=?',
                (json.dumps(meta, ensure_ascii=False), tid))
    return changed


def run(db, apply=False, backup_path=None):
    """Execute the migration. Returns the plan (list of change dicts)."""
    plan = [c for c in plan_changes(db) if c['cls'] in ('P', 'R')]
    unknown = [c for c in plan_changes(db) if c['cls'] == 'UNKNOWN']

    by_conv = {}
    for c in plan:
        by_conv.setdefault(c['conv_id'], {})[c['idx']] = c['cls']

    if apply and plan and backup_path:
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        backup = {'conversations': {}, 'mirror': {}, 'task_results': {}}
        for conv_id in by_conv:
            row = db.execute(
                'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)).fetchone()
            if row:
                backup['conversations'][conv_id] = {'messages': row[0], 'settings': row[1]}
            for idx in by_conv[conv_id]:
                mrow = db.execute(
                    'SELECT meta FROM conversation_messages WHERE conv_id=? AND seq=?',
                    (conv_id, str(idx))).fetchone()
                if mrow:
                    backup['mirror'][f'{conv_id}:{idx}'] = mrow[0]
        for c in plan:
            if c['cls'] == 'R' and c['task_id']:
                trow = db.execute(
                    'SELECT metadata FROM task_results WHERE task_id=?',
                    (c['task_id'],)).fetchone()
                if trow:
                    backup['task_results'][c['task_id']] = trow[0]
        with open(backup_path, 'w', encoding='utf-8') as f:
            json.dump(backup, f, ensure_ascii=False, indent=1)

    for conv_id, cls_by_idx in sorted(by_conv.items()):
        conv_changed, _ = _apply_to_conv(db, conv_id, cls_by_idx, apply)
        mirror_changed = _apply_to_mirror(db, conv_id, cls_by_idx, apply)
        r_tasks = [c['task_id'] for c in plan
                   if c['conv_id'] == conv_id and c['cls'] == 'R']
        tr_changed = _apply_to_task_results(db, r_tasks, apply)
        for c in plan:
            if c['conv_id'] != conv_id:
                continue
            c['applied_conv'] = bool(conv_changed)
            c['applied_mirror'] = c['idx'] in mirror_changed
            c['applied_task_results'] = (c['cls'] == 'R'
                                         and c['task_id'] in tr_changed)
    if apply and plan:
        db.commit()
    return plan, unknown


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--apply', action='store_true')
    p.add_argument('--backup', default='')
    args = p.parse_args()

    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    backup = args.backup or os.path.join(
        'data', 'migration_backups',
        f'reaper_terminal_cleanup_{int(time.time())}.json')

    plan, unknown = run(db, apply=args.apply, backup_path=backup if args.apply else None)

    mode = 'APPLY' if args.apply else 'DRY-RUN'
    print(f'═══ reaper-terminal cleanup ({mode}) ═══')
    for c in plan:
        print(f"  [{c['cls']}] conv={c['conv_id']} idx={c['idx']} "
              f"task={str(c['task_id'])[:8]} clen={c['content_len']} tail={c['is_tail']}")
        print(f"      reason : {c['reason']}")
        before_err = c['before'].get('error')
        print(f"      before : finishReason={c['before']['finishReason']!r} "
              f"error={'present' if before_err else None}")
        print(f"      after  : finishReason={c['after']['finishReason']!r} "
              f"error={'present' if c['after']['error'] else None}")
        if args.apply:
            print(f"      wrote  : conv={c['applied_conv']} mirror={c['applied_mirror']} "
                  f"task_results={c['applied_task_results']}")
    for c in unknown:
        print(f"  [UNKNOWN] conv={c['conv_id']} idx={c['idx']} — {c['reason']}")
    nP = sum(1 for c in plan if c['cls'] == 'P')
    nR = sum(1 for c in plan if c['cls'] == 'R')
    print(f'═══ {mode}: {nP} provenance-clean (P), {nR} restamp (R), '
          f'{len(unknown)} unknown skipped ═══')
    if args.apply and plan:
        print(f'    backup: {backup}')


if __name__ == '__main__':
    main()
