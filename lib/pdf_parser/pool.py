"""lib/pdf_parser/pool.py — Off-load CPU-bound PDF parsing to a process pool.

PyMuPDF (the MuPDF C core) is NOT thread-safe, so every in-process parse runs
serialised behind ``_common.PYMUPDF_LOCK`` AND pins the GIL for the whole
parse — starving the event loop and every other sync route handler thread.

Running the parse in a separate process removes both problems at once: each
worker has its own interpreter (its own GIL + its own MuPDF lock → genuine
parallelism) and the CPU work leaves the web process entirely.

Submission is bounded before compressed PDF bytes enter the executor. A timed
out Future keeps its admission lease until it actually settles, so repeated
timeouts cannot create overlapping generations or a second in-process parse.
Only pool creation/submission failure (before work starts) falls back once.

Environment:
    TOFU_PDF_PROCESSES     — launch-derived worker count (hard ceiling 16)
    TOFU_PDF_PARSE_CAPACITY — aggregate running + queued Future ceiling
    TOFU_PDF_MP_START      — multiprocessing start method (default: 'spawn')
    TOFU_PDF_PARSE_TIMEOUT — launch-derived per-caller wait ceiling
    TOFU_PDF_WORKER_IDLE_SECONDS — quiet window before children retire
"""

import atexit
import math
import os
import threading
from concurrent.futures import (BrokenExecutor, ProcessPoolExecutor,
                                 TimeoutError as FuturesTimeout)

from lib.log import get_logger
from lib.pdf_parser.admission import (
    CLASSIC_PDF_ADMISSION,
    PdfParseCapacityExceeded,
    PdfParseTimeoutError,
)
from lib.pdf_parser.core import _parse_pdf_without_admission as _parse_pdf_inproc
from lib.pdf_parser.policy import (
    classic_pdf_worker_idle_seconds,
    resolve_classic_pdf_budget,
)

logger = get_logger(__name__)

__all__ = [
    'PdfParseCapacityExceeded',
    'PdfParseTimeoutError',
    'parse_pdf_pooled',
    'pdf_pool_metrics',
    'shutdown_pdf_pool',
]

_POOL = None
_POOL_LOCK = threading.Lock()
_ADMISSION = CLASSIC_PDF_ADMISSION
_POOL_FUTURES = 0
_POOL_ACTIVITY_TOKEN = 0
_POOL_IDLE_TIMER = None


class _PoolFutureSettlement:
    """Settle aggregate admission and pool residency exactly once."""

    def __init__(self, lease) -> None:
        self._lease = lease
        self._lock = threading.Lock()
        self._settled = False

    def settle(self) -> None:
        with self._lock:
            if self._settled:
                return
            self._settled = True
        _pool_future_settled(self._lease)


def _max_workers() -> int:
    return resolve_classic_pdf_budget().processes


def pdf_pool_metrics() -> dict[str, int | bool]:
    """Expose low-cardinality residency evidence without PDF identifiers."""
    budget = resolve_classic_pdf_budget()
    result: dict[str, int | bool] = _ADMISSION.snapshot()
    with _POOL_LOCK:
        pool_started = _POOL is not None
        pool_unfinished = _POOL_FUTURES
        idle_retirement_scheduled = _POOL_IDLE_TIMER is not None
    result.update({
        'workers': budget.processes,
        'unfinished_capacity': budget.unfinished_capacity,
        'pool_started': pool_started,
        'pool_unfinished': pool_unfinished,
        'idle_retirement_scheduled': idle_retirement_scheduled,
    })
    return result


def _invalidate_idle_retirement_locked() -> None:
    """Cancel a stale retirement generation while holding ``_POOL_LOCK``."""
    global _POOL_ACTIVITY_TOKEN, _POOL_IDLE_TIMER
    _POOL_ACTIVITY_TOKEN += 1
    timer = _POOL_IDLE_TIMER
    _POOL_IDLE_TIMER = None
    if timer is not None:
        timer.cancel()


def _shutdown_detached_pool(pool, *, reason: str) -> None:
    """Request child exit without holding the lifecycle lock."""
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception as exc:
        logger.debug('[PDF Pool] %s shutdown failed: %s', reason, exc)


def _retire_idle_pool(activity_token: int) -> None:
    """Detach one still-idle pool generation and release its child RSS."""
    global _POOL, _POOL_ACTIVITY_TOKEN, _POOL_IDLE_TIMER
    with _POOL_LOCK:
        if (
            activity_token != _POOL_ACTIVITY_TOKEN
            or _POOL_FUTURES > 0
            or _POOL is None
        ):
            return
        pool = _POOL
        _POOL = None
        _POOL_IDLE_TIMER = None
        _POOL_ACTIVITY_TOKEN += 1
    logger.info('[PDF Pool] Retiring idle process pool')
    _shutdown_detached_pool(pool, reason='idle retirement')


def _schedule_idle_retirement_locked() -> None:
    """Schedule retirement for the current idle generation under the lock."""
    global _POOL_ACTIVITY_TOKEN, _POOL_IDLE_TIMER
    if _POOL is None or _POOL_FUTURES > 0:
        return
    idle_seconds = classic_pdf_worker_idle_seconds()
    if idle_seconds <= 0:
        return
    _invalidate_idle_retirement_locked()
    activity_token = _POOL_ACTIVITY_TOKEN
    timer = threading.Timer(
        idle_seconds,
        _retire_idle_pool,
        args=(activity_token,),
    )
    timer.daemon = True
    _POOL_IDLE_TIMER = timer
    timer.start()


def _get_pool() -> ProcessPoolExecutor:
    """Return the lazily-created process pool. Caller-safe under contention."""
    global _POOL
    with _POOL_LOCK:
        _invalidate_idle_retirement_locked()
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
    """Tear down a broken pool so the next submission creates a fresh one."""
    global _POOL
    with _POOL_LOCK:
        _invalidate_idle_retirement_locked()
        pool = _POOL
        _POOL = None
    if pool is not None:
        _shutdown_detached_pool(pool, reason='reset')


def shutdown_pdf_pool() -> None:
    """Gracefully shut the pool down (registered with atexit)."""
    _reset_pool()


def _pool_future_settled(lease) -> None:
    """Release payload admission and start the idle window exactly once."""
    global _POOL_FUTURES
    lease.release()
    with _POOL_LOCK:
        _POOL_FUTURES = max(0, _POOL_FUTURES - 1)
        if _POOL_FUTURES == 0:
            _schedule_idle_retirement_locked()


def parse_pdf_pooled(pdf_bytes: bytes, *, timeout: float = None, **kwargs) -> dict:
    """Parse through one finite process lane without ambiguous duplicate work.

    Accepts the same keyword arguments as :func:`lib.pdf_parser.core.parse_pdf`
    except ``progress_callback`` (not picklable — silently dropped; the
    synchronous /api/pdf/parse route never sets it).

    Args:
        pdf_bytes: Raw PDF bytes.
        timeout: Per-caller wait limit. Zero/malformed input uses the launch
            policy and larger values are clamped to that effective ceiling.

    Returns:
        The parse result dict (see ``core.parse_pdf``).
    """
    kwargs.pop('progress_callback', None)
    budget = resolve_classic_pdf_budget()
    try:
        requested_timeout = float(timeout or 0)
    except (TypeError, ValueError, OverflowError):
        requested_timeout = 0.0
    if not math.isfinite(requested_timeout) or requested_timeout <= 0:
        requested_timeout = float(budget.timeout_seconds)
    wait_seconds = min(float(budget.timeout_seconds), requested_timeout)
    lease = _ADMISSION.reserve(budget.unfinished_capacity)

    try:
        pool = _get_pool()
        fut = pool.submit(_parse_pdf_inproc, pdf_bytes, **kwargs)
    except (BrokenExecutor, OSError, RuntimeError) as exc:
        logger.error(
            '[PDF Pool] Work was not submitted (%s); resetting and using '
            'one bounded in-process fallback', exc)
        _reset_pool()
        try:
            return _parse_pdf_inproc(pdf_bytes, **kwargs)
        finally:
            lease.release()
    except BaseException:
        lease.release()
        raise

    global _POOL_FUTURES
    with _POOL_LOCK:
        _POOL_FUTURES += 1
    settlement = _PoolFutureSettlement(lease)

    # The executor's management thread may finish after this caller times out.
    # That real lifecycle—not the HTTP wait—owns compressed-payload admission.
    fut.add_done_callback(lambda _future: settlement.settle())
    try:
        return fut.result(timeout=wait_seconds)
    except FuturesTimeout as exc:
        cancelled = fut.cancel()
        logger.error(
            '[PDF Pool] Parse exceeded %.0fs (cancelled_before_start=%s); '
            'capacity remains held until the Future settles',
            wait_seconds,
            cancelled,
        )
        raise PdfParseTimeoutError(
            f'PDF parse exceeded {wait_seconds:.0f}s') from exc
    except BrokenExecutor as exc:
        logger.error(
            '[PDF Pool] Worker failed after submission; refusing an ambiguous '
            'second parse: %s', exc)
        _reset_pool()
        raise RuntimeError('PDF parse worker failed after submission') from exc
    finally:
        # Future callbacks normally settle first, but Future.set_result()
        # notifies waiters before invoking callbacks. Close that small window
        # so an immediately following request cannot observe false saturation.
        if fut.done():
            settlement.settle()


atexit.register(shutdown_pdf_pool)
