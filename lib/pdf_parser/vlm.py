"""lib/pdf_parser/vlm.py — VLM-based PDF parsing (Gemini Flash Lite).

Renders each PDF page to a JPEG image, sends batches to a VLM via
OpenAI-compatible API for transcription to high-quality Markdown.
"""

import base64
import re
import threading
import time as _time
import uuid

from lib.log import get_logger
from lib.pdf_parser.images import render_pdf_pages

logger = get_logger(__name__)

__all__ = ['vlm_parse_pdf', 'start_vlm_task', 'get_vlm_task']


_VLM_SYSTEM_PROMPT = """\
You are a precise document transcriber. Convert the provided PDF page image(s) into clean Markdown.

Rules:
- Preserve ALL text content faithfully — do not summarize or omit anything.
- Tables → Markdown pipe tables with header separators (| col | col |\\n|---|---|).
- Mathematical formulas:
  • Inline formulas → LaTeX in single dollars: $E = mc^2$
  • Display / block formulas → LaTeX in double dollars on their own lines:
    $$
    \\int_0^\\infty e^{-x^2} dx = \\frac{\\sqrt{\\pi}}{2}
    $$
  • Multi-line aligned equations → use \\\\begin{aligned}...\\\\end{aligned} inside $$.
- Tables MUST be transcribed as Markdown pipe tables — NEVER summarize a table into a
  single line or bracket notation.  Even if a table has colors, symbols (✓✗), or unusual
  formatting, reproduce every row and column faithfully as a pipe table.
- Figures / charts (NOT tables) → [Figure N: brief description of what it shows].
  A figure is an image, graph, or diagram — NOT a data table.
- Section headings → proper Markdown heading levels (# ## ###).
- Bullet / numbered lists → preserve as-is.
- Do NOT add commentary, explanation, or meta-text — output ONLY the transcribed Markdown.
- When content continues across page boundaries, just continue naturally without page markers.\
"""


def _get_vlm_models() -> list[str]:
    """Return list of available VLM-capable models for parallel dispatch."""
    try:
        from lib.llm_dispatch import get_dispatcher
        d = get_dispatcher()
        seen = []
        for s in d.pick_best_slots('vision', n=10):
            if s.model not in seen:
                seen.append(s.model)
        if seen:
            return seen
    except Exception as e:
        logger.warning('[PDF] VLM model discovery via dispatcher failed, using fallback: %s',
                       e, exc_info=True)
    from lib import GEMINI_MODEL
    return [GEMINI_MODEL or 'gemini-2.5-flash']


def _vlm_call_pages(page_images: list[bytes], page_range: str,
                     model: str) -> str:
    """Send page image(s) to VLM and get Markdown back."""
    from lib.llm_dispatch import smart_chat

    content = [{'type': 'text',
                'text': f'Transcribe the following PDF page(s) ({page_range}) to Markdown:'}]
    for img_bytes in page_images:
        b64 = base64.b64encode(img_bytes).decode('ascii')
        content.append({
            'type': 'image_url',
            'image_url': {'url': f'data:image/jpeg;base64,{b64}'},
        })

    messages = [
        {'role': 'system', 'content': _VLM_SYSTEM_PROMPT},
        {'role': 'user',   'content': content},
    ]

    text, _usage = smart_chat(
        messages=messages,
        max_tokens=16384, temperature=0.1,
        capability='vision', model=model,
        timeout=180, log_prefix=f'[PDF-VLM/{model.split("/")[-1][:20]}]',
    )
    return text or ''


def vlm_parse_pdf(pdf_bytes: bytes, *,
                  model: str | None = None,
                  dpi: int = 150,

                  progress_cb=None) -> str:
    """Parse a PDF via VLM for high-quality Markdown output.

    Renders every page to an image and sends each page as its own
    independent VLM call — ALL pages run concurrently via a thread pool.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if model is not None:
        models = [model]
    else:
        models = _get_vlm_models()

    logger.info('VLM parse: rendering %d-dpi page images...', dpi)
    page_images = render_pdf_pages(pdf_bytes, dpi=dpi)
    total = len(page_images)
    logger.info('VLM parse: %d pages → %d concurrent VLM calls, models=%s',
                total, total, [m.split('/')[-1] for m in models])

    batches = []
    for i in range(total):
        label = f'p.{i + 1}'
        batch_model = models[i % len(models)]
        batches.append((i, i, i + 1, [page_images[i]], label, batch_model))

    results: dict[int, str] = {}
    done_pages = 0

    def _process_batch(idx, imgs, label, use_model):
        md = _vlm_call_pages(imgs, label, use_model)
        md = re.sub(r'^```(?:markdown)?\s*\n', '', md)
        md = re.sub(r'\n```\s*$', '', md)
        return idx, md.strip()

    with ThreadPoolExecutor(max_workers=total,
                            thread_name_prefix='vlm') as pool:
        future_map = {
            pool.submit(_process_batch, idx, imgs, label, batch_model):
                (idx, start, end, label)
            for idx, start, end, imgs, label, batch_model in batches
        }

        for future in as_completed(future_map):
            idx, start, end, label = future_map[future]
            try:
                batch_idx, md = future.result()
                results[batch_idx] = md
                logger.debug('VLM parse: %s done ✓', label)
            except Exception as exc:
                logger.error('VLM parse: %s failed: %s', label, exc, exc_info=True)
                results[idx] = f'\n\n<!-- VLM error on {label}: {exc} -->\n\n'

            done_pages += (end - start)
            if progress_cb:
                progress_cb(done_pages, total)

    parts = [results[i] for i in range(len(batches))]
    result = '\n\n'.join(parts)
    logger.info('VLM parse: complete — %d chars, %d pages', len(result), total)
    return result


# ── Async task management ─────────────────────────────

_vlm_tasks: dict[str, dict] = {}
_vlm_lock = threading.Lock()
_TASK_TTL = 1800  # 30 min


def start_vlm_task(pdf_bytes: bytes, filename: str = 'document.pdf',
                   model: str | None = None) -> str:
    """Launch a background VLM parse. Returns *task_id* for polling."""
    task_id = uuid.uuid4().hex[:12]

    with _vlm_lock:
        _vlm_tasks[task_id] = {
            'status': 'processing', 'progress': '0/?',
            'result': None, 'error': None,
            'filename': filename, 'created': _time.time(),
        }

    def _run():
        try:
            def _prog(done, total):
                with _vlm_lock:
                    t = _vlm_tasks.get(task_id)
                    if t:
                        t['progress'] = f'{done}/{total}'
            md = vlm_parse_pdf(pdf_bytes, model=model, progress_cb=_prog)
            with _vlm_lock:
                t = _vlm_tasks.get(task_id)
                if t:
                    t['status'] = 'done'
                    t['result'] = md
        except Exception as exc:
            logger.error('VLM task %s failed: %s', task_id, exc, exc_info=True)
            with _vlm_lock:
                t = _vlm_tasks.get(task_id)
                if t:
                    t['status'] = 'error'
                    t['error'] = str(exc)
        finally:
            _cleanup_old_tasks()

    threading.Thread(target=_run, daemon=True, name=f'vlm-{task_id}').start()
    return task_id


def get_vlm_task(task_id: str) -> dict | None:
    """Return task status dict, or None if not found."""
    with _vlm_lock:
        t = _vlm_tasks.get(task_id)
        return dict(t) if t else None


def _cleanup_old_tasks():
    now = _time.time()
    with _vlm_lock:
        expired = [k for k, v in _vlm_tasks.items()
                   if now - v['created'] > _TASK_TTL]
        for k in expired:
            del _vlm_tasks[k]
