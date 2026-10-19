"""Ordered subscription lanes with independent demand HTTP dispatch.

DeliveryCoordinator owns pending references, physical Future completions and
retry timing on one coordinator thread. Injected ports own current subscription
validation, HTTP effects, retry classification, clock and physical worker close.
No active-count quota, tenant global or persistence authority lives here.
"""
from collections import deque
from dataclasses import dataclass, field
from queue import Empty


@dataclass(frozen=True)
class _Completion:
    item: dict
    future: object


@dataclass
class _Lane:
    items: deque = field(default_factory=deque)
    future: object = None


class DeliveryCoordinator:
    def __init__(self, *, queue, stop, submit, is_active, deliver,
                 retryable, delayed, schedule_retry, clock, on_error):
        self.queue, self.stop, self.submit = queue, stop, submit
        self.is_active, self.deliver, self.retryable = is_active, deliver, retryable
        self.delayed, self.schedule_retry = delayed, schedule_retry
        self.clock, self.on_error = clock, on_error
        self.lanes = {}

    @staticmethod
    def identity(item):
        sub = item.get('sub') or {}
        return str(sub.get('owner_user_id') or ''), str(sub.get('id') or '')

    def _attempt(self, item):
        # Re-check revocation at physical entry, not while awaiting an idle
        # carrier. An already-running HTTP request retains physical ownership.
        if self.stop.is_set() or not self.is_active(item.get('sub') or {}):
            return None
        return self.deliver(item)

    def _start(self, lane):
        item = lane.items[0]
        try:
            future = self.submit(self._attempt, item)
        except Exception as error:
            self.on_error(item, error)
            self.schedule_retry(self.delayed, item, now=self.clock())
            return
        lane.future = future
        # Completion is processed on the coordinator only. Publishing it never
        # mutates a lane from a worker or serializes independent HTTP effects.
        future.add_done_callback(lambda done: self.queue.put_nowait(_Completion(item, done)))

    def _completed(self, completion):
        item, future = completion.item, completion.future
        key = self.identity(item)
        lane = self.lanes.get(key)
        if lane is None or lane.future is not future:
            return  # completion from a closed generation
        lane.future = None
        try:
            outcome = future.result()
        except Exception as error:
            self.on_error(item, error)
            outcome = False
        if outcome is False and self.retryable(item):
            self.schedule_retry(self.delayed, item, now=self.clock())
            return
        if outcome is None:
            lane.items.clear()  # deletion/disable revokes this identity's lane
        else:
            lane.items.popleft()
        if lane.items:
            self._start(lane)
        else:
            self.lanes.pop(key, None)

    def run(self):
        try:
            while not self.stop.is_set():
                item = self.delayed.pop_ready(self.clock())
                if item is not None:
                    lane = self.lanes.get(self.identity(item))
                    if lane is not None and lane.items and lane.items[0] is item and lane.future is None:
                        self._start(lane)
                    item = None
                    continue
                next_ready = self.delayed.next_ready_at()
                timeout = 5.0 if next_ready is None else min(5.0, max(0.0, next_ready - self.clock()))
                try:
                    item = self.queue.get(timeout=timeout)
                except Empty:
                    continue
                try:
                    if item is None or self.stop.is_set():
                        continue
                    if isinstance(item, _Completion):
                        self._completed(item)
                    else:
                        key = self.identity(item)
                        lane = self.lanes.setdefault(key, _Lane())
                        lane.items.append(item)
                        if len(lane.items) == 1:
                            self._start(lane)
                finally:
                    self.queue.task_done()
                    item = None  # idle coordination cannot pin a completed body
        finally:
            # Worker arguments/completion callbacks retain running effects.
            # The injected physical owner joins them at the shutdown boundary.
            self.lanes.clear()
