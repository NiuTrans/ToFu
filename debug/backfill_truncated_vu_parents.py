"""Backfill autopilot VU-parent assistant messages truncated at a checkpoint.

Rationale:
  Before the orchestrator pre-sync fix (see
  .tofu/skills/autopilot-presync-parent-before-vu-spawn.md), the autopilot
  end-of-turn hook appended the virtual-user (VU) turn and spawned the
  follow-up task BEFORE persist_task_result ran.  The follow-up registered
  as the conversation's latest task, so the freshness guard in
  manager._sync_result_to_conversation dropped the parent's FINAL write.
  The parent assistant message was left frozen at its last streaming
  checkpoint — truncated content, no finishReason/model/usage — even though
  the COMPLETE reply was correctly persisted in task_results.content.

  Symptom in the DB: an assistant message that
    * has no finishReason,
    * is immediately followed by a user message with _isVirtualUser=True,
    * whose content is a strict PREFIX of a longer task_results.content row
      for the same conversation.

This script finds those messages and rewrites them in place from the
matching task_results row: content, thinking, finishReason, model,
provider_id, usage, preset, apiRounds, modifiedFiles/modifiedFileList,
toolSummary, and cost (mirroring what _sync_result_to_conversation writes).
It then rebuilds search_text/search_tsv and bumps updated_at.

Matching is conservative — a candidate is repaired ONLY when exactly one
task_results row for the conversation is a strict superstring (prefix) of
the stored content.  Equal-length or shorter rows (the message is already
complete) and ambiguous multi-match cases are skipped and reported.

Idempotent: a repaired message gains a finishReason, so it no longer
matches the candidate filter on a second run.

Usage:
    python debug/backfill_truncated_vu_parents.py             # dry-run
    python debug/backfill_truncated_vu_parents.py --apply      # write
    python debug/backfill_truncated_vu_parents.py --conv <id>  # one conv
"""

import argparse
import json
import logging
import os
import sys
import time

# Ensure project root on path when run directly
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import DOMAIN_CHAT, _BACKEND, get_thread_db
from lib.log import get_logger

logger = get_logger('backfill_truncated_vu_parents')
_console = logging.StreamHandler(sys.stdout)
_console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(_console)
logger.setLevel(logging.INFO)

_IS_PG = _BACKEND in ('pg', 'postgres', 'postgresql')

# Metadata keys copied from the task_results metadata blob onto the message,
# mirroring lib.tasks_pkg.manager._sync_result_to_conversation.
_META_COPY_KEYS = (
    'finishReason', 'usage', 'preset', 'toolSummary', 'model', 'provider_id',
    'thinkingDepth', 'apiRounds', 'modifiedFiles', 'modifiedFileList',
    'fallbackModel', 'fallbackFrom', 'fallbackReason', 'fallbackKind',
)


def _load_task_results_for_conv(db, conv_id):
    """Return list of (task_id, content, metadata_dict) for a conversation."""
    if _IS_PG:
        rows = db.execute(
            'SELECT task_id, content, metadata FROM task_results WHERE conv_id=%s',
            (conv_id,)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT task_id, content, metadata FROM task_results WHERE conv_id=?',
            (conv_id,)
        ).fetchall()
    out = []
    for r in rows:
        content = r['content'] or ''
        md = r['metadata']
        if isinstance(md, str):
            try:
                md = json.loads(md)
            except (json.JSONDecodeError, TypeError):
                md = {}
        out.append((r['task_id'], content, md or {}))
    return out


def _find_full_match(stored_content, task_results):
    """Find the unique task_results row whose content is a longer superstring.

    Returns (task_id, full_content, metadata) or None when there is no
    match or the match is ambiguous (more than one distinct candidate).
    """
    matches = [
        (tid, content, md)
        for (tid, content, md) in task_results
        if len(content) > len(stored_content) and content.startswith(stored_content)
    ]
    if not matches:
        return None
    # Collapse to distinct contents — multiple task_results rows with the
    # SAME full content (e.g. checkpoint + final) are not ambiguous.
    distinct = {content for (_, content, _) in matches}
    if len(distinct) > 1:
        # Prefer the longest unique candidate only if it uniquely extends;
        # otherwise treat as ambiguous and skip for safety.
        longest = max(distinct, key=len)
        if sum(1 for c in distinct if c == longest) != 1:
            return None
        for tid, content, md in matches:
            if content == longest:
                return tid, content, md
        return None
    return matches[0]


def _repair_message(msg, full_content, meta):
    """Mutate ``msg`` in place with the full content + metadata. Returns True if changed."""
    changed = False
    if msg.get('content') != full_content:
        msg['content'] = full_content
        changed = True
    # Thinking is checkpointed too, but task_results.thinking lives in its
    # own column we did not load; the message's thinking is already the
    # checkpointed value and is not truncated by this bug (content is the
    # field that gets the trailing final delta). Leave it as-is.
    for key in _META_COPY_KEYS:
        val = meta.get(key)
        if val is None:
            continue
        if msg.get(key) != val:
            msg[key] = val
            changed = True
    # Cost snapshot — only if usage + model present and not already stamped.
    if msg.get('usage') and not msg.get('cost'):
        try:
            from lib.cost import compute_cost
            c = compute_cost(
                msg['usage'],
                model_id=msg.get('model') or meta.get('model') or '',
                provider_id=msg.get('provider_id') or meta.get('provider_id') or None,
            )
            if c:
                msg['cost'] = c
                changed = True
        except Exception as e:
            logger.debug('cost stamp skipped: %s', e)
    return changed


def _iter_candidates(messages):
    """Yield (idx, msg) for assistant messages frozen at a truncated checkpoint.

    Predicate: role=assistant, no finishReason, has content, and the NEXT
    message is a virtual-user turn (the autopilot follow-up).
    """
    for i in range(len(messages) - 1):
        m = messages[i]
        nxt = messages[i + 1]
        if (m.get('role') == 'assistant'
                and not m.get('finishReason')
                and m.get('content')
                and nxt.get('role') == 'user'
                and nxt.get('_isVirtualUser')):
            yield i, m


def _load_convs(db, conv_filter):
    """Load conversations that contain at least one VU message."""
    if conv_filter:
        if _IS_PG:
            rows = db.execute(
                'SELECT id, user_id, messages::text AS m FROM conversations '
                'WHERE id=%s AND user_id=1', (conv_filter,)
            ).fetchall()
        else:
            rows = db.execute(
                'SELECT id, user_id, messages AS m FROM conversations '
                'WHERE id=? AND user_id=1', (conv_filter,)
            ).fetchall()
        return rows
    if _IS_PG:
        return db.execute(
            "SELECT id, user_id, messages::text AS m FROM conversations "
            "WHERE user_id=1 AND messages::text LIKE %s",
            ('%_isVirtualUser%',)
        ).fetchall()
    return db.execute(
        "SELECT id, user_id, messages AS m FROM conversations "
        "WHERE user_id=1 AND messages LIKE ?",
        ('%_isVirtualUser%',)
    ).fetchall()


def _write_conv(db, conv_id, user_id, messages):
    """Persist repaired messages + rebuild search_text/search_tsv + bump updated_at."""
    try:
        from routes.conversations import build_search_text
        search_text = build_search_text(messages)
    except Exception as e:
        logger.debug('build_search_text failed for %s: %s', conv_id, e)
        search_text = ''
    msgs_json = json.dumps(messages, ensure_ascii=False)
    now_ms = int(time.time() * 1000)
    if _IS_PG:
        db.execute(
            "UPDATE conversations SET messages=%s, updated_at=%s, msg_count=%s, "
            "search_text=%s, search_tsv=to_tsvector('simple', left(%s, 50000)) "
            "WHERE id=%s AND user_id=%s",
            (msgs_json, now_ms, len(messages), search_text, search_text,
             conv_id, user_id)
        )
        db.commit()
    else:
        db.execute(
            "UPDATE conversations SET messages=?, updated_at=?, msg_count=?, "
            "search_text=? WHERE id=? AND user_id=?",
            (msgs_json, now_ms, len(messages), search_text, conv_id, user_id)
        )
        db.execute(
            "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
            "SELECT rowid, ? FROM conversations WHERE id=?",
            (search_text, conv_id)
        )
        db.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true',
                        help='Actually write changes (default: dry-run)')
    parser.add_argument('--conv', default='',
                        help='Repair a single conversation id (default: all)')
    args = parser.parse_args()

    db = get_thread_db(DOMAIN_CHAT)
    rows = _load_convs(db, args.conv.strip())
    logger.info('[backfill] Scanning %d conversation(s) with VU messages', len(rows))

    convs_changed = 0
    msgs_repaired = 0
    skipped_no_match = 0
    skipped_ambiguous = 0
    errors = 0
    t0 = time.monotonic()

    for row in rows:
        conv_id = row['id']
        user_id = row['user_id']
        try:
            messages = json.loads(row['m'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[backfill] conv=%s parse failed: %s', conv_id, e)
            errors += 1
            continue

        candidates = list(_iter_candidates(messages))
        if not candidates:
            continue

        task_results = _load_task_results_for_conv(db, conv_id)
        conv_repaired = 0
        for idx, msg in candidates:
            stored = msg.get('content') or ''
            match = _find_full_match(stored, task_results)
            if match is None:
                # Distinguish ambiguous vs no-match for reporting.
                any_prefix = any(
                    len(c) > len(stored) and c.startswith(stored)
                    for (_, c, _) in task_results
                )
                if any_prefix:
                    skipped_ambiguous += 1
                    logger.info('[backfill] conv=%s msg[%d]: ambiguous task_results '
                                'match — skipped (%d chars)', conv_id, idx, len(stored))
                else:
                    skipped_no_match += 1
                    logger.info('[backfill] conv=%s msg[%d]: no longer task_results '
                                'superstring — likely already complete, skipped '
                                '(%d chars)', conv_id, idx, len(stored))
                continue

            tid, full_content, meta = match
            if _repair_message(msg, full_content, meta):
                conv_repaired += 1
                msgs_repaired += 1
                logger.info('[backfill] conv=%s msg[%d]: %d -> %d chars '
                            '(finishReason=%s model=%s task=%s)',
                            conv_id, idx, len(stored), len(full_content),
                            meta.get('finishReason'), meta.get('model'),
                            (tid or '')[:8])

        if conv_repaired:
            convs_changed += 1
            if args.apply:
                try:
                    _write_conv(db, conv_id, user_id, messages)
                except Exception as e:
                    logger.error('[backfill] conv=%s write failed: %s',
                                 conv_id, e, exc_info=True)
                    errors += 1

    elapsed = time.monotonic() - t0
    mode = 'APPLIED' if args.apply else 'DRY-RUN'
    logger.info('[backfill] %s — convs_changed=%d msgs_repaired=%d '
                'skipped_no_match=%d skipped_ambiguous=%d errors=%d elapsed=%.2fs',
                mode, convs_changed, msgs_repaired, skipped_no_match,
                skipped_ambiguous, errors, elapsed)
    if not args.apply and msgs_repaired > 0:
        logger.info('[backfill] Re-run with --apply to actually write changes.')


if __name__ == '__main__':
    main()
