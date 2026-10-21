#!/usr/bin/env python3
"""Readiness 503s are state, not incidents.

``/api/ready`` answers 503 until the boot is complete and the frontend polls it
BY DESIGN. Recording that at ERROR put 41 non-incidents into a four-minute boot
window (2026-10-21) and diluted the lines that did mean something. These tests
pin the split: the readiness contract at DEBUG, a real 5xx still at ERROR.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
        tests/server/test_expected_unavailable_503_log_level.py -v
"""

import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest


@pytest.fixture(scope='module')
def srv():
    try:
        import quart  # noqa: F401
        import hypercorn  # noqa: F401
    except ImportError as e:
        pytest.skip('quart/hypercorn not installed: %s' % e)
    import server
    from lib.http_transport import request_lifecycle as lifecycle
    return SimpleNamespace(
        app=server.app,
        _log_response=lifecycle.log_response,
        _lifecycle_log=lifecycle.logger,
        _expected=lifecycle._EXPECTED_UNAVAILABLE_PATHS,
    )


def _records_for(srv, path, status):
    """Return [(levelno, message)] emitted by one log_response call."""
    import asyncio
    from quart import Response

    records = []

    class _Grab(logging.Handler):
        def emit(self, rec):
            records.append((rec.levelno, rec.getMessage()))

    handler = _Grab()
    log = srv._lifecycle_log
    log.addHandler(handler)
    previous = log.level
    log.setLevel(logging.DEBUG)
    try:
        async def go():
            async with srv.app.test_request_context(path, method='GET'):
                await srv._log_response(Response(b'{}', status=status))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(go())
        finally:
            loop.close()
    finally:
        log.removeHandler(handler)
        log.setLevel(previous)
    return records


@pytest.mark.unit
class TestExpectedUnavailableIsNotAnIncident:
    def test_readiness_503_is_recorded_below_error(self, srv):
        assert '/api/ready' in srv._expected
        records = _records_for(srv, '/api/ready', 503)
        request_rows = [r for r in records if '← GET' in r[1]]
        assert request_rows, 'log_response emitted no request row'
        assert all(level < logging.ERROR for level, _ in request_rows), (
            'readiness 503 logged at ERROR: %r' % (request_rows,))

    def test_ordinary_500_stays_an_error(self, srv):
        records = _records_for(srv, '/api/v4/conversations/x', 500)
        request_rows = [r for r in records if '← GET' in r[1]]
        assert request_rows
        assert any(level == logging.ERROR for level, _ in request_rows), (
            'a real 5xx must stay at ERROR: %r' % (request_rows,))

    def test_unexpected_503_on_a_data_path_stays_an_error(self, srv):
        # A storage-fenced write is a genuine unavailability signal, not the
        # readiness contract, and must keep its ERROR.
        records = _records_for(srv, '/api/v4/conversations/x/sync', 503)
        request_rows = [r for r in records if '← GET' in r[1]]
        assert request_rows
        assert any(level == logging.ERROR for level, _ in request_rows), (
            'unexpected 503 lost its ERROR level: %r' % (request_rows,))


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
