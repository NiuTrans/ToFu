"""Compile production native HTTP policy from the existing transport owner.

Entry compile_native_http_policy is explicit trusted application composition.
It reads the LLM owner's cached launch-scaled keepalive and timeout settings;
it creates no probe, client, task, pool or production backend selection.
"""
from collections.abc import Mapping
import math


_NULLABLE = frozenset({'request_headers_ms', 'write_ms', 'stream_idle_ms'})
_FIELDS = frozenset({
    'connections', 'idle_connections', 'keepalive_ms', 'connect_ms',
    'request_headers_ms', 'write_ms', 'pool_wait_ms', 'stream_idle_ms',
    'drain_timeout_ms', 'drain_bytes', 'drain_chunks',
})
_U64_MAX = (1 << 64) - 1


def _milliseconds(seconds):
    if (type(seconds) not in (int, float) or not 0 < seconds <= _U64_MAX / 1000
            or not math.isfinite(seconds)):
        raise ValueError('native HTTP duration requires finite positive transport policy')
    return math.ceil(seconds * 1000)


def compile_native_http_policy(overrides=None):
    """Return a detached policy; explicit overrides never alter the owner.

    Header waiting is governed by caller Stop/deadline. Upload and post-header
    stream inactivity retain separate budgets. Retired active-connection and
    pool-wait fields remain positive wire compatibility values, not admission.
    """
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, Mapping) or overrides.keys() - _FIELDS:
        raise ValueError('native HTTP policy overrides require known transport fields')
    # Lazy, explicit composition uses the same cached resource probe and
    # environment interpretation as ordinary Python transport.
    from lib.llm import _transport as owner

    idle_seconds = owner.stream_idle_timeout_seconds()
    policy = {
        'connections': max(1, owner.LLM_MAX_KEEPALIVE_CONNECTIONS),
        'idle_connections': owner.LLM_MAX_KEEPALIVE_CONNECTIONS,
        'keepalive_ms': _milliseconds(owner.LLM_KEEPALIVE_EXPIRY_S),
        'connect_ms': _milliseconds(owner.CONNECT_TIMEOUT),
        'request_headers_ms': None,
        'write_ms': _milliseconds(owner.WRITE_TIMEOUT),
        'pool_wait_ms': _milliseconds(owner.CONNECT_TIMEOUT),
        'stream_idle_ms': _milliseconds(idle_seconds) if idle_seconds > 0 else None,
        'drain_timeout_ms': _milliseconds(owner.POST_TERMINAL_DRAIN_TIMEOUT_S),
        'drain_bytes': owner.POST_TERMINAL_DRAIN_MAX_BYTES,
        'drain_chunks': owner.POST_TERMINAL_DRAIN_MAX_CHUNKS,
    }
    policy.update(overrides)
    for key, value in policy.items():
        if value is None and key in _NULLABLE:
            continue
        minimum = 0 if key == 'idle_connections' else 1
        if type(value) is not int or not minimum <= value <= _U64_MAX:
            raise ValueError('native HTTP policy requires valid byte/count/millisecond bounds')
    return policy
