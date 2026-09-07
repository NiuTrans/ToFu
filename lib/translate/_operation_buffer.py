"""Finite operation buffer for one incremental-translation accumulator.

Segment previews are reconstructible and may be coalesced or evicted under
pressure.  A finalize/stamp/cancel terminal handoff is never dropped: it
reserves room by evicting the oldest queued previews.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any


class IncrementalOperationBuffer:
    def __init__(self, capacity: int) -> None:
        self.capacity = max(2, int(capacity))
        self._condition = threading.Condition()
        self._items: deque[Any] = deque()
        self._terminal_queued = False
        self._dropped_segments = 0
        self._peak_depth = 0

    def put_segment(self, key: int | str, text: str) -> int:
        """Queue the newest preview; return evicted count, or -1 after terminal.

        ``key`` is a collision-free segment blockId. Integer ``llmRound`` keys
        remain accepted for compatibility with old producers. Identical keys
        replace their queued predecessor.
        """
        item = ('segment', key, text)
        with self._condition:
            if self._terminal_queued:
                return -1
            for index in range(len(self._items) - 1, -1, -1):
                existing = self._items[index]
                if existing[0] == 'segment' and existing[1] == key:
                    self._items[index] = item
                    return 0
            dropped = 0
            if len(self._items) >= self.capacity:
                self._items.popleft()
                self._dropped_segments += 1
                dropped = 1
            self._items.append(item)
            self._peak_depth = max(self._peak_depth, len(self._items))
            self._condition.notify()
            return dropped

    def put_terminal(
        self,
        item: Any,
        *,
        replace: bool = False,
        preserve_segment_keys: frozenset[int | str] = frozenset(),
    ) -> int:
        """Queue a terminal item, evicting previews so delivery is guaranteed.

        ``preserve_segment_keys`` keeps explicitly terminal-owned enrichment
        (currently the final reasoning block) immediately before the handoff.
        """
        with self._condition:
            if self._terminal_queued and not replace:
                return -1
            dropped = 0
            if replace:
                retained = deque(
                    queued for queued in self._items
                    if (
                        isinstance(queued, tuple)
                        and queued[0] == 'segment'
                        and queued[1] in preserve_segment_keys
                    )
                )
                dropped = sum(
                    1 for queued in self._items
                    if isinstance(queued, tuple)
                    and queued[0] == 'segment'
                    and queued[1] not in preserve_segment_keys
                )
                self._items = retained
            else:
                while len(self._items) >= self.capacity:
                    self._items.popleft()
                    dropped += 1
            self._dropped_segments += dropped
            self._items.append(item)
            self._terminal_queued = True
            self._peak_depth = max(self._peak_depth, len(self._items))
            self._condition.notify()
            return dropped

    def get(self, timeout: float) -> Any:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: bool(self._items), timeout=max(0.0, float(timeout)))
            if not ready:
                raise queue.Empty
            return self._items.popleft()

    def snapshot(self) -> dict[str, int | bool]:
        with self._condition:
            return {
                'capacity': self.capacity,
                'depth': len(self._items),
                'peakDepth': self._peak_depth,
                'droppedSegments': self._dropped_segments,
                'terminalQueued': self._terminal_queued,
            }
