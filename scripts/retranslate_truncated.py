#!/usr/bin/env python3
"""Re-translate truncated message translations across all conversations.

WHY THIS EXISTS
---------------
Before the ``_translate_freetext`` chunking fix (see
``lib/translate/engine.py`` and the ``chat-translation-long-message-truncation``
memory), long assistant messages were translated in a SINGLE cheap-tier LLM
call that silently stopped early.  The result was committed as final
(``_translateDone=true``) even though ``translatedContent`` was only a small
fraction of ``content``.  The stale-partial guard only re-translates below a
15% ratio, so anything in the ~15-40% band is stuck with a half-finished
translation until manually re-triggered.

This script finds every message whose ``translatedContent`` is suspiciously
short relative to its ``content`` (the truncation fingerprint) and re-runs the
translation through the now-chunked engine, committing the full result.

It is GENERAL: by default it scans ALL conversations.  Pass ``--conv <id>`` to
limit it to one (e.g. the original report ``mq62ukzwj8i2v6``).

DETECTION
---------
A message is flagged when ALL hold:
  * it has a non-empty ``translatedContent``;
  * ``len(content) >= --min-content`` (default 2000 — short messages can have
    legitimately low ratios, e.g. a mostly-code block);
  * ``len(translatedContent) / len(content) < --cutoff`` (default 0.40 —
    normal English→Chinese compression lands at 40-60%).

USAGE
-----
    # dry-run: scan everything, print the flagged messages, change nothing
    python scripts/retranslate_truncated.py

    # dry-run a single conversation
    python scripts/retranslate_truncated.py --conv mq62ukzwj8i2v6

    # actually clear + re-translate the flagged messages
    python scripts/retranslate_truncated.py --apply
    python scripts/retranslate_truncated.py --conv mq62ukzwj8i2v6 --apply

    # tune the detection thresholds
    python scripts/retranslate_truncated.py --cutoff 0.35 --min-content 3000

Re-translation reuses the production engine (``_translate_freetext`` +
notranslate-block handling) and commits via ``_commit_translation_to_db``
(CAS on ``updated_at``), so it is safe to run against a live server.
"""

import argparse
import json
import os
import sys
import time

# Allow running as `python scripts/retranslate_truncated.py` from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Progress ledger: records every (conv, message) we have already ATTEMPTED a
# fresh re-translation for, so batched runs converge.  Without it, two effects
# stop the scan from ever emptying:
#   1. a fresh re-translation of a genuinely code-heavy message can still land
#      below --cutoff (legitimately), so it re-flags forever;
#   2. committing bumps updated_at, churning any updated_at-ordered scan.
# Keyed on conv_id + msgId (or conv_id + ':idx:' + idx when no stable id).
_PROGRESS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'retranslate_truncated_done.json')


def _msg_key(conv_id, entry):
    """Stable ledger key for one flagged message."""
    if entry.get('msgId'):
        return '%s:%s' % (conv_id, entry['msgId'])
    return '%s:idx:%d' % (conv_id, entry['idx'])


def _load_done():
    try:
        with open(_PROGRESS_PATH, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as e:
        logger.warning('[Retranslate] progress ledger unreadable (%s) — starting fresh', e)
        return set()


def _save_done(done):
    try:
        os.makedirs(os.path.dirname(_PROGRESS_PATH), exist_ok=True)
        tmp = _PROGRESS_PATH + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(sorted(done), f)
        os.replace(tmp, _PROGRESS_PATH)
    except OSError as e:
        logger.warning('[Retranslate] failed to persist progress ledger: %s', e)

from lib.database import DOMAIN_CHAT, pooled_db  # noqa: E402
from lib.database.conversation_repository import (  # noqa: E402
    iter_conversation_snapshots,
)
from lib.database.sqlite_owner import maintenance_write_authority  # noqa: E402
from lib.log import get_logger  # noqa: E402
from lib.translate import (  # noqa: E402
    _build_translate_prompt,
    _commit_translation_to_db,
    _extract_notranslate_blocks,
    _reattach_notranslate_blocks,
    _strip_notranslate_tags,
    _translate_freetext,
)
from lib.translate.constants import DEFAULT_USER_ID  # noqa: E402

logger = get_logger(__name__)


def _iter_conversations(conv_id=None):
    """Yield (conv_id, messages_list) for the target conversation(s)."""
    def _invalid(cid, exc):
        logger.warning(
            '[Retranslate] conv=%s failed repository integrity gate: %s',
            str(cid)[:8], exc)

    # Keep only one authoritative transcript materialized at a time. The
    # iterator selects stable IDs first and routes every transcript through the
    # row/archive authority gate, so this maintenance command cannot resurrect
    # the frozen conversations.messages mirror after cutover.
    with pooled_db(DOMAIN_CHAT) as db:
        for snapshot in iter_conversation_snapshots(
                db,
                user_id=DEFAULT_USER_ID,
                ids=[conv_id] if conv_id else None,
                order_by='id_asc',
                on_invalid=_invalid):
            yield str(snapshot['id']), snapshot.messages


def _find_truncated(messages, cutoff, min_content):
    """Return a list of flagged message dicts for one conversation.

    Each entry: {idx, msgId, role, content_len, tc_len, ratio, content}.
    """
    flagged = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        tc = msg.get('translatedContent')
        content = msg.get('content') or ''
        if not tc or not content:
            continue
        clen = len(content)
        tclen = len(tc)
        if clen < min_content:
            continue
        ratio = tclen / clen if clen else 0.0
        if ratio < cutoff:
            flagged.append({
                'idx': i,
                'msgId': msg.get('_msgId') or '',
                'role': msg.get('role', '?'),
                'content_len': clen,
                'tc_len': tclen,
                'ratio': ratio,
                'content': content,
            })
    return flagged


def _retranslate_one(content, source, target):
    """Run the chunked engine on one message's content.

    Mirrors ``lib/translate/runtime.py::_do_translate``: extract notranslate
    blocks, translate the remainder, reattach.  Returns (translated, model).
    """
    system_prompt = _build_translate_prompt(target, source)
    inner, nt_blocks = _extract_notranslate_blocks(content)
    if nt_blocks and not inner.strip():
        return _strip_notranslate_tags(content), 'skipped'

    translated, usage = _translate_freetext(
        inner if nt_blocks else content, system_prompt,
        chunk_label=':retranslate', source=source, target=target,
        use_cache=False,  # the on-disk cache may hold the OLD truncated result
    )
    translated = (translated or '').strip()
    if nt_blocks and translated:
        translated = _reattach_notranslate_blocks(translated, nt_blocks)

    model = 'unknown'
    if isinstance(usage, dict):
        disp = usage.get('_dispatch', {}) or {}
        model = disp.get('model', usage.get('model', 'unknown'))
    return translated, model


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true',
                    help='actually clear + re-translate (default: dry-run)')
    ap.add_argument('--conv', default=None,
                    help='limit to a single conversation id (default: all)')
    ap.add_argument('--cutoff', type=float, default=0.40,
                    help='flag messages with tc/content ratio below this (default 0.40)')
    ap.add_argument('--min-content', type=int, default=2000,
                    help='ignore messages whose content is shorter than this (default 2000)')
    ap.add_argument('--limit', type=int, default=0,
                    help='max messages to re-translate (0 = no limit)')
    ap.add_argument('--source', default='English',
                    help="source language of the assistant content (default 'English')")
    ap.add_argument('--target', default='Chinese',
                    help="target language of translatedContent (default 'Chinese')")
    ap.add_argument('--retry-done', action='store_true',
                    help='also re-attempt messages already in the progress ledger '
                         '(default: skip them so batched runs converge)')
    args = ap.parse_args()

    done = set() if args.retry_done else _load_done()

    print('=' * 72)
    print('Truncated-translation scan  (mode=%s, cutoff=%.0f%%, min_content=%d)' %
          ('APPLY' if args.apply else 'DRY-RUN', args.cutoff * 100, args.min_content))
    if args.conv:
        print('Scope: conversation %s' % args.conv)
    else:
        print('Scope: ALL conversations')
    print('=' * 72)

    convs_scanned = 0
    total_flagged = 0
    skipped_done = 0
    all_flagged = []  # (conv_id, flagged_entry)

    for cid, messages in _iter_conversations(args.conv):
        convs_scanned += 1
        flagged = _find_truncated(messages, args.cutoff, args.min_content)
        if not flagged:
            continue
        # Drop messages we've already attempted (unless --retry-done).
        pending = [f for f in flagged if _msg_key(cid, f) not in done]
        skipped_done += len(flagged) - len(pending)
        if not pending:
            continue
        total_flagged += len(pending)
        print('\nconv %s — %d flagged message(s):' % (cid, len(pending)))
        for f in pending:
            print('  msg[%d] role=%s id=%s  content=%d  tc=%d  ratio=%.1f%%' %
                  (f['idx'], f['role'], (f['msgId'][:8] or '-'),
                   f['content_len'], f['tc_len'], f['ratio'] * 100))
            all_flagged.append((cid, f))

    print('\n' + '-' * 72)
    print('Scanned %d conversation(s); flagged %d message(s); '
          'skipped %d already-attempted.' %
          (convs_scanned, total_flagged, skipped_done))

    if not args.apply:
        print('\nDRY-RUN only — nothing changed. Re-run with --apply to re-translate.')
        return

    if not all_flagged:
        print('\nNothing to do.')
        return

    if args.limit and len(all_flagged) > args.limit:
        print('\n--limit=%d → processing first %d of %d flagged messages.' %
              (args.limit, args.limit, len(all_flagged)))
        all_flagged = all_flagged[:args.limit]

    print('\nRe-translating %d message(s)…\n' % len(all_flagged))
    ok = 0
    failed = 0
    for n, (cid, f) in enumerate(all_flagged, 1):
        label = 'conv %s msg[%d] (%d chars)' % (cid[:8], f['idx'], f['content_len'])
        print('[%d/%d] %s …' % (n, len(all_flagged), label), end=' ', flush=True)
        t0 = time.time()
        # Mark as attempted up-front and persist immediately, so a crash /
        # quota-abort mid-batch never re-attempts this message on resume.
        done.add(_msg_key(cid, f))
        _save_done(done)
        try:
            translated, model = _retranslate_one(f['content'], args.source, args.target)
            if not translated or not translated.strip():
                print('FAILED (empty result)')
                logger.error('[Retranslate] empty result for conv=%s msg=%d',
                             cid[:8], f['idx'])
                failed += 1
                continue
            new_ratio = len(translated) / f['content_len'] if f['content_len'] else 0.0
            with maintenance_write_authority(
                    'retranslate truncated conversation message'):
                _commit_translation_to_db(
                    cid, f['idx'], 'translatedContent', translated,
                    original_text=f['content'], model=model,
                    msg_id=f['msgId'] or None,
                )
            print('OK  %d→%d chars (%.1f%%) model=%s in %.0fs' %
                  (f['tc_len'], len(translated), new_ratio * 100, model, time.time() - t0))
            logger.info('[Retranslate] conv=%s msg=%d re-translated %d→%d chars model=%s',
                        cid[:8], f['idx'], f['tc_len'], len(translated), model)
            ok += 1
        except Exception as e:
            print('FAILED (%s)' % e)
            logger.error('[Retranslate] conv=%s msg=%d failed: %s',
                         cid[:8], f['idx'], e, exc_info=True)
            failed += 1

    _save_done(done)
    print('\n' + '=' * 72)
    print('DONE — re-translated %d, failed %d (of %d flagged).' %
          (ok, failed, len(all_flagged)))
    print('Progress ledger: %d message(s) attempted total (%s).' %
          (len(done), _PROGRESS_PATH))


if __name__ == '__main__':
    main()
