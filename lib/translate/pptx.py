"""PPTX file translation (formatting-preserving).

Core engine adapted from
``tristan-mcinnis/PPT-Translator-Formatting-Intact-with-LLMs``
(https://github.com/tristan-mcinnis/PPT-Translator-Formatting-Intact-with-LLMs).

PPTX translation tasks share the same TaskRuntime as text translation
but include extra fields: ``type='pptx'``, ``filename``, ``download_url``.
"""

import os
import time

from lib.log import get_logger

from .engine import _translate_one_chunk
from .prompt import _build_translate_prompt
from .runtime import _translate_tasks, _translate_tasks_lock

logger = get_logger(__name__)


# Upload directory (under <repo>/uploads/pptx). The path mirrors the
# original module so existing on-disk assets remain reachable.
_PPTX_UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'uploads', 'pptx',
)
_MAX_PPTX_BYTES = 50 * 1024 * 1024  # 50 MB


def _ensure_pptx_upload_dir():
    """Ensure the PPTX upload directory exists."""
    os.makedirs(_PPTX_UPLOAD_DIR, exist_ok=True)


def _do_translate_pptx(task_id, input_path, filename, target, source):
    """Background thread: translate a PPTX file."""
    with _translate_tasks_lock:
        task = _translate_tasks.get(task_id)
    if not task:
        return

    system_prompt = _build_translate_prompt(target, source)

    def _translate_segment(text):
        """Translate a single text segment using our existing infrastructure."""
        if not text or not text.strip():
            return text
        c, _u = _translate_one_chunk(text, system_prompt,
                                     chunk_label=f':pptx-{task_id[:6]}',
                                     source=source, target=target)
        return c

    def _progress_cb(current, total, _status_msg):
        with _translate_tasks_lock:
            t = _translate_tasks.get(task_id)
            if t:
                t['progress'] = f'{current}/{total}'

    try:
        from lib.pptx_translator import translate_pptx

        stem = os.path.splitext(filename)[0]
        output_filename = f'{stem}_translated_{task_id}.pptx'
        output_path = os.path.join(_PPTX_UPLOAD_DIR, output_filename)

        result = translate_pptx(
            input_path,
            output_path,
            translate_fn=_translate_segment,
            progress_fn=_progress_cb,
        )

        if not result.get('ok'):
            from lib.error_envelope import make_envelope as _make_env
            _err_text = result.get('error') or 'Translation failed'
            envelope = _make_env(
                'generic',
                detail=str(_err_text),
                context='pptx-translate',
                source='routes.translate:pptx',
                raw=str(_err_text),
            )
            with _translate_tasks_lock:
                task['status'] = 'error'
                task['error'] = envelope
                task['completed_at'] = time.time()
            logger.error('[PPTX-Translate] Task %s failed: %s', task_id[:8],
                         _err_text)
            return

        with _translate_tasks_lock:
            task['status'] = 'done'
            task['result'] = {
                'filename': output_filename,
                'download_url': f'/api/translate/pptx/download/{output_filename}',
                'slides': result.get('slides', 0),
                'segments': result.get('segments', 0),
                'chars_translated': result.get('chars_translated', 0),
                'errors': result.get('errors', 0),
                'elapsed': result.get('elapsed', 0),
            }
            task['completed_at'] = time.time()

        logger.info('[PPTX-Translate] Task %s done: %s — %d slides, %d segments, '
                    '%d chars, %.1fs',
                    task_id[:8], filename, result.get('slides', 0),
                    result.get('segments', 0), result.get('chars_translated', 0),
                    result.get('elapsed', 0))

    except Exception as e:
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, context='pptx-translate',
            source='routes.translate:pptx',
        )
        with _translate_tasks_lock:
            task['status'] = 'error'
            task['error'] = envelope
            task['completed_at'] = time.time()
        logger.error('[PPTX-Translate] Task %s failed: %s', task_id[:8], e, exc_info=True)
    finally:
        # Clean up input file (translated file kept for download)
        try:
            if os.path.isfile(input_path):
                os.remove(input_path)
        except Exception as e:
            logger.debug('[PPTX-Translate] Failed to clean up input: %s', e)
