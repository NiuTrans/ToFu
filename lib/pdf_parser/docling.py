"""lib/pdf_parser/docling.py — Layout-aware PDF parsing via IBM Docling.

Docling is a structured-document model (HuggingFace, IBM) that does:
- Layout detection (text / table / figure / equation regions)
- TableFormer for borderless / complex tables → proper Markdown pipe tables
- Internal equation model for math regions → LaTeX
- Single-pass conversion → Markdown / JSON

Compared to ``pymupdf4llm`` (current default):
- Better tables on PDFs without ruling lines (academic CS / ML papers)
- Better math formulas (no rule-based heuristics needed)
- Comparable speed on CPU once models are warm (~1-3 s/page)

Trade-offs:
- Heavy install (~2 GB with torch + model weights on first run)
- Optional, NOT default. Caller opts in via ``mode='structured'`` in
  ``extract_pdf_text`` or ``parse_pdf``.

This module degrades gracefully: if ``docling`` is not installed, every
public function logs a single warning and returns ``None`` so the caller
can fall back to ``pymupdf4llm``.
"""

import io
import os
import threading
import time as _time

from lib.log import get_logger
from lib.pdf_parser._common import HAS_DOCLING

logger = get_logger(__name__)

__all__ = ['extract_pdf_text_docling', 'is_available']


def _allowed_cpu_count() -> int:
    """Return the number of CPUs this process is actually allowed to run on.

    On cgroup/cpuset-restricted hosts (containers, k8s pods, YARN/Hope
    containers, ``taskset``-launched jobs) ``os.cpu_count()`` reports the
    HOST core count, but the kernel only permits the process to schedule
    on a subset. ONNX Runtime (pulled in by Docling) otherwise spawns one
    worker per host core and tries to ``pthread_setaffinity_np`` each one
    to a physical core that may not be in the allowed set, producing a
    storm of ``EINVAL`` errors in stderr.
    """
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except (AttributeError, OSError) as _e_audit:
        logger.debug('[docling] _allowed_cpu_count caught %s: %s', type(_e_audit).__name__, _e_audit)
        return max(1, os.cpu_count() or 1)


def _docling_thread_count() -> int:
    override = os.environ.get('TOFU_DOCLING_THREADS', '').strip()
    if override:
        try:
            n = int(override)
            if n > 0:
                return n
        except ValueError as e:
            logger.debug('[PDF/Docling] Invalid TOFU_DOCLING_THREADS=%r: %s',
                         override, e)
    return min(8, _allowed_cpu_count())


# NOTE: ORT/OMP thread caps are seeded into the environment in
# ``lib/pdf_parser/_common.py`` BEFORE ``import docling`` runs — putting
# them here would be too late if onnxruntime is loaded transitively by
# the docling import chain.


# ── Lazy-loaded converter ──
# Docling's DocumentConverter spins up torch and loads layout / table /
# equation models on first construction (~10-30 s, CPU). We cache one
# instance per process — subsequent calls reuse the loaded weights.
_converter = None
_converter_lock = threading.Lock()
_warned_missing = False


def is_available() -> bool:
    """Returns True iff the docling package is importable."""
    return HAS_DOCLING


def _warn_missing_once() -> None:
    global _warned_missing
    if _warned_missing:
        return
    _warned_missing = True
    logger.info(
        "[PDF/Docling] mode='structured' requested but docling is not "
        "installed. Falling back to pymupdf4llm. To enable structured "
        "parsing (better tables + formulas), run: "
        "pip install 'docling' --extra-index-url "
        "https://download.pytorch.org/whl/cpu  "
        "(adds ~2 GB; downloads model weights on first use). "
        "Or re-run install.sh --with-docling."
    )


def _get_converter():
    """Lazy-load and cache a DocumentConverter. Returns None if unavailable."""
    global _converter
    if _converter is not None:
        return _converter
    if not HAS_DOCLING:
        _warn_missing_once()
        return None
    with _converter_lock:
        if _converter is not None:
            return _converter
        try:
            from docling.document_converter import DocumentConverter
            t0 = _time.time()
            num_threads = _docling_thread_count()
            logger.info('[PDF/Docling] Loading DocumentConverter '
                        '(first call may download model weights, '
                        'num_threads=%d)...', num_threads)
            try:
                from docling.datamodel.accelerator_options import (
                    AcceleratorDevice,
                    AcceleratorOptions,
                )
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import PdfFormatOption

                pipeline_options = PdfPipelineOptions()
                pipeline_options.accelerator_options = AcceleratorOptions(
                    num_threads=num_threads,
                    device=AcceleratorDevice.AUTO,
                )
                _converter = DocumentConverter(format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_options=pipeline_options,
                    ),
                })
            except ImportError as e:
                # Older docling versions don't expose AcceleratorOptions.
                # Fall back to the default constructor — env vars set at
                # module import still cap ORT/OMP threads.
                logger.debug('[PDF/Docling] AcceleratorOptions unavailable '
                             '(%s); using default DocumentConverter()', e)
                _converter = DocumentConverter()
            logger.info('[PDF/Docling] Converter ready in %.1fs',
                        _time.time() - t0)
        except Exception as e:
            logger.warning('[PDF/Docling] Converter init failed: %s '
                           '(falling back to pymupdf4llm)', e, exc_info=True)
            _converter = None
            return None
    return _converter


def extract_pdf_text_docling(pdf_bytes: bytes, *,
                             max_chars: int = 0,
                             url: str = '',
                             progress_callback=None) -> str | None:
    """Extract Markdown from PDF using Docling's layout-aware pipeline.

    Args:
        pdf_bytes: Raw PDF bytes.
        max_chars: Soft cap on output length (0 = no cap).
        url: Optional URL/filename for log context.
        progress_callback: ``Callable[[int, int], None]`` invoked once with
            ``(0, total_pages)`` before conversion and once with
            ``(total_pages, total_pages)`` after. Docling does not expose
            mid-conversion page progress, so we cannot stream true progress
            here — for honest per-page progress, use ``mode='rich'``
            (pymupdf4llm) instead.

    Returns:
        Markdown string, or ``None`` if docling is unavailable / failed.
        Caller should fall back to ``pymupdf4llm`` on ``None``.
    """
    converter = _get_converter()
    if converter is None:
        return None

    try:
        from docling.datamodel.base_models import DocumentStream
    except ImportError as e:
        logger.warning('[PDF/Docling] DocumentStream import failed: %s', e)
        return None

    try:
        from docling_core.types.doc import ImageRefMode  # noqa: F401  type: ignore[import-untyped]
    except ImportError:
        # docling_core ships with docling — if missing, our import of
        # docling above would have already failed. Defensive only.
        pass

    t0 = _time.time()
    try:
        # DocumentStream wraps an in-memory file so we don't have to
        # write the PDF to disk just for parsing.
        stream = DocumentStream(name=(url or 'document.pdf'),
                                stream=io.BytesIO(pdf_bytes))

        # Optimistic "starting" tick. Real per-page progress isn't
        # exposed by docling's high-level API.
        if progress_callback:
            try:
                progress_callback(0, 1)
            except Exception as e:
                logger.debug('[PDF/Docling] progress_callback raised: %s', e)

        result = converter.convert(stream)
        doc = getattr(result, 'document', None)
        if doc is None:
            logger.warning('[PDF/Docling] convert() returned no document for %s',
                           url[:80])
            return None

        # Try to grab page count for the final progress tick — best-effort.
        total_pages = 1
        try:
            total_pages = max(1, len(doc.pages)) if hasattr(doc, 'pages') else 1
        except (TypeError, AttributeError) as e:
            logger.debug('[PDF/Docling] doc.pages length probe failed: %s', e)
            total_pages = 1

        md = doc.export_to_markdown()
        if not isinstance(md, str):
            md = str(md or '')

        if max_chars > 0 and len(md) > max_chars:
            md = md[:max_chars] + f'\n[…truncated at {max_chars:,} chars]'

        if progress_callback:
            try:
                progress_callback(total_pages, total_pages)
            except Exception as e:
                logger.debug('[PDF/Docling] progress_callback raised: %s', e)

        elapsed = _time.time() - t0
        logger.info('[PDF/Docling] OK: %d pages, %s chars, %.1fs — %s',
                    total_pages, f'{len(md):,}', elapsed, url[:60])
        return md

    except Exception as e:
        logger.warning('[PDF/Docling] convert failed for %s: %s '
                       '(falling back)', url[:80] if url else '?', e,
                       exc_info=True)
        return None
