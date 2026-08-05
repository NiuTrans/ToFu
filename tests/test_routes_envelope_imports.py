#!/usr/bin/env python3
"""Envelope-helper import ratchet for routes/** — the missing-import bug class.

WHY (epic pt_551fc875f3034f38)
------------------------------
The api-contract migration (batches 9/18/…) rewrote hundreds of ``jsonify``
call sites to the ``api_*`` envelope family and, in three files, dropped the
import that keeps the name resolvable:

  * ``routes/api_v1/auth.py``      — ``api_error`` used ×3, never imported:
    EVERY rejection through the global auth gate (bad token 401 / no
    credential 401 / rate-limit 429) raised NameError → 500 instead of the
    intended 401/429 (17 requires_auth tests red).
  * ``routes/conversations.py``    — ``jsonify`` used in ``_Defer(jsonify, …)``
    for every save_conv 409 (blocked_rev_conflict / blocked_empty_overwrite /
    blocked_msg_regression): PRODUCTION 500 observed 2026-08-03 13:05:01
    ("name 'jsonify' is not defined" on PUT /api/v1/conversations).
  * ``routes/common.py``           — ``api_error`` used in ``_db_safe``'s
    503 'database_busy' path.

Source-parity suites pin the api_* CALL (so the migration looked complete)
while nothing pinned the IMPORT — exactly the layer this ratchet adds.

WHAT IT CHECKS
--------------
For every ``routes/**/*.py``, walk the AST and collect:

  * imported names — from ANY ``Import``/``ImportFrom`` node at ANY scope
    (module-level AND function-level lazy imports, e.g. chat_queue.py's
    in-function ``from lib.api_response import api_bad_request``);
  * locally-defined names — ``def``, ``class``, assignments, ``for`` targets,
    ``with ... as``, function parameters (a local binding shadows an import);

then every bare ``Name`` load of an envelope helper (the ``api_*`` family +
``jsonify``) must resolve to one of those names. Attribute access
(``module.api_ok(...)`` / ``getattr``) is out of scope — only bare names can
NameError this way.

VACUITY GUARD (NEUTER-equivalent): the disk-neuter harness can't apply (this
ratchet reads source from disk), so the checker's bite is proven on SYNTHETIC
sources: a missing import must be flagged, the lazy-import and local-binding
patterns must be accepted.
"""

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Names that must never be referenced unbound in routes/**. The api_response
#: public family + flask.jsonify — every one has been (or could be) dropped
#: by an envelope-migration edit.
WATCH_NAMES = frozenset({
    'api_ok', 'api_error', 'api_payload', 'api_created',
    'api_bad_request', 'api_unauthorized', 'api_forbidden',
    'api_not_found', 'api_internal_error', 'api_no_content',
    'jsonify',
})


def _collect_bound_names(tree) -> set:
    """All names bound ANYWHERE in the module (any scope)."""
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add(a.asname or a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == '*':
                    continue
                bound.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                for a in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                    bound.add(a.arg)
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(
                node.ctx, (ast.Store, ast.Param)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def find_unbound_envelope_uses(src: str, path: str = '<src>') -> list:
    """Return ['path:LINE name', ...] for every envelope helper referenced
    but never bound (imported or defined) anywhere in the source."""
    tree = ast.parse(src, filename=path)
    bound = _collect_bound_names(tree)
    problems = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id in WATCH_NAMES and node.id not in bound):
            problems.append(f'{path}:{node.lineno} {node.id}')
    return problems


# ───────────────────────── vacuity guard (synthetic) ─────────────────────────

def test_checker_flags_missing_import():
    """The checker MUST bite: a bare api_error( call with no import is the
    exact auth.py bug — flagged with its line number."""
    src = (
        'def gate():\n'
        '    return api_error({"kind": "unauthorized"}, status=401)\n'
    )
    problems = find_unbound_envelope_uses(src, 'synth.py')
    assert problems == ['synth.py:2 api_error'], problems


def test_checker_accepts_module_and_lazy_imports():
    """Both import styles count: module-level AND the in-function lazy import
    (chat_queue.py's legitimate pattern) — neither may be flagged."""
    src = (
        'from lib.api_response import api_ok\n'
        'def a():\n'
        '    return api_ok({' '})\n'
        'def b():\n'
        '    from lib.api_response import api_bad_request\n'
        '    return api_bad_request("x")\n'
        'def c():\n'
        '    from lib.api_response import (\n'
        '        api_error,\n'
        '        api_not_found,\n'
        '    )\n'
        '    return api_error("y") or api_not_found("z")\n'
    )
    assert find_unbound_envelope_uses(src) == []


def test_checker_accepts_local_binding_and_params():
    """A local assignment / def / parameter binding shadows the import need."""
    src = (
        'jsonify = lambda x: x\n'
        'def make(jsonify=None):\n'
        '    return jsonify({})\n'
        'def api_ok(x):\n'
        '    return x\n'
        'def use():\n'
        '    return api_ok({})\n'
    )
    assert find_unbound_envelope_uses(src) == []


# ───────────────────────── the ratchet ─────────────────────────

def _iter_route_files():
    for root, _dirs, files in os.walk(os.path.join(_ROOT, 'routes')):
        for f in sorted(files):
            if f.endswith('.py'):
                yield os.path.join(root, f)


def test_routes_tree_envelope_imports_clean():
    """Every routes/**/*.py resolves every envelope helper it references."""
    problems = []
    for path in _iter_route_files():
        with open(path, encoding='utf-8') as fh:
            problems.extend(find_unbound_envelope_uses(
                fh.read(), os.path.relpath(path, _ROOT)))
    assert not problems, (
        'envelope helper(s) referenced but never imported/defined '
        '(migration-era missing-import bug class):\n  ' + '\n  '.join(problems))


# ───────────────────────── standalone runner ─────────────────────────

def main():
    fns = [test_checker_flags_missing_import,
           test_checker_accepts_module_and_lazy_imports,
           test_checker_accepts_local_binding_and_params,
           test_routes_tree_envelope_imports_clean]
    ok = True
    for fn in fns:
        try:
            fn()
            print(' ', '\033[32m✓\033[0m', fn.__name__)
        except AssertionError as e:
            ok = False
            print(' ', f'\033[31m✗\033[0m {fn.__name__}: {e}')
    print()
    print('\033[32m═══ envelope-import ratchet PASSED ═══\033[0m' if ok
          else '\033[31m═══ FAILURES ═══\033[0m')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
