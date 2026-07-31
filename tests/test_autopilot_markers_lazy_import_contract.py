#!/usr/bin/env python3
"""Two-module cycle-elimination guard for autopilot_markers ↔ autopilot_run_lifecycle.

**Why this test exists**

The slice-2 extraction (commit 01d2db3a) moved ``disarm_autopilot`` from
``lib/tasks_pkg/autopilot.py`` into ``lib/tasks_pkg/autopilot_markers.py``.
``disarm_autopilot`` calls ``conclude_run``, which at slice-2 time STAYED in
``autopilot.py``. And ``autopilot.py`` in turn re-exports the marker cluster
from ``autopilot_markers`` for facade identity — the two modules mutually
depended, and the slice-2 fix threaded the ``conclude_run`` import THROUGH
the function body of ``disarm_autopilot`` (lazy import) so no runtime cycle
existed. The invariant at that point was "the import MUST be inside the
function body — hoisting it would recreate the cycle".

Slice 3 (pt_00459503) moves ``conclude_run`` (and its three helpers
``_store_run_record``, ``_emit_run_concluded``,
``_emit_run_concluded_event``) into a brand-new LEAF module
``lib/tasks_pkg/autopilot_run_lifecycle.py`` that has ZERO top-level imports
from ``autopilot.py``. With ``conclude_run`` no longer living inside
``autopilot.py``, the two-module cycle is ELIMINATED at the graph level:

    autopilot.py  ──(re-export)──▶ autopilot_run_lifecycle.py
                                              │
                                              ▼
                                    autopilot_state.py
    autopilot_markers.py  ──(top-level)──▶ autopilot_run_lifecycle.py
                                                         │
                                                         ▼
                                               autopilot_state.py

The invariant therefore FLIPS at slice 3: the ``conclude_run`` import inside
``autopilot_markers.py`` is now expected to be at MODULE TOP (pointing at
the leaf module ``autopilot_run_lifecycle``), and there must be NO import
from ``lib.tasks_pkg.autopilot`` anywhere in ``autopilot_markers.py`` (module
scope OR function scope) — because the whole point of the leaf module is to
ensure autopilot.py never needs to be pulled in during autopilot_markers's
own initialisation.

This file locks that post-slice-3 layout with FOUR structural guards on the
package graph, independent of runtime behaviour:

  1. ``test_autopilot_markers_imports_alone_in_fresh_subprocess`` — runs
     ``python -c "import lib.tasks_pkg.autopilot_markers"`` in a FRESH
     subprocess with a clean ``sys.modules``. On success, the subprocess
     must (a) exit 0 with the three extracted callables present, and
     (b) confirm that ``lib.tasks_pkg.autopilot`` was NOT pulled in as a
     side effect. A regression where autopilot_markers reacquires a
     dependency on autopilot (e.g. someone edits it to re-import
     ``conclude_run`` from the facade instead of the leaf) would flip the
     assertion.

  2. ``test_autopilot_markers_conclude_run_import_targets_leaf_module`` —
     parses ``autopilot_markers.py`` and confirms:
       (a) there is a TOP-LEVEL ``from lib.tasks_pkg.autopilot_run_lifecycle
           import conclude_run`` line — proves the leaf-module wiring is
           in place, and
       (b) ``conclude_run`` is NOT lazy-imported anywhere inside the
           ``disarm_autopilot`` function body — proves the slice-2 lazy
           hack has been fully lifted (a leftover in-function import would
           mask a botched top-level rewrite).

  3. ``test_autopilot_markers_has_no_import_from_autopilot`` — walks the
     WHOLE AST of ``autopilot_markers.py`` (module scope AND function
     scopes) and confirms NO ``from lib.tasks_pkg.autopilot import`` node
     exists at ANY nesting depth. This is the strongest cycle guard: as
     long as autopilot_markers has zero dependency on autopilot, no
     circular import can ever be reintroduced no matter how a future edit
     restructures the module.

  4. ``test_autopilot_py_reexports_markers_and_run_lifecycle_at_module_top``
     — the complementary check on autopilot.py's side: the facade
     re-exports from BOTH ``autopilot_markers`` and
     ``autopilot_run_lifecycle`` must remain at module scope so external
     callers (routes/chat_queue.py, lib/chat_dispatch.py, and the
     ``monkeypatch.setattr(ap, 'conclude_run', ...)`` sibling tests) keep
     working through the facade identity.

All four target the STRUCTURE, not observable side effects, so they survive
future rewrites of ``disarm_autopilot``'s body as long as the cycle-free
graph shape holds.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MARKERS_PATH = os.path.join(_ROOT, 'lib', 'tasks_pkg', 'autopilot_markers.py')
_AUTOPILOT_PATH = os.path.join(_ROOT, 'lib', 'tasks_pkg', 'autopilot.py')
_LEAF_PATH = os.path.join(
    _ROOT, 'lib', 'tasks_pkg', 'autopilot_run_lifecycle.py')


# ══════════════════════════════════════════════════════════
#  Guard 1 — fresh-subprocess import-order test
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_autopilot_markers_imports_alone_in_fresh_subprocess():
    """``lib.tasks_pkg.autopilot_markers`` must be import-safe as the FIRST
    thing loaded — NO top-level dependency on ``lib.tasks_pkg.autopilot``.

    Runs in a FRESH ``python -c`` subprocess so the current test process's
    already-imported ``autopilot`` module cannot mask a regression: the
    subprocess starts with a clean sys.modules and imports ONLY
    autopilot_markers plus its true (non-cyclic) dependencies. The leaf
    module ``autopilot_run_lifecycle`` is expected to be pulled in as a
    top-level dep; ``autopilot`` itself must NOT be.
    """
    script = textwrap.dedent("""
        import sys
        assert 'lib.tasks_pkg.autopilot' not in sys.modules, (
            'unexpected pre-import of lib.tasks_pkg.autopilot in subprocess')
        assert 'lib.tasks_pkg.autopilot_markers' not in sys.modules, (
            'unexpected pre-import of lib.tasks_pkg.autopilot_markers')

        import lib.tasks_pkg.autopilot_markers as m

        # Sanity: the three extracted symbols must be defined at module top
        # after the fresh import (proves the module's __init__ ran to
        # completion, not aborted midway on a cycle).
        for name in ('arm_autopilot', 'disarm_autopilot', '_marker_exists'):
            assert callable(getattr(m, name, None)), (
                'lib.tasks_pkg.autopilot_markers.' + name +
                ' missing after fresh import — the cycle-free contract has '
                'regressed and the module failed to initialise.')

        # The leaf module IS an expected top-level dep (that's how the
        # cycle was eliminated).
        assert 'lib.tasks_pkg.autopilot_run_lifecycle' in sys.modules, (
            'autopilot_markers must import conclude_run from '
            'autopilot_run_lifecycle at module top; the leaf module is '
            'missing from sys.modules after the import.')

        # Prove `lib.tasks_pkg.autopilot` was NOT pulled in as a side
        # effect. If a future refactor reintroduced a dependency on
        # autopilot (e.g. someone reverted to importing conclude_run
        # through the facade), this assertion would flip.
        assert 'lib.tasks_pkg.autopilot' not in sys.modules, (
            'importing autopilot_markers pulled in lib.tasks_pkg.autopilot '
            '— the cycle-free contract has regressed. autopilot_markers '
            'must depend on autopilot_run_lifecycle (the leaf module), '
            'NOT on autopilot itself.')

        # Bound-callable check: the conclude_run visible on autopilot_markers
        # must BE the leaf module's conclude_run (identity, not a copy).
        import lib.tasks_pkg.autopilot_run_lifecycle as leaf
        assert m.conclude_run is leaf.conclude_run, (
            'autopilot_markers.conclude_run identity does not match the '
            'leaf module; the top-level import wiring is wrong.')

        print('OK')
    """).strip()

    proc = subprocess.run(
        [sys.executable, '-c', script],
        cwd=_ROOT,
        env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        'Fresh-subprocess import of lib.tasks_pkg.autopilot_markers failed — '
        'the cycle-free contract is broken.\n'
        f'stdout:\n{proc.stdout}\n'
        f'stderr:\n{proc.stderr}'
    )
    assert 'OK' in proc.stdout, (
        f'Subprocess exited 0 but did not confirm OK. stdout: {proc.stdout!r}')


# ══════════════════════════════════════════════════════════
#  Guard 2 — top-level import targets the LEAF module, not the facade
# ══════════════════════════════════════════════════════════

def _collect_imports_from(tree: ast.Module, module_name: str,
                          scope: str = 'module') -> list[ast.ImportFrom]:
    """Return ``from <module_name> import ...`` nodes at the requested scope.

    ``scope='module'`` walks only Module.body children (direct top-level).
    ``scope='function'`` walks INSIDE function bodies (any nesting depth).
    ``scope='any'`` walks the whole AST.
    """
    out: list[ast.ImportFrom] = []
    if scope == 'module':
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == module_name:
                out.append(node)
        return out
    if scope == 'function':
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef):
                continue
            for sub in ast.walk(func):
                if isinstance(sub, ast.ImportFrom) and sub.module == module_name:
                    out.append(sub)
        return out
    # 'any'
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module_name:
            out.append(node)
    return out


@pytest.mark.unit
def test_autopilot_markers_conclude_run_import_targets_leaf_module():
    """AST-level lock on the post-slice-3 import layout.

    Two structural assertions on ``lib/tasks_pkg/autopilot_markers.py``:

      1. A TOP-LEVEL ``from lib.tasks_pkg.autopilot_run_lifecycle import
         conclude_run`` line exists — proves the leaf-module wiring is in
         place (the whole reason the cycle was eliminated).

      2. ``conclude_run`` is NOT lazy-imported anywhere inside the
         ``disarm_autopilot`` function body. A leftover in-function import
         would mask a botched top-level rewrite AND recreate the old lazy
         hack — the invariant is "top-level, targeting the leaf module,
         nothing else".
    """
    with open(_MARKERS_PATH, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)

    # (1) Top-level import of conclude_run from the leaf module.
    top_level = _collect_imports_from(
        tree, 'lib.tasks_pkg.autopilot_run_lifecycle', scope='module')
    assert top_level, (
        'lib/tasks_pkg/autopilot_markers.py MUST import conclude_run from '
        'lib.tasks_pkg.autopilot_run_lifecycle at MODULE TOP (post-slice-3 '
        'cycle-elimination). Without this import the leaf-module wiring '
        'is broken and disarm_autopilot cannot resolve conclude_run.'
    )
    imported_names: set[str] = set()
    for n in top_level:
        for alias in n.names:
            imported_names.add(alias.asname or alias.name)
    assert 'conclude_run' in imported_names, (
        'the top-level import from autopilot_run_lifecycle must include '
        f'conclude_run; found: {sorted(imported_names)}'
    )

    # (2) No lazy conclude_run import anywhere inside disarm_autopilot.
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef) or func.name != 'disarm_autopilot':
            continue
        for sub in ast.walk(func):
            if not isinstance(sub, ast.ImportFrom):
                continue
            if sub.module not in (
                'lib.tasks_pkg.autopilot',
                'lib.tasks_pkg.autopilot_run_lifecycle',
            ):
                continue
            if any(alias.name == 'conclude_run' for alias in sub.names):
                pytest.fail(
                    'lib/tasks_pkg/autopilot_markers.py::disarm_autopilot has '
                    'a lazy `from ... import conclude_run` at line '
                    f'{sub.lineno} — the slice-3 refactor lifted this to '
                    'module top; a residual in-function import means the '
                    'top-level wiring is bypassed (and the lazy hack is '
                    'reintroducing the old cycle risk if it targets '
                    'lib.tasks_pkg.autopilot).'
                )


# ══════════════════════════════════════════════════════════
#  Guard 3 — the strongest cycle-elimination guard
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_autopilot_markers_has_no_import_from_autopilot():
    """autopilot_markers.py must have ZERO ``from lib.tasks_pkg.autopilot
    import ...`` nodes at ANY nesting depth — the cycle is broken graph-
    wide, not merely at module scope.

    This is the strongest structural guard against reintroducing the
    two-module cycle: as long as autopilot_markers has no code path (top
    level, function body, class body, try/except, whatever) that names
    ``lib.tasks_pkg.autopilot`` as an import target, the pair cannot form
    a cycle regardless of how the file is restructured in the future.
    """
    with open(_MARKERS_PATH, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)
    offenders = _collect_imports_from(tree, 'lib.tasks_pkg.autopilot',
                                      scope='any')
    if offenders:
        details = ', '.join(
            f'line {n.lineno} → {[a.name for a in n.names]}'
            for n in offenders)
        pytest.fail(
            'lib/tasks_pkg/autopilot_markers.py MUST NOT import from '
            'lib.tasks_pkg.autopilot at ANY nesting depth (post-slice-3 '
            'cycle-elimination). Every symbol it needs from the run '
            'lifecycle now lives in lib.tasks_pkg.autopilot_run_lifecycle '
            '(the leaf module).\n'
            f'Offender(s) found: {details}'
        )


# ══════════════════════════════════════════════════════════
#  Guard 4 — complementary check on autopilot.py's facade
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_autopilot_py_reexports_markers_and_run_lifecycle_at_module_top():
    """autopilot.py's facade re-exports from BOTH ``autopilot_markers`` and
    ``autopilot_run_lifecycle`` are load-bearing at module top.

    The facade contract downstream callers rely on:
      - ``from lib.tasks_pkg.autopilot import arm_autopilot / disarm_autopilot
        / _marker_exists`` (routes/chat_queue.py, lib/chat_dispatch.py,
        tests/test_autopilot_arm.py)
      - ``from lib.tasks_pkg.autopilot import conclude_run / _store_run_record
        / _emit_run_concluded / _emit_run_concluded_event``
        (tests/test_settings_cache_invalidation.py + monkeypatch-based
        sibling tests)

    Both re-exports must remain at module scope. A well-intentioned
    "move the re-export inside a helper for lazy loading" edit would
    break every external caller in one commit. This guard is the
    complement side of guards 1-3: autopilot_markers has zero top-level
    dep on autopilot (guards 1+3), and autopilot_run_lifecycle is a pure
    leaf, so it is safe for autopilot to have a top-level dep on both.
    """
    with open(_AUTOPILOT_PATH, encoding='utf-8') as f:
        src = f.read()
    tree = ast.parse(src)

    # (a) marker cluster re-export
    marker_reexports = _collect_imports_from(
        tree, 'lib.tasks_pkg.autopilot_markers', scope='module')
    assert marker_reexports, (
        'lib/tasks_pkg/autopilot.py MUST re-export from autopilot_markers '
        'at MODULE TOP — otherwise the facade contract '
        '(from lib.tasks_pkg.autopilot import arm_autopilot / '
        'disarm_autopilot / _marker_exists) breaks for every external '
        'caller.'
    )
    marker_names: set[str] = set()
    for n in marker_reexports:
        for alias in n.names:
            marker_names.add(alias.asname or alias.name)
    for required in ('arm_autopilot', 'disarm_autopilot', '_marker_exists'):
        assert required in marker_names, (
            f'facade re-export from autopilot_markers must include '
            f'{required}; found: {sorted(marker_names)}'
        )

    # (b) run-lifecycle cluster re-export (post-slice-3)
    leaf_reexports = _collect_imports_from(
        tree, 'lib.tasks_pkg.autopilot_run_lifecycle', scope='module')
    assert leaf_reexports, (
        'lib/tasks_pkg/autopilot.py MUST re-export from '
        'autopilot_run_lifecycle at MODULE TOP (post-slice-3) — '
        'otherwise the facade contract '
        '(from lib.tasks_pkg.autopilot import conclude_run / '
        '_store_run_record / _emit_run_concluded / '
        '_emit_run_concluded_event) breaks for every external caller '
        'including tests/test_settings_cache_invalidation.py.'
    )
    leaf_names: set[str] = set()
    for n in leaf_reexports:
        for alias in n.names:
            leaf_names.add(alias.asname or alias.name)
    for required in ('_store_run_record', '_emit_run_concluded',
                     'conclude_run', '_emit_run_concluded_event'):
        assert required in leaf_names, (
            f'facade re-export from autopilot_run_lifecycle must include '
            f'{required}; found: {sorted(leaf_names)}'
        )


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
