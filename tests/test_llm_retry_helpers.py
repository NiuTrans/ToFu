"""Unit tests for the shared stream-retry helpers in ``lib/llm/_transport.py``.

These three helpers were extracted from the duplicated retry loops in
``lib/llm/stream.py`` (sync) and ``lib/llm/astream.py`` (async), which were
line-for-line identical except for the sleep idiom. The sleep itself stays in
each transport module (so the ``abortable_sleep`` monkeypatch seam the streaming
tests rely on is untouched); only the sleep-free DECISION logic is shared here.
"""

import pytest

from lib.llm._transport import (
    MAX_STREAM_RETRIES,
    apply_model_limit_retry,
    attach_limit_learned,
    prepare_retryable_wait,
)
from lib.llm_errors import AbortedError, RetryableAPIError

pytestmark = pytest.mark.unit


# ── attach_limit_learned ──────────────────────────────────────────────────

def test_attach_limit_learned_noop_when_falsy():
    usage = {'tokens': 5}
    assert attach_limit_learned(usage, None) is usage
    assert 'tokens' in usage and '_model_limit_learned' not in usage


def test_attach_limit_learned_creates_usage_when_none():
    marker = {'model': 'm', 'old_limit': 1, 'new_limit': 2}
    got = attach_limit_learned(None, marker)
    assert got == {'_model_limit_learned': marker}


def test_attach_limit_learned_mutates_existing_usage():
    usage = {'tokens': 3}
    marker = {'model': 'm', 'old_limit': 1, 'new_limit': 2}
    got = attach_limit_learned(usage, marker)
    assert got is usage
    assert usage['_model_limit_learned'] == marker


# ── apply_model_limit_retry ───────────────────────────────────────────────

class _FakeModelLimitErr:
    def __init__(self):
        self.model = 'gpt-x'
        self.requested_limit = 8000
        self.detected_limit = 4096


def test_apply_model_limit_retry_clamps_body_and_returns_marker():
    body = {'max_tokens': 8000}
    err = _FakeModelLimitErr()
    marker = apply_model_limit_retry(body, err, log_prefix='[t]')
    assert body['max_tokens'] == 4096
    assert marker == {'model': 'gpt-x', 'old_limit': 8000, 'new_limit': 4096}


# ── prepare_retryable_wait ────────────────────────────────────────────────

def test_prepare_retryable_wait_returns_positive_wait_on_nonfinal_attempt():
    wait = prepare_retryable_wait(0, RetryableAPIError('boom'), abort_check=None,
                                  log_prefix='[t]')
    assert isinstance(wait, float) and wait > 0


def test_prepare_retryable_wait_raises_original_on_final_attempt():
    err = RetryableAPIError('boom')
    with pytest.raises(RetryableAPIError):
        prepare_retryable_wait(MAX_STREAM_RETRIES, err, abort_check=None,
                               log_prefix='[t]')


def test_prepare_retryable_wait_honors_abort_before_sleep():
    with pytest.raises(AbortedError):
        prepare_retryable_wait(0, RetryableAPIError('boom'),
                               abort_check=lambda: True, log_prefix='[t]')


# ── §2.2 log discipline: no traceback on self-healing attempts ───────────
# error.log captures WARNING+, so an exc_info=True on the intermediate
# retry warning dumps a full traceback into the error log even when the
# very next attempt succeeds (2026-07-24 gateway-degradation window:
# 9 mid-stream breaks, 0 final exhaustions — yet error.log held 9 full
# tracebacks). The final-exhaustion ERROR is the only place a traceback
# belongs. Both transports (stream.py sync + astream.py async) share this
# helper, so one fix covers both paths.

def test_prepare_retryable_wait_intermediate_warning_carries_no_traceback(monkeypatch):
    """Intermediate retry: WARNING with type/message/backoff but NO exc_info."""
    from unittest.mock import MagicMock

    import lib.llm._transport as _t
    mock_logger = MagicMock()
    monkeypatch.setattr(_t, 'logger', mock_logger)

    wait = prepare_retryable_wait(0, RetryableAPIError('boom'),
                                  abort_check=None, log_prefix='[t]')

    assert isinstance(wait, float) and wait > 0
    assert mock_logger.warning.call_count == 1
    args, kwargs = mock_logger.warning.call_args
    assert kwargs.get('exc_info') in (None, False)
    rendered = args[0] % args[1:]
    assert 'Transient error' in rendered and 'attempt 1' in rendered
    assert 'RetryableAPIError' in rendered and 'boom' in rendered
    mock_logger.error.assert_not_called()


def test_prepare_retryable_wait_final_exhaustion_keeps_traceback(monkeypatch):
    """Final attempt: ERROR with exc_info=True, then the original error raises."""
    from unittest.mock import MagicMock

    import lib.llm._transport as _t
    mock_logger = MagicMock()
    monkeypatch.setattr(_t, 'logger', mock_logger)

    err = RetryableAPIError('boom')
    with pytest.raises(RetryableAPIError):
        prepare_retryable_wait(MAX_STREAM_RETRIES, err, abort_check=None,
                               log_prefix='[t]')

    assert mock_logger.error.call_count == 1
    _, kwargs = mock_logger.error.call_args
    assert kwargs.get('exc_info') is True
    mock_logger.warning.assert_not_called()


def test_both_transports_share_the_same_wait_helper():
    """stream.py (sync) and astream.py (async) must resolve
    ``prepare_retryable_wait`` to the SAME function — the premise that one
    fix in _transport.py covers both transports."""
    import lib.llm._transport as _t
    import lib.llm.astream as _a
    import lib.llm.stream as _s

    assert _s.prepare_retryable_wait is _t.prepare_retryable_wait
    assert _a.prepare_retryable_wait is _t.prepare_retryable_wait
