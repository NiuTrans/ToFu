"""lib/pdf_parser/vlm/_parse.py — Synchronous VLM PDF parsing.

Renders each PDF page to a JPEG image, sends batches to a VLM via an
OpenAI-compatible API for transcription to high-quality Markdown.
"""

import base64
from collections.abc import Callable
import re
import threading
import sys

from lib.log import get_logger
from lib.pdf_parser.images import render_pdf_pages_isolated as render_pdf_pages
from lib.pdf_parser.vlm._config import (
    _VLM_SYSTEM_PROMPT,
    _env_int,
    _get_vlm_models,
)
from lib.pdf_parser.vlm._policy import (
    vlm_call_workers,
    vlm_max_429_attempts,
)

logger = get_logger(__name__)


def _vlm_call_pages(page_images: list[bytes], page_range: str,
                     model: str, max_tokens: int = 16384, *,
                     abort_check: Callable[[], bool] | None = None,
                     max_429_attempts: int | None = None) -> str:
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
        abort_check=abort_check,
        max_429_attempts=(max_429_attempts
                          if max_429_attempts is not None
                          else vlm_max_429_attempts()),
        log_prefix=f'[PDF-VLM/{model.split("/")[-1][:20]}]',
        max_retries=5,  # retry harder — 429s are routine on shared keys
    )
    return text or ''


def vlm_parse_pdf(pdf_bytes: bytes, *,
                  model: str | None = None,
                  dpi: int = 150,
                  batch_pages: int | None = None,
                  max_workers: int | None = None,
                  max_pages: int | None = None,
                  progress_cb=None,
                  abort_check: Callable[[], bool] | None = None) -> str:
    """Parse a PDF via VLM for high-quality Markdown output.

    Renders every page to an image, groups pages into batches of
    ``batch_pages`` (default from env ``PDF_VLM_BATCH_PAGES``, fallback 4),
    and sends each batch as a single VLM call. Independent batches start through
    the shared demand executor. Worker settings size idle reuse only.

    Why batch?  A 64-page paper used to fan out 64 single-page calls,
    causing 429-storms on shared keys and ~60-page-worth of HTTP
    round-trip overhead. Batching to 4 pages/call cuts that to 16 calls
    with the same total image bytes — usually 2-3× faster end-to-end.

    Args:
        pdf_bytes: Raw PDF bytes.
        model: Force a specific model (skips dispatcher capability lookup).
        dpi: Image render DPI per page.
        batch_pages: Pages per VLM call. ``None`` → env / 4.
        max_workers: Optional idle worker reuse hint; never an active call quota.
        max_pages: Optional explicit caller page budget, checked before rendering.
        progress_cb: ``Callable[[done_pages, total_pages], None]``.
        abort_check: Cooperative cancellation/deadline callback.
    """
    if model is not None:
        models = [model]
    else:
        models = _get_vlm_models()

    if batch_pages is None:
        batch_pages = _env_int('PDF_VLM_BATCH_PAGES', 4, lo=1, hi=16)

    logger.info('VLM parse: rendering %d-dpi page images...', dpi)
    page_images = render_pdf_pages(
        pdf_bytes,
        dpi=dpi,
        max_pages=max_pages,
        spool=True,
        abort_check=abort_check,
    )
    try:
        return _transcribe_rendered_pages(
            page_images, models=models, batch_pages=batch_pages,
            max_workers=max_workers, progress_cb=progress_cb, abort_check=abort_check)
    finally:
        # Executor exit joins every reader before the source spool closes.
        close = getattr(page_images, 'close', None)
        if close is not None:
            close()


def _transcribe_rendered_pages(page_images, *, models, batch_pages, max_workers,
                              progress_cb, abort_check):
    from concurrent.futures import Future, as_completed
    from lib.observability import InstrumentedThreadPoolExecutor

    total = len(page_images)

    # ── Group pages into batches ──
    batches: list[tuple[int, int, int, str, str]] = []
    for i in range(0, total, batch_pages):
        end = min(i + batch_pages, total)
        idx = len(batches)
        label = f'p.{i + 1}-{end}' if end > i + 1 else f'p.{i + 1}'
        batch_model = models[idx % len(models)]
        # tuple shape: (batch_idx, page_start, page_end, label, model)
        batches.append((idx, i, end, label, batch_model))

    n_batches = len(batches)
    call_idle_reuse_hint = vlm_call_workers()
    if max_workers is None:
        max_workers = _env_int(
            'PDF_VLM_MAX_WORKERS',
            call_idle_reuse_hint,
            lo=1,
            hi=sys.maxsize,
        )
    max_workers = max(1, int(max_workers))

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

    def _process_batch(idx, p_start, p_end, label, use_model):
        if abort_check is not None and abort_check():
            from lib.llm_errors import AbortedError
            raise AbortedError('VLM PDF transcription aborted')
        md = _vlm_call_pages(
            page_images[p_start:p_end],
            label,
            use_model,
            max_tokens=max_tokens,
            abort_check=abort_check,
            max_429_attempts=vlm_max_429_attempts(),
        )
        md = re.sub(r'^```(?:markdown)?\s*\n', '', md)
        md = re.sub(r'\n```\s*$', '', md)
        return idx, md.strip()

    with InstrumentedThreadPoolExecutor(max_workers=max_workers,
                            metric_pool='vlm-parse',
                            thread_name_prefix='vlm') as pool:
        future_map = {}
        for idx, p_start, p_end, label, batch_model in batches:
            try:
                future = pool.submit(_process_batch, idx, p_start, p_end, label, batch_model)
            except Exception as error:
                # Failed publication is unstarted. This batch enters the
                # ordinary error/progress lane; published siblings remain owned.
                future = Future()
                future.set_exception(error)
            future_map[future] = (idx, p_start, p_end, label)

        for future in as_completed(future_map):
            idx, p_start, p_end, label = future_map[future]
            n_pages = p_end - p_start
            try:
                batch_idx, md = future.result()
                results[batch_idx] = md
                logger.debug('VLM parse: %s done ✓', label)
            except Exception as exc:
                from lib.llm_errors import AbortedError
                if isinstance(exc, AbortedError):
                    for pending in future_map:
                        pending.cancel()
                    raise
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
