"""Regression: the sync streaming transport must close the RawSSEDumper file
handle on EVERY exit path — including a connect-phase failure.

``prepare_request`` opens the RawSSEDumper fd (when ``LLM_DEBUG_RAW_SSE`` is
enabled). The connect-phase re-raise in ``_stream_chat_once`` used to escape
BEFORE the try/finally that closes the dumper, leaking one fd per retry against
a down endpoint — exactly when that debug flag is on. A single outer
try/finally now guards all exits.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_stream_dumper_fd_leak.py
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
def test_connect_failure_closes_dumper_fd(monkeypatch):
    """A connect-phase ConnectionError must still close the dumper fd."""
    # Enable the raw-SSE dumper so prepare_request opens a real fd.
    monkeypatch.setenv('LLM_DEBUG_RAW_SSE', '1')

    import requests

    import lib.llm.diagnostics as diag
    import lib.llm.stream as stream
    from lib.llm_errors import EndpointUnreachableError

    # Force the dumper to re-read the (now enabled) env filter.
    monkeypatch.setattr(diag, '_RAW_SSE_FILTER', '1', raising=False)

    opened: list = []
    real_open = diag.RawSSEDumper._open

    def _tracking_open(self):
        real_open(self)
        if self._fh is not None:
            opened.append(self)

    monkeypatch.setattr(diag.RawSSEDumper, '_open', _tracking_open)

    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError('SYN dropped')

    class _FakeSession:
        post = staticmethod(_boom)

    monkeypatch.setattr(stream, 'get_sync_session', lambda: _FakeSession())

    body = {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 16}
    with pytest.raises(EndpointUnreachableError):
        stream.stream_chat(body, log_prefix='[test]', base_url='http://127.0.0.1:1',
                           api_key='k')

    # The dumper fd must have been opened (flag on) AND closed (fd is None).
    assert opened, 'RawSSEDumper fd was never opened — test did not exercise the leak path'
    for dumper in opened:
        assert dumper._fh is None, 'RawSSEDumper fd leaked on connect-phase failure'


@pytest.mark.unit
def test_success_path_still_closes_dumper_fd(monkeypatch):
    """The normal (200 + [DONE]) path must also leave the fd closed."""
    monkeypatch.setenv('LLM_DEBUG_RAW_SSE', '1')

    import lib.llm.diagnostics as diag
    import lib.llm.stream as stream

    monkeypatch.setattr(diag, '_RAW_SSE_FILTER', '1', raising=False)

    opened: list = []
    real_open = diag.RawSSEDumper._open

    def _tracking_open(self):
        real_open(self)
        if self._fh is not None:
            opened.append(self)

    monkeypatch.setattr(diag.RawSSEDumper, '_open', _tracking_open)

    class _FakeResp:
        status_code = 200
        headers: dict = {}
        encoding = 'utf-8'

        def iter_lines(self, decode_unicode=True):
            yield 'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":"stop"}]}'
            yield 'data: [DONE]'

        def close(self):
            pass

    class _FakeSession:
        post = staticmethod(lambda *a, **k: _FakeResp())

    monkeypatch.setattr(stream, 'get_sync_session', lambda: _FakeSession())

    body = {'model': 'test-model', 'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 16}
    msg, finish_reason, usage = stream.stream_chat(
        body, log_prefix='[test]', base_url='http://127.0.0.1:1', api_key='k')

    assert msg['content'] == 'hello'
    assert finish_reason == 'stop'
    assert opened, 'RawSSEDumper fd was never opened'
    for dumper in opened:
        assert dumper._fh is None, 'RawSSEDumper fd left open on success path'
