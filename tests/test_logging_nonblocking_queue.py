"""Proves the production logging path is NON-BLOCKING under a log storm.

Root cause guarded: in production, WARNING/ERROR from every thread funnels
through file handlers whose ``error.log`` lives on a FUSE/NFS mount. Those
handlers do SYNCHRONOUS I/O under a per-handler lock, so a storm (a total
upstream 502 outage emitting thousands of lines) serializes EVERY logging
thread behind slow network writes — including the sync threads serving
``GET /`` and the health/conversation endpoints. That turns any log storm into
a dead frontend ("backend alive, frontend can't be served").

Fix (server.py): the root logger gets a bounded queue handler whose
``emit()`` is a non-blocking ``put_nowait()``; a background
``QueueListener`` thread drains the queue and does the actual slow I/O. A
logging caller therefore returns in microseconds regardless of how slow /
backed-up the sinks are.

This suite reconstructs the EXACT production wiring (QueueHandler +
QueueListener over a deliberately SLOW handler) and asserts:

  1. A logging caller returns immediately even when the underlying handler
     sleeps per record — i.e. the caller is decoupled from sink latency.
  2. Compared against attaching the same slow handler DIRECTLY (the old
     synchronous config), the queued caller is dramatically faster — the
     load-bearing behaviour. (NEUTER: swap the queue for the direct handler
     and the same assertion FAILS, proving the queue is what carries it.)
  3. Ordinary bounded bursts remain lossless; saturation stays memory-bounded
     and emits one recovery summary rather than recursive tracebacks.
  4. Levels/filters and exc_info tracebacks survive the queue round-trip.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_logging_nonblocking_queue.py -q
"""
from __future__ import annotations

import logging
import queue as _queue_mod
import threading
import time
import pytest

from server import _BoundedQueueHandler, _BoundedQueueListener

pytestmark = pytest.mark.unit


class _SlowHandler(logging.Handler):
    """A handler that models a slow FUSE/NFS write: each emit sleeps.

    Records every message it actually wrote (under its own lock) so the test
    can assert nothing was lost."""

    def __init__(self, per_record_delay=0.02):
        super().__init__()
        self._delay = per_record_delay
        self.written = []
        self._lock2 = threading.Lock()

    def emit(self, record):
        time.sleep(self._delay)          # simulate slow mount I/O
        with self._lock2:
            self.written.append(self.format(record))


def _make_queued_logger(delay=0.02, capacity=1000):
    """Reconstruct the production QueueHandler + QueueListener wiring.

    Mirrors server.py: the QueueHandler carries a ``%(message)s`` formatter
    (so prepare() doesn't bake a second layout into record.msg) and the REAL
    (slow) handler owns the full layout applied once on the listener thread."""
    slow = _SlowHandler(per_record_delay=delay)
    slow.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    q = _queue_mod.Queue(maxsize=capacity)
    qh = _BoundedQueueHandler(q)
    qh.setFormatter(logging.Formatter('%(message)s'))
    listener = _BoundedQueueListener(q, slow, respect_handler_level=True)

    lg = logging.getLogger('test.nonblocking.queued.%d' % id(slow))
    lg.handlers[:] = [qh]
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg, slow, listener


def _make_direct_logger(delay=0.02):
    """The OLD synchronous config: slow handler attached DIRECTLY."""
    slow = _SlowHandler(per_record_delay=delay)
    slow.setFormatter(logging.Formatter('%(levelname)s %(message)s'))
    lg = logging.getLogger('test.nonblocking.direct.%d' % id(slow))
    lg.handlers[:] = [slow]
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg, slow


N_STORM = 200
DELAY = 0.01  # 200 records × 10ms = 2s of sink work if synchronous


class TestNonBlockingLogging:
    def test_queued_caller_returns_before_sink_drains(self):
        """A storm of N logs must return to the caller in far less than the
        N×delay the sink actually needs — proving the caller is decoupled."""
        lg, slow, listener = _make_queued_logger(delay=DELAY)
        listener.start()
        try:
            t0 = time.perf_counter()
            for i in range(N_STORM):
                lg.warning('storm line %d', i)
            caller_elapsed = time.perf_counter() - t0

            sink_work = N_STORM * DELAY  # ~2.0s
            # The caller should be near-instant (enqueue only). Allow generous
            # slack for CI, but it MUST be a small fraction of the sink work.
            assert caller_elapsed < sink_work * 0.5, (
                f'queued caller took {caller_elapsed:.3f}s — not decoupled '
                f'from {sink_work:.1f}s of sink work')
        finally:
            listener.stop()

    def test_no_records_lost_after_drain(self):
        """Every record below the queue cap reaches the sink after drain."""
        lg, slow, listener = _make_queued_logger(delay=0.001)
        listener.start()
        for i in range(N_STORM):
            lg.warning('line %d', i)
        listener.stop()  # enqueues sentinel + joins drain thread
        assert len(slow.written) == N_STORM

    def test_overload_is_bounded_and_recovery_is_summarized(self):
        """A stopped sink cannot grow retained records past the hard cap."""
        q = _queue_mod.Queue(maxsize=2)
        qh = _BoundedQueueHandler(q)
        qh.setFormatter(logging.Formatter('%(message)s'))
        for i in range(5):
            qh.emit(logging.LogRecord(
                'lib.storm', logging.INFO, __file__, 1,
                'line %d', (i,), None))
        assert q.qsize() == 2
        assert qh._dropped_pending == 3
        assert qh._dropped_total == 3

        # Make room for both the first recovered record and its drop summary.
        q.get_nowait(); q.task_done()
        q.get_nowait(); q.task_done()
        qh.emit(logging.LogRecord(
            'lib.storm', logging.INFO, __file__, 1,
            'recovered', (), None))
        records = [q.get_nowait(), q.get_nowait()]
        messages = [record.getMessage() for record in records]
        assert 'recovered' in messages
        assert any('shed 3 record(s)' in msg for msg in messages)
        assert qh._dropped_pending == 0

    def test_queue_is_faster_than_direct_and_neuter(self):
        """Load-bearing comparison + NEUTER.

        Queued caller (production) must be much faster than the same storm
        through the DIRECT handler (old synchronous config). The direct path
        is the NEUTER: it removes the queue and the decoupling vanishes."""
        # Queued path
        lg_q, slow_q, listener = _make_queued_logger(delay=DELAY)
        listener.start()
        try:
            t0 = time.perf_counter()
            for i in range(N_STORM):
                lg_q.warning('q %d', i)
            queued_elapsed = time.perf_counter() - t0
        finally:
            listener.stop()

        # NEUTER: direct (synchronous) path — the caller eats the full sink cost
        lg_d, slow_d = _make_direct_logger(delay=DELAY)
        t0 = time.perf_counter()
        for i in range(N_STORM):
            lg_d.warning('d %d', i)
        direct_elapsed = time.perf_counter() - t0

        # The synchronous caller pays ~N×delay; the queued caller pays ~enqueue.
        assert direct_elapsed > queued_elapsed * 5, (
            f'expected direct ({direct_elapsed:.3f}s) >> queued '
            f'({queued_elapsed:.3f}s); queue not load-bearing')

    def test_queued_output_is_not_double_formatted(self):
        """Regression: basicConfig() attaches BASIC_FORMAT to a formatter-less
        QueueHandler, so prepare() would bake ``LEVEL:name:msg`` into record.msg
        and the real handler would format it AGAIN → doubled lines. The
        ``%(message)s`` formatter on the QueueHandler prevents that: queued
        output must be byte-identical to the direct (synchronous) output."""
        # Queued path (mirrors production wiring)
        lg_q, slow_q, listener = _make_queued_logger(delay=0.0)
        listener.start()
        try:
            lg_q.warning('hello %s', 'world')
            time.sleep(0.2)
        finally:
            listener.stop()

        # Direct path — the ground-truth single-format output
        lg_d, slow_d = _make_direct_logger(delay=0.0)
        lg_d.warning('hello %s', 'world')

        assert slow_q.written == ['WARNING hello world']
        assert slow_q.written == slow_d.written, (
            f'queued output {slow_q.written!r} != direct {slow_d.written!r} '
            '— double-formatting regression')

    def test_levels_and_traceback_survive_queue(self):
        """Handler level filtering + exc_info tracebacks round-trip the queue."""
        lg, slow, listener = _make_queued_logger(delay=0.0)
        slow.setLevel(logging.WARNING)  # DEBUG/INFO must be dropped at sink
        listener.start()
        try:
            lg.info('should be filtered by handler level')
            try:
                raise ValueError('boom-xyz')
            except ValueError:
                lg.error('with traceback', exc_info=True)
            time.sleep(0.2)  # let the listener drain
        finally:
            listener.stop()

        joined = '\n'.join(slow.written)
        assert 'should be filtered' not in joined
        assert 'with traceback' in joined
        assert 'ValueError: boom-xyz' in joined  # traceback survived
        assert 'Traceback (most recent call last)' in joined


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
