"""Task-owned derived image LRU; no process cache or source-byte retention.

The caller owns lifetime and owner identity. Lean defaults apply on every host;
constructor overrides are clamped to hard ceilings. Entries contain SHA-256
content evidence and immutable wire URLs, never mutable request dictionaries.
"""
from collections import OrderedDict
import hashlib
import sys


class ImagePreparationReuse:
    DEFAULT_ENTRIES = 8
    DEFAULT_BYTES = 4 * 1024 * 1024
    HARD_ENTRIES = 32
    HARD_BYTES = 16 * 1024 * 1024

    def __init__(self, *, user_id, max_entries=DEFAULT_ENTRIES,
                 max_bytes=DEFAULT_BYTES):
        self.user_id = user_id
        self.max_entries = max(0, min(int(max_entries), self.HARD_ENTRIES))
        self.max_bytes = max(0, min(int(max_bytes), self.HARD_BYTES))
        self._entries = OrderedDict()
        self.retained_bytes = 0

    def key(self, source, policy, *, user_id):
        if user_id != self.user_id:
            raise ValueError('image preparation owner mismatch')
        return hashlib.sha256(source.encode('utf-8')).digest(), policy

    def get(self, key):
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, key, wire_url):
        # Conservative per-entry overhead includes key/policy/LRU bookkeeping.
        size = sys.getsizeof(wire_url) + sys.getsizeof(key[0]) + 1024
        previous = self._entries.pop(key, None)
        if previous is not None:
            self.retained_bytes -= previous[1]
        if not self.max_entries or size > self.max_bytes:
            return
        while self._entries and (len(self._entries) >= self.max_entries
                                 or self.retained_bytes + size > self.max_bytes):
            _, (_, dropped_size) = self._entries.popitem(last=False)
            self.retained_bytes -= dropped_size
        self._entries[key] = (wire_url, size)
        self.retained_bytes += size

    def clear(self):
        self._entries.clear()
        self.retained_bytes = 0

    def __len__(self):
        return len(self._entries)
