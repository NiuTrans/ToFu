"""Compatibility state owned by live provider views and credential snapshots.

Browser pacing follows its captured provider lifetime and never holds content.
Cookie replay obtains current source authority before reuse. A changed source
retires the retained guard; older callers keep their own borrowed guard until
completion and cannot republish it over a newer lookup. Weak binding keys reclaim
retired provider state without a timer, scope quota or raw credential index.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from weakref import WeakKeyDictionary

from tofu_search.search.engines._xhs_guard import RiskGuard


@dataclass(frozen=True)
class SourceSnapshot:
    source: dict | None
    guard: RiskGuard | None


def _credential_key(domain, source):
    if not isinstance(source, dict) or not source.get('enabled') or not source.get('cookies'):
        return None
    payload = json.dumps(
        [domain, source['cookies'], source.get('proxy') or ''],
        sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).digest()


class _SessionState:
    def __init__(self):
        self.lock = Lock()
        self.browser_guard = None
        self.source_key = None
        self.source_guard = None
        self.sequence = 0
        self.published_sequence = 0

    def capture_source(self, provider, domain):
        with self.lock:
            self.sequence += 1
            sequence, prior_key, prior_guard = (
                self.sequence, self.source_key, self.source_guard)
        try:
            source = deepcopy(provider.get_source(domain))
            key = _credential_key(domain, source)
        except BaseException:
            self._publish_source(sequence, None, None)
            raise
        with self.lock:
            if key is None:
                guard = None
            elif key == self.source_key:
                guard = self.source_guard
            elif key == prior_key:
                guard = prior_guard
            else:
                guard = RiskGuard()
            if sequence >= self.published_sequence:
                self.source_key, self.source_guard = key, guard
                self.published_sequence = sequence
        return SourceSnapshot(source, guard)

    def _publish_source(self, sequence, key, guard):
        with self.lock:
            if sequence >= self.published_sequence:
                self.source_key, self.source_guard = key, guard
                self.published_sequence = sequence


_lock = Lock()
_states = WeakKeyDictionary()


def _state(binding):
    with _lock:
        state = _states.get(binding)
        if state is None:
            state = _SessionState()
            _states[binding] = state
        return state


def browser_guard(binding):
    state = _state(binding)
    with state.lock:
        if state.browser_guard is None:
            state.browser_guard = RiskGuard()
        return state.browser_guard


def capture_source(binding, domain):
    if binding is None:
        return SourceSnapshot(None, None)
    return _state(binding).capture_source(binding.provider, domain)
