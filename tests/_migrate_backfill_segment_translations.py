#!/usr/bin/env python3
"""One-shot backfill: STAMP per-round ``translatedText`` onto the narration
segments of conversations that were ALREADY translated before the write-path
fix landed.

WHY
---
Auto-translate has two commit paths that populate the settled interleaved
render (``msg.segments[*].translatedText``, read by the segment-timeline
renderer):

  • The live incremental worker (``lib.translate.incremental``) stamps each
    narration segment as its round closes — forward-looking, streaming turns.
  • The whole-message / retro path (``lib.translate.runtime._do_translate``)
    now BUILDS the ``{llmRound: 中文}`` map and stamps too — but only when it
    RUNS. It breaks early for a conversation that already has a non-stale
    ``translatedContent`` committed, so it will NOT re-run for history that was
    translated before the fix.

So every conversation translated before the fix keeps its Chinese only in the
bottom ``translatedContent`` blob, with the narration segments un-stamped →
the settled render falls back to English narration and the Chinese "clusters at
the bottom" exactly as reported. A write-path fix is incomplete without a
backfill (the conv-OOM lesson): this migration walks existing conversations
ONCE, translates their narration segments, and stamps ``translatedText`` so the
historical turns interleave in place permanently.

SINGLE SOURCE OF TRUTH
----------------------
Reuses the EXACT translate core the live retro path uses
(``lib.translate.runtime._translate_segments_to_map`` — same segment
eligibility, notranslate extraction, already-Chinese skip, enrich-only) and the
EXACT stamp helper the commit path uses
(``lib.translate.commit._stamp_segment_translations``). It does NOT re-implement
"which segment gets translated" or "how the field is stamped" — a divergent copy
would drift from the render-feeding write path.

ELIGIBILITY (a conversation/message is a candidate iff)
-------------------------------------------------------
  • role == 'assistant' AND has a non-empty ``translatedContent`` (already
    translated — we are enriching, not initiating translation), AND
  • carries a ``segments`` list containing at least one NON-deliverable
    ``text`` segment with an ``llmRound`` and NO (non-empty) ``translatedText``.
Pre-v36 rows (no segments) and messages whose narration is already stamped are
skipped — no write.

REV-CAS NEUTRALITY (identical discipline to _migrate_backfill_segments_from_task_results)
-----------------------------------------------------------------------------------------
Stamping genuinely changes the ``messages`` column, which would fire
``conversations_rev_bump_trg`` (rev → rev+1) and make every open client eat a
spurious CAS 409. The write is done in ONE transaction that (1) UPDATEs
``messages`` guarded on the rev we read (skip on a concurrent writer), then
(2) resets ``rev`` back to that value — the rev-only reset does not touch
``messages`` so it does not re-fire the ``OF messages``-scoped trigger.
``updated_at`` is left untouched → no sidebar re-sort, no cross-device
staleness flip. Idempotent: a re-run finds every narration already stamped →
``_translate_segments_to_map`` returns ``{}`` → no write.

SAFETY
------
  • Dry-run by default: prints per-conv counts, writes nothing AND makes NO LLM
    calls. ``--apply`` translates + writes.
  • Enrich-only: never overwrites an existing ``translatedText``, never touches
    ``content`` / ``translatedContent`` / any other field, never drops a message.
  • Per-row isolation: one bad row logs + is skipped without aborting the batch.
  • Makes REAL LLM calls per narration segment under ``--apply`` — scope with
    ``--id`` / ``--limit`` and review the dry-run counts first.

Usage:
    python tests/_migrate_backfill_segment_translations.py                  # dry-run, all rows
    python tests/_migrate_backfill_segment_translations.py --id mrak...      # one row
    python tests/_migrate_backfill_segment_translations.py --limit 50        # first 50 rows
    python tests/_migrate_backfill_segment_translations.py --apply           # TRANSLATE + WRITE
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import async_fetchall, async_fetchone  # noqa: E402
from lib.database.aio import async_transaction  # noqa: E402
from lib.database._wrappers import json_dumps_pg  # noqa: E402
from lib.log import audit_log, get_logger  # noqa: E402
from lib.translate.commit import _stamp_segment_translations  # noqa: E402
from lib.translate.prompt import _build_translate_prompt  # noqa: E402
from lib.translate.runtime import _translate_segments_to_map  # noqa: E402

logger = get_logger(__name__)

# Auto-translate direction is hard-pinned to Chinese project-wide (see
# translation.js / routes/translate.py rule 0). Source unspecified → the engine
# auto-detects per segment.
_TARGET = 'Chinese'
_SOURCE = ''


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


def _needs_segment_translation(msg):
    """True iff this assistant message is already translated but has narration
    segments lacking ``translatedText`` (the backfill candidate condition).

    Pure read — mirrors the eligibility the live core enforces so dry-run counts
    match what ``--apply`` would translate.
    """
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        return False
    if not (msg.get('translatedContent') or '').strip():
        return False
    segs = msg.get('segments')
    if not isinstance(segs, list) or not segs:
        return False
    for seg in segs:
        if not isinstance(seg, dict):
            continue
        if seg.get('type') != 'text' or seg.get('deliverable'):
            continue
        if seg.get('llmRound') is None:
            continue
        if not (seg.get('text') or '').strip():
            continue
        if not (seg.get('translatedText') or '').strip():
            return True
    return False


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


async def _rev_neutral_write(cid, messages_json, expected_rev):
    """Persist the stamped messages while keeping ``rev`` unchanged.

    The messages UPDATE fires ``conversations_rev_bump_trg`` (rev → rev+1); we
    reset rev to ``expected_rev`` in the SAME transaction. Guarded on
    ``expected_rev`` so a concurrent writer's change is not clobbered.

    Returns True if written, False if a concurrent writer moved rev (skip).
    """
    async with async_transaction() as conn:
        cur = await conn.execute(
            'UPDATE conversations SET messages=? WHERE id=? AND rev=?',
            (messages_json, cid, expected_rev))
        if getattr(cur, 'rowcount', 0) == 0:
            return False
        await conn.execute(
            'UPDATE conversations SET rev=? WHERE id=?', (expected_rev, cid))
    return True


async def run(apply, only_id, limit):
    candidates = await _candidate_ids(only_id, limit)
    mode = 'APPLY' if apply else 'DRY-RUN'
    print(f'\n  ═══ backfill-segment-translations [{mode}] — '
          f'{len(candidates)} candidate conversation(s) ═══\n')
    if not candidates:
        print('  (no rows match)\n')
        return

    system_prompt = _build_translate_prompt(_TARGET, _SOURCE)
    convs_touched = 0
    msgs_stamped_total = 0
    segs_stamped_total = 0
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

            eligible = [m for m in messages if _needs_segment_translation(m)]
            if not eligible:
                continue

            # ── DRY-RUN: report eligibility only, make NO LLM calls ──
            if not apply:
                seg_est = 0
                for m in eligible:
                    for s in m.get('segments', []):
                        if (isinstance(s, dict) and s.get('type') == 'text'
                                and not s.get('deliverable')
                                and s.get('llmRound') is not None
                                and (s.get('text') or '').strip()
                                and not (s.get('translatedText') or '').strip()):
                            seg_est += 1
                print(f'  {cid:20s}  {len(eligible):2d} msg(s), '
                      f'~{seg_est:3d} narration segment(s) to translate')
                convs_touched += 1
                msgs_stamped_total += len(eligible)
                segs_stamped_total += seg_est
                continue

            # ── APPLY: translate + stamp in memory ──
            msgs_stamped = 0
            segs_stamped = 0
            for m in eligible:
                seg_map = _translate_segments_to_map(
                    m.get('segments'), system_prompt, _SOURCE, _TARGET,
                    log_tag=cid[:8])
                if not seg_map:
                    continue
                before = sum(1 for s in m.get('segments', [])
                             if isinstance(s, dict) and (s.get('translatedText') or '').strip())
                _stamp_segment_translations(m, seg_map)
                after = sum(1 for s in m.get('segments', [])
                            if isinstance(s, dict) and (s.get('translatedText') or '').strip())
                gained = after - before
                if gained > 0:
                    msgs_stamped += 1
                    segs_stamped += gained
            if segs_stamped == 0:
                continue  # nothing actually translatable (all already-Chinese matched existing?)

            expected_rev = 0
            try:
                expected_rev = int(row['rev'] or 0)
            except (TypeError, ValueError):
                expected_rev = 0

            messages_json = json_dumps_pg(messages)
            wrote = await _rev_neutral_write(cid, messages_json, expected_rev)
            if not wrote:
                skipped_concurrent += 1
                print(f'  {cid:20s}  (skipped — concurrent writer moved rev; next run retries)')
                continue

            print(f'  {cid:20s}  stamped {segs_stamped:3d} segment(s) on '
                  f'{msgs_stamped:2d} msg(s)  (rev stays {expected_rev})')
            convs_touched += 1
            msgs_stamped_total += msgs_stamped
            segs_stamped_total += segs_stamped
            logger.info('[segment-xlate-backfill] conv=%s stamped %d segment(s) on '
                        '%d message(s) (rev held at %d)', cid, segs_stamped,
                        msgs_stamped, expected_rev)
            try:
                audit_log('conversation_segment_translation_backfill', conv_id=cid,
                          segments_stamped=segs_stamped, messages=msgs_stamped,
                          rev=expected_rev)
            except Exception as e:
                logger.debug('[segment-xlate-backfill] audit_log failed (non-fatal): %s', e)
        except Exception as e:
            errored += 1
            logger.error('[segment-xlate-backfill] row %s failed (%s): %s — skipped',
                         cid, type(e).__name__, e, exc_info=True)
            print(f'  {cid:20s}  ERROR ({type(e).__name__}: {e}) — skipped')

    print(f'\n  ─── {mode} summary ───')
    print(f'    conversations {"enriched" if apply else "eligible"} : {convs_touched}')
    print(f'    messages {"stamped" if apply else "eligible"}       : {msgs_stamped_total}')
    print(f'    narration segments {"stamped" if apply else "to translate"} : {segs_stamped_total}')
    if apply:
        print(f'    skipped (concurrent rev move) : {skipped_concurrent}')
    print(f'    rows errored (skipped) : {errored}')
    if not apply and convs_touched:
        print('\n  (dry-run — no rows written, no LLM calls. Re-run with --apply to translate + write.)')
    print()


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--apply', action='store_true', help='translate + write (default: dry-run)')
    p.add_argument('--id', default='', help='restrict to a single conversation id')
    p.add_argument('--limit', type=int, default=0, help='cap number of rows processed (0 = all)')
    args = p.parse_args()
    asyncio.run(run(args.apply, args.id or '', args.limit))


if __name__ == '__main__':
    main()
