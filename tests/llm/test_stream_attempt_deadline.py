"""Per-attempt transport bounds: silence is bounded, and expiry is retryable.

A rolling idle window measures SILENCE. It cannot bound an attempt whose
provider accepted the socket and then sent nothing (no headers) or only
keep-alives, because the pre-headers wait sits inside ``session.post`` where no
response handle exists to renew anything.

That gap is what let one dead translation slot consume a whole task budget: the
caller asked for a 90s total, the single attempt sat for 90.7s waiting on
headers it never received, and no retry was ever dispatched (conv
mvhiznrwv669ue, turn f85740dd, 2026-10-21). These specs pin the two bounds that
close it, and pin that expiry is a RETRYABLE failure — a user Stop and a
per-attempt timeout must never be conflated, because only one of them should be
retried on another slot.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.llm._transport import (  # noqa: E402
    StreamIdleWatchdog,
    post_headers_abortable,
)
from lib.llm.llm_errors import (  # noqa: E402
    AbortedError,
    ResponseReadTimeoutError,
    RetryableAPIError,
)

pytestmark = pytest.mark.unit


class _Resp:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True
        self.closed_flag = True

    closed_flag = False


class TestFirstByteDeadline:
    def test_silent_header_wait_expires_as_retryable_timeout(self):
        release = threading.Event()
        resp = _Resp()

        def _post():
            release.wait(timeout=30)
            return resp

        started = time.monotonic()
        try:
            with pytest.raises(ResponseReadTimeoutError) as raised:
                post_headers_abortable(
                    _post,
                    is_aborted=lambda: False,
                    poll_interval=0.02,
                    first_byte_deadline=time.monotonic() + 0.2,
                    timeout_seconds=0.2,
                )
        finally:
            release.set()

        error = raised.value
        assert time.monotonic() - started < 5, (
            'the header wait must not outlive its own deadline')
        assert error.cause_type == 'response_headers'
        # Retryable, and emphatically not a user abort: dispatch must be free
        # to rotate this pair to another slot inside the caller's budget.
        assert isinstance(error, RetryableAPIError)
        assert not isinstance(error, AbortedError)

    def test_headers_that_arrive_in_time_are_untouched(self):
        resp = _Resp()

        assert post_headers_abortable(
            lambda: resp,
            is_aborted=lambda: False,
            poll_interval=0.02,
            first_byte_deadline=time.monotonic() + 30,
        ) is resp

    def test_user_abort_still_wins_over_the_deadline(self):
        release = threading.Event()

        def _post():
            release.wait(timeout=30)
            return _Resp()

        try:
            with pytest.raises(AbortedError):
                post_headers_abortable(
                    _post,
                    is_aborted=lambda: True,
                    poll_interval=0.02,
                    # Long deadline: the abort path must not depend on it.
                    first_byte_deadline=time.monotonic() + 300,
                )
        finally:
            release.set()


class TestWatchdogAttemptDeadline:
    def test_attempt_deadline_interrupts_a_quiet_attempt(self):
        interrupted = threading.Event()
        watchdog = StreamIdleWatchdog(heartbeat_interval=0.02)
        watchdog.arm_attempt_deadline(0.1, interrupted.set)
        try:
            assert interrupted.wait(timeout=5), (
                'a per-attempt deadline must fire without upstream traffic')
            assert watchdog.attempt_deadline_expired is True
        finally:
            watchdog.cancel()

    def test_expiry_is_not_reported_as_a_user_abort(self):
        watchdog = StreamIdleWatchdog(heartbeat_interval=0.02)
        watchdog.arm_attempt_deadline(0.1, lambda: None)
        try:
            deadline = time.monotonic() + 5
            while (not watchdog.attempt_deadline_expired
                   and time.monotonic() < deadline):
                time.sleep(0.01)
        finally:
            watchdog.cancel()

        # ``aborted`` drives AbortedError (never retried). Expiry must leave it
        # clear so the transport reports a transport timeout instead.
        assert watchdog.aborted is False
        assert watchdog.idle_timed_out is False

    def test_non_positive_bound_is_ignored(self):
        watchdog = StreamIdleWatchdog(heartbeat_interval=0.02)
        watchdog.arm_attempt_deadline(0, lambda: pytest.fail('must not arm'))
        watchdog.arm_attempt_deadline(None, lambda: pytest.fail('must not arm'))
        time.sleep(0.1)
        watchdog.cancel()
        assert watchdog.attempt_deadline_expired is False


class TestStreamChatForwardsTheBounds:
    def test_stream_chat_passes_both_bounds_to_the_single_attempt(
            self, monkeypatch):
        from lib.llm import stream
        from lib.llm.stream_result import ProviderStreamResult

        captured = {}

        def _fake_once(body, **kwargs):
            captured.update(kwargs)
            return ProviderStreamResult.from_legacy(
                {'role': 'assistant', 'content': 'ok'}, 'stop', {})

        monkeypatch.setattr(stream, '_stream_chat_once', _fake_once)

        stream.stream_chat(
            {'model': 'm', 'messages': []},
            attempt_timeout=25.0, first_byte_timeout=8.0,
        )

        assert captured['attempt_timeout'] == 25.0
        assert captured['first_byte_timeout'] == 8.0

    def test_a_silent_provider_is_abandoned_with_only_an_attempt_bound(
            self, monkeypatch):
        """End-to-end: the exact production shape of the 2026-10-21 failure.

        A translation passes an attempt bound; the provider accepts the socket
        and never sends response headers. Before this contract the call sat
        until the caller's whole budget expired, so no retry was ever made.
        """
        monkeypatch.setattr('lib.llm._transport.ABORT_POLL_INTERVAL', 0.02)
        release = threading.Event()

        class _WedgedSession:
            def post(self, *a, **k):
                release.wait(timeout=30)
                raise AssertionError(
                    'a silent provider must be abandoned, not awaited')

        monkeypatch.setattr('lib.llm.stream.get_sync_session',
                            lambda: _WedgedSession())
        import lib.desktop.egress as _eg
        monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')

        from lib.llm.stream import _stream_chat_once

        started = time.monotonic()
        try:
            with pytest.raises(ResponseReadTimeoutError):
                _stream_chat_once(
                    {'model': 'm',
                     'messages': [{'role': 'user', 'content': 'hi'}]},
                    api_key='sk-x', base_url='http://fake.local/v1',
                    log_prefix='[t]',
                    abort_check=lambda: False,
                    attempt_timeout=0.2,
                )
        finally:
            release.set()

        assert time.monotonic() - started < 5, (
            'the attempt bound must bound the header wait too')

    def test_interactive_callers_are_unchanged_by_default(self, monkeypatch):
        from lib.llm import stream
        from lib.llm.stream_result import ProviderStreamResult

        captured = {}

        def _fake_once(body, **kwargs):
            captured.update(kwargs)
            return ProviderStreamResult.from_legacy(
                {'role': 'assistant', 'content': 'ok'}, 'stop', {})

        monkeypatch.setattr(stream, '_stream_chat_once', _fake_once)

        stream.stream_chat({'model': 'm', 'messages': []})

        assert captured['attempt_timeout'] is None
        assert captured['first_byte_timeout'] is None
