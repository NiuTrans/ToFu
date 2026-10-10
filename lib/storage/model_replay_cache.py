"""Bounded node-local repository for disposable model replay projections.

This cache is not transcript authority. Callers validate its Turn witnesses
through Sidecar before use. One latest entry per owner/conversation/lane,
atomic replacement, a 24-hour TTL, and a launch-probed total disk budget bound
retention. Eviction/restart on another node is an ordinary cache miss.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time

from lib.json_store import locked_path, write_bytes_atomic
from lib.runtime_paths import data_root
from runtime_guards import resolve_resource_budget

MAX_ENTRY_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 256
TTL_SECONDS = 86400
_HEADER_PREFIX = b'{"sha256":"'
_HEADER_SUFFIX = b'","payload":'
_PAYLOAD_OFFSET = len(_HEADER_PREFIX) + 64 + len(_HEADER_SUFFIX)


class ModelReplayCache:
    """Backend-neutral get/put boundary; keys always include repository owner."""

    def __init__(self, *, root=None, max_bytes=None, clock=time.time):
        self.root = Path(root) if root is not None else Path(data_root()) / 'model_replay_cache'
        self.max_bytes = (max(0, int(max_bytes)) if max_bytes is not None else
                          resolve_resource_budget('TOFU_MODEL_REPLAY_CACHE_MAX_MIB',
                                                  minimum=1, maximum=256) * 1024 * 1024)
        self.clock = clock

    def _path(self, user_id, conversation_id, lane_id):
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
            raise ValueError('model replay cache requires an explicit owner')
        key = json.dumps([user_id, conversation_id, lane_id], ensure_ascii=False)
        return self.root / (hashlib.sha256(key.encode()).hexdigest() + '.json')

    def get(self, *, user_id, conversation_id, lane_id):
        path = self._path(user_id, conversation_id, lane_id)
        try:
            if path.stat().st_size > min(MAX_ENTRY_BYTES, self.max_bytes):
                return None
            with path.open('rb') as stream:
                raw = stream.read(MAX_ENTRY_BYTES + 1)
            if len(raw) > MAX_ENTRY_BYTES:
                return None
            if (not raw.startswith(_HEADER_PREFIX) or not raw.endswith(b'}')
                    or raw[len(_HEADER_PREFIX) + 64:_PAYLOAD_OFFSET] != _HEADER_SUFFIX):
                return None
            payload = raw[_PAYLOAD_OFFSET:-1]
            if hashlib.sha256(payload).hexdigest().encode('ascii') != raw[len(_HEADER_PREFIX):len(_HEADER_PREFIX) + 64]:
                return None
            value = json.loads(payload)
            if (not isinstance(value, dict) or value.get('owner') != user_id
                    or value.get('expires', 0) <= self.clock()):
                return None
            return value.get('projection')
        except (OSError, ValueError, TypeError):
            return None

    def put(self, *, user_id, conversation_id, lane_id, projection):
        path = self._path(user_id, conversation_id, lane_id)
        now = self.clock()
        value = {'owner': user_id, 'expires': now + TTL_SECONDS, 'projection': projection}
        # Stop serialization at the budget; never allocate a second unbounded
        # conversation-sized string just to decide that it cannot be cached.
        encoded = bytearray()
        limit = min(MAX_ENTRY_BYTES, self.max_bytes) - _PAYLOAD_OFFSET - 1
        for part in json.JSONEncoder(ensure_ascii=False, separators=(',', ':')).iterencode(value):
            if len(part) + len(encoded) > limit:
                return False
            chunk = part.encode('utf-8')
            if len(encoded) + len(chunk) > limit:
                return False
            encoded.extend(chunk)
        serialized = (_HEADER_PREFIX + hashlib.sha256(encoded).hexdigest().encode('ascii')
                      + _HEADER_SUFFIX + bytes(encoded) + b'}')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # One bounded lock file, including across processes on shared POSIX
        # volumes. No per-conversation lock files or in-memory payload index.
        with locked_path(str(self.root / 'budget')):
            entries = []
            for candidate in self.root.glob('*.json'):
                if candidate == path:
                    continue  # atomic replace keeps the old entry on failure
                stat = candidate.stat()
                if now - stat.st_mtime >= TTL_SECONDS:
                    candidate.unlink(missing_ok=True)
                else:
                    entries.append((stat.st_mtime, candidate, stat.st_size))
            total = sum(row[2] for row in entries)
            entries.sort()
            while entries and (total + len(serialized) > self.max_bytes
                               or len(entries) >= MAX_ENTRIES):
                _, evicted, size = entries.pop(0)
                evicted.unlink(missing_ok=True)
                total -= size
            write_bytes_atomic(str(path), serialized, fsync=False, mode=0o600)
        return True
