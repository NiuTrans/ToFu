"""Demand-driven process isolation for classic PDF parsing.

Each busy parser owns a one-worker executor, so independent documents never
queue behind a process-count permit. Launch-probed process counts govern only
idle reuse. A timed-out caller retains its exact unfinished-work ownership until
the Future settles; worker/submission failures never start a duplicate parse.
The sole inline fallback is pool construction failure before publication.
"""
from __future__ import annotations

import atexit
import math
import multiprocessing as mp
import os
import threading
import time
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass

from lib.log import get_logger
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION, PdfParseTimeoutError
from lib.pdf_parser.core import _parse_pdf_without_admission as _parse_pdf_inproc
from lib.pdf_parser.policy import classic_pdf_worker_idle_seconds, resolve_classic_pdf_budget

logger = get_logger(__name__)

@dataclass(eq=False)
class _ParserProcess:
    executor: ProcessPoolExecutor
    busy: bool = True
    retired: bool = False
    idle_at: float = 0.0

_WORKERS: set[_ParserProcess] = set()
_POOL_LOCK = threading.RLock()
_ADMISSION = CLASSIC_PDF_ADMISSION
_POOL_FUTURES = 0
_POOL_ACTIVITY_TOKEN = 0
_POOL_IDLE_TIMER = None


def _invalidate_idle_retirement_locked() -> None:
    global _POOL_ACTIVITY_TOKEN, _POOL_IDLE_TIMER
    _POOL_ACTIVITY_TOKEN += 1
    timer, _POOL_IDLE_TIMER = _POOL_IDLE_TIMER, None
    if timer is not None:
        timer.cancel()


def _shutdown_detached_pool(worker: _ParserProcess, *, reason: str, wait=False) -> None:
    try:
        worker.executor.shutdown(wait=wait, cancel_futures=True)
    except Exception as error:
        logger.debug('[PDF Pool] %s shutdown failed: %s', reason, error)
        if wait:
            raise


def _retire_idle_pool(activity_token: int) -> None:
    global _POOL_IDLE_TIMER
    with _POOL_LOCK:
        if activity_token != _POOL_ACTIVITY_TOKEN:
            return
        _POOL_IDLE_TIMER = None
        idle = [worker for worker in _WORKERS if not worker.busy]
        for worker in idle:
            worker.retired = True
            _WORKERS.remove(worker)
    for worker in idle:
        _shutdown_detached_pool(worker, reason='idle retirement')


def _schedule_idle_retirement_locked() -> None:
    if not any(not worker.busy for worker in _WORKERS):
        return
    idle_seconds = classic_pdf_worker_idle_seconds()
    if idle_seconds <= 0:
        return
    global _POOL_IDLE_TIMER
    _invalidate_idle_retirement_locked()
    timer = threading.Timer(idle_seconds, _retire_idle_pool, args=(_POOL_ACTIVITY_TOKEN,))
    timer.daemon = True
    _POOL_IDLE_TIMER = timer
    try:
        timer.start()
    except BaseException:
        _POOL_IDLE_TIMER = None
        timer.cancel()
        raise


def _get_pool() -> _ParserProcess:
    global _POOL_FUTURES
    retire = []
    with _POOL_LOCK:
        _invalidate_idle_retirement_locked()
        worker = max((item for item in _WORKERS if not item.busy and not item.retired),
                     key=lambda item: item.idle_at, default=None)
        if worker is None:
            method = os.environ.get('TOFU_PDF_MP_START', 'spawn').strip() or 'spawn'
            try:
                context = mp.get_context(method)
            except ValueError:
                logger.warning('[PDF Pool] unknown start method %r; using spawn', method)
                context = mp.get_context('spawn')
            worker = _ParserProcess(ProcessPoolExecutor(max_workers=1, mp_context=context))
            _WORKERS.add(worker)
        worker.busy = True
        _POOL_FUTURES += 1
        try:
            _schedule_idle_retirement_locked()
        except Exception as error:
            # Idle maintenance cannot refuse the newly claimed parser.
            retire = [item for item in _WORKERS if not item.busy]
            for item in retire:
                item.retired = True
                _WORKERS.remove(item)
            logger.warning('[PDF Pool] idle timer could not start: %s', error)
        except BaseException:
            worker.busy = False
            _POOL_FUTURES -= 1
            raise
    for item in retire:
        _shutdown_detached_pool(item, reason='idle timer failure')
    return worker


def _reset_pool() -> None:
    with _POOL_LOCK:
        _invalidate_idle_retirement_locked()
        workers = tuple(_WORKERS)
        _WORKERS.clear()
        for worker in workers:
            worker.retired = True
    for worker in workers:
        _shutdown_detached_pool(worker, reason='shutdown')


def shutdown_pdf_pool() -> None:
    _reset_pool()


class _PoolFutureSettlement:
    def __init__(self, lease, worker: _ParserProcess) -> None:
        self._lease = lease
        self._worker = worker
        self._lock = threading.Lock()
        self._settled = False

    def settle(self) -> None:
        global _POOL_FUTURES
        with self._lock:
            if self._settled:
                return
            self._settled = True
        self._lease.release()
        worker = self._worker
        retire = False
        with _POOL_LOCK:
            _POOL_FUTURES -= 1
            worker.busy = False
            worker.idle_at = time.monotonic()
            if worker.retired:
                retire = True
            elif sum(not item.busy for item in _WORKERS) > resolve_classic_pdf_budget().processes:
                worker.retired = True
                _WORKERS.discard(worker)
                retire = True
            try:
                _schedule_idle_retirement_locked()
            except Exception as error:
                # A failed timer cannot strand a reusable process indefinitely.
                worker.retired = True
                _WORKERS.discard(worker)
                retire = True
                logger.warning('[PDF Pool] idle timer could not start: %s', error)
        if retire:
            _shutdown_detached_pool(worker, reason='settlement')


def _submission_failed(worker: _ParserProcess, settlement: _PoolFutureSettlement) -> None:
    """Join ambiguous submission ownership before releasing its unfinished work."""
    with _POOL_LOCK:
        worker.retired = True
        _WORKERS.discard(worker)
    def clean_up():
        while True:
            try:
                _shutdown_detached_pool(worker, reason='submission failure', wait=True)
            except Exception as error:
                # Failed joining is not evidence that an ambiguous job ended.
                logger.warning('[PDF Pool] unresolved submission cleanup; retrying: %s', error)
                threading.Event().wait(.1)
                continue
            settlement.settle()
            return
    cleanup = threading.Thread(target=clean_up, name='pdf-submission-cleanup', daemon=True)
    try:
        cleanup.start()
    except BaseException:
        clean_up()
        raise


def pdf_pool_metrics() -> dict[str, int | bool]:
    result = _ADMISSION.snapshot()
    with _POOL_LOCK:
        result.update(workers=len(_WORKERS), idle_reuse_workers=resolve_classic_pdf_budget().processes,
            unfinished_capacity=0, pool_started=bool(_WORKERS), pool_unfinished=_POOL_FUTURES,
            idle_retirement_scheduled=_POOL_IDLE_TIMER is not None)
    return result


def parse_pdf_pooled(pdf_bytes: bytes, *, timeout: float | None = None, **kwargs) -> dict:
    kwargs.pop('progress_callback', None)
    budget = resolve_classic_pdf_budget()
    if timeout is not None and (type(timeout) not in (int, float)
            or not math.isfinite(timeout) or timeout < 0):
        raise ValueError('PDF parse timeout must be a nonnegative finite number or None')
    explicit = [
        float(value) for value in (timeout, budget.timeout_seconds)
        if value is not None and value > 0
    ]
    wait_seconds = min(explicit) if explicit else None
    lease = _ADMISSION.reserve()
    try:
        worker = _get_pool()
    except (BrokenExecutor, OSError, RuntimeError) as error:
        logger.error('[PDF Pool] construction failed before publication; inline fallback: %s', error)
        try:
            return _parse_pdf_inproc(pdf_bytes, **kwargs)
        finally:
            lease.release()
    except BaseException:
        lease.release()
        raise
    settlement = _PoolFutureSettlement(lease, worker)
    try:
        future = worker.executor.submit(_parse_pdf_inproc, pdf_bytes, **kwargs)
    except BaseException:
        _submission_failed(worker, settlement)
        raise
    def completed(completed_future):
        if not completed_future.cancelled() and isinstance(completed_future.exception(), BrokenExecutor):
            with _POOL_LOCK:
                worker.retired = True
                _WORKERS.discard(worker)
        settlement.settle()
    future.add_done_callback(completed)
    try:
        return future.result(timeout=wait_seconds)
    except FuturesTimeout as error:
        if future.done():
            raise  # This TimeoutError belongs to the completed parser itself.
        future.cancel()
        raise PdfParseTimeoutError(f'PDF parse exceeded {wait_seconds:.0f}s') from error
    except BrokenExecutor as error:
        with _POOL_LOCK:
            worker.retired = True
            _WORKERS.discard(worker)
        _shutdown_detached_pool(worker, reason='broken worker')
        raise RuntimeError('PDF parse worker failed after submission') from error
    finally:
        if future.done():
            settlement.settle()

atexit.register(shutdown_pdf_pool)
__all__ = ['PdfParseTimeoutError', 'parse_pdf_pooled', 'pdf_pool_metrics', 'shutdown_pdf_pool']
