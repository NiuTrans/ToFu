"""lib/pdf_parser/vlm.py — VLM-based PDF parsing (Gemini Flash Lite).

Renders each PDF page to a JPEG image, sends batches to a VLM via
OpenAI-compatible API for transcription to high-quality Markdown.

Speed knobs (env-tunable, all optional):
    PDF_VLM_BATCH_PAGES   — pages per VLM call (default 4). Larger = fewer
                            HTTP round trips, fewer 429-cycles, but more
                            tokens per call (and a higher chance the model
                            hits its output cap on dense pages). Set to 1
                            to restore the legacy one-page-per-call mode.
    PDF_VLM_MAX_WORKERS   — concurrent VLM calls (default = number of
                            batches, i.e. fully parallel). Lower this on
                            shared keys to avoid 429 storms.
    PDF_VLM_MAX_TOKENS    — output-token cap per call (default 16384,
                            scaled with batch size).
"""

import base64
import os
import re
import threading
import time as _time
import uuid

from lib.log import get_logger
from lib.pdf_parser.images import render_pdf_pages

logger = get_logger(__name__)


def _env_int(name: str, default: int, lo: int = 1, hi: int = 1024) -> int:
    """Parse an env var as an int, with bounds. Logs at debug on bad input."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except (ValueError, TypeError) as e:
        logger.debug('[PDF] %s=%r ignored, using default %d (%s)',
                     name, raw, default, e)
        return default
    return max(lo, min(hi, v))

__all__ = ['vlm_parse_pdf', 'start_vlm_task', 'get_vlm_task',
           'find_vlm_tasks_by_filename']


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
                     model: str, max_tokens: int = 16384) -> str:
    """Send page image(s) to VLM and get Markdown back.

    ``max_tokens`` should scale with batch size — caller is expected to
    pass roughly ``4096 * len(page_images)`` for dense pages.
    """
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

    # Per-call timeout scales with batch size: 60s base + 30s/page,
    # capped at 8 min to bound stuck-key recovery time.
    timeout = min(60 + 30 * len(page_images), 480)

    text, _usage = smart_chat(
        messages=messages,
        max_tokens=max_tokens, temperature=0.1,
        capability='vision', model=model,
        timeout=timeout,
        log_prefix=f'[PDF-VLM/{model.split("/")[-1][:20]}]',
        max_retries=5,  # retry harder — 429s are routine on shared keys
    )
    return text or ''


def vlm_parse_pdf(pdf_bytes: bytes, *,
                  model: str | None = None,
                  dpi: int = 150,
                  batch_pages: int | None = None,
                  max_workers: int | None = None,
                  progress_cb=None) -> str:
    """Parse a PDF via VLM for high-quality Markdown output.

    Renders every page to an image, groups pages into batches of
    ``batch_pages`` (default from env ``PDF_VLM_BATCH_PAGES``, fallback 4),
    and sends each batch as a single VLM call. Batches run concurrently
    via a thread pool capped by ``max_workers`` (default = #batches,
    i.e. fully parallel; env override ``PDF_VLM_MAX_WORKERS``).

    Why batch?  A 64-page paper used to fan out 64 single-page calls,
    causing 429-storms on shared keys and ~60-page-worth of HTTP
    round-trip overhead. Batching to 4 pages/call cuts that to 16 calls
    with the same total image bytes — usually 2-3× faster end-to-end.

    Args:
        pdf_bytes: Raw PDF bytes.
        model: Force a specific model (skips dispatcher capability lookup).
        dpi: Image render DPI per page.
        batch_pages: Pages per VLM call. ``None`` → env / 4.
        max_workers: Cap on concurrent VLM calls. ``None`` → unlimited
            (one thread per batch).
        progress_cb: ``Callable[[done_pages, total_pages], None]``.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if model is not None:
        models = [model]
    else:
        models = _get_vlm_models()

    if batch_pages is None:
        batch_pages = _env_int('PDF_VLM_BATCH_PAGES', 4, lo=1, hi=16)

    logger.info('VLM parse: rendering %d-dpi page images...', dpi)
    page_images = render_pdf_pages(pdf_bytes, dpi=dpi)
    total = len(page_images)

    # ── Group pages into batches ──
    batches: list[tuple[int, int, int, list[bytes], str, str]] = []
    for i in range(0, total, batch_pages):
        end = min(i + batch_pages, total)
        idx = len(batches)
        imgs = page_images[i:end]
        label = f'p.{i + 1}-{end}' if end > i + 1 else f'p.{i + 1}'
        batch_model = models[idx % len(models)]
        # tuple shape: (batch_idx, page_start, page_end, imgs, label, model)
        batches.append((idx, i, end, imgs, label, batch_model))

    n_batches = len(batches)
    if max_workers is None:
        max_workers = _env_int('PDF_VLM_MAX_WORKERS', n_batches,
                               lo=1, hi=max(n_batches, 1))
    max_workers = max(1, min(max_workers, n_batches))

    # Output-token cap scales with batch size — 4096 tokens/page is the
    # rough budget for dense academic content.
    max_tokens = _env_int('PDF_VLM_MAX_TOKENS', 4096 * batch_pages,
                          lo=2048, hi=131072)

    logger.info(
        'VLM parse: %d pages → %d batches (×%d pages), workers=%d, '
        'max_tokens=%d, models=%s',
        total, n_batches, batch_pages, max_workers, max_tokens,
        [m.split('/')[-1] for m in models])

    results: dict[int, str] = {}
    done_pages = 0
    _done_lock = threading.Lock()

    def _process_batch(idx, imgs, label, use_model):
        md = _vlm_call_pages(imgs, label, use_model, max_tokens=max_tokens)
        md = re.sub(r'^```(?:markdown)?\s*\n', '', md)
        md = re.sub(r'\n```\s*$', '', md)
        return idx, md.strip()

    with ThreadPoolExecutor(max_workers=max_workers,
                            thread_name_prefix='vlm') as pool:
        future_map = {
            pool.submit(_process_batch, idx, imgs, label, batch_model):
                (idx, p_start, p_end, label)
            for idx, p_start, p_end, imgs, label, batch_model in batches
        }

        for future in as_completed(future_map):
            idx, p_start, p_end, label = future_map[future]
            n_pages = p_end - p_start
            try:
                batch_idx, md = future.result()
                results[batch_idx] = md
                logger.debug('VLM parse: %s done ✓', label)
            except Exception as exc:
                logger.error('VLM parse: %s failed: %s', label, exc,
                             exc_info=True)
                results[idx] = f'\n\n<!-- VLM error on {label}: {exc} -->\n\n'
            with _done_lock:
                done_pages += n_pages
                _snap = done_pages
            if progress_cb:
                progress_cb(_snap, total)

    parts = [results[i] for i in range(n_batches)]
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


def find_vlm_tasks_by_filename(filename: str) -> list[dict]:
    """Find all active VLM tasks matching *filename*.

    Returns a list of ``{taskId, status, progress, filename, created}``
    dicts, most-recent first.  Useful for reconnecting after a page
    refresh when the frontend lost the task_id.
    """
    with _vlm_lock:
        matches = []
        for tid, t in _vlm_tasks.items():
            if t['filename'] == filename:
                matches.append({
                    'taskId': tid,
                    'status': t['status'],
                    'progress': t['progress'],
                    'filename': t['filename'],
                    'created': t['created'],
                })
        matches.sort(key=lambda x: x['created'], reverse=True)
        return matches


def _cleanup_old_tasks():
    now = _time.time()
    with _vlm_lock:
        expired = [k for k, v in _vlm_tasks.items()
                   if now - v['created'] > _TASK_TTL]
        for k in expired:
            del _vlm_tasks[k]
