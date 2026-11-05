"""Own one declared phase's startup, cancellation, sessions and joined teardown.

WorkloadWorkers keeps the benchmark's explicit carriers and timing boundary;
this is not an executor, a queue or a worker-admission policy.
"""
from __future__ import annotations

import threading
import time


class WorkloadWorkers:
    def __init__(self, errors: list[str]):
        self.start = threading.Event()
        self.cancelled = threading.Event()
        self.errors = errors
        self._workers: list[tuple[threading.Thread, threading.Event]] = []

    def add(self, name, target, *args):
        finished = threading.Event()

        def run():
            try:
                # A start interrupted before bootstrap may still launch later.
                # Fence that carrier before it can acquire an engine session.
                if not self.cancelled.is_set():
                    target(*args)
            except BaseException as error:
                self.errors.append(f'{name}: {type(error).__name__}: {error}')
                raise  # Preserve the thread's existing diagnostic hook.
            finally:
                finished.set()

        thread = threading.Thread(target=run, name=name, daemon=True)
        self._workers.append((thread, finished))

    def run_worker(self, engine, label, operation):
        """Settle an owned session once, retaining execution and close failures."""
        session = None
        owned = False
        try:
            if self.cancelled.is_set():
                return
            opener = getattr(engine, 'open_session', None)
            owned = opener is not None
            session = opener() if owned else engine
            self.start.wait()
            if not self.cancelled.is_set():
                operation(session)
        except Exception as error:  # noqa: BLE001
            self.errors.append(f'{label}: {type(error).__name__}: {error}')
        finally:
            if owned and session is not None:
                try:
                    closer = getattr(session, 'close', None)
                    if closer is not None:
                        closer()
                except Exception as error:  # noqa: BLE001
                    self.errors.append(f'{label} close: {type(error).__name__}: {error}')

    @staticmethod
    def _settle(attempted, failure):
        for thread, finished in attempted:
            if thread.ident is None:
                # No target can have passed the cancellation fence without an
                # assigned thread identity. A late bootstrap is inert.
                continue
            while True:
                try:
                    # A join interrupted by a signal is not a completion proof.
                    # Wait for the callback and its session finalizer as well.
                    finished.wait()
                    thread.join()
                    break
                except (KeyboardInterrupt, SystemExit) as error:
                    if failure is None:
                        failure = error
        return failure

    def run(self, timeout):
        attempted = []
        phase_started = None
        failure = None
        try:
            for worker in self._workers:
                attempted.append(worker)
                worker[0].start()
            phase_started = time.perf_counter_ns()
            self.start.set()
            deadline = time.monotonic() + timeout
            for thread, _finished in attempted:
                thread.join(timeout=max(1.0, deadline - time.monotonic()))
            stuck = [thread.name for thread, finished in attempted
                     if not finished.is_set() or thread.is_alive()]
            if stuck:
                self.errors.append('threads past join deadline: ' + ', '.join(stuck))
        except BaseException as error:
            failure = error
        finally:
            # A failed/expired phase admits no next operation. In-flight calls
            # retain their original deadlines and must finish before disposal.
            self.cancelled.set()
            self.start.set()
            failure = self._settle(attempted, failure)
        if failure is not None:
            raise failure
        return phase_started
