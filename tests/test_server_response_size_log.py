#!/usr/bin/env python3
"""Tests for response-size instrumentation in http_request_lifecycle.log_response.

Root cause this guards (see JOURNAL 2026-07-19 "溯源"): a fast-but-heavy
response (a 2.9MB conversation fetch) was logged at INFO as
``← GET /... 200 (0.061s)`` — no byte count — so "server is fast yet the
client experience is heavy" was invisible in server logs and only traceable
via the client-side CLIENT-ERROR feed. These tests pin:

  (a) a large response surfaces its byte size in the log line;
  (b) a streaming response (no Content-Length) does NOT raise and appends
      no size suffix.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_server_response_size_log.py -v
"""

import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture(scope='module')
def srv():
    try:
        import quart  # noqa: F401
        import hypercorn  # noqa: F401
    except ImportError as e:
        pytest.skip('quart/hypercorn not installed: %s' % e)
    import server
    from lib import http_request_lifecycle as lifecycle
    return SimpleNamespace(
        app=server.app,
        _fmt_size=lifecycle.format_size,
        _response_size=lifecycle.response_size,
        _log_response=lifecycle.log_response,
        _lifecycle_log=lifecycle.logger,
    )


class _FakeResp:
    """Minimal stand-in for a Quart Response for the pure size helper."""

    def __init__(self, content_length=None):
        self.headers = {}
        if content_length is not None:
            self.headers['Content-Length'] = content_length


@pytest.mark.unit
class TestFormatSize:
    def test_bytes(self, srv):
        assert srv._fmt_size(0) == '0B'
        assert srv._fmt_size(512) == '512B'

    def test_kb(self, srv):
        assert srv._fmt_size(1536) == '1.5KB'

    def test_mb(self, srv):
        # 2940728 bytes == the real 2.9MB conversation fetch from the incident
        assert srv._fmt_size(2940728) == '2.8MB'

    def test_unknown_is_empty(self, srv):
        assert srv._fmt_size(None) == ''
        assert srv._fmt_size('nope') == ''
        assert srv._fmt_size(-5) == ''


@pytest.mark.unit
class TestResponseSize:
    def test_from_content_length(self, srv):
        assert srv._response_size(_FakeResp('2940728')) == 2940728

    def test_missing_is_none(self, srv):
        # Streaming / chunked responses carry no Content-Length.
        assert srv._response_size(_FakeResp(None)) is None

    def test_bad_value_is_none_no_raise(self, srv):
        assert srv._response_size(_FakeResp('garbage')) is None


@pytest.mark.unit
class TestLogResponseIntegration:
    def _capture(self, srv, response, method='GET', path='/api/v1/conversations/x'):
        """Drive _log_response inside a request context, capturing its log line."""
        import asyncio
        records = []

        class _Grab(logging.Handler):
            def emit(self, rec):
                records.append(rec.getMessage())

        lg = srv._lifecycle_log
        h = _Grab()
        lg.addHandler(h)
        old_level = lg.level
        lg.setLevel(logging.DEBUG)
        try:
            async def go():
                async with srv.app.test_request_context(path, method=method):
                    await srv._log_response(response)
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(go())
            finally:
                loop.close()
        finally:
            lg.removeHandler(h)
            lg.setLevel(old_level)
        return records

    def test_large_response_logs_size(self, srv):
        from quart import Response
        resp = Response(b'x' * 2940728, status=200)
        msgs = self._capture(srv, resp)
        assert msgs, 'expected a lifecycle log line'
        line = msgs[-1]
        assert 'MB' in line, 'byte size missing from heavy-response log: %r' % line
        assert '2.8MB' in line

    def test_streaming_response_no_raise(self, srv):
        from quart import Response

        async def _gen():
            yield b'data: hello\n\n'

        resp = Response(_gen(), status=200)
        # Must not raise even though there is no Content-Length.
        msgs = self._capture(srv, resp)
        assert msgs, 'expected a lifecycle log line even for streaming'
        # No size suffix appended when size is unknown.
        assert 'MB' not in msgs[-1] and 'KB' not in msgs[-1]

    def test_no_negative_elapsed_when_start_time_missing(self, srv):
        """A request that reaches _log_response WITHOUT request._start_time
        (early-error / middleware paths that skip before_request) must NOT log
        a negative elapsed. The bare test_request_context here never sets
        _start_time, reproducing that path. The log line must carry no '(-'
        elapsed — root cause was `time.time() - getattr(request, '_start_time',
        time.time())` evaluating left-before-default → a slightly negative
        span. Size (if known) must still be emitted."""
        from quart import Response
        resp = Response(b'x' * 2940728, status=200)
        msgs = self._capture(srv, resp)
        assert msgs, 'expected a lifecycle log line'
        line = msgs[-1]
        assert '(-' not in line, 'negative elapsed leaked into log: %r' % line
        # The size must still surface even though elapsed is unknown.
        assert '2.8MB' in line, 'size dropped along with elapsed: %r' % line

    def test_positive_elapsed_still_shown_when_start_time_present(self, srv):
        """Regression guard: when _start_time IS present the elapsed is still
        logged (we must not blanket-drop timing)."""
        import time as _time
        from quart import Response
        import asyncio

        records = []

        class _Grab(logging.Handler):
            def emit(self, rec):
                records.append(rec.getMessage())

        lg = srv._lifecycle_log
        h = _Grab()
        lg.addHandler(h)
        old_level = lg.level
        lg.setLevel(logging.DEBUG)
        try:
            async def go():
                async with srv.app.test_request_context('/api/v1/x', method='GET'):
                    from quart import request
                    request._start_time = _time.time() - 0.05  # 50ms ago
                    await srv._log_response(Response(b'ok', status=200))
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(go())
            finally:
                loop.close()
        finally:
            lg.removeHandler(h)
            lg.setLevel(old_level)
        assert records
        line = records[-1]
        assert '(-' not in line
        # A real positive elapsed appears (0.0xxs), not the size-only form.
        assert 's,' in line or 's)' in line


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
