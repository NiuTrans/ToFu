"""Collision-free human-gate request identities for one execution tree."""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
from typing import Any


def _request_fragment(value: Any, fallback: str) -> str:
    raw = str(value or '').strip()
    if not raw:
        return fallback
    fragment = re.sub(r'[^A-Za-z0-9_.-]+', '-', raw).strip('-_.')
    if fragment == raw and len(fragment) <= 64:
        return fragment
    digest = hashlib.blake2s(raw.encode('utf-8'), digest_size=4).hexdigest()
    return (fragment or fallback)[:48] + '-' + digest


class HumanGateRequestIdentity:
    """One collision-free request-id sequence shared by an execution tree."""

    def __init__(self, scope: str = '') -> None:
        self.scope = _request_fragment(
            scope, 'exec-' + secrets.token_hex(8))
        self._sequence = 0
        self._lock = threading.Lock()

    def next(self, node_id: Any) -> str:
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        return 'orch_' + self.scope + '_' + _request_fragment(
            node_id, 'human') + '_' + str(sequence)


__all__ = ['HumanGateRequestIdentity']
