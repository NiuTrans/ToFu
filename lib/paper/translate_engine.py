"""Background worker for Babel-mode whole-paper translation.

Splits ``paper_text`` on paragraph boundaries (preferring ``\\n\\n``) so
individual sentences/equations don't get cut in half by a fixed-width
split. Streams LLM completions for each chunk, persists the joined result
to ``paper_translations`` on success.
"""

import re
import time

import lib as _lib
from lib.database import get_thread_db
from lib.llm_dispatch.api import dispatch_stream
from lib.log import get_logger

from .translate_runtime import (
    _LANG_NAMES,
    _TRANSLATE_CHUNK_SIZE,
    _append_translate_event,
    _cleanup_stale_translate_tasks,
)

logger = get_logger(__name__)


def _run_translate_task(task, paper_text):
    """Background worker: chunk + translate + persist.

    Splits ``paper_text`` on paragraph boundaries (preferring ``\\n\\n``) so
    individual sentences/equations don't get cut in half by a fixed-width
    split — this is one of the things the old client implementation got
    wrong.
    """
    task['status'] = 'running'
    lang = task['lang']
    target_name = _LANG_NAMES.get(lang, lang)
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
    task['progress']['total'] = total
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
                from lib.error_envelope import make_envelope as _make_env
                envelope = _make_env(
                    'aborted',
                    detail=f'Aborted at chunk {ci}/{total}',
                    context='paper-translate',
                    source='routes.paper:translate',
                    raw='abort_event set',
                )
                task['status'] = 'error'
                task['error'] = envelope
                task['finished_at'] = time.time()
                _append_translate_event(task, {'type': 'error', 'error': envelope})
                return

            messages = [
                {'role': 'system', 'content': sys_prompt},
                {'role': 'user', 'content': chunk},
            ]
            collected = []
            try:
                dispatch_stream(
                    messages,
                    on_content=lambda t: collected.append(t),
                    max_tokens=8192,
                    temperature=0,
                    prefer_model=model,
                    strict_model=bool(model),
                    log_prefix='[Paper:Translate]',
                )
            except Exception as e:
                logger.warning('[Paper:Translate] Chunk %d/%d failed: %s', ci + 1, total, e)
                collected = [f'[Translation error for this section: {e}]']

            piece = ''.join(collected).strip()
            translated_parts.append(piece)
            task['full_text'] = '\n\n'.join(translated_parts)
            task['progress']['done'] = ci + 1
            _append_translate_event(task, {
                'type': 'chunk', 'index': ci, 'total': total,
                'text': piece,
            })

        full_text = '\n\n'.join(translated_parts)
        task['full_text'] = full_text
        task['status'] = 'done'
        task['finished_at'] = time.time()

        try:
            db = get_thread_db()
            from lib.database._core_schema import PAPER_TRANSLATIONS, upsert
            upsert(db, PAPER_TRANSLATIONS, {
                'paper_hash': task['paper_hash'], 'lang': lang, 'text': full_text,
                'model': model or _lib.LLM_MODEL, 'created_at': int(time.time()),
            }, retry=True)
            logger.info('[Paper:Translate] Task %s done — %d chars persisted',
                        task['task_id'], len(full_text))
        except Exception as e:
            logger.warning('[Paper:Translate] Persist failed: %s', e)

        _append_translate_event(task, {'type': 'done', 'text': full_text})

    except Exception as e:
        logger.error('[Paper:Translate] Task %s crashed: %s',
                     task['task_id'], e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model=model or '', context='paper-translate',
            source='routes.paper:translate',
        )
        task['status'] = 'error'
        task['error'] = envelope
        task['finished_at'] = time.time()
        _append_translate_event(task, {'type': 'error', 'error': envelope})
    finally:
        _cleanup_stale_translate_tasks()
