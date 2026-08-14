"""tests/test_log_pytest_sink_isolation.py — 2026-07-13, REPOINTED 2026-07-27.

Regression guard: the test suite must never write into the PRODUCTION logs.

WHY (unchanged since 2026-07-13): ``tests/conftest.py`` does ``import server``
at collection time, which runs ``logging.basicConfig(handlers=[...])`` and
attaches the rotating FILE handlers (app.log / error.log / vendor.log /
access.log) to the ROOT logger for the whole session. The suite deliberately
drives error paths with mocked exceptions ('llm down', 'disk full', 'reload
boom', a merge-conflict 'bad.js'). Without isolation every test's
``logger.error()`` bleeds those fixtures into the REAL ``logs/error.log`` and
``logs/app.log`` — poisoning the operator's #1 diagnostic surface, because a
reader cannot tell a fixture from a real outage.

★ WHY THIS FILE WAS REWRITTEN (2026-07-27)

The original guard asserted a SPECIFIC IMPLEMENTATION: ``server._UNDER_PYTEST``
and ``server._FILE_LOG_DIR == <repo>/logs/pytest``. Both symbols are GONE from
server.py — no ``git log -S`` hit, so they vanished in a wholesale rewrite of
the logging block rather than a traceable deletion. The guard had therefore
been RED for ~14 days, and with it dead the leak came back: measured
2026-07-27, one test emitting a single ``logger.error()`` + ``audit_log()``
grew the production ``app.log`` by 83 bytes and landed markers in BOTH
``app.log`` and ``audit.log``. Meanwhile that same app.log reached 9.1 GB in a
day (96% of it one wedged sub-agent), with test noise mixed in.

The replacement isolation is UPSTREAM of the handlers: ``tests/conftest.py``
sets ``TOFU_DATA_DIR`` to a per-worker temp dir BEFORE ``import server``, so
``lib/log.py`` resolves ``LOG_DIR`` (frozen at import time) into that temp dir
and EVERY consumer follows — the four file handlers, ``audit_log()``'s own
``AUDIT_LOG_FILE`` path, and anything else derived from the base dir. That is
strictly broader than the old ``logs/pytest/`` sink, which redirected only the
four handlers and still wrote inside the repo.

So these tests now assert the OUTCOME (nothing lands in <repo>/logs) rather
than the mechanism, which is what a guard should have pinned in the first
place — an implementation-shaped assertion is exactly what let this one rot
into a false red while the real protection silently disappeared.

Companion: tests/test_log_isolation_guard.py (path + write-through checks on
the lib.log constants). This file covers the server.py handler objects.
"""

from __future__ import annotations

import logging
import os
import uuid

import pytest

# conftest already did ``import server`` at collection time; this is a no-op
# re-import that returns the cached module.
import server

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROD_LOG_DIR = os.path.join(_ROOT, 'logs')


def _prod(name: str) -> str:
    return os.path.join(_PROD_LOG_DIR, name)


def test_log_dir_is_isolated_from_the_repo():
    """server.LOG_DIR must not be the production <repo>/logs directory."""
    assert os.path.abspath(server.LOG_DIR) != os.path.abspath(_PROD_LOG_DIR), (
        'the test session resolved LOG_DIR to the PRODUCTION log dir; set '
        'TOFU_DATA_DIR in conftest BEFORE `import server` (LOG_DIR is frozen '
        'at import time in lib/log.py)')


def test_all_file_handlers_write_outside_production():
    """Every rotating FILE handler must live outside <repo>/logs.

    Asserts the OUTCOME, not a particular sink layout — the previous version
    pinned ``server._FILE_LOG_DIR``, which no longer exists.
    """
    handlers = [(n, getattr(server, n, None)) for n in
                ('_app_handler', '_error_handler',
                 '_vendor_handler', '_access_handler')]
    present = [(n, h) for n, h in handlers if h is not None]
    assert present, 'no file handlers found on server — has the logging block moved?'

    for name, handler in present:
        base = getattr(handler, 'baseFilename', None)
        if base is None:
            continue
        parent = os.path.abspath(os.path.dirname(base))
        assert parent != os.path.abspath(_PROD_LOG_DIR), (
            f'{name} writes to {base} — inside the PRODUCTION log dir')


def test_console_handler_still_targets_stderr():
    """The stderr console handler is unchanged so -s / caplog keep working."""
    h = getattr(server, '_console_handler', None)
    if h is None:
        pytest.skip('server._console_handler not exposed')
    assert isinstance(h, logging.StreamHandler)
    # A StreamHandler over stderr has no baseFilename — it must NOT have been
    # accidentally swapped for a file handler.
    assert not hasattr(h, 'baseFilename')


def test_mock_error_does_not_reach_production_error_log():
    """A biz-logger error emitted during a test must not append to the real
    logs/error.log (nor app.log)."""
    # Minted PER RUN, deliberately. A hardcoded literal made this guard
    # SELF-POISONING: once the marker leaked into logs/error.log (measured
    # 2026-07-27 17:58, a single occurrence), the content assertion below
    # failed on every subsequent run FOREVER — even once isolation was fully
    # restored — because the string it searches for was already in the file.
    # A red that cannot be cleared by fixing the bug is indistinguishable from
    # noise, which is precisely how this file's predecessor rotted for 14 days.
    marker = 'TOFU_SINK_ISOLATION_PROBE_' + uuid.uuid4().hex[:12]
    prod_error, prod_app = _prod('error.log'), _prod('app.log')

    def _size(p):
        return os.path.getsize(p) if os.path.exists(p) else 0

    def _read(p):
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                return f.read()
        except FileNotFoundError:
            return ''

    before_error, before_app = _size(prod_error), _size(prod_app)

    logging.getLogger('lib.test_sink_probe').error(marker)
    for h in logging.getLogger().handlers:
        h.flush()

    assert _size(prod_error) == before_error, (
        'probe grew the PRODUCTION error.log — isolation broken')
    assert _size(prod_app) == before_app, (
        'probe grew the PRODUCTION app.log — isolation broken')
    assert marker not in _read(prod_error), (
        'probe leaked into the PRODUCTION error.log')
