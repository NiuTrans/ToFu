"""Optional request-local phase evidence; no transport or execution policy.

Entry points: DiagnosticTiming, bind_timing, timing_stage, timed_stage,
timed_call. Inert outside an explicitly admitted translation scope. Timings
are inclusive; parent IDs distinguish nesting/overlap from additive phases.
No prompt, endpoint, header map, exception text or credentials are retained.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json
import math
import threading
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)
MAX_PHASE_SAMPLES = 48
MAX_PHASE_NAMES = 48
MAX_PHASE_EVENTS = 96
_FIELDS = frozenset({
    'kind', 'job_id', 'conversation_id', 'turn_id', 'task_id', 'model',
    'provider_id', 'protocol', 'trace_id', 'route_id', 'transport', 'status_code',
    'error_type', 'error_code', 'measurement', 'response_headers_ms',
    'response_body_ms', 'first_content_ms', 'after_first_content_ms',
    'headers_observed', 'body_complete', 'reasoning_chars', 'output_chars',
    'output_tokens', 'reasoning_tokens', 'queue_cancelled', 'header_observations',
    'first_response_headers_ms', 'response_parse_ms', 'translation_outcome',
})
_current: ContextVar[DiagnosticTiming | None] = ContextVar('diagnostic_timing', default=None)
_parent: ContextVar[int | None] = ContextVar('diagnostic_timing_parent', default=None)


def _fields(values):
    result = {}
    for key, value in values.items():
        if key not in _FIELDS or value is None:
            continue
        if isinstance(value, bool):
            result[key] = value
        elif isinstance(value, int):
            if -(2**63) <= value < 2**63:
                result[key] = value
        elif isinstance(value, float) and math.isfinite(value):
            result[key] = value
        elif isinstance(value, str):
            result[key] = value[:128]
    return result


class TimingPhase:
    def __init__(self, owner, ordinal, name, parent, fields):
        self.owner, self.ordinal, self.name, self.parent = owner, ordinal, name, parent
        self.started = owner.clock()
        self.fields = _fields(fields)
        self.closed = False

        self._parent_token = None

    def activate(self):
        """Enter a caller-owned lexical phase that must finish on this context."""
        if self._parent_token is None:
            self._parent_token = _parent.set(self.ordinal)
        return self

    def annotate(self, **fields):
        with self.owner._lock:
            self.fields.update(_fields(fields))

    def observe(self, **fields):
        with self.owner._lock:
            self.annotate(**fields)
            self.owner._emit('phase_observation', {'phase': self.name,
                'span_id': self.ordinal, **_fields(fields)})

    def finish(self, error=None):
        if error is not None:
            # Exception properties are not diagnostic authority; some custom
            # errors expose a code getter that can itself raise.
            try:
                code = getattr(error, 'code', None)
            except Exception as diagnostic_error:
                # Swallow only diagnostic inspection of this exception's code;
                # the original operation error is still propagated unchanged.
                logger.debug('finish failure handled: %s', type(diagnostic_error).__name__)
                code = None
            self.annotate(error_type=type(error).__name__, error_code=code)
        try:
            self.owner._finish_phase(self, 'error' if error is not None else 'ok')
        finally:
            if self._parent_token is not None:
                token, self._parent_token = self._parent_token, None
                _parent.reset(token)


class DiagnosticTiming:
    def __init__(self, *, kind, identity=None, clock=None, emit=None):
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.timing_id = uuid.uuid4().hex
        self.identity = _fields({'kind': kind, **(identity or {})})
        self._emit_port = emit
        self._lock = threading.RLock()
        self._ordinal = self._events = self._dropped = 0
        self._totals, self._active, self._samples = {}, {}, []

        self._last_request = {}
        self._uncounted_stages = 0

        self._counters = {'http_response_headers': 0, 'http_429_headers': 0, 'http_transport_errors': 0}
        self._closed = False

        self._summary = None

    def _emit(self, event, payload, *, bounded=True):
        if bounded:
            if self._events >= MAX_PHASE_EVENTS:
                self._dropped += 1
                return
            self._events += 1
        record = {'event': event, 'timing_id': self.timing_id, **self.identity, **payload}
        try:
            if self._emit_port is not None:
                self._emit_port(record)
            else:
                logger.info('[Translation:timing] %s', json.dumps(record, separators=(',', ':'), ensure_ascii=True))
        except Exception:
            # Observability must never replace cancellation, errors or output.
            pass

    def start(self, name, **fields):
        name = name[:64] if isinstance(name, str) else 'unknown'
        with self._lock:
            self._ordinal += 1
            phase = TimingPhase(self, self._ordinal, name, _parent.get(), fields)

            if self._closed:
                self._emit('late_phase_start', {'phase': name, 'span_id': phase.ordinal,
                    'parent_span_id': phase.parent, **phase.fields})
                return phase
            # Count all known-stage occurrences, retain only bounded samples.
            if name not in self._totals and len(self._totals) < MAX_PHASE_NAMES:
                self._totals[name] = {'count': 0, 'errors': 0, 'total_ms': 0.0, 'max_ms': 0.0}

            if name not in self._totals:
                self._uncounted_stages += 1
            if len(self._active) < MAX_PHASE_SAMPLES:
                self._active[phase.ordinal] = phase
            self._emit('phase_start', {'phase': name, 'span_id': phase.ordinal,
                'parent_span_id': phase.parent, 'offset_ms': round((phase.started-self.started)*1000, 3),
                **phase.fields})
            return phase

    def _finish_phase(self, phase, outcome):
        with self._lock:
            if phase.closed:
                return
            phase.closed = True
            self._active.pop(phase.ordinal, None)

            if phase.name in {'provider_send', 'provider_call', 'provider_stream'}:
                self._last_request.update(phase.fields)
                self._last_request['outcome'] = outcome
            duration = max(0.0, (self.clock() - phase.started) * 1000)

            if self._closed:
                self._emit('late_phase_complete', {'phase': phase.name,
                    'span_id': phase.ordinal, 'parent_span_id': phase.parent,
                    'duration_ms': round(duration, 3), 'outcome': outcome, **phase.fields})
                return
            total = self._totals.get(phase.name)

            if phase.name == 'http_request' and outcome != 'ok':
                self._counters['http_transport_errors'] += 1
            if total is not None:
                total['count'] += 1
                total['errors'] += int(outcome != 'ok')
                total['total_ms'] += duration
                total['max_ms'] = max(total['max_ms'], duration)
            record = {'phase': phase.name, 'span_id': phase.ordinal,
                'parent_span_id': phase.parent, 'duration_ms': round(duration, 3),
                'outcome': outcome, **phase.fields}
            if len(self._samples) < MAX_PHASE_SAMPLES:
                self._samples.append(record)
            self._emit('phase_complete', record)

    def count(self, name, delta=1):
        with self._lock:
            if not self._closed and name in self._counters:
                self._counters[name] += delta

    def finish_open_children(self, parent_id, error):
        with self._lock:
            children = [p for p in self._active.values() if p.parent == parent_id]
        for phase in children:
            phase.finish(error)

    def snapshot(self):
        with self._lock:

            if self._summary is not None:
                # The root caller's terminal verdict never changes with late work.
                from copy import deepcopy
                return deepcopy(self._summary)
            return {'timing_id': self.timing_id,
                'elapsed_ms': round(max(0.0, (self.clock()-self.started)*1000), 3),
                'measurement': 'monotonic_inclusive_phases',
                'stages': {name: {key: round(value, 3) if isinstance(value, float) else value
                                  for key, value in total.items()}
                           for name, total in self._totals.items()},
                'samples': [dict(item) for item in self._samples],
                'active_spans': [{'phase': p.name, 'span_id': p.ordinal,
                                  'elapsed_ms': round(max(0.0, (self.clock()-p.started)*1000), 3)}
                                 for p in self._active.values()],
                'sample_limit': MAX_PHASE_SAMPLES, 'event_limit': MAX_PHASE_EVENTS,
                'events_suppressed': self._dropped,

                'uncounted_stage_occurrences': self._uncounted_stages,
                'last_provider_request': dict(self._last_request),

                'counters': dict(self._counters),
                'coverage': 'sampled' if self._ordinal > MAX_PHASE_SAMPLES or self._dropped or self._uncounted_stages else 'complete',
                'unobserved': ['dns', 'tcp_tls', 'provider_internal_queue', 'nonstream_first_token']}

    def complete(self, outcome='completed'):
        with self._lock:
            if self._closed:
                return
            self._summary = self.snapshot()
            self._closed = True
            summary = self.snapshot()
            # Per-phase records already carry samples; do not duplicate them
            # in a large summary that could be clipped by the log formatter.
            summary.pop('samples', None)
            self._emit('summary', {**summary, 'outcome': outcome}, bounded=False)


def current_timing():
    return _current.get()


def current_parent():
    return _parent.get()


@contextmanager
def bind_timing(timing, parent=None):
    token, parent_token = _current.set(timing), _parent.set(parent)
    try:
        yield timing
    finally:
        _parent.reset(parent_token)
        _current.reset(token)


@contextmanager
def timing_stage(name, **fields):
    timing = _current.get()
    if timing is None:
        yield None
        return
    phase = timing.start(name, **fields)
    token = _parent.set(phase.ordinal)
    try:
        yield phase
    except BaseException as error:
        phase.finish(error)
        raise
    else:
        phase.finish()
    finally:
        _parent.reset(token)


def timed_stage(name):
    def decorate(function):
        @wraps(function)
        def observed(*args, **kwargs):
            if _current.get() is None:
                return function(*args, **kwargs)
            with timing_stage(name):
                return function(*args, **kwargs)
        return observed
    return decorate


def timed_call(name, function, *args, **kwargs):
    if _current.get() is None:
        return function(*args, **kwargs)
    with timing_stage(name):
        return function(*args, **kwargs)


def timed_call_with_fields(name, fields, function, *args, **kwargs):
    if _current.get() is None:
        return function(*args, **kwargs)
    with timing_stage(name, **fields):
        return function(*args, **kwargs)
