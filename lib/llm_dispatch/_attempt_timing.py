"""Bounded observation of dispatcher transport invocations, not HTTP retries.

No storage dependencies: callers must supply a nonblocking event sink. Inner
transport route/retry loops are intentionally included in this scope.
"""
import time
import uuid

from lib.log import get_logger
from lib.llm.stream_result import ensure_provider_stream_result
from lib.secret_envelope import secret_hint
from ._route_trace import RouteAttemptTrace

logger = get_logger(__name__)


def _label(value, limit=160):
    value = str(value or '')
    # Identifiers only: never serialize endpoints, credentials or free text.
    if any(token in value.lower() for token in ('://', '?', '@', 'bearer ', 'sk-')):
        return ''
    return value[:limit]


def _key_hint(slot):
    """One-way masked credential label (``first4…last4`` form the Settings
    key card already shows) so a failed attempt names WHICH key served it.
    By construction the hint can never operate as the credential; slots
    without key material (OAuth) emit nothing."""
    return secret_hint(getattr(slot, 'api_key', '') or '')[:64]


class AttemptObserver:
    def __init__(self, callback):
        self.callback = callback
        self.ordinal = 0
        self.route_trace = RouteAttemptTrace()

    def _notify(self, kind, fields):
        if self.callback is not None:
            try:
                self.callback(kind=kind, **fields)
            except Exception as e:
                # Observer failures cannot change retry, health, or billing.
                logger.debug('[AttemptTiming] observer callback dropped: %s', e)

    def note_cause(self, cause):
        """Record why the next attempt uses a different route (or retried)."""
        self.route_trace.note_cause(cause)

    def _start(self, slot):
        self.route_trace.start(slot)
        self.ordinal += 1
        fields = dict(spanId='provider:' + uuid.uuid4().hex,
                      requestLevel='provider_attempt', attempt=self.ordinal,
                      attemptScope='dispatcher', model=_label(slot.model),
                      providerId=_label(slot.routing_provider_id or slot.provider_id),
                      keyHint=_key_hint(slot))
        started = time.monotonic()
        self._notify('start', fields)
        return fields, started

    def _complete(self, fields, started, result=None, error=None):
        fields = dict(fields, durationMs=max(0, int(
            (time.monotonic() - started) * 1000)))
        if error is not None:
            fields.update(status='aborted' if type(error).__name__ in
                          ('AbortedError', 'CancelledError', 'GeneratorExit')
                          else 'failed', errorKind=type(error).__name__[:160])
            code = getattr(error, 'status_code', None)
            if isinstance(code, int) and 0 < code <= 599:
                fields['statusCode'] = code
        else:
            fields.update(status='succeeded' if result.is_verified_complete else 'failed',
                          finishReason=_label(result[1], 80),
                          streamState=result.state.value)
        self._notify('complete', fields)

    def call(self, transport, slot, *args, **kwargs):
        fields, started = self._start(slot)
        try:
            result = ensure_provider_stream_result(transport(*args, **kwargs))
        except BaseException as error:
            self.note_cause(type(error).__name__)
            self._complete(fields, started, error=error)
            raise
        self._complete(fields, started, result=result)
        return result

    async def acall(self, transport, slot, *args, **kwargs):
        fields, started = self._start(slot)
        try:
            result = ensure_provider_stream_result(await transport(*args, **kwargs))
        except BaseException as error:
            self.note_cause(type(error).__name__)
            self._complete(fields, started, error=error)
            raise
        self._complete(fields, started, result=result)
        return result
