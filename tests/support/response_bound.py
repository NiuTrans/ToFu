"""Model the storage authority's deterministic per-response byte bound.

Native TofuDB refuses a read whose response exceeds MAX_TURN_RESPONSE_BYTES
(64 MiB less 4 KiB) with a non-retryable ``database_resource_exhausted``; the
identical read re-hits it. ``BoundedReads`` applies the same refusal at a size
a unit test can build: every single Turn fits, a long whole history does not.
"""
from __future__ import annotations

import json

RESPONSE_BOUND_BYTES = 96 * 1024


class BoundedReads:
    """Refuse an oversized read with the authority's deterministic resource bound.

    ``refusals`` (optional, caller-owned) records each refused operation so a
    test can prove the bounded path actually ran.
    """

    def __init__(self, client, refusals: list[str] | None = None):
        self._client = client
        self._refusals = refusals

    def query(self, operation, payload, *args, **kwargs):
        from lib.storage.errors import StorageError
        result = self._client.query(operation, payload, *args, **kwargs)
        encoded = json.dumps(result, ensure_ascii=False, default=str).encode()
        if len(encoded) > RESPONSE_BOUND_BYTES:
            if self._refusals is not None:
                self._refusals.append(operation)
            raise StorageError('database_resource_exhausted',
                               f'{operation} exceeds its bounded streamed payload',
                               operation_id=operation)
        return result

    def __getattr__(self, name):
        return getattr(self._client, name)
