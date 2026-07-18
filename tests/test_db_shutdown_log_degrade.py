#!/usr/bin/env python3
"""Regression: during a clean shutdown, EXPECTED PG-stopping errors must log as a
single concise line — not a full ERROR traceback per call site.

WHY (the restart-log cascade, 2026-07-11)
-----------------------------------------
When the local postmaster is stopped on server exit, background finalize writes
still in flight (commit-round daemon, profile-consolidation daemon, a just-
finished run_task's persist/sync/checkpoint chain, killed-recovery re-stamp,
queue auto-dispatch) each fail with ``FATAL: the database system is shutting
down`` / ``server closed the connection unexpectedly``. Every one logged a full
``exc_info=True`` traceback → dozens of scary stacks per restart. These are
EXPECTED shutdown races, not bugs.

THE FIX
-------
A process-level flag (`mark_pg_stopping`, set at the top of
`stop_local_pg_if_owned`) + a shared predicate `is_expected_shutdown_error`.
The DB wrapper degrades its own SQL-failure log, and the upper finalize sites
call `log_db_finalize_error(...)` which drops the traceback to one INFO line
ONLY while a stop is in progress AND the error is a shutdown signature.

Tests (pure — no real PG stop; the flag + fake exceptions drive the logic):
  1. predicate: shutdown signature + flag set → True; flag unset → False;
     non-shutdown error + flag set → False.
  2. log_db_finalize_error: flag set + shutdown err → INFO, no exc_info.
  3. NC (byte-revert equivalent): flag UNSET → same error logs ERROR + exc_info
     (the loud path the fix removes only during shutdown).
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


class _FakeShutdownErr(Exception):
    """Stands in for psycopg2.OperationalError with a shutdown message."""


_SHUTDOWN_MSG = ('connection to server at "127.0.0.1", port 15439 failed: '
                 'FATAL:  the database system is shutting down')
_CLOSED_MSG = 'server closed the connection unexpectedly'
_REAL_MSG = 'duplicate key value violates unique constraint "conversations_pkey"'


def _reset_flag():
    import lib.database._core as core
    core._PG_STOPPING = False


def test_predicate_gated_on_flag_and_signature():
    import lib.database._core as core
    _reset_flag()
    # Flag unset → even a shutdown message is NOT "expected" (genuine dead PG).
    assert core.is_expected_shutdown_error(_FakeShutdownErr(_SHUTDOWN_MSG)) is False
    core.mark_pg_stopping()
    try:
        assert core.is_expected_shutdown_error(_FakeShutdownErr(_SHUTDOWN_MSG)) is True
        assert core.is_expected_shutdown_error(_FakeShutdownErr(_CLOSED_MSG)) is True
        # A genuine app error during shutdown is NOT masked.
        assert core.is_expected_shutdown_error(_FakeShutdownErr(_REAL_MSG)) is False
    finally:
        _reset_flag()
    _ok('is_expected_shutdown_error gated on BOTH the stopping flag AND signature')


def test_finalize_helper_degrades_to_info_during_shutdown(caplog):
    import lib.database._core as core
    log = logging.getLogger('test_db_finalize')
    _reset_flag()
    core.mark_pg_stopping()
    try:
        caplog.clear()
        with caplog.at_level(logging.INFO, logger='test_db_finalize'):
            core.log_db_finalize_error(log, 'error', _FakeShutdownErr(_SHUTDOWN_MSG),
                                       '[Checkpoint ab] conv=cd Failed to checkpoint')
        recs = [r for r in caplog.records if r.name == 'test_db_finalize']
        assert len(recs) == 1, f'expected exactly one record, got {len(recs)}'
        assert recs[0].levelno == logging.INFO, f'expected INFO, got {recs[0].levelname}'
        assert recs[0].exc_info is None, 'must NOT carry a traceback during shutdown'
        assert 'aborted during shutdown' in recs[0].getMessage()
    finally:
        _reset_flag()
    _ok('log_db_finalize_error → single INFO line, no traceback, during shutdown')


def test_NC_finalize_helper_keeps_error_and_traceback_when_not_stopping(caplog):
    """NC: flag UNSET (byte-revert of the shutdown state) → the SAME error logs
    at ERROR WITH exc_info — the loud path is only removed DURING shutdown."""
    import lib.database._core as core
    log = logging.getLogger('test_db_finalize_nc')
    _reset_flag()  # NOT stopping
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='test_db_finalize_nc'):
        core.log_db_finalize_error(log, 'error', _FakeShutdownErr(_SHUTDOWN_MSG),
                                   '[Checkpoint ab] conv=cd Failed to checkpoint')
    recs = [r for r in caplog.records if r.name == 'test_db_finalize_nc']
    assert len(recs) == 1, f'expected one record, got {len(recs)}'
    assert recs[0].levelno == logging.ERROR, f'expected ERROR, got {recs[0].levelname}'
    assert recs[0].exc_info is not None, 'must carry a traceback when NOT shutting down'
    _ok('NC: not-stopping → ERROR + traceback preserved (loud path intact)')


def test_genuine_error_during_shutdown_still_loud(caplog):
    """A real app error (unique-constraint) even DURING shutdown keeps ERROR +
    traceback — the degrade only masks shutdown-signature errors."""
    import lib.database._core as core
    log = logging.getLogger('test_db_finalize_real')
    _reset_flag()
    core.mark_pg_stopping()
    try:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger='test_db_finalize_real'):
            core.log_db_finalize_error(log, 'error', _FakeShutdownErr(_REAL_MSG),
                                       '[Sync] conv=cd failed')
        recs = [r for r in caplog.records if r.name == 'test_db_finalize_real']
        assert len(recs) == 1 and recs[0].levelno == logging.ERROR, recs
        assert recs[0].exc_info is not None, 'genuine error must keep its traceback'
    finally:
        _reset_flag()
    _ok('genuine error during shutdown stays ERROR + traceback (not masked)')


def main():
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_db_shutdown_log_degrade.__main__')

    class _CapLog:
        """Minimal caplog shim for standalone runs."""
        def __init__(self):
            self.records = []
            self._handler = None
        def clear(self):
            self.records = []
        class _Ctx:
            def __init__(self, outer, level, logger):
                self.outer, self.level, self.lname = outer, level, logger
            def __enter__(self):
                lg = logging.getLogger(self.lname)
                self._prev = lg.level
                lg.setLevel(self.level)
                h = logging.Handler()
                h.emit = lambda rec: self.outer.records.append(rec)
                lg.addHandler(h)
                self._h, self._lg = h, lg
                return self
            def __exit__(self, *a):
                self._lg.removeHandler(self._h)
                self._lg.setLevel(self._prev)
                return False
        def at_level(self, level, logger=''):
            return self._Ctx(self, level, logger)

    print()
    print(_color('═══ DB shutdown-log-degrade tests ═══', '36'))
    print()
    cap = _CapLog()
    tests = [
        (test_predicate_gated_on_flag_and_signature, ()),
        (test_finalize_helper_degrades_to_info_during_shutdown, (cap,)),
        (test_NC_finalize_helper_keeps_error_and_traceback_when_not_stopping, (cap,)),
        (test_genuine_error_during_shutdown_still_loud, (cap,)),
    ]
    for fn, args in tests:
        try:
            fn(*args)
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} DB SHUTDOWN-LOG TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
