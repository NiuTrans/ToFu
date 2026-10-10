"""Bounded whole-paper translation worker.

Responsibility: split one paper into large semantic slices, translate each
slice through the shared validated translation engine, and publish exactly one
owner-scoped artifact only after every slice succeeds.  The task runtime owns
progress/cancellation; :mod:`lib.translate.engine` owns provider selection,
cache/MT routing, retries, and content-quality validation.
"""

import re
import time
import uuid

import lib as _lib
from lib.log import get_logger
from lib.paper.contracts import (
    PAPER_TRANSLATION_MAX_OUTPUT_BYTES,
    PAPER_TRANSLATION_MAX_OUTPUT_CHARS,
)
from lib.translate.engine import TranslationContentRefused, _translate_one_chunk

from .translate_runtime import (
    _LANG_NAMES,
    _TRANSLATE_CHUNK_SIZE,
    _TRANSLATE_MAX_CHUNKS,
    _TRANSLATE_MAX_SOURCE_CHARS,
    _TRANSLATE_TASK_DEADLINE_SECONDS,
    _append_translate_event,
    _cleanup_stale_translate_tasks,
    _translate_runtime,
)

logger = get_logger(__name__)


# Sentence endings are preferred close to the slice ceiling.  Whitespace is a
# secondary boundary and a hard cut is the final fallback for minified/code-
# heavy input.  The zero-width alternative handles Chinese prose, where the
# next sentence commonly starts immediately after ``。`` without a space.
_TRANSLATION_BOUNDARY_RE = re.compile(
    r'(?<=[。！？；.!?])(?:[ \t]+|\n+)?'
)
_TRANSLATION_WHITESPACE_RE = re.compile(r'\s+')


def _split_oversized_translation_block(text: str, max_chars: int) -> list[str]:
    """Split one paragraph while keeping every non-final slice near the cap."""
    pieces = []
    remaining = text.strip()
    preferred_floor = max(1, int(max_chars * 0.75))
    while len(remaining) > max_chars:
        window = remaining[:max_chars + 1]
        sentence_cuts = [
            match.end()
            for match in _TRANSLATION_BOUNDARY_RE.finditer(window)
            if preferred_floor <= match.end() <= max_chars
        ]
        if sentence_cuts:
            cut_at = sentence_cuts[-1]
        else:
            whitespace_cuts = [
                match.end()
                for match in _TRANSLATION_WHITESPACE_RE.finditer(window)
                if preferred_floor <= match.end() <= max_chars
            ]
            cut_at = whitespace_cuts[-1] if whitespace_cuts else max_chars
        piece = remaining[:cut_at].strip()
        if not piece:
            cut_at = max_chars
            piece = remaining[:cut_at]
        pieces.append(piece)
        remaining = remaining[cut_at:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _split_paper_translation_chunks(
    text: str, *, max_chars: int = _TRANSLATE_CHUNK_SIZE,
) -> list[str]:
    """Return paragraph/sentence-aware, non-empty slices bounded by ``max_chars``."""
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
        raise ValueError('paper translation max_chars must be a positive integer')
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r'\n[ \t]*\n+', text.strip())
        if paragraph.strip()
    ]
    chunks: list[str] = []
    buffer = ''
    for paragraph in paragraphs:
        for piece in _split_oversized_translation_block(paragraph, max_chars):
            candidate = f'{buffer}\n\n{piece}' if buffer else piece
            if len(candidate) <= max_chars:
                buffer = candidate
                continue
            if buffer:
                chunks.append(buffer)
            buffer = piece
    if buffer:
        chunks.append(buffer)
    if any(not chunk or len(chunk) > max_chars for chunk in chunks):
        raise AssertionError('paper translation splitter violated its size invariant')
    return chunks


def _translation_model_from_usage(usage) -> str:
    """Extract the provider/model provenance attached by the shared engine."""
    if not isinstance(usage, dict):
        return ''
    trace = usage.get('_translate_trace')
    dispatch = usage.get('_dispatch')
    candidates = (
        trace.get('model') if isinstance(trace, dict) else '',
        dispatch.get('model') if isinstance(dispatch, dict) else '',
        usage.get('model'),
    )
    for candidate in candidates:
        normalized = str(candidate or '').strip()
        if normalized and normalized not in {'?', 'cache'}:
            return normalized
    return ''


def _run_translate_task(task, paper_text):
    """Translate every bounded slice, then atomically publish one artifact."""
    task_id = task['task_id']
    _translate_runtime.mark_running(task_id)
    lang = task['lang']
    # `lang` is normally a bare code ('zh'/'ja'/…) but a caller may pass a
    # COMPOSITE cache key (e.g. a review translation keyed 'review:neurips:zh')
    # to keep it out of the whole-paper translation cache. The human
    # target-language name comes from the FINAL ':'-segment; the composite key
    # itself remains the distinct (paper_hash, lang) cache row.
    real_lang = lang.rsplit(':', 1)[-1] if ':' in lang else lang
    target_name = _LANG_NAMES.get(real_lang, real_lang)
    model = task['model'] or None
    translated_parts = []
    translated_chars = 0
    translated_bytes = 0
    progress = {'done': 0, 'total': 0}
    try:
        if len(paper_text) > _TRANSLATE_MAX_SOURCE_CHARS:
            raise ValueError(
                f'paper_text exceeds {_TRANSLATE_MAX_SOURCE_CHARS} characters')
        chunks = _split_paper_translation_chunks(paper_text)
        if not chunks:
            raise ValueError('paper_text has no translatable content')
        if len(chunks) > _TRANSLATE_MAX_CHUNKS:
            raise ValueError(
                f'paper translation requires {len(chunks)} chunks; '
                f'limit is {_TRANSLATE_MAX_CHUNKS}')

        total = len(chunks)
        progress = {'done': 0, 'total': total}
        _translate_runtime.update_fields(
            task_id, fields={'progress': progress}, only_if_status='running')
        _append_translate_event(
            task, {'type': 'status', 'status': 'running', 'total': total})
        logger.info('[Paper:Translate] Task %s — lang=%s chunks=%d hash=%s',
                    task_id, lang, total, task['paper_hash'])

        sys_prompt = (
            f'You are a professional academic translator. Translate the following '
            f'text into {target_name}. Preserve all formatting (Markdown, LaTeX/KaTeX '
            f'math, bullet structure), equations, and technical terms. Output ONLY '
            f'the translation — no preamble, no commentary.'
        )
        task_deadline_at = (
            time.monotonic() + _TRANSLATE_TASK_DEADLINE_SECONDS)
        use_chunk_cache = not bool(task.get('force')) and model is None
        resolved_models = []

        for ci, chunk in enumerate(chunks):
            if task['abort_event'].is_set():
                raise RuntimeError('paper translation aborted by owner')
            remaining_seconds = task_deadline_at - time.monotonic()
            if remaining_seconds <= 0:
                raise TimeoutError(
                    f'paper translation exceeded '
                    f'{_TRANSLATE_TASK_DEADLINE_SECONDS}s deadline')

            piece, usage = _translate_one_chunk(
                chunk,
                sys_prompt,
                chunk_label=f':paper-{ci + 1}/{total}',
                source='',
                target=target_name,
                overall_deadline=min(600.0, remaining_seconds),
                use_cache=use_chunk_cache,
                abort_check=task['abort_event'].is_set,
                prefer_model=model,
                strict_model=bool(model),
                allow_mt=not bool(model),
                stream=True,
                capability='text',
                temperature=0,
                accept_truncated=False,
            )
            if not isinstance(piece, str) or not piece.strip():
                raise ValueError(
                    f'empty translation result for paper chunk {ci + 1}/{total}')
            piece = piece.strip()
            resolved_model = _translation_model_from_usage(usage)
            if resolved_model:
                resolved_models.append(resolved_model)
            separator_size = 2 if translated_parts else 0
            next_chars = translated_chars + separator_size + len(piece)
            next_bytes = (
                translated_bytes + separator_size + len(piece.encode('utf-8')))
            if next_chars > PAPER_TRANSLATION_MAX_OUTPUT_CHARS:
                raise ValueError(
                    'paper translation output exceeds '
                    f'{PAPER_TRANSLATION_MAX_OUTPUT_CHARS} characters')
            if next_bytes > PAPER_TRANSLATION_MAX_OUTPUT_BYTES:
                raise ValueError(
                    'paper translation output exceeds '
                    f'{PAPER_TRANSLATION_MAX_OUTPUT_BYTES} UTF-8 bytes')
            translated_parts.append(piece)
            translated_chars = next_chars
            translated_bytes = next_bytes
            progress = {'done': ci + 1, 'total': total}
            _translate_runtime.update_fields(
                task_id,
                fields={'progress': progress},
                only_if_status='running',
            )
            _append_translate_event(task, {
                'type': 'chunk', 'index': ci, 'total': total,
                'text': piece,
            })

        full_text = '\n\n'.join(translated_parts)
        _translate_runtime.update_fields(
            task_id,
            fields={'full_text': full_text, 'progress': progress},
            only_if_status='running',
        )

        if task['abort_event'].is_set():
            raise RuntimeError('paper translation aborted before persistence')
        from lib.paper.artifact_repository import (
            PaperArtifactRepository,
            PaperTranslation,
        )
        non_identity_models = [
            item for item in resolved_models if item != 'identity']
        artifact_model = (
            model
            or (non_identity_models[-1] if non_identity_models else '')
            or (resolved_models[-1] if resolved_models else '')
            or _lib.LLM_MODEL
        )
        saved = PaperArtifactRepository(
            int(task['_userId'])).put_translation(
                PaperTranslation(
                    paper_hash=task['paper_hash'],
                    lang=lang,
                    text=full_text,
                    model=artifact_model,
                    created_at=int(time.time()),
                ),
                command_id=f'paper.translation.upsert:{uuid.uuid4().hex}',
            )
        if not saved:
            raise RuntimeError('paper translation repository did not confirm persistence')
        logger.info('[Paper:Translate] Task %s done — %d chars persisted',
                    task_id, len(full_text))

        _translate_runtime.finish(
            task_id,
            terminal_event_fields={'type': 'done', 'text': full_text},
        )

    except Exception as e:
        if task['abort_event'].is_set():
            _translate_runtime.finish(
                task_id,
                terminal_event_fields={
                    'type': 'aborted',
                    'partial': '\n\n'.join(translated_parts),
                    'progress': dict(progress),
                },
            )
            logger.info('[Paper:Translate] Task %s aborted during dispatch',
                        task_id)
            return
        logger.error('[Paper:Translate] Task %s crashed: %s',
                     task_id, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model=model or '', context='paper-translate',
            source='routes.paper:translate',
            kind=('content_refused'
                  if isinstance(e, TranslationContentRefused) else None),
        )
        _translate_runtime.finish(
            task_id,
            error=envelope,
            error_context='paper-translate',
        )
    finally:
        _cleanup_stale_translate_tasks()
