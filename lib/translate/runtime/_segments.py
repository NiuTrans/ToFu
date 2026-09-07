"""Segment-level translation helpers.

Reads the target turn projection's ``segments`` and builds the
``{llmRound: 中文}`` map that stamps ``translatedText`` onto each
non-deliverable narration segment — the retro / on-open / manual / toggle
path's equivalent of the live incremental worker's per-round narration.

``_translate_segments_to_map`` is the pure eligibility and translation core
used by whole-turn and incremental translation.  Consecutive cache misses are
translated in bounded batches so a settled long task does not spend one model
request per reasoning/narration segment.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from lib import translate_cache
from lib.log import get_logger
from lib.text_lang import is_predominantly_chinese

from ..engine import _translate_freetext
from ..notranslate import (
    _extract_notranslate_blocks,
    _has_translatable_text,
    _reattach_notranslate_blocks,
)

logger = get_logger(__name__)


# One request carries at most 16 short segments while staying below the
# translation engine's 6k-character medium-request threshold. Both dimensions
# are hard bounds: one pathological segment falls back to the existing
# singleton path instead of inflating every neighbor's request.
_SEGMENT_BATCH_MAX_ITEMS = 16
_SEGMENT_BATCH_MAX_CHARS = 6000
_SEGMENT_BOUNDARY_OVERHEAD_CHARS = 32
# Settled-turn segment text is reconstructible enrichment. It must never hold
# the already-completed whole-turn result behind the engine's default ten-
# minute deadline. One worker invocation shares this wall budget across its
# batch and any validation-only singleton fallback.
_SEGMENT_ENRICHMENT_DEADLINE_SECONDS = 15.0


class _SegmentBatchValidationError(ValueError):
    """A provider response arrived, but damaged segment boundary markers."""


def _raise_if_aborted(abort_check) -> None:
    if abort_check is not None and abort_check():
        from lib.llm import AbortedError

        raise AbortedError('Translation segment enrichment aborted')


@dataclass(frozen=True)
class _PendingSegmentTranslation:
    """One cache miss ready for a singleton or bounded batch dispatch."""

    key: Any
    body: str
    protected_blocks: tuple[dict, ...]


def _emit_progress(seg_map, progress_cb, log_tag):
    if progress_cb is None:
        return
    try:
        progress_cb({str(key): value for key, value in seg_map.items()})
    except Exception as exc:
        logger.debug(
            '[Translate] segment progress_cb failed for %s: %s',
            log_tag,
            exc,
        )


def _pending_batch_would_overflow(pending, candidate):
    if len(pending) >= _SEGMENT_BATCH_MAX_ITEMS:
        return True
    current_chars = sum(len(item.body) for item in pending)
    separator_chars = _SEGMENT_BOUNDARY_OVERHEAD_CHARS * len(pending)
    return bool(
        pending
        and current_chars + separator_chars + len(candidate.body)
        > _SEGMENT_BATCH_MAX_CHARS
    )


def _globalize_protected_placeholders(entry, next_marker_index):
    """Give one entry collision-free NT markers for a shared request."""
    body = entry.body
    marker_pairs = []
    for block in entry.protected_blocks:
        local_marker = str(block.get('placeholder') or '')
        if not local_marker or local_marker not in body:
            raise _SegmentBatchValidationError(
                'protected placeholder missing before batching')
        global_marker = f'⟦NT_{next_marker_index}⟧'
        next_marker_index += 1
        body = body.replace(local_marker, global_marker, 1)
        marker_pairs.append((global_marker, local_marker))
    return body, tuple(marker_pairs), next_marker_index


def _translate_segment_batch(
        entries, system_prompt, source, target, *, abort_check=None,
        overall_deadline=None, max_429_attempts=None,
        defer_on_shared_contention=False):
    """Translate 2+ entries in one call and prove every boundary survived.

    The existing translation prompt already treats ``⟦NT_N⟧`` as an immutable
    marker.  We allocate collision-free markers to protected blocks and to the
    boundaries between segments, then require every boundary exactly once and
    in source order.  Protected markers must also remain inside their owning
    segment.  Any model damage raises and lets the caller use the established
    per-segment path; incomplete or cross-wired text is never committed.
    """
    if len(entries) < 2:
        raise ValueError('segment batch requires at least two entries')

    next_marker_index = 0
    globalized_entries = []
    for entry in entries:
        body, marker_pairs, next_marker_index = (
            _globalize_protected_placeholders(entry, next_marker_index)
        )
        globalized_entries.append((entry, body, marker_pairs))

    boundary_markers = []
    combined_parts = []
    for index, (_entry, body, _marker_pairs) in enumerate(globalized_entries):
        if index:
            boundary = f'⟦NT_{next_marker_index}⟧'
            next_marker_index += 1
            boundary_markers.append(boundary)
            combined_parts.extend(('', boundary, ''))
        combined_parts.append(body)
    combined = '\n'.join(combined_parts)

    translated, usage = _translate_freetext(
        combined,
        system_prompt,
        source=source,
        target=target,
        abort_check=abort_check,
        overall_deadline=overall_deadline,
        max_429_attempts=max_429_attempts,
        defer_on_shared_contention=defer_on_shared_contention,
    )
    try:
        translated = str(translated or '').strip()
        if not translated:
            raise _SegmentBatchValidationError(
                'segment batch produced empty content')

        translated_parts = []
        cursor = 0
        for boundary in boundary_markers:
            if translated.count(boundary) != 1:
                raise _SegmentBatchValidationError(
                    'segment boundary was dropped or duplicated')
            position = translated.find(boundary, cursor)
            if position < cursor:
                raise _SegmentBatchValidationError(
                    'segment boundaries were reordered')
            translated_parts.append(translated[cursor:position].strip())
            cursor = position + len(boundary)
        translated_parts.append(translated[cursor:].strip())
        if len(translated_parts) != len(entries) or any(
                not part for part in translated_parts):
            raise _SegmentBatchValidationError(
                'segment batch returned an empty or missing segment')

        results = {}
        for (entry, _body, marker_pairs), part in zip(
                globalized_entries, translated_parts, strict=True):
            local_part = part
            for global_marker, local_marker in marker_pairs:
                if translated.count(global_marker) != 1:
                    raise _SegmentBatchValidationError(
                        'protected marker was dropped or duplicated')
                if global_marker not in local_part:
                    raise _SegmentBatchValidationError(
                        'protected marker crossed a segment boundary')
                local_part = local_part.replace(
                    global_marker, local_marker, 1)
            restored = _reattach_notranslate_blocks(
                local_part,
                list(entry.protected_blocks),
            ).strip()
            if not restored:
                raise _SegmentBatchValidationError(
                    'segment batch restored empty content')
            results[entry.key] = restored
    except _SegmentBatchValidationError:
        # The engine caches successful provider output before this stricter
        # caller-side boundary proof runs. Never let a malformed batch become
        # a persistent warning/fallback loop on every later page open.
        translate_cache.remove(combined, source, target)
        raise

    dispatch = usage.get('_dispatch', {}) if isinstance(usage, dict) else {}
    logger.debug(
        '[Translate] segment batch translated %d items/%d chars in one call '
        'model=%s',
        len(entries),
        len(combined),
        dispatch.get('model') or 'unknown',
    )
    return results


def _translate_pending_batch(
        entries, system_prompt, source, target, log_tag, *, abort_check=None,
        remaining_deadline_seconds=None, max_429_attempts=None,
        defer_on_shared_contention=False):
    """Translate one planned batch, falling back without weakening output."""
    def call_deadline():
        if remaining_deadline_seconds is None:
            return None
        remaining = remaining_deadline_seconds()
        if remaining is None:
            return None
        return max(0.0, float(remaining))

    def deadline_expired():
        remaining = call_deadline()
        return remaining is not None and remaining <= 0.0

    if len(entries) == 1:
        if deadline_expired():
            return {}
        entry = entries[0]
        translated, _usage = _translate_freetext(
            entry.body,
            system_prompt,
            source=source,
            target=target,
            abort_check=abort_check,
            overall_deadline=call_deadline(),
            max_429_attempts=max_429_attempts,
            defer_on_shared_contention=defer_on_shared_contention,
        )
        restored = _reattach_notranslate_blocks(
            str(translated or '').strip(),
            list(entry.protected_blocks),
        ).strip()
        return {entry.key: restored} if restored else {}

    try:
        return _translate_segment_batch(
            entries,
            system_prompt,
            source,
            target,
            abort_check=abort_check,
            overall_deadline=call_deadline(),
            max_429_attempts=max_429_attempts,
            defer_on_shared_contention=defer_on_shared_contention,
        )
    except _SegmentBatchValidationError as exc:
        logger.warning(
            '[Translate] segment batch validation failed for %s; '
            'falling back to %d isolated calls: %s',
            log_tag,
            len(entries),
            exc,
        )
    except Exception as exc:
        from lib.llm import AbortedError

        if isinstance(exc, AbortedError):
            raise
        logger.warning(
            '[Translate] optional segment batch dispatch failed for %s; '
            'skipping %d segments without isolated fan-out: %s',
            log_tag,
            len(entries),
            exc,
        )
        return {}

    results = {}
    for entry in entries:
        _raise_if_aborted(abort_check)
        if deadline_expired():
            logger.info(
                '[Translate] segment enrichment deadline reached for %s; '
                'skipping remaining isolated calls',
                log_tag,
            )
            break
        try:
            translated, _usage = _translate_freetext(
                entry.body,
                system_prompt,
                source=source,
                target=target,
                abort_check=abort_check,
                overall_deadline=call_deadline(),
                max_429_attempts=max_429_attempts,
                defer_on_shared_contention=defer_on_shared_contention,
            )
            restored = _reattach_notranslate_blocks(
                str(translated or '').strip(),
                list(entry.protected_blocks),
            ).strip()
            if restored:
                results[entry.key] = restored
        except Exception as exc:
            from lib.llm import AbortedError

            if isinstance(exc, AbortedError):
                raise
            logger.warning(
                '[Translate] segment key=%s translate failed for %s: %s',
                entry.key,
                log_tag,
                exc,
            )
    return results


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
                                   progress_cb=None, abort_check=None):
    """Translate each non-deliverable narration segment of the target message.

    Returns ``{blockId: translation}`` so the turn commit can stamp
    ``translatedText`` onto exactly one matching segment — making the retro /
    on-open / manual / toggle path interleave the settled timeline exactly like
    the live incremental worker does. Symmetric with
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
    seg_map = _translate_segments_to_map(
        segs,
        system_prompt,
        source,
        target,
        log_tag=(conv_id or '?')[:8],
        progress_cb=progress_cb,
        abort_check=abort_check,
        overall_deadline=_SEGMENT_ENRICHMENT_DEADLINE_SECONDS,
        max_429_attempts=1,
        defer_on_shared_contention=True,
    )
    return seg_map or None


def _translate_segments_to_map(segs, system_prompt, source, target, *,
                               log_tag='?', progress_cb=None,
                               abort_check=None, overall_deadline=None,
                               max_429_attempts=None,
                               defer_on_shared_contention=False):
    """Translate narration/reasoning segments into a stable block-id map.

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
    retained timeline both render ``translatedText`` for reasoning). Modern
    narration and reasoning are both keyed by collision-free ``blockId``;
    legacy narration without a block id falls back to its integer round key.
    This is required because ``llmRound`` restarts after Continue.

    ``progress_cb`` (optional): called with ``{str(blockId): 中文}`` — the
    accumulated map so far — after EACH narration segment finishes. This is the
    unification lever that makes the retro / on-open / manual path STREAM its
    per-round narration exactly like the live incremental worker's
    ``partialByRound`` frames, instead of landing every round at once at the
    end. No-op / pure when omitted (the backfill migration passes nothing).
    """
    seg_map = {}
    pending = []
    deadline_at = (
        time.monotonic() + max(0.0, float(overall_deadline))
        if overall_deadline is not None else None
    )

    def remaining_deadline_seconds():
        if deadline_at is None:
            return None
        return max(0.0, deadline_at - time.monotonic())

    def flush_pending():
        if not pending:
            return
        _raise_if_aborted(abort_check)
        try:
            translated = _translate_pending_batch(
                tuple(pending),
                system_prompt,
                source,
                target,
                log_tag,
                abort_check=abort_check,
                remaining_deadline_seconds=remaining_deadline_seconds,
                max_429_attempts=max_429_attempts,
                defer_on_shared_contention=defer_on_shared_contention,
            )
        except Exception as exc:
            from lib.llm import AbortedError

            pending.clear()
            if isinstance(exc, AbortedError):
                raise
            logger.warning(
                '[Translate] optional segment enrichment failed for %s; '
                'keeping partial map: %s',
                log_tag,
                exc,
            )
            return
        for entry in pending:
            value = translated.get(entry.key)
            if value:
                seg_map[entry.key] = value
                _emit_progress(seg_map, progress_cb, log_tag)
        pending.clear()

    for seg in (segs or []):
        _raise_if_aborted(abort_check)
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
            key = seg.get('blockId') or seg.get('llmRound')
            if key is None:
                continue
        if (seg.get('translatedText') or '').strip():
            continue  # enrich-only: never re-translate / overwrite
        original = (seg.get('text') or '').strip()
        if not original:
            continue
        try:
            if is_predominantly_chinese(original):
                flush_pending()
                seg_map[key] = original
                _emit_progress(seg_map, progress_cb, log_tag)
            else:
                body, nt_blocks = _extract_notranslate_blocks(original)
                if not _has_translatable_text(body):
                    flush_pending()
                    restored = _reattach_notranslate_blocks(
                        body, nt_blocks).strip()
                    if restored:
                        seg_map[key] = restored
                        _emit_progress(seg_map, progress_cb, log_tag)
                else:
                    cached = translate_cache.get(body, source, target)
                    cached_text = (
                        cached.get('translated')
                        if isinstance(cached, dict) else None
                    )
                    if cached_text:
                        flush_pending()
                        restored = _reattach_notranslate_blocks(
                            cached_text, nt_blocks).strip()
                        if restored:
                            seg_map[key] = restored
                            _emit_progress(seg_map, progress_cb, log_tag)
                    else:
                        candidate = _PendingSegmentTranslation(
                            key=key,
                            body=body,
                            protected_blocks=tuple(nt_blocks),
                        )
                        if _pending_batch_would_overflow(pending, candidate):
                            flush_pending()
                        pending.append(candidate)
        except Exception as e:
            from lib.llm import AbortedError

            if isinstance(e, AbortedError):
                raise
            logger.warning('[Translate] segment key=%s translate failed for '
                           '%s: %s', key, log_tag, e)
    flush_pending()
    if seg_map:
        logger.info('[Translate] built segment translation map for %s: '
                    '%d/%d segments', log_tag, len(seg_map), len(segs))
    return seg_map
