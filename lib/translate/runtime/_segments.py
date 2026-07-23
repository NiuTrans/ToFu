"""Segment-level translation helpers.

Reads the target message's ``segments`` from the DB and builds the
``{llmRound: 中文}`` map that stamps ``translatedText`` onto each
non-deliverable narration segment — the retro / on-open / manual / toggle
path's equivalent of the live incremental worker's per-round narration.

``_translate_segments_to_map`` is the SINGLE source of truth shared by the
live retro path (:func:`_build_segment_translation_map`) and the one-shot
backfill migration (``lib.translate.segment_backfill`` imports it from the
package facade), so the two never diverge on eligibility / notranslate /
already-Chinese handling.
"""

import json

from lib.database import DOMAIN_CHAT
from lib.log import get_logger
from lib.text_lang import is_predominantly_chinese

from ..constants import DEFAULT_USER_ID
from ..engine import _translate_freetext  # noqa: F401 (re-exported via facade; call site resolves dynamically)
from ..notranslate import _extract_notranslate_blocks, _reattach_notranslate_blocks

logger = get_logger(__name__)


def _read_message_segments(conv_id, msg_id, msg_idx):
    """Read the target assistant message's ``segments`` list from the DB.

    Resolves the message by stable id first (robust against concurrent
    inserts), then by position. Returns the segments list or ``None`` when the
    conversation / message / segments are absent (a pre-v36 row → the caller
    treats it as a no-op). Never raises — best-effort enrichment only.
    """
    try:
        from lib.database import get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=?',
            (conv_id, DEFAULT_USER_ID)
        ).fetchone()
        if not row:
            return None
        messages = json.loads(row['messages'] or '[]')
    except Exception as e:
        logger.warning('[Translate] segment read failed for conv=%s: %s',
                       (conv_id or '?')[:8], e)
        return None

    msg = None
    if msg_id:
        for candidate in messages:
            if isinstance(candidate, dict) and candidate.get('_msgId') == msg_id:
                msg = candidate
                break
    if msg is None and msg_idx is not None:
        try:
            idx = int(msg_idx)
        except (ValueError, TypeError) as e:
            logger.debug('[Translate] segment read: bad msg_idx %r: %s', msg_idx, e)
            idx = -1
        if 0 <= idx < len(messages):
            msg = messages[idx]
    if not isinstance(msg, dict):
        return None
    segs = msg.get('segments')
    return segs if isinstance(segs, list) and segs else None


def _build_segment_translation_map(conv_id, msg_id, msg_idx, system_prompt,
                                   source, target, progress_cb=None):
    """Translate each non-deliverable narration segment of the target message.

    Returns ``{llmRound: 中文}`` so ``_commit_translation_to_db`` can stamp
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
    segs = _read_message_segments(conv_id, msg_id, msg_idx)
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
        if seg.get('type') != 'text' or seg.get('deliverable'):
            continue
        if (seg.get('translatedText') or '').strip():
            continue  # enrich-only: never re-translate / overwrite
        lr = seg.get('llmRound')
        if lr is None:
            continue
        original = (seg.get('text') or '').strip()
        if not original:
            continue
        try:
            if is_predominantly_chinese(original):
                seg_map[lr] = original
            else:
                body, nt_blocks = _extract_notranslate_blocks(original)
                if not body.strip():
                    seg_map[lr] = original
                else:
                    # Resolve through the package facade so tests that
                    # monkeypatch ``lib.translate.runtime._translate_freetext``
                    # (as the pre-split single module allowed) are honoured.
                    import lib.translate.runtime as _rt_pkg
                    translated, _usage = _rt_pkg._translate_freetext(
                        body, system_prompt, source=source, target=target)
                    translated = (translated or '').strip()
                    if nt_blocks:
                        translated = _reattach_notranslate_blocks(translated, nt_blocks)
                    if translated:
                        seg_map[lr] = translated
        except Exception as e:
            logger.warning('[Translate] segment round=%s translate failed for '
                           '%s: %s', lr, log_tag, e)
        # ★ Progressive per-round push (unification): emit the accumulated map
        #   after each segment so the retro path streams round-by-round. Guarded
        #   + best-effort — a callback failure must never break the map build.
        if progress_cb is not None and lr in seg_map:
            try:
                progress_cb({str(rn): txt for rn, txt in seg_map.items()})
            except Exception as pe:
                logger.debug('[Translate] segment progress_cb failed for %s: %s',
                             log_tag, pe)
    if seg_map:
        logger.info('[Translate] built segment translation map for %s: '
                    '%d/%d narration segments', log_tag, len(seg_map), len(segs))
    return seg_map
