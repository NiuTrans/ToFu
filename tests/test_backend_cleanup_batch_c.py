#!/usr/bin/env python3
"""Batch C backend-cleanup test: routes/api_v1/memory._project_path dedup.

Finding: _project_path() parsed request JSON TWICE (once in the `if` guard,
once to read .get('project_path')). Under the sync→loop shim each parse is a
cross-thread hop to the event loop, so the guard doubled the
body-read cost of every memory route. Deduped to a single parse while keeping
the JSON-body branch's precedence over the query-string branch.

Static + behavioral guard (no live request context needed for the static one).
Run standalone (``python tests/test_backend_cleanup_batch_c.py``) or via pytest.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Read the source WITHOUT importing (routes.api_v1.memory transitively imports
# routes.push, whose @push_bp.websocket decorator needs the server.py
# Flask→Quart shim installed — a known test-harness artifact unrelated to this
# change). A static-source guard is exactly what this dedup needs.
_MEM_SRC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'routes', 'api_v1', 'memory.py')


def _project_path_source() -> str:
    src = open(_MEM_SRC_PATH, encoding='utf-8').read()
    start = src.index('\ndef _project_path()')
    # End at the next top-level def (the function body includes the inline
    # `from lib.request_parser import decode_proxy_path_arg` fallback).
    nxt = src.index('\ndef ', start + 1)
    return src[start:nxt]


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_project_path_parses_body_once():
    """Static guard: _project_path calls request_json at most once."""
    src = _project_path_source()
    n = src.count('request_json(')
    assert n <= 1, f'_project_path should call request_json at most once, found {n}'
    _ok(f'_project_path: request_json called {n}× (deduped from 2)')


def test_project_path_json_branch_precedence_documented():
    """The JSON body branch still precedes the query-string branch (in CODE,
    not counting the docstring which mentions decode_proxy_path_arg in prose)."""
    src = _project_path_source()
    # Drop the docstring so prose mentions don't skew position checks.
    code = re.sub(r'""".*?"""', '', src, count=1, flags=re.S)
    json_pos = code.find('request_json(')
    qs_pos = code.find('decode_proxy_path_arg')
    assert json_pos != -1 and qs_pos != -1 and json_pos < qs_pos, \
        'JSON body branch must precede the query-string fallback'
    _ok('_project_path: JSON body branch still precedes query-string fallback')


def main():
    print()
    print(_color('═══ Backend Cleanup Batch C (routes dedup) ═══', '36'))
    print()
    tests = [
        test_project_path_parses_body_once,
        test_project_path_json_branch_precedence_documented,
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
