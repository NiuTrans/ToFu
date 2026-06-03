"""lib/pdf_parser/pool.py — Off-load CPU-bound PDF parsing to a process pool.

PyMuPDF (the MuPDF C core) is NOT thread-safe, so every in-process parse runs
serialised behind ``_common.PYMUPDF_LOCK`` AND pins the GIL for the whole
parse — starving the event loop and every other sync route handler thread.

Running the parse in a separate process removes both problems at once: each
worker has its own interpreter (its own GIL + its own MuPDF lock → genuine
parallelism) and the CPU work leaves the web process entirely.

If the pool can't be created or a worker dies (restricted container, fork
hazard, OOM), :func:`parse_pdf_pooled` transparently falls back to an
in-process parse so the feature never breaks — it's just GIL-bound again.

Environment:
    TOFU_PDF_PROCESSES     — worker count (default: min(4, allowed CPUs))
    TOFU_PDF_MP_START      — multiprocessing start method (default: 'spawn')
    TOFU_PDF_PARSE_TIMEOUT — per-parse hard timeout in seconds (default: 300)
"""

import atexit
import os
import threading
from concurrent.futures import (BrokenExecutor, ProcessPoolExecutor,
                                 TimeoutError as FuturesTimeout)

from lib.log import get_logger
from lib.pdf_parser.core import parse_pdf as _parse_pdf_inproc

logger = get_logger(__name__)

__all__ = ['parse_pdf_pooled', 'shutdown_pdf_pool']

_POOL = None
_POOL_LOCK = threading.Lock()


def _max_workers() -> int:
    try:
        n = int(os.environ.get('TOFU_PDF_PROCESSES', '0') or '0')
    except (ValueError, TypeError) as e:
        logger.debug('[PDF Pool] Invalid TOFU_PDF_PROCESSES: %s', e)
        n = 0
    if n <= 0:
        try:
            cpu = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            cpu = os.cpu_count() or 2
        n = max(1, min(4, cpu))
    return n


def _get_pool() -> ProcessPoolExecutor:
    """Return the lazily-created process pool. Caller-safe under contention."""
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            workers = _max_workers()
            # 'spawn' avoids fork-in-a-multithreaded-ASGI-process deadlocks
            # with native libraries (MuPDF, onnxruntime). The child only
            # re-imports lib.pdf_parser.core (cheap), not server.__main__.
            method = (os.environ.get('TOFU_PDF_MP_START', 'spawn').strip()
                      or 'spawn')
            import multiprocessing as mp
            try:
                ctx = mp.get_context(method)
            except ValueError:
                logger.warning('[PDF Pool] Unknown start method %r — using spawn', method)
                ctx = mp.get_context('spawn')
            _POOL = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
            logger.info('[PDF Pool] Created process pool: workers=%d start=%s',
                        workers, method)
        return _POOL


def _reset_pool() -> None:
    """Tear down the pool so a fresh one is created on the next parse.

    Used after a broken worker or a timeout — a hung/dead pool must not be
    reused.
    """
    global _POOL
    with _POOL_LOCK:
        if _POOL is not None:
            try:
                _POOL.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.debug('[PDF Pool] shutdown during reset failed: %s', e)
            _POOL = None


def shutdown_pdf_pool() -> None:
    """Gracefully shut the pool down (registered with atexit)."""
    _reset_pool()


def parse_pdf_pooled(pdf_bytes: bytes, *, timeout: float = None, **kwargs) -> dict:
    """Parse a PDF in a worker process; fall back to in-process on any failure.

    Accepts the same keyword arguments as :func:`lib.pdf_parser.core.parse_pdf`
    except ``progress_callback`` (not picklable — silently dropped; the
    synchronous /api/pdf/parse route never sets it).

    Args:
        pdf_bytes: Raw PDF bytes.
        timeout: Hard per-parse timeout (seconds). Defaults to
            ``TOFU_PDF_PARSE_TIMEOUT`` or 300s.

    Returns:
        The parse result dict (see ``core.parse_pdf``).
    """
    kwargs.pop('progress_callback', None)
    if timeout is None:
        try:
            timeout = float(os.environ.get('TOFU_PDF_PARSE_TIMEOUT', '300') or '300')
        except (ValueError, TypeError):
            timeout = 300.0
    try:
        pool = _get_pool()
        fut = pool.submit(_parse_pdf_inproc, pdf_bytes, **kwargs)
        return fut.result(timeout=timeout)
    except FuturesTimeout:
        logger.error('[PDF Pool] Parse exceeded %.0fs — resetting pool, '
                     'falling back in-process', timeout)
        _reset_pool()
    except (BrokenExecutor, OSError) as e:
        logger.error('[PDF Pool] Worker pool broken (%s) — resetting, '
                     'falling back in-process', e)
        _reset_pool()
    except Exception as e:
        logger.warning('[PDF Pool] Pooled parse failed (%s) — falling back '
                       'in-process', e, exc_info=True)
    # Fallback: in-process parse. Correct, just GIL-bound (original behaviour).
    return _parse_pdf_inproc(pdf_bytes, **kwargs)


atexit.register(shutdown_pdf_pool)
