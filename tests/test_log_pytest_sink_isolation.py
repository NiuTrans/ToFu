"""tests/test_log_pytest_sink_isolation.py — 2026-07-13.

Regression guard for the pytest log-sink isolation added in server.py.

WHY: ``tests/conftest.py`` does ``import server`` at collection time, which
runs ``logging.basicConfig(handlers=[...])`` and attaches the rotating FILE
handlers (app.log / error.log / vendor.log / access.log) to the ROOT logger
for the whole session. The suite deliberately drives error paths with mocked
exceptions ('llm down', 'disk full', 'reload boom', a merge-conflict 'bad.js').
Without isolation, every test's ``logger.error()`` bled those fixtures into the
REAL ``logs/error.log`` & ``logs/app.log`` — poisoning the operator's #1
diagnostic surface (a reader can't tell a fixture from a real outage).

The fix: under pytest, redirect the FILE handlers to an isolated
``logs/pytest/`` sink. Production logging is untouched when not under pytest.
"""

from __future__ import annotations

import logging
import os

import pytest

# conftest already did ``import server`` at collection time; this is a no-op
# re-import that returns the cached module.
import server

pytestmark = pytest.mark.unit


def test_running_under_pytest_flag_is_set():
    """The suite itself must be recognised as running under pytest."""
    assert server._UNDER_PYTEST is True


def test_file_handlers_point_into_pytest_sink():
    """All four rotating FILE handlers must write under logs/pytest/, never the
    production logs/ directory."""
    expected_dir = os.path.join(server.LOG_DIR, 'pytest')
    assert server._FILE_LOG_DIR == expected_dir

    for handler in (server._error_handler, server._app_handler,
                    server._vendor_handler, server._access_handler):
        parent = os.path.dirname(handler.baseFilename)
        assert parent == expected_dir, (
            f'{handler.baseFilename} escaped the pytest sink '
            f'(expected under {expected_dir})'
        )


def test_console_handler_still_targets_stderr():
    """The stderr console handler is unchanged so -s / caplog keep working."""
    assert isinstance(server._console_handler, logging.StreamHandler)
    # A StreamHandler over stderr has no baseFilename — it must NOT have been
    # accidentally swapped for a file handler.
    assert not hasattr(server._console_handler, 'baseFilename')


def test_mock_error_lands_in_sink_not_production():
    """A biz-logger error emitted during a test must land in the pytest sink's
    error.log and NOT append to the production logs/error.log."""
    prod_error_log = os.path.join(server.LOG_DIR, 'error.log')
    sink_error_log = os.path.join(server._FILE_LOG_DIR, 'error.log')

    # Guard: the fix must actually redirect — the two paths must differ.
    assert prod_error_log != sink_error_log

    marker = 'TOFU_SINK_ISOLATION_PROBE_9c3f21'

    def _read(path):
        try:
            with open(path, encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            return ''

    prod_before = _read(prod_error_log)

    # Emit through a biz-prefixed logger (passes _BizAndServerOnly), forcing a
    # flush so the write is observable synchronously.
    logging.getLogger('lib.test_sink_probe').error(marker)
    server._error_handler.flush()

    assert marker in _read(sink_error_log), 'probe did not reach the pytest sink'
    assert marker not in _read(prod_error_log), (
        'probe leaked into the PRODUCTION error.log — sink isolation broken'
    )
    # Production error.log must be byte-untouched by the probe.
    assert _read(prod_error_log) == prod_before
