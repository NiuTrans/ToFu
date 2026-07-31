"""Guard: the test suite must never write into the PRODUCTION log files.

WHY
---
2026-07-27: ``logs/app.log`` reached 9.1 GB in a day. 96% of it (53,366,229
lines) came from ONE wedged swarm sub-agent — and the reason those lines
reached the production log at all is that the test process resolves
``lib.log.LOG_DIR`` to the REAL ``<repo>/logs`` directory.

MEASURED (not theoretical): a single test that calls ``get_logger(...).error()``
plus ``audit_log(...)`` grew the production ``app.log`` by 83 bytes and landed
its marker in both ``app.log`` and ``audit.log``. Four production file handlers
(2x TimedRotatingFileHandler + 2x RotatingFileHandler) are attached to the root
logger during the run, because ``import server`` in conftest builds the real
logging stack.

``tests/conftest.py`` already isolates the DB (after the 2026-06-28 incident
deleted ~2300 real conversations), the scheduler, the netpath prober, and
mlock. Logs were the one shared resource with NO isolation — and the netpath
comment in conftest even names the hazard out loud ("would write into the
production logs/app.log"), which shows the risk was understood but only ever
patched case-by-case instead of closed at the seam.

THE SEAM: ``lib/log.py::_writable_base_dir()`` honours ``TOFU_DATA_DIR`` first
(line ~95), and ``LOG_DIR`` is computed AT IMPORT TIME (line ~147). So the env
var must be set BEFORE ``import server`` — i.e. in conftest's
``_install_shim_for_collection()`` block, next to the ``TOFU_DB_PATH``
isolation that has the identical ordering constraint.

Per-worker keying mirrors ``TOFU_DB_PATH``: each xdist worker is its own
process and inherits the controller's env, so the path is keyed on
``PYTEST_XDIST_WORKER`` to avoid workers sharing one log dir.
"""

from __future__ import annotations

import os
import unittest

import pytest

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROD_LOG_DIR = os.path.join(_ROOT, 'logs')


class TestLogIsolation(unittest.TestCase):

    def test_log_dir_is_not_the_production_dir(self):
        """The resolved LOG_DIR must be a throwaway dir, not <repo>/logs."""
        import lib.log as L
        self.assertNotEqual(
            os.path.abspath(L.LOG_DIR), os.path.abspath(_PROD_LOG_DIR),
            'the test process resolves LOG_DIR to the PRODUCTION log dir; a '
            'test that logs (or any background thread it starts) appends to '
            'the real app.log — measured 2026-07-27: one test added 83 bytes '
            'to a 9.1 GB production app.log. Set TOFU_DATA_DIR in conftest '
            'BEFORE `import server` (LOG_DIR is frozen at import time).')

    def test_app_log_path_is_isolated(self):
        import lib.log as L
        self.assertNotEqual(
            os.path.abspath(L.APP_LOG),
            os.path.abspath(os.path.join(_PROD_LOG_DIR, 'app.log')),
            'APP_LOG points at the production file')

    def test_audit_log_path_is_isolated(self):
        """audit_log() writes via its own path constant — isolate it too."""
        import lib.log as L
        self.assertNotEqual(
            os.path.abspath(L.AUDIT_LOG_FILE),
            os.path.abspath(os.path.join(_PROD_LOG_DIR, 'audit.log')),
            'AUDIT_LOG_FILE points at the production audit trail')

    def test_writing_a_log_line_does_not_touch_production_app_log(self):
        """End-to-end: emit a real record + audit entry, assert prod is inert.

        This is the behavioural complement to the path assertions above — it
        catches a handler that was constructed with a stale path even if the
        module constants were later corrected.
        """
        import logging

        from lib.log import audit_log, get_logger
        prod_app = os.path.join(_PROD_LOG_DIR, 'app.log')
        prod_audit = os.path.join(_PROD_LOG_DIR, 'audit.log')
        before_app = os.path.getsize(prod_app) if os.path.exists(prod_app) else 0
        before_audit = (os.path.getsize(prod_audit)
                        if os.path.exists(prod_audit) else 0)

        get_logger('lib.log_isolation_probe').error(
            'LOG_ISOLATION_PROBE_MARKER')
        audit_log('log_isolation_probe', note='guard')
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass

        after_app = os.path.getsize(prod_app) if os.path.exists(prod_app) else 0
        after_audit = (os.path.getsize(prod_audit)
                       if os.path.exists(prod_audit) else 0)
        self.assertEqual(before_app, after_app,
                         f'test wrote {after_app - before_app} bytes into the '
                         f'PRODUCTION app.log')
        self.assertEqual(before_audit, after_audit,
                         f'test wrote {after_audit - before_audit} bytes into '
                         f'the PRODUCTION audit.log')


if __name__ == '__main__':
    unittest.main(verbosity=2)
