"""Stop-during-header-wait contract for the sync streaming transport.

A blocked ``session.post(..., stream=True)`` sits inside the header read with
no response handle, so the idle watchdog's ``resp.close()`` cannot reach the
socket. ``post_headers_abortable`` moves that wait onto a daemon thread and
polls the abort latch, so a Stop pressed while the upstream is silent lands
within one poll interval instead of whenever the upstream first speaks
(2026-08-28 kimi-k3 incident: 170s of pre-headers silence swallowed 4 Stop
clicks).
"""

from __future__ import annotations

import os
import sys
import logging
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm._transport import post_headers_abortable  # noqa: E402
from lib.llm_errors import AbortedError  # noqa: E402

pytestmark = pytest.mark.unit


class _BlockingResp:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _BrokenCloseResp:
    def __init__(self):
        self.close_attempted = threading.Event()

    def close(self):
        self.close_attempted.set()
        raise OSError('synthetic close failure')


class TestPostHeadersAbortable:
    def test_passthrough_returns_response(self):
        resp = _BlockingResp()
        assert post_headers_abortable(
            lambda: resp, is_aborted=lambda: False) is resp

    def test_post_error_propagates(self):
        with pytest.raises(ConnectionError):
            post_headers_abortable(
                lambda: (_ for _ in ()).throw(ConnectionError('refused')),
                is_aborted=lambda: False)

    def test_abort_lands_while_post_is_blocked(self):
        release = threading.Event()
        resp = _BlockingResp()

        def _post():
            release.wait(timeout=30)
            return resp

        aborted_at = time.monotonic() + 0.2
        t0 = time.monotonic()
        with pytest.raises(AbortedError):
            post_headers_abortable(
                _post, is_aborted=lambda: time.monotonic() >= aborted_at,
                poll_interval=0.02)
        assert time.monotonic() - t0 < 5, (
            'abort must not wait for the blocked post to return')

        # The abandoned worker closes the response once the upstream answers.
        release.set()
        deadline = time.monotonic() + 5
        while not resp.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert resp.closed

    def test_no_abort_waits_for_headers(self):
        resp = _BlockingResp()

        def _post():
            time.sleep(0.1)
            return resp

        assert post_headers_abortable(
            _post, is_aborted=lambda: False, poll_interval=0.02) is resp

    def test_abandoned_close_failure_keeps_abort_and_leaves_evidence(
        self,
        caplog,
    ):
        release = threading.Event()
        resp = _BrokenCloseResp()

        def _post():
            release.wait(timeout=30)
            return resp

        with caplog.at_level(logging.WARNING, logger='lib.llm._transport'):
            with pytest.raises(AbortedError):
                post_headers_abortable(
                    _post, is_aborted=lambda: True, poll_interval=0.01)
            release.set()
            assert resp.close_attempted.wait(timeout=5)

        assert 'abandoned response close failed: OSError' in caplog.text


class TestStreamChatOnceHeaderWaitAbort:
    def test_stop_during_silent_header_wait_unwinds_fast(self, monkeypatch):
        """End-to-end through _stream_chat_once: a Stop pressed while the
        upstream never sends headers must raise AbortedError promptly."""
        monkeypatch.setattr('lib.llm._transport.ABORT_POLL_INTERVAL', 0.02)
        monkeypatch.setattr('lib.llm._transport.IDLE_HEARTBEAT_S', 0.05)

        release = threading.Event()

        class _WedgedSession:
            def post(self, *a, **k):
                release.wait(timeout=30)
                raise AssertionError(
                    'post must be abandoned, not awaited, after Stop')

        monkeypatch.setattr('lib.llm.stream.get_sync_session',
                            lambda: _WedgedSession())
        import lib.desktop.egress as _eg
        monkeypatch.setattr(_eg, 'route_request', lambda url, **kw: 'direct')

        from lib.llm.stream import _stream_chat_once

        aborted_at = time.monotonic() + 0.2
        t0 = time.monotonic()
        try:
            with pytest.raises(AbortedError):
                _stream_chat_once(
                    {'model': 'm',
                     'messages': [{'role': 'user', 'content': 'hi'}]},
                    api_key='sk-x', base_url='http://fake.local/v1',
                    log_prefix='[t]',
                    abort_check=lambda: time.monotonic() >= aborted_at)
        finally:
            release.set()
        assert time.monotonic() - t0 < 5, (
            'Stop during the header wait must not wait for the upstream')
