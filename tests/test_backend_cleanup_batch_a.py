#!/usr/bin/env python3
"""Batch A backend-cleanup regression tests (bugs + dead code).

Three findings from the exhaustive backend audit, all in git-clean files:

  1. lib/doc_parser/_dispatch.py:51 — `is_supported_document` computed
     ``ext = os.path.splitext(filename)[0]`` (the STEM, wrongly commented
     "bug-safe") then immediately overwrote it with ``[1].lower()``. The first
     line was dead AND semantically wrong. Lock correct extension detection.
  2. lib/idempotency.py — ``idempotent_post(ttl=...)`` accepted a param it then
     ``del``'d ("reserved"); no caller ever passed it and TTLCache has no
     per-entry TTL. Misleading dead API — removed. Lock the decorator still
     works with no args.
  3. lib/log.py — trailing "Log Analysis Utilities" banner was an empty stub
     for a removed module. Lock it's gone.

Run standalone (``python tests/test_backend_cleanup_batch_a.py``) or via pytest.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


# ── 1. doc_parser extension detection ─────────────────────────────────

def test_is_supported_document_extensions():
    from lib.doc_parser._dispatch import is_supported_document
    assert is_supported_document('report.docx') is True
    assert is_supported_document('a.PDF'.replace('PDF', 'md')) is True  # .md
    assert is_supported_document('UPPER.DOCX') is True   # case-insensitive
    assert is_supported_document('archive.zip') is False
    assert is_supported_document('noext') is False
    # A filename whose STEM looks like an ext must NOT be misclassified
    # (the old `[0]` stem bug would have fed the stem into the lookup).
    assert is_supported_document('.md') is False   # splitext('.md') → ('.md','')
    _ok('is_supported_document: extension (not stem) drives the decision')


def test_dispatch_no_dead_stem_line():
    """Static guard: the dead `[0]  # bug-safe` stem line is gone."""
    import lib.doc_parser._dispatch as d
    src = inspect.getsource(d.is_supported_document)
    assert 'splitext(filename)[0]' not in src, 'dead stem line still present'
    assert src.count('splitext(filename)[1]') == 1, 'expected one [1] ext read'
    _ok('_dispatch: dead `splitext(...)[0]  # bug-safe` line removed')


# ── 2. idempotency ttl-param removal ──────────────────────────────────

def test_idempotent_post_signature_has_no_ttl():
    from lib.idempotency import idempotent_post
    sig = inspect.signature(idempotent_post)
    assert 'ttl' not in sig.parameters, 'dead ttl param should be removed'
    _ok('idempotent_post: dead ttl param removed from signature')


def test_idempotent_post_still_decorates_and_passes_through():
    """No-arg decorator still wraps a sync handler and passes through when no
    Idempotency-Key header/context is present (outside request context)."""
    from lib.idempotency import idempotent_post

    @idempotent_post()
    def handler():
        return {'ok': True, 'v': 42}

    # Called outside a request context → the guard's RuntimeError path just
    # invokes the wrapped fn.
    out = handler()
    assert out == {'ok': True, 'v': 42}, out
    _ok('idempotent_post(): still wraps + passes through with no ttl arg')


# ── 3. log.py dead stub removed ───────────────────────────────────────

def test_log_dead_analysis_stub_removed():
    import lib.log as log
    src = inspect.getsource(log)
    assert 'project_error_tracker.py (removed)' not in src, \
        'dead Log Analysis Utilities banner should be removed'
    assert 'No log-analysis utilities live here anymore' not in src
    _ok('log.py: dead "Log Analysis Utilities" stub banner removed')


def test_log_public_api_intact():
    """The removal must not touch the real logging API."""
    import lib.log as log
    for name in ('get_logger', 'log_exception', 'audit_log', 'log_context',
                 'log_route', 'log_external', 'log_suppressed', 'set_req_id',
                 'req_id'):
        assert hasattr(log, name), f'log.{name} missing after cleanup'
    _ok('log.py: public logging API intact')


def main():
    print()
    print(_color('═══ Backend Cleanup Batch A (bugs + dead code) ═══', '36'))
    print()
    tests = [
        test_is_supported_document_extensions,
        test_dispatch_no_dead_stem_line,
        test_idempotent_post_signature_has_no_ttl,
        test_idempotent_post_still_decorates_and_passes_through,
        test_log_dead_analysis_stub_removed,
        test_log_public_api_intact,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
