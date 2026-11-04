"""Invocation-owned observation of one inherited cancellation source.

Entry: ChildCancellationScope is a callable control port shared by one graph or
Swarm; watch(task) borrows it for one physically active child. A scope has one
observer while borrowed, no global registry and no idle worker. Plain external
predicates remain supported through a private scope. Local Stop, deadlines and
delivery failures remain child-owned; a closed watch cannot receive late work.

Dependencies: standard-library threading and the canonical invocation verdicts.
The predicate must only observe its owner's cancellation, never admit effects.
"""
from __future__ import annotations

import threading


# Existing child-invocation control-observation cadence, formerly one polling
# thread per child. External predicates have no notification contract. This is
# the same liveness fallback, not a capacity or performance tuning parameter.
_INHERITED_CONTROL_POLL_SECONDS = 0.1


class _Generation:
    def __init__(self, scope):
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.watches = set()
        self.subscription = None
        self.thread = threading.Thread(
            target=scope._observe, args=(self,), name='child-cancellation', daemon=True)


class ChildCancellationWatch:
    """Disposable membership; close joins any signal already in progress."""

    def __init__(self, scope, generation, task):
        self._scope, self._generation = scope, generation
        self._task = task
        self._lock = threading.Lock()
        self._stopped = False

    def _observe(self, requested, failure):
        from lib.tasks_pkg.invocation import invocation_deadline_requested
        with self._lock:
            task = self._task
            if task is None or self._stopped:
                return False
            if task.get('aborted') or task['abort_event'].is_set():
                task['aborted'] = True
                task['abort_event'].set()
            elif requested:
                if failure is not None:
                    task['_invocationDeliveryError'] = failure
                task['aborted'] = True
                task['abort_event'].set()
            elif not invocation_deadline_requested(task):
                return True
            self._stopped = True
            return False

    def close(self):
        with self._lock:
            if self._task is None:
                return
            self._task = None
            scope, generation = self._scope, self._generation
            self._scope = self._generation = None
        scope._release(generation, self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class ChildCancellationScope:
    """One explicitly borrowed parent control source, reusable across waves."""

    def __init__(self, abort_check, *, source_signal=None):
        if not callable(abort_check):
            raise TypeError('Child cancellation requires a callable owner control port')
        self._abort_check = abort_check
        from lib.agent_core.abort_signal import AbortSignal
        # Older callers may expose a plain Event or only a predicate. Their
        # source still uses the inherited control observation contract.
        self._source_signal = source_signal if isinstance(source_signal, AbortSignal) else None
        self._lock = threading.Lock()
        self._generation = None
        self._generations = set()

    def __call__(self):
        # Effect/admission boundaries observe the live owner, never a cached
        # answer from the background propagation observer.
        return self._abort_check()

    @property
    def child_count(self):
        with self._lock:
            return sum(len(generation.watches) for generation in self._generations)

    @property
    def observer_count(self):
        with self._lock:
            return sum(generation.thread.is_alive() for generation in self._generations)

    def watch(self, task):
        with self._lock:
            generation = self._generation
            fresh = generation is None
            if fresh:
                generation = self._generation = _Generation(self)
                self._generations.add(generation)
            watch = ChildCancellationWatch(self, generation, task)
            generation.watches.add(watch)
            try:
                if fresh:
                    if self._source_signal is not None:
                        generation.subscription = self._source_signal.subscribe_event(generation.wake)
                    generation.thread.start()
            except BaseException:
                generation.watches.remove(watch)
                self._generations.remove(generation)
                self._generation = None
                if generation.subscription is not None:
                    generation.subscription.close()
                raise
            generation.wake.set()
            return watch

    def _release(self, generation, watch):
        with self._lock:
            generation.watches.discard(watch)
            last = not generation.watches
            if last:
                generation.stop.set()
                if self._generation is generation:
                    self._generation = None
            generation.wake.set()
        if last:
            # Join outside both locks. A later wave may already have acquired
            # its own generation; the retired one cannot observe that wave.
            generation.thread.join()
            with self._lock:
                self._generations.discard(generation)

    def _observe(self, generation):
        active = True
        try:
            while True:
                generation.wake.wait(_INHERITED_CONTROL_POLL_SECONDS if active else None)
                generation.wake.clear()
                if generation.stop.is_set():
                    return
                failure = None
                try:
                    requested = bool(self._abort_check())
                except Exception as error:
                    failure, requested = error, True
                with self._lock:
                    watches = tuple(generation.watches)
                active = False
                for watch in watches:
                    active = watch._observe(requested, failure) or active
        except BaseException as error:
            # A control observer may not disappear while its children retain
            # work. Preserve the failure on each live child before unwinding.
            with self._lock:
                watches = tuple(generation.watches)
            for watch in watches:
                watch._observe(True, error)
            raise
        finally:
            if generation.subscription is not None:
                generation.subscription.close()
            with self._lock:
                if self._generation is generation:
                    self._generation = None
