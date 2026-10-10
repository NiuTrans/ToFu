"""Explicit context-local observation of decoded provider payloads before parsing.

No files, shared logs, headers, request bodies or replay authority. Callers pass
owner identity and memory bounds, retain snapshots themselves, and close the
scope. This captures consumed event text, not original network bytes or all
HTTP attempts. Thread workers must enter their own scope explicitly.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import threading
from lib.identity import require_user_id

_current = ContextVar('tofu_provider_payload_capture', default=None)


class ProviderPayloadCapture:
    def __init__(self, *, owner_user_id, request_label, max_bytes, max_events):
        self.owner_user_id = require_user_id(owner_user_id, context='payload capture owner')
        if not isinstance(request_label, str) or not 1 <= len(request_label) <= 160:
            raise ValueError('capture label must contain 1..160 characters')
        if any(type(value) is not int or value <= 0 for value in (max_bytes, max_events)):
            raise ValueError('capture requires explicit positive byte/event bounds')
        self.label, self.max_bytes, self.max_events = request_label, max_bytes, max_events
        self._lock = threading.RLock()
        self._streams, self._events = [], []
        self._bytes = 0
        self._closed = self._incomplete = False

    def new_stream(self, model, trace_id):
        with self._lock:
            if self._closed or len(self._streams) >= self.max_events:
                self._incomplete = True
                return None
            index = len(self._streams)
            self._streams.append({'model':model[:256] if isinstance(model,str) else '',
                'trace_id':trace_id[:256] if isinstance(trace_id,str) else '',
                'model_truncated':isinstance(model,str) and len(model)>256,
                'trace_id_truncated':isinstance(trace_id,str) and len(trace_id)>256, 'finalized':False,
                'delivery_state':'unknown', 'observed_events':0})
            return index

    def record(self, stream, data):
        if stream is None:
            return
        with self._lock:
            self._streams[stream]['observed_events'] += 1
            if self._closed:
                self._incomplete = True
                return
            if len(data) > self.max_bytes - self._bytes:
                self._incomplete = True
                return
            try:
                size = len(data.encode('utf-8'))
            except UnicodeError:
                self._incomplete = True
                return
            if len(self._events) >= self.max_events or self._bytes + size > self.max_bytes:
                self._incomplete = True
                return  # Never retain a misleading partial frame.
            self._events.append({'stream':stream,'data':data})
            self._bytes += size

    def finish(self, stream, state, malformed):
        if stream is None:
            return
        with self._lock:
            self._streams[stream].update(finalized=True, delivery_state=str(state))
            if malformed:
                self._incomplete = True

    def failed(self):
        with self._lock:
            self._incomplete = True

    def close(self):
        with self._lock:
            if any(not row['finalized'] for row in self._streams):
                self._incomplete = True
            self._closed = True

    def snapshot(self):
        with self._lock:
            return {'contract_version':'tofu-provider-payload-capture/v1',
                'source_kind':'decoded_consumed_event_text','owner_user_id':self.owner_user_id,
                'request_label':self.label,'max_bytes':self.max_bytes,'max_events':self.max_events,
                'retained_bytes':self._bytes,'closed':self._closed,
                'coverage_complete':self._closed and bool(self._events) and not self._incomplete,
                'streams':deepcopy(self._streams),'events':deepcopy(self._events)}


def current_payload_capture():
    return _current.get()


@contextmanager
def capture_provider_payloads(**kwargs):
    if _current.get() is not None:
        raise ValueError('provider payload capture scopes cannot overlap in one context')
    capture = ProviderPayloadCapture(**kwargs)
    token = _current.set(capture)
    try:
        yield capture
    finally:
        capture.close()
        _current.reset(token)
