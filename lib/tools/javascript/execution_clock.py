"""Lazy engine epoch ticker; Stores own deadlines and active execution budgets.

Entry points: ExecutionClock.acquire/release/close. Depends only on threading.
One shared thread exists while programs own Stores; last release joins it.
The worker holds no Store, task, callback, receipt, or user identity.
"""
import threading


class ExecutionClock:
    tick_seconds = 0.01

    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.Lock()
        self.owners = 0
        self.thread = None
        self.stop = None

    def acquire(self):
        with self.lock:
            if not self.owners:
                stop = threading.Event()
                thread = threading.Thread(target=self._tick, args=(stop,),
                                          name='tofu-javascript-clock', daemon=True)
                thread.start()
                self.stop, self.thread = stop, thread
            self.owners += 1

    def _tick(self, stop):
        while not stop.wait(self.tick_seconds):
            self.engine.increment_epoch()

    def _stop_locked(self):
        if self.thread is not None:
            self.stop.set()
            # The ticker never acquires this lock. Join fences native engine close
            # and prevents overlapping tickers when a new invocation arrives.
            self.thread.join()
            self.thread = self.stop = None

    def release(self):
        with self.lock:
            self.owners -= 1
            if not self.owners:
                self._stop_locked()

    def close(self):
        with self.lock:
            self._stop_locked()
