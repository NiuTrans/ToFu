#!/usr/bin/env python3
# Incident anchor: born in commit e274bc41 — refactor(autopilot): pt_00459503 slice 4 — extract baton-handoff clus...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""Wire-parity for pt_00459503 slice 4 — extract baton-handoff cluster.

Post-cutover extraction (pt_8dc03017 step-3 complete at 6286913d):
move the 6-function baton-handoff cluster out of autopilot.py into a new
leaf module autopilot_baton.py:

  * _presync_parent_reply(task)                    — parent's final assistant DB pre-sync
  * _has_pending_real_message(conv_id)             — queue-depth defer gate
  * _successor_already_running(task, conv_id)      — dedup guard for baton spawn
  * _append_vu_message_to_conv(conv_id, vu_msg_id, ...) — VU turn append + notify
  * _maybe_auto_translate_vu(conv_id, vu_msg_id, content) — VU auto-translate hook
  * _start_followup_task(task, conv_id)            — follow-up task spawner

All 6 are used ONLY by ``maybe_run_autopilot`` (which stays in autopilot.py)
plus one external consumer: lib/tasks_pkg/endpoint/_translate.py imports
_maybe_auto_translate_vu at CALL time (``from lib.tasks_pkg.autopilot
import _maybe_auto_translate_vu``). Facade re-export from autopilot.py
preserves both patterns byte-identically.

Failing-first — 4 assertions (RED before extraction, GREEN after):

  1. lib.tasks_pkg.autopilot_baton exists AND exports all 6 helpers as callables.
  2. autopilot.py re-exports the 6 helpers (facade continuity for
     _translate.py's _maybe_auto_translate_vu import + monkeypatch surface).
  3. autopilot.py NO LONGER contains an inline `def <name>(` definition for
     any of the 6 helpers (source moved, not duplicated).
  4. pt_8dc03017 CUTOVER TOKEN ISOLATION — the extracted module MUST NOT
     re-carry any deleted-in-cutover code paths (_VUEventForwarder,
     _autopilot_deciding latch, convId='' opt-out). Guards against future
     re-introduction via revert.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_BATON_SYMBOLS = (
    '_presync_parent_reply',
    '_has_pending_real_message',
    '_successor_already_running',
    '_append_vu_message_to_conv',
    '_maybe_auto_translate_vu',
    '_start_followup_task',
)


def _read(rel_path: str) -> str:
    with open(os.path.join(_ROOT, rel_path), encoding='utf-8') as f:
        return f.read()


@_unit
def test_autopilot_baton_module_exists_and_exports_six():
    """Slice 4: lib.tasks_pkg.autopilot_baton exists and every baton
    symbol is a callable there."""
    import importlib
    mod = importlib.import_module('lib.tasks_pkg.autopilot_baton')
    for name in _BATON_SYMBOLS:
        assert hasattr(mod, name), (
            f'lib.tasks_pkg.autopilot_baton missing {name}'
        )
        assert callable(getattr(mod, name)), (
            f'lib.tasks_pkg.autopilot_baton.{name} is not callable'
        )


@_unit
def test_autopilot_facade_reexports_baton_symbols():
    """Slice 4: autopilot.py must re-export each baton symbol so
    `from lib.tasks_pkg.autopilot import _maybe_auto_translate_vu`
    (used at CALL time in lib/tasks_pkg/endpoint/_translate.py) keeps
    resolving — and so test monkey-patches at the facade layer keep
    working."""
    import importlib
    ap = importlib.import_module('lib.tasks_pkg.autopilot')
    for name in _BATON_SYMBOLS:
        assert hasattr(ap, name), (
            f'lib.tasks_pkg.autopilot facade must re-export {name} '
            f'from autopilot_baton'
        )


@_unit
def test_autopilot_facade_does_not_redefine_baton_symbols_inline():
    """Slice 4: autopilot.py MUST NOT still carry an inline `def <name>(`
    for any of the 6 baton symbols. A leftover inline definition would
    shadow the extracted leaf and the extraction is only partial.

    Comment mentions are fine — this scans for actual `def` keywords."""
    src = _read('lib/tasks_pkg/autopilot.py')
    for name in _BATON_SYMBOLS:
        # `def <name>(` at start of line (function def, not comment).
        m = re.search(rf'^def\s+{re.escape(name)}\s*\(', src, re.M)
        assert not m, (
            f'lib/tasks_pkg/autopilot.py must NOT still declare `def {name}(...)` '
            f'inline — extracted to autopilot_baton.py'
        )


@_unit
def test_autopilot_baton_pt_8dc03017_token_isolation():
    """Slice 4: the extracted baton module MUST NOT re-introduce any of
    the tokens that pt_8dc03017 step-3 deleted from the LIVE CODE PATH:

      * `_VUEventForwarder` — deleted in 6286913d (event forwarding gone)
      * `_autopilot_deciding` — deleted in 3e2ec0c3 (withhold latch gone)
      * `convId=''` opt-out — retired in 6286913d (VU under real convId)

    A future revert that pastes the deleted CODE (not historical
    docstring mentions) back into autopilot_baton.py would silently
    undo the fix. This test is the belt-and-braces guard.

    Docstring / comment mentions of these tokens are LEGITIMATE — the
    module's own header docstring explains that the extraction runs on
    the CLEANED post-cutover surface. Only CODE-line hits fail the
    guard."""
    import ast
    src = _read('lib/tasks_pkg/autopilot_baton.py')
    # Parse and strip the module docstring so it does not shadow live-code
    # hits. Then scan LINES: skip pure comments; strip inline comment tails;
    # skip string-literal-only lines (approximate — we don't need perfect
    # AST-level string extraction here, only "is this token used as an
    # identifier in executable code").
    tree = ast.parse(src)
    docstring_lines: set[int] = set()
    if (isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        # Module docstring — mark its line range as ignored.
        node = tree.body[0]
        for ln_no in range(node.lineno, (node.end_lineno or node.lineno) + 1):
            docstring_lines.add(ln_no)

    forbidden = ('_VUEventForwarder', '_autopilot_deciding')
    for lineno, line in enumerate(src.split('\n'), start=1):
        if lineno in docstring_lines:
            continue
        stripped = line.lstrip()
        if stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        code_only = line.split('#', 1)[0]
        for token in forbidden:
            assert token not in code_only, (
                f'autopilot_baton.py L{lineno}: must NOT re-introduce '
                f'pt_8dc03017 cutover-deleted token {token!r} in LIVE '
                f'CODE — extraction is a code MOVE, not a resurrection '
                f'of pre-cutover logic. Offending line: {line!r}'
            )
        assert "convId=''" not in code_only, (
            f"autopilot_baton.py L{lineno}: must NOT pass convId='' as "
            f"a kwarg — pt_8dc03017 retired the opt-out. Line: {line!r}"
        )


if __name__ == '__main__':
    for fn in [
        test_autopilot_baton_module_exists_and_exports_six,
        test_autopilot_facade_reexports_baton_symbols,
        test_autopilot_facade_does_not_redefine_baton_symbols_inline,
        test_autopilot_baton_pt_8dc03017_token_isolation,
    ]:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
