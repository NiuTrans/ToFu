#!/usr/bin/env python3
"""One-shot backfill: auto-translate ALREADY-PERSISTED autopilot VU turns that
were persisted before the ``_maybe_auto_translate_vu`` wire landed.

WHY
---
Autopilot virtual-user (VU) turns are persisted by
``autopilot._append_vu_message_to_conv`` on a path SEPARATE from
``manager._sync_result_to_conversation`` (which owns the assistant/critic
auto-translate safety net). Before the ``_maybe_auto_translate_vu`` wire
(autopilot.py, 2026-07-10) that path had ZERO ``_maybe_auto_translate_*`` calls,
so every VU turn in an autopilot run was left untranslated unless a viewer
happened to fire a manual translate — the reported "conv mre58lxth33ncr never
triggers auto-translate" bug (its last VU turn, msg4, sits untranslated).

The forward wire fixes NEW turns. This migration fixes the ones ALREADY on
disk: it walks conversations, finds untranslated VU turns, and hands each to
the SAME ``_maybe_auto_translate_vu`` the live path now calls.

SINGLE SOURCE OF TRUTH — REUSE, don't copy (the conv-OOM lesson)
----------------------------------------------------------------
  • "Which VU turns qualify" is the shared pure predicate
    ``lib.conversations.vu_translate_backfill.collect_untranslated_vu_turns``.
  • The ACTUAL translation is delegated to the production
    ``lib.tasks_pkg.autopilot._maybe_auto_translate_vu`` — so the safety net's
    OWN gates run verbatim and this script cannot drift from the live path:
      - ``resolve_auto_translate(settings)`` OFF  → skip (honored),
      - content already predominantly Chinese     → skip (honored),
      - message already has ``translatedContent``  → skip (idempotent),
      - a translation already in-flight            → stand down (dedup).
    This script adds NO gate + NO write of its own; the collector is only a
    cheap pre-filter so we don't fire the safety net at rows that plainly
    don't qualify.

REV / IDEMPOTENCY
-----------------
The sole state change is the translation landing through the production commit
(``_commit_translation_to_db`` — CAS on ``updated_at`` + a ``notify_conv_changed``
push so open clients sync). That is EXACTLY what a live translation does; the
migration itself performs no separate/raw ``messages`` write and no gratuitous
rev bump. Idempotent: a second run sees ``translatedContent`` present and the
collector skips it (0 to do); already-Chinese / autoTranslate-off rows are
skipped by the safety net forever.

ASYNC SETTLE
------------
``_maybe_auto_translate_vu`` returns immediately after SPAWNING the translate
worker (a daemon thread), exactly like the live path. In ``--apply`` we then
POLL the conversation row until the VU turn gains a non-empty
``translatedContent`` (or ``--settle-timeout`` elapses) so the migration can
report a real, verified result rather than fire-and-forget.

SAFETY
------
  • Dry-run by default: prints per-conv untranslated VU turns, fires nothing.
  • ``--apply`` translates. Per-row isolation: one bad row logs + is skipped.
  • Never removes / truncates a message; only the safety net's normal
    ``translatedContent`` write occurs.

Usage:
    python tests/_migrate_backfill_vu_translations.py                    # dry-run, all convs
    python tests/_migrate_backfill_vu_translations.py --id mre58lxth33ncr
    python tests/_migrate_backfill_vu_translations.py --limit 50
    python tests/_migrate_backfill_vu_translations.py --id mre58lxth33ncr --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.conversations.vu_translate_backfill import (  # noqa: E402
    collect_untranslated_vu_turns,
)
from lib.database import DOMAIN_CHAT, get_thread_db  # noqa: E402
from lib.log import audit_log, get_logger  # noqa: E402
from lib.tasks_pkg.autopilot import _maybe_auto_translate_vu  # noqa: E402

logger = get_logger(__name__)

DEFAULT_USER_ID = 1


def _load_messages(db, conv_id):
    """Return the parsed messages list for a conv, or None on miss/parse error."""
    row = db.execute(
        'SELECT messages FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID),
    ).fetchone()
    if not row:
        return None
    raw = row[0]
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw or '[]')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[vu-xlate-backfill] conv=%s unparseable messages: %s',
                       conv_id[:8], e)
        return None


def _candidate_ids(db, only_id, limit):
    """Conversation ids to consider (newest first)."""
    if only_id:
        row = db.execute(
            'SELECT id FROM conversations WHERE id=? AND user_id=?',
            (only_id, DEFAULT_USER_ID)).fetchone()
        return [row[0]] if row else []
    rows = db.execute(
        'SELECT id FROM conversations WHERE user_id=? AND msg_count > 0 '
        'ORDER BY updated_at DESC', (DEFAULT_USER_ID,)).fetchall()
    ids = [r[0] for r in rows]
    return ids[:limit] if limit else ids


def _translated_len(db, conv_id, msg_id):
    """Current len(translatedContent) for the VU row identified by msg_id."""
    messages = _load_messages(db, conv_id)
    if not messages:
        return 0
    for m in messages:
        if isinstance(m, dict) and m.get('_msgId') == msg_id:
            return len(m.get('translatedContent') or '')
    return 0


def _settle_one(db, conv_id, hit, settle_timeout, poll_interval):
    """Fire the safety net for one VU turn, then poll until translatedContent
    lands (or timeout). Returns the final translatedContent length (0 = did not
    land — either a gate skipped it, or it is still running past the timeout)."""
    _maybe_auto_translate_vu(conv_id, hit['msgId'], hit['content'])
    deadline = time.time() + settle_timeout
    while time.time() < deadline:
        time.sleep(poll_interval)
        tclen = _translated_len(db, conv_id, hit['msgId'])
        if tclen > 0:
            return tclen
    return _translated_len(db, conv_id, hit['msgId'])


def run(apply, only_id, limit, settle_timeout, poll_interval):
    db = get_thread_db(DOMAIN_CHAT)
    candidates = _candidate_ids(db, only_id, limit)
    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'\n  ═══ backfill-vu-translations [{mode}] — '
          f'{len(candidates)} candidate conversation(s) ═══\n')
    if not candidates:
        print('  (no rows match)\n')
        return

    convs_touched = 0
    turns_seen = 0
    turns_translated = 0
    turns_skipped_by_gate = 0
    errored = 0

    for cid in candidates:
        try:
            messages = _load_messages(db, cid)
            if messages is None:
                continue
            hits = collect_untranslated_vu_turns(messages)
            if not hits:
                continue
            convs_touched += 1
            turns_seen += len(hits)
            print(f'  {cid:20s}  {len(hits)} untranslated VU turn(s):')
            for h in hits:
                print(f'      idx={h["idx"]:<3d} msgId={(h["msgId"][:8] or "-")}  '
                      f'content={len(h["content"])} chars  translatedContent=0')
            if not apply:
                continue
            for h in hits:
                tclen = _settle_one(db, cid, h, settle_timeout, poll_interval)
                if tclen > 0:
                    turns_translated += 1
                    print(f'      → idx={h["idx"]} TRANSLATED '
                          f'({tclen} chars translatedContent)')
                    logger.info('[vu-xlate-backfill] conv=%s idx=%d msgId=%s '
                                'translatedContent=%d chars', cid, h['idx'],
                                (h['msgId'][:8] or '-'), tclen)
                    try:
                        audit_log('conversation_vu_translate_backfill', conv_id=cid,
                                  msg_idx=h['idx'], translated_chars=tclen)
                    except Exception as e:
                        logger.debug('[vu-xlate-backfill] audit_log failed: %s', e)
                else:
                    turns_skipped_by_gate += 1
                    print(f'      → idx={h["idx"]} not translated '
                          f'(gate skipped: autoTranslate off / already-Chinese, '
                          f'or still running past {settle_timeout}s)')
        except Exception as e:
            errored += 1
            logger.error('[vu-xlate-backfill] conv %s failed (%s): %s — skipped',
                         cid, type(e).__name__, e, exc_info=True)
            print(f'  {cid:20s}  ERROR ({type(e).__name__}: {e}) — skipped')

    print(f'\n  ─── {mode} summary ───')
    print(f'    conversations with untranslated VU turns : {convs_touched}')
    print(f'    untranslated VU turns seen               : {turns_seen}')
    if apply:
        print(f'    turns translated (verified in DB)        : {turns_translated}')
        print(f'    turns skipped by a safety-net gate       : {turns_skipped_by_gate}')
    print(f'    rows errored (skipped)                   : {errored}')
    if not apply and turns_seen:
        print('\n  (dry-run — nothing translated. Re-run with --apply.)')
    print()


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--apply', action='store_true',
                   help='translate (default: dry-run)')
    p.add_argument('--id', default='', help='restrict to a single conversation id')
    p.add_argument('--limit', type=int, default=0,
                   help='cap number of conversations processed (0 = all)')
    p.add_argument('--settle-timeout', type=float, default=120.0,
                   help='seconds to wait for one VU turn to translate (default 120)')
    p.add_argument('--poll-interval', type=float, default=2.0,
                   help='seconds between DB polls while settling (default 2)')
    args = p.parse_args()
    run(args.apply, args.id or '', args.limit, args.settle_timeout, args.poll_interval)


if __name__ == '__main__':
    main()
