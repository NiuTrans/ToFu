"""lib/translate/segment_backfill.py — narration-segment translation backfill.

THE GAP THIS CLOSES
-------------------
Auto-translate stamps per-round Chinese onto ``msg.segments[*].translatedText``
(read by the settled segment-timeline renderer) via two paths:

  • the LIVE incremental worker (``lib.translate.incremental``) — stamps each
    narration segment as its round closes, for forward-looking streaming turns;
  • the retro / whole-message path (``lib.translate.runtime._do_translate``) —
    builds ``{llmRound: 中文}`` and stamps too, but ONLY when it RUNS.

The retro path breaks early for a turn that already carries a non-stale
``translatedContent`` (its DELIVERABLE was translated), so a turn translated
before segments were stamped keeps its Chinese only in the bottom
``translatedContent`` blob — the interleaved narration falls back to English and
NEVER recovers on its own (no client/server path re-requests it, because both
``needsTranslation`` and the retro guard treat the turn as "done").

``_migrate_backfill_segment_translations`` fixes EXISTING rows once. This module
is the FORWARD fix: it is invoked on the conversation-GET path so that opening a
conversation with auto-translate on backfills any such ``_translateDone`` turn's
narration in the background (fire-and-forget), the exact "when I open a
conversation" behaviour.

SINGLE SOURCE OF TRUTH
----------------------
Reuses the EXACT translate core the live retro path uses
(``lib.translate.runtime._translate_segments_to_map`` — same eligibility,
notranslate handling, already-Chinese skip, enrich-only) and the EXACT stamp
helper (``lib.translate.commit._stamp_segment_translations``). It does NOT
re-implement "which segment is translatable" or "how the field is stamped". The
one-shot migration imports ``needs_segment_narration_translation`` from HERE so
the predicate is defined once.

REV-CAS NEUTRALITY
------------------
Stamping changes the ``messages`` column, which fires
``conversations_rev_bump_trg`` (rev → rev+1) and would make every open client
eat a spurious CAS 409. The write is guarded on the rev we read (skip on a
concurrent writer) and then resets rev back to that value in the SAME
transaction; ``updated_at`` is left untouched (no sidebar re-sort). Idempotent:
a re-run finds every narration already stamped → the core returns ``{}`` → no
write.

Thinking segments are intentionally never translated (backend contract — the
translator only stamps non-deliverable ``text`` narration segments).
"""

from __future__ import annotations

import asyncio
import os
import time as _time

from lib.log import audit_log, get_logger, log_context

logger = get_logger(__name__)

_TARGET = 'Chinese'
_SOURCE = ''

# ── Concurrency ceiling for OFF-LOOP backfill work ────────────────────────
# Every conversation OPEN can trigger one backfill. The translate core it runs
# is SYNCHRONOUS (requests-based LLM calls), so it MUST run off the event loop
# (via asyncio.to_thread) — running it inline once FROZE the serving event loop
# for the whole duration of a blocked upstream HTTP call (see JOURNAL
# 2026-07-15: faulthandler caught the loop wedged in ssl.read under this exact
# call chain). ``_INFLIGHT`` already dedups per-conv; this semaphore caps the
# number of backfills doing blocking work ACROSS convs, so a burst of opens
# cannot spawn unbounded to_thread workers and exhaust the default executor
# (which would trade one wedge for another — a starved request pool).
try:
    _MAX_CONCURRENT_BACKFILLS = max(
        1, int(os.environ.get('TOFU_NARRATION_BACKFILL_CONCURRENCY', '') or '2'))
except (ValueError, TypeError):
    _MAX_CONCURRENT_BACKFILLS = 2
try:
    _SLOW_BACKFILL_SECS = float(
        os.environ.get('TOFU_NARRATION_BACKFILL_SLOW_SECS', '') or '15')
except (ValueError, TypeError):
    _SLOW_BACKFILL_SECS = 15.0

# The semaphore binds to whichever event loop is running when first awaited
# (Python 3.10+ no longer takes a loop arg). Cache it per-loop so tests that
# spin up their own loop get a fresh one instead of a "bound to a different
# loop" error. Only ever touched on the (single-threaded) event loop → no lock.
_sem = None
_sem_loop = None


def _get_backfill_semaphore():
    """Return the process-wide backfill concurrency semaphore for THIS loop."""
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(_MAX_CONCURRENT_BACKFILLS)
        _sem_loop = loop
    return _sem


def _translate_and_stamp_eligible(eligible, system_prompt, tag):
    """Blocking: translate + stamp narration for each eligible message.

    Runs the SYNCHRONOUS (requests-based) translate core, so it MUST be invoked
    OFF the event loop (via :func:`asyncio.to_thread`). Mutates each message's
    ``segments`` in place (the same dict objects the caller then serialises) and
    returns ``(messages_stamped, segments_stamped)``. Never raises for a single
    bad segment — :func:`_translate_segments_to_map` logs and skips those.
    """
    from lib.translate.commit import _stamp_segment_translations
    from lib.translate.runtime import _translate_segments_to_map

    msgs_stamped = segs_stamped = 0
    for m in eligible:
        seg_map = _translate_segments_to_map(
            m.get('segments'), system_prompt, _SOURCE, _TARGET, log_tag=tag)
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
    return msgs_stamped, segs_stamped

# Process-wide in-flight guard: conv_ids with a backfill task currently running.


def _read_message(conv_id, msg_id, msg_idx):
    """Read the target assistant message dict from the DB (by id → position).

    Returns the message dict or ``None`` when the conversation / message is
    absent. Never raises — best-effort. Distinct from
    :func:`lib.translate.runtime._read_message_segments`, which returns only the
    ``segments`` list; here we need the whole message so the segment-less path
    can reach ``toolRounds`` and ``_translatePartialByRound``.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database.conversation_repository import load_conversation
    from lib.translate.constants import DEFAULT_USER_ID
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = load_conversation(db, conv_id, user_id=DEFAULT_USER_ID)
        if row is None:
            return None
        messages = row.messages
    except Exception as e:
        logger.warning('[narration-backfill] message read failed for conv=%s: %s',
                       (conv_id or '?')[:8], e)
        return None
    if msg_id:
        for cand in messages:
            if isinstance(cand, dict) and cand.get('_msgId') == msg_id:
                return cand
    if msg_idx is not None:
        try:
            idx = int(msg_idx)
        except (ValueError, TypeError) as _e:
            logger.debug('read message: unparseable/unexpected type (%s)', _e)
            idx = -1
        if 0 <= idx < len(messages):
            m = messages[idx]
            return m if isinstance(m, dict) else None
    return None


def _tool_round_narrations(msg):
    """Return ``{llmRound: english_narration}`` for a segment-less turn.

    The per-round assistant commentary of a turn persisted WITHOUT a ``segments``
    array (the reported bug: only ``toolRounds`` survived) lives in
    ``toolRounds[*].assistantContent``. Key by ``llmRound`` (≡ the round_num the
    incremental translator / segment timeline use). Skips tool-only rounds
    (empty ``assistantContent``). Returns ``{}`` when there are no toolRounds or
    none carry narration.
    """
    rounds = msg.get('toolRounds') if isinstance(msg, dict) else None
    if not isinstance(rounds, list) or not rounds:
        return {}
    out = {}
    for r in rounds:
        if not isinstance(r, dict):
            continue
        lr = r.get('llmRound')
        if lr is None:
            lr = r.get('roundNum')
        if lr is None:
            continue
        text = (r.get('assistantContent') or '').strip()
        if not text:
            continue
        # First narration wins for a given round (parallel-batch siblings share
        # an llmRound; the first carries the round's prose).
        out.setdefault(lr, text)
    return out


def _narration_map_from_tool_rounds(msg, system_prompt, source, target, *,
                                    log_tag='?'):
    """Build ``{llmRound: 中文}`` for a SEGMENT-LESS turn from its toolRounds.

    The durable path-independent fix for a turn persisted with ``toolRounds`` but
    no ``segments`` (so :func:`_read_message_segments` — and every narration
    path keyed on it — is blind). Two zero-drift sources, cheapest first:

      1. ``_translatePartialByRound`` — the Chinese the LIVE incremental worker
         already computed and persisted on the message before the turn was
         flagged already-target. Reused VERBATIM → ZERO LLM calls.
      2. For any round that field doesn't cover, translate the round's
         ``toolRounds[*].assistantContent`` via the SAME engine the segment core
         uses (notranslate handling + already-target skip), so the two paths
         never diverge on eligibility.

    Enrich-only: returns only rounds that gained Chinese. ``{}`` when the turn
    has no toolRounds narration. Never raises for a single bad round.
    """
    narrations = _tool_round_narrations(msg)
    if not narrations:
        return {}
    # Chinese already computed live and persisted on the message — reuse free.
    partial = msg.get('_translatePartialByRound')
    partial = partial if isinstance(partial, dict) else {}

    from lib.text_lang import is_predominantly_chinese
    from lib.translate.notranslate import (_extract_notranslate_blocks,
                                           _reattach_notranslate_blocks)
    import lib.translate.runtime as _rt_pkg

    seg_map = {}
    for lr, english in narrations.items():
        # (1) Reuse the persisted per-round translation (str-keyed on the wire).
        cached = partial.get(str(lr))
        if cached is None:
            cached = partial.get(lr)
        if isinstance(cached, str) and cached.strip():
            seg_map[lr] = cached.strip()
            continue
        # (2) Translate afresh — same core the segment path uses.
        try:
            if is_predominantly_chinese(english):
                seg_map[lr] = english
                continue
            body, nt_blocks = _extract_notranslate_blocks(english)
            if not body.strip():
                seg_map[lr] = english
                continue
            translated, _usage = _rt_pkg._translate_freetext(
                body, system_prompt, source=source, target=target)
            translated = (translated or '').strip()
            if nt_blocks:
                translated = _reattach_notranslate_blocks(translated, nt_blocks)
            if translated:
                seg_map[lr] = translated
        except Exception as e:
            logger.warning('[narration-backfill] toolRound round=%s translate '
                           'failed for %s: %s', lr, log_tag, e)
    return seg_map


def _synthesize_narration_segments(narrations, seg_map):
    """Build thin narration text segments to splice onto a segment-less message.

    Each round with narration becomes a non-deliverable ``text`` segment
    carrying the English ``text`` (from ``toolRounds[*].assistantContent``) plus
    its ``translatedText`` (from ``seg_map``) — enough for
    :func:`lib.translate.commit._stamp_segment_translations` and the settled
    ``renderSegmentTimelineHTML`` to render the interleaved narration
    bilingually. Ordered by ``llmRound`` so the timeline reads in round order.
    These are DISPLAY-side narration segments only — the full tool bodies still
    live in the sibling ``toolRounds`` column (single source of truth), so this
    is not a second source of truth for the tools.
    """
    segs = []
    for lr in sorted(seg_map.keys()):
        segs.append({'type': 'text', 'deliverable': False, 'llmRound': lr,
                     'text': narrations.get(lr, ''),
                     'translatedText': seg_map[lr]})
    return segs

# Process-wide in-flight guard: conv_ids with a backfill task currently running.
# The candidate gate reads the SERVED (uncommitted) messages, so re-opening a
# conv while its (multi-second, LLM-calling) backfill is still in flight would
# otherwise spawn a SECOND task that burns duplicate LLM calls for the same
# segments (rev-CAS keeps the write safe, but the work is wasted). asyncio is
# single-threaded, so a plain set checked-and-added before the first ``await``
# in :func:`backfill_conv_narration_segments` is atomic within the event loop.
_INFLIGHT: set = set()


def is_backfill_inflight(conv_id: str) -> bool:
    """True iff a backfill task for ``conv_id`` is currently running.

    Callers use this as a cheap pre-check to avoid spawning a task that would
    immediately no-op; the AUTHORITATIVE dedup lives inside
    :func:`backfill_conv_narration_segments` (two callers can both pass this
    check before either task starts).
    """
    return conv_id in _INFLIGHT


def needs_segment_narration_translation(msg) -> bool:
    """True iff ``msg`` is an already-translated assistant turn whose narration
    segments are missing ``translatedText`` (the backfill candidate condition).

    Pure read — mirrors the eligibility the live translate core enforces so a
    dry-run count matches what an apply would translate. Shared by the on-open
    trigger AND the one-shot migration (single source of truth).
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


def has_untranslated_narration(msg) -> bool:
    """True iff ``msg`` has ANY non-deliverable narration text segment whose
    ``translatedText`` is still empty — regardless of whether the DELIVERABLE
    (``translatedContent``) is translated.

    Superset of :func:`needs_segment_narration_translation` (which additionally
    requires ``translatedContent`` to be set, for the on-open backfill). This
    predicate is the path-independent gate: it also fires for a turn whose
    deliverable was already in the target language (so it has NO
    ``translatedContent``) yet whose interleaved narration is still English —
    the ``already in target language`` early-return case.
    """
    if not isinstance(msg, dict):
        return False
    segs = msg.get('segments')
    if isinstance(segs, list) and segs:
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
    # ★ SEGMENT-LESS turn (the reported bug): no segments array, so the
    #   per-round narration lives only in toolRounds[*].assistantContent. It is
    #   a candidate iff any narration round has no Chinese yet in
    #   ``_translatePartialByRound`` (the field the live worker persisted).
    narrations = _tool_round_narrations(msg)
    if not narrations:
        return False
    partial = msg.get('_translatePartialByRound')
    partial = partial if isinstance(partial, dict) else {}
    for lr in narrations:
        cached = partial.get(str(lr))
        if cached is None:
            cached = partial.get(lr)
        if not (isinstance(cached, str) and cached.strip()):
            return True
    return False


def backfill_message_narration_sync(conv_id, msg_idx, msg_id, target,
                                    source='English', *, log_tag='') -> int:
    """Path-independent TERMINAL narration backfill for ONE message (synchronous).

    Every auto-translate terminal path settles the DELIVERABLE
    (``translatedContent``) but only STAMPS the interleaved narration segments
    as a side-effect of the whole-message LLM branch actually running. The
    incremental-owned finalize (narration only from its live cache), and the
    whole-message ``already has translatedContent`` / ``already in target
    language`` early-returns, all leave untranslated narration in English. This
    helper makes the narration stamp a FIRST-CLASS, path-independent step: it
    reads the target message's segments from the DB, builds the
    ``{llmRound: 中文}`` map for any narration segment still missing
    ``translatedText`` (via the SHARED enrich-only core
    :func:`lib.translate.runtime._build_segment_translation_map`), and commits a
    STAMP-ONLY (``field=None``) write that leaves ``translatedContent`` /
    ``content`` untouched.

    Enrich-only + idempotent: a re-run finds every narration already stamped →
    the core returns ``{}`` → no write, no LLM call. Makes REAL LLM calls per
    still-missing narration segment, so callers run it OFF the hot path (a
    daemon thread / an existing background worker), never blocking the agent
    loop. Never raises — best-effort enrichment.

    Returns the number of narration segments stamped (0 = nothing to do).
    """
    tag = log_tag or (conv_id or '?')[:8]
    if not conv_id:
        return 0
    try:
        import lib.translate.runtime as _rt_pkg
        from lib.translate.commit import _commit_translation_to_db
        from lib.translate.prompt import _build_translate_prompt

        system_prompt = _build_translate_prompt(target, source)
        seg_map = _rt_pkg._build_segment_translation_map(
            conv_id, msg_id or '', msg_idx, system_prompt, source, target)
        if seg_map:
            # ── Path 1: the message HAS segments → stamp them in place. ──
            # Stamp-only commit (field=None): leaves translatedContent/content
            # alone, writes ONLY the per-round translatedText onto the segments.
            _commit_translation_to_db(conv_id, msg_idx, None, '',
                                      msg_id=msg_id or None,
                                      segment_translations=seg_map)
            logger.info('[narration-backfill] conv=%s stamped %d narration '
                        'segment(s) (target=%s)', tag, len(seg_map), target)
            try:
                audit_log('narration_segment_backfill', conv_id=conv_id,
                          segments=len(seg_map), target=target,
                          trigger='terminal')
            except Exception as ae:
                logger.debug('[narration-backfill] audit_log failed (non-fatal): %s', ae)
            return len(seg_map)

        # ── Path 2: SEGMENT-LESS turn (the reported bug) ──────────────────
        # ``_build_segment_translation_map`` returned nothing because the DB
        # message carries no ``segments`` array — the turn persisted with only
        # ``toolRounds`` (a crash/kill-recovery or live-persist write that never
        # assembled segments; the GET-path rehydrate is display-only and never
        # wrote them back). Every narration path keyed on ``msg.segments`` is
        # blind to it, so the interleaved English narration in ``toolRounds`` was
        # NEVER translatable — even though its Chinese may already sit unused in
        # ``_translatePartialByRound``. Recover it here: synthesise the narration
        # map from ``toolRounds[*].assistantContent`` (reusing that field, zero
        # LLM) and SPLICE thin narration segments onto the message in the same
        # self-heal commit so the settled timeline can render the Chinese.
        msg = _read_message(conv_id, msg_id or '', msg_idx)
        if not isinstance(msg, dict):
            return 0
        has_segments = isinstance(msg.get('segments'), list) and msg.get('segments')
        has_tool_rounds = isinstance(msg.get('toolRounds'), list) and msg.get('toolRounds')
        if has_segments or not has_tool_rounds:
            return 0
        # ★ TRACEABILITY (owner requirement): a turn with toolRounds but NO
        #   segments is exactly the "invisible narration" shape — make it one
        #   grep away next time instead of a DB dig. conv/msg/_taskId included.
        logger.warning('[narration-backfill] conv=%s msg_idx=%s msgId=%s taskId=%s '
                       'has toolRounds but no segments — narration was untranslatable '
                       'via the segment path; synthesising from toolRounds.assistantContent',
                       tag, msg_idx, (msg_id or '-')[:8],
                       msg.get('_taskId') or '-')
        seg_map = _narration_map_from_tool_rounds(
            msg, system_prompt, source, target, log_tag=tag)
        if not seg_map:
            return 0
        synth_segments = _synthesize_narration_segments(
            _tool_round_narrations(msg), seg_map)
        # Self-heal commit: fallback_segments splices the synthesised narration
        # segments onto the segment-less row in the SAME CAS write, then stamps.
        _commit_translation_to_db(conv_id, msg_idx, None, '',
                                  msg_id=msg_id or None,
                                  segment_translations=seg_map,
                                  fallback_segments=synth_segments)
        logger.info('[narration-backfill] conv=%s stamped %d narration segment(s) '
                    'synthesised from toolRounds (target=%s)', tag, len(seg_map), target)
        try:
            audit_log('narration_segment_backfill', conv_id=conv_id,
                      segments=len(seg_map), target=target, trigger='toolrounds')
        except Exception as ae:
            logger.debug('[narration-backfill] audit_log failed (non-fatal): %s', ae)
        return len(seg_map)
    except Exception as e:
        logger.warning('[narration-backfill] conv=%s failed (%s): %s',
                       tag, type(e).__name__, e, exc_info=True)
        return 0


def conv_has_backfill_candidates(messages) -> bool:
    """Cheap pre-check: does this conversation carry ANY backfill candidate?

    Used by the GET path to decide whether to spawn the (LLM-calling) background
    task at all — the common case (no candidates) does zero extra work.
    """
    return any(needs_segment_narration_translation(m)
               for m in (messages or []) if isinstance(m, dict))


async def backfill_conv_narration_segments(conv_id: str, *, log_tag: str = '') -> dict:
    """Translate + stamp missing narration ``translatedText`` for one conversation.

    Enrich-only, rev-CAS-neutral, best-effort. Reuses the live translate core +
    stamp helper (single source of truth). Makes REAL LLM calls per missing
    narration segment, so callers gate on :func:`conv_has_backfill_candidates`
    and run this OFF the request path (background task) — it must never block a
    GET.

    Returns a summary ``{convId, messagesStamped, segmentsStamped, skipped,
    wrote}``. Never raises: per-row failures are logged and folded into the
    summary so a bad conversation cannot break the caller.
    """
    from lib.translate.prompt import _build_translate_prompt

    tag = log_tag or (conv_id or '?')[:8]
    summary = {'convId': conv_id, 'messagesStamped': 0, 'segmentsStamped': 0,
               'skipped': False, 'wrote': False}
    # Authoritative in-flight dedup. This check-and-add runs BEFORE the first
    # ``await`` below, so on a single-threaded event loop it is atomic: a second
    # task spawned for the same conv while this one is mid-flight sees the id in
    # the set and no-ops instead of re-running the (LLM-calling) translate.
    if conv_id in _INFLIGHT:
        summary['skipped'] = True
        logger.debug('[segment-xlate-onopen] conv=%s backfill already in flight '
                     '— skipping duplicate spawn', tag)
        return summary
    _INFLIGHT.add(conv_id)
    try:
        def _load_offloop():
            from lib.database import DOMAIN_CHAT, get_thread_db
            from lib.database.conversation_repository import load_conversation
            return load_conversation(get_thread_db(DOMAIN_CHAT), conv_id)

        row = await asyncio.to_thread(_load_offloop)
        if row is None:
            return summary
        messages = row.messages
        eligible = [m for m in messages
                    if needs_segment_narration_translation(m)]
        if not eligible:
            return summary
        # Phase 5: the positions narration-stamping will touch (computed
        # pre-stamp — same objects/criteria as `eligible`).
        eligible_seqs = [i for i, m in enumerate(messages)
                         if needs_segment_narration_translation(m)]

        system_prompt = _build_translate_prompt(_TARGET, _SOURCE)
        # ★ ROOT-CAUSE FIX: the translate core is SYNCHRONOUS (requests-based
        #   blocking HTTP). Run it OFF the event loop via ``asyncio.to_thread``
        #   under a bounded semaphore so (a) a blocked upstream can never freeze
        #   the serving loop, and (b) a burst of conversation-opens cannot spawn
        #   unbounded blocking workers. ``log_context`` records start/end/
        #   duration; a slow run additionally logs a WARNING (§2.3).
        _t0 = _time.monotonic()
        async with _get_backfill_semaphore():
            with log_context('narration_backfill', logger=logger):
                msgs_stamped, segs_stamped = await asyncio.to_thread(
                    _translate_and_stamp_eligible, eligible, system_prompt, tag)
        _elapsed = _time.monotonic() - _t0
        if _elapsed >= _SLOW_BACKFILL_SECS:
            logger.warning('[narration_backfill] conv=%s SLOW: %.1fs for %d msg / '
                           '%d seg (threshold=%.0fs) — off-loop, did NOT block the '
                           'serving loop', tag, _elapsed, msgs_stamped, segs_stamped,
                           _SLOW_BACKFILL_SECS)
        if segs_stamped == 0:
            return summary

        try:
            expected_rev = int(row['rev'] or 0)
        except (TypeError, ValueError) as e:
            logger.debug('[SegmentBackfill] rev int parse failed, using fallback: %s', e)
            expected_rev = 0

        def _persist_offloop():
            from lib.database import DOMAIN_CHAT, get_thread_db
            from lib.database.conversation_repository import replace_messages
            return replace_messages(
                get_thread_db(DOMAIN_CHAT), conv_id, messages,
                expected_rev=expected_rev,
                changed_seqs=eligible_seqs,
                preserve_rev=True,
            ).applied

        if not await asyncio.to_thread(_persist_offloop):
            summary['skipped'] = True
            logger.info('[segment-xlate-onopen] conv=%s skipped — concurrent '
                        'writer moved rev; next open retries', tag)
            return summary

        summary.update(messagesStamped=msgs_stamped, segmentsStamped=segs_stamped,
                       wrote=True)
        logger.info('[segment-xlate-onopen] conv=%s stamped %d segment(s) on '
                    '%d message(s) (rev held at %d)', tag, segs_stamped,
                    msgs_stamped, expected_rev)
        try:
            audit_log('conversation_segment_translation_backfill', conv_id=conv_id,
                      segments_stamped=segs_stamped, messages=msgs_stamped,
                      rev=expected_rev, trigger='on_open')
        except Exception as e:
            logger.debug('[segment-xlate-onopen] audit_log failed (non-fatal): %s', e)
        return summary
    except Exception as e:
        logger.error('[segment-xlate-onopen] conv=%s failed (%s): %s',
                     tag, type(e).__name__, e, exc_info=True)
        return summary
    finally:
        _INFLIGHT.discard(conv_id)
