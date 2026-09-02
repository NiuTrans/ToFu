"""Background worker for Babel-mode whole-paper translation.

Splits ``paper_text`` on paragraph boundaries (preferring ``\\n\\n``) so
individual sentences/equations don't get cut in half by a fixed-width
split. Streams LLM completions for each chunk, persists the joined result
to ``paper_translations`` on success.
"""

import re
import time
import uuid

import lib as _lib
from lib.llm_dispatch.api import dispatch_stream
from lib.log import get_logger

from .translate_runtime import (
    _LANG_NAMES,
    _TRANSLATE_CHUNK_SIZE,
    _append_translate_event,
    _cleanup_stale_translate_tasks,
    _translate_runtime,
)

logger = get_logger(__name__)


def _run_translate_task(task, paper_text):
    """Background worker: chunk + translate + persist.

    Splits ``paper_text`` on paragraph boundaries (preferring ``\\n\\n``) so
    individual sentences/equations don't get cut in half by a fixed-width
    split — this is one of the things the old client implementation got
    wrong.
    """
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

    # Paragraph-aware chunking: greedy fill up to _TRANSLATE_CHUNK_SIZE,
    # never breaking a paragraph mid-way unless it itself exceeds the cap.
    paragraphs = re.split(r'\n\n+', paper_text)
    chunks, buf = [], ''
    for para in paragraphs:
        if not para.strip():
            continue
        if len(para) > _TRANSLATE_CHUNK_SIZE:
            if buf:
                chunks.append(buf); buf = ''
            for i in range(0, len(para), _TRANSLATE_CHUNK_SIZE):
                chunks.append(para[i:i + _TRANSLATE_CHUNK_SIZE])
            continue
        if buf and len(buf) + len(para) + 2 > _TRANSLATE_CHUNK_SIZE:
            chunks.append(buf); buf = para
        else:
            buf = (buf + '\n\n' + para) if buf else para
    if buf:
        chunks.append(buf)

    total = len(chunks)
    progress = {'done': 0, 'total': total}
    _translate_runtime.update_fields(
        task_id, fields={'progress': progress}, only_if_status='running')
    _append_translate_event(task, {'type': 'status', 'status': 'running', 'total': total})
    logger.info('[Paper:Translate] Task %s — lang=%s chunks=%d hash=%s',
                task['task_id'], lang, total, task['paper_hash'])

    sys_prompt = (
        f'You are a professional academic translator. Translate the following '
        f'text into {target_name}. Preserve all formatting (Markdown, LaTeX/KaTeX '
        f'math, bullet structure), equations, and technical terms. Output ONLY '
        f'the translation — no preamble, no commentary.'
    )

    translated_parts = []
    try:
        for ci, chunk in enumerate(chunks):
            if task['abort_event'].is_set():
                logger.info('[Paper:Translate] Task %s aborted at chunk %d/%d',
                            task['task_id'], ci, total)
                _translate_runtime.abort(task_id)
                _translate_runtime.finish(
                    task_id,
                    terminal_event_fields={
                        'type': 'aborted',
                        'partial': '\n\n'.join(translated_parts),
                        'progress': dict(progress),
                    },
                )
                return

            messages = [
                {'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': chunk},
            ]
            collected = []
            try:
                from lib.llm.stream_result import (
                    require_verified_provider_stream_result,
                )
                require_verified_provider_stream_result(dispatch_stream(
                    messages,
                    on_content=lambda t: collected.append(t),
                    max_tokens=8192,
                    temperature=0,
                    prefer_model=model,
                    strict_model=bool(model),
                    log_prefix='[Paper:Translate]',
                ), context='paper translation chunk')
            except Exception as e:
                logger.warning('[Paper:Translate] Chunk %d/%d failed: %s', ci + 1, total, e)
                collected = [f'[Translation error for this section: {e}]']

            piece = ''.join(collected).strip()
            translated_parts.append(piece)
            full_text_so_far = '\n\n'.join(translated_parts)
            progress = {'done': ci + 1, 'total': total}
            _translate_runtime.update_fields(
                task_id,
                fields={'full_text': full_text_so_far, 'progress': progress},
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

        try:
            from lib.paper.artifact_repository import (
                PaperArtifactRepository,
                PaperTranslation,
            )
            PaperArtifactRepository(int(task['_userId'])).put_translation(
                PaperTranslation(
                    paper_hash=task['paper_hash'],
                    lang=lang,
                    text=full_text,
                    model=model or _lib.LLM_MODEL,
                    created_at=int(time.time()),
                ),
                command_id=f'paper.translation.upsert:{uuid.uuid4().hex}',
            )
            logger.info('[Paper:Translate] Task %s done — %d chars persisted',
                        task['task_id'], len(full_text))
        except Exception as e:
            logger.warning('[Paper:Translate] Persist failed: %s', e)

        _translate_runtime.finish(
            task_id,
            result=full_text,
            terminal_event_fields={'type': 'done', 'text': full_text},
        )

    except Exception as e:
        logger.error('[Paper:Translate] Task %s crashed: %s',
                     task['task_id'], e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model=model or '', context='paper-translate',
            source='routes.paper:translate',
        )
        _translate_runtime.finish(
            task_id,
            error=envelope,
            error_context='paper-translate',
        )
    finally:
        _cleanup_stale_translate_tasks()
