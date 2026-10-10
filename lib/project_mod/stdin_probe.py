"""Optional procfs stdin sensing, isolated from command execution control.

One lazy sensor per command, bounded process-wide by the cached agent-worker
envelope. Sensors hold immutable process/pipe ids and private observation state,
never task objects, callbacks, model authority, or command file descriptors.
Closing delivery fences late samples; a blocked sensor retains its physical
pool slot until it returns. This is not a command owner or a kill mechanism.
"""
from __future__ import annotations

import threading

from lib.log import get_logger

logger = get_logger(__name__)


class ProbePool:
    """No queue or eager workers; physically unfinished sensors retain slots."""
    def __init__(self, capacity):
        self.capacity = max(1, int(capacity))
        self._lock = threading.Lock()
        self._threads = set()

    def launch(self, observe):
        def run():
            try:
                observe()
            except Exception as exc:
                logger.debug('stdin sensor unavailable: %s', type(exc).__name__)
            finally:
                with self._lock:
                    self._threads.discard(threading.current_thread())
        worker = threading.Thread(target=run, name='tofu-stdin-probe', daemon=True)
        with self._lock:
            if len(self._threads) >= self.capacity:
                return False
            self._threads.add(worker)
        try:
            worker.start()
        except BaseException:
            with self._lock:
                self._threads.discard(worker)
            raise
        return True

    @property
    def active(self):
        with self._lock:
            return len(self._threads)


_pool = None
_pool_lock = threading.Lock()


def _default_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            from runtime_guards import MAX_TASK_CONCURRENCY, resolve_resource_budget
            # Uses the existing cached launch observation and explicit override;
            # each optional sensor is lighter than an admitted agent worker.
            _pool = ProbePool(resolve_resource_budget(
                'TOFU_AGENT_WORKERS', minimum=1, maximum=MAX_TASK_CONCURRENCY))
        return _pool


class StdinProbe:
    """Poll a stateless sensor without waiting for procfs reads to return."""
    def __init__(self, detect, parent_pid, pipe_inode, *, pool=None):
        self._detect = detect
        self._pid = parent_pid
        self._inode = pipe_inode
        self._pool = pool if pool is not None else _default_pool()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._started = False
        self._reader = None
        self._generation = 0

    def _observe(self):
        while not self._stop.is_set():
            with self._lock:
                generation = self._generation
            reader = self._detect(self._pid, self._inode, stop_event=self._stop)
            with self._lock:
                if not self._closed and generation == self._generation:
                    self._reader = reader
            self._stop.wait(0.2)

    def poll(self):
        with self._lock:
            if self._closed:
                return None
            if not self._started:
                self._started = self._pool.launch(self._observe)
            reader, self._reader = self._reader, None
            return reader

    def invalidate(self):
        """A delivered answer invalidates samples started before that write."""
        with self._lock:
            self._generation += 1
            self._reader = None

    def close(self):
        """Fence delivery; the pool owns physical retirement until read exits."""
        with self._lock:
            self._closed = True
            self._generation += 1
            self._reader = None
            self._stop.set()
