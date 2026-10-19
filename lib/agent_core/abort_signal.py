"""Task-owned threading.Event with loop-only wake subscriptions.

Entry AbortSignal.subscribe_async. Signal remains the sole abort fact; listeners
only enqueue Future wakes, never execute cleanup under the setter's owner lock.
No worker, timer, polling loop or global registry is created. Subscriptions have
explicit disposal and retain only active loop/Future references.
"""
import asyncio
import threading


def _wake(future):
    if not future.done():future.set_result(None)


class AbortSubscription:
    __slots__=('future','_signal','_token')
    def __init__(self,signal,token,future):
        self.future,self._signal,self._token=future,signal,token
    def close(self):
        signal,self._signal=self._signal,None
        if signal is not None:
            with signal._observer_lock:
                if signal._observers is not None:
                    signal._observers.pop(self._token,None)
                    if not signal._observers:signal._observers=None


class AbortSignal(threading.Event):
    def __init__(self):
        super().__init__()
        self._observer_lock=threading.Lock()
        self._observers=None

    def subscribe_async(self):
        loop=asyncio.get_running_loop();future=loop.create_future();token=object()
        with self._observer_lock:
            if self.is_set():future.set_result(None)
            else:
                if self._observers is None:self._observers={}
                self._observers[token]=(loop,future)
        return AbortSubscription(self,token,future)

    def set(self):
        # Publish the original Event first. Registration checks it under this
        # lock, so set/subscribe cannot lose a wake even on different threads.
        super().set()
        with self._observer_lock:
            observers=self._observers
            self._observers=None
        for loop,future in (observers or {}).values():
            try:loop.call_soon_threadsafe(_wake,future)
            except RuntimeError:
                # A disposed loop has no live executor to wake. Event remains
                # set; a later subscriber observes it immediately.
                pass
