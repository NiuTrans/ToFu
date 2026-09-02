"""Segment-level translation helpers.

Reads the target turn projection's ``segments`` and builds the
``{llmRound: 中文}`` map that stamps ``translatedText`` onto each
non-deliverable narration segment — the retro / on-open / manual / toggle
path's equivalent of the live incremental worker's per-round narration.

``_translate_segments_to_map`` is the pure eligibility and translation core
used by whole-turn and incremental translation.
"""

from lib.log import get_logger
from lib.text_lang import is_predominantly_chinese

from ..engine import _translate_freetext
from ..notranslate import _extract_notranslate_blocks, _reattach_notranslate_blocks

logger = get_logger(__name__)


def _read_turn_segments(conv_id, turn_id, *, user_id):
    """Read narration segments from one owner-scoped turn projection."""
    try:
        from lib.turn_lifecycle import get_turn

        turn = get_turn(conv_id, turn_id, user_id=user_id)
    except Exception as e:
        logger.warning(
            '[Translate] segment read failed conv=%s turn=%s: %s',
            (conv_id or '?')[:8], (turn_id or '?')[:8], e,
        )
        return None

    projection = turn.get('projection') or {}
    segs = projection.get('segments')
    return segs if isinstance(segs, list) and segs else None


def _build_segment_translation_map(conv_id, turn_id, system_prompt,
                                   source, target, *, user_id,
                                   progress_cb=None):
    """Translate each non-deliverable narration segment of the target message.

    Returns ``{llmRound: translation}`` so the turn commit can stamp
    ``translatedText`` onto the matching segments — making the retro / on-open /
    manual / toggle path interleave the settled timeline exactly like the live
    incremental worker does. Symmetric with
    :meth:`lib.translate.incremental._Acc._do_finalize_inner`'s ``seg_trans``
    build: same per-segment notranslate extraction + already-Chinese skip.

    ``progress_cb`` (optional): forwarded to :func:`_translate_segments_to_map`
    so the caller can stream a ``partialByRound`` push frame after each round.

    A no-op returning ``None`` when the message has no segments (pre-v36 row).
    Per-segment failures are logged and skipped (the whole-message
    ``translatedContent`` commit is unaffected — this is pure enrichment).
    """
    segs = _read_turn_segments(conv_id, turn_id, user_id=user_id)
    if not segs:
        return None
    seg_map = _translate_segments_to_map(segs, system_prompt, source, target,
                                         log_tag=(conv_id or '?')[:8],
                                         progress_cb=progress_cb)
    return seg_map or None


def _translate_segments_to_map(segs, system_prompt, source, target, *,
                               log_tag='?', progress_cb=None):
    """Pure core: translate the non-deliverable narration segments → ``{llmRound: 中文}``.

    Shared by the live retro path (:func:`_build_segment_translation_map`, which
    reads ``segs`` from the DB first) and the one-shot backfill migration (which
    already holds ``segs``). Kept as a SINGLE source of truth so the two paths
    never diverge on which segments are translatable or how notranslate blocks /
    already-Chinese text are handled.

    ENRICH-ONLY: a segment that already carries a non-empty ``translatedText`` is
    skipped (not re-translated) — the map only contains rounds that gained a
    translation, so stamping is idempotent and cheap on re-run. ``tool_use`` and
    the deliverable/terminal ``text`` segment are excluded (the deliverable is
    rendered via ``translatedContent``). Per-segment failures are logged and
    skipped; returns ``{}`` when nothing was translatable.

    ``thinking`` segments ARE translated (the typed conversation surface and
    the retained timeline both render ``translatedText`` for reasoning), but
    they share their ``llmRound`` with the round's narration text, so they are
    keyed by their collision-free ``blockId`` (``thinking:llm-N`` /
    ``thinking:terminal``) instead of the round number.
    ``_stamp_segment_translations`` resolves text segments by round key and
    thinking segments by blockId key.

    ``progress_cb`` (optional): called with ``{str(llmRound): 中文}`` — the
    accumulated map so far — after EACH narration segment finishes. This is the
    unification lever that makes the retro / on-open / manual path STREAM its
    per-round narration exactly like the live incremental worker's
    ``partialByRound`` frames, instead of landing every round at once at the
    end. No-op / pure when omitted (the backfill migration passes nothing).
    """
    seg_map = {}
    for seg in (segs or []):
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get('type')
        if seg_type == 'thinking':
            # Reasoning shares llmRound with the round's narration prose, so
            # the round key cannot address both — the blockId can.
            key = seg.get('blockId')
            if not key:
                continue
        else:
            if seg_type != 'text' or seg.get('deliverable'):
                continue
            key = seg.get('llmRound')
            if key is None:
                continue
        if (seg.get('translatedText') or '').strip():
            continue  # enrich-only: never re-translate / overwrite
        original = (seg.get('text') or '').strip()
        if not original:
            continue
        try:
            if is_predominantly_chinese(original):
                seg_map[key] = original
            else:
                body, nt_blocks = _extract_notranslate_blocks(original)
                if not body.strip():
                    seg_map[key] = original
                else:
                    translated, _usage = _translate_freetext(
                        body, system_prompt, source=source, target=target)
                    translated = (translated or '').strip()
                    if nt_blocks:
                        translated = _reattach_notranslate_blocks(translated, nt_blocks)
                    if translated:
                        seg_map[key] = translated
        except Exception as e:
            logger.warning('[Translate] segment key=%s translate failed for '
                           '%s: %s', key, log_tag, e)
        # Progressive per-round push (unification): emit the accumulated map
        #   after each segment so the retro path streams round-by-round. Guarded
        #   + best-effort — a callback failure must never break the map build.
        if progress_cb is not None and key in seg_map:
            try:
                progress_cb({str(rn): txt for rn, txt in seg_map.items()})
            except Exception as pe:
                logger.debug('[Translate] segment progress_cb failed for %s: %s',
                             log_tag, pe)
    if seg_map:
        logger.info('[Translate] built segment translation map for %s: '
                    '%d/%d segments', log_tag, len(seg_map), len(segs))
    return seg_map
