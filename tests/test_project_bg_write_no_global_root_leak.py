"""Regression guard: a BACKGROUND task's absolute-path write must NOT leak a
new workspace root into the process-GLOBAL registry (the project bar source).

WHY
---
``_resolve_write_path`` §2 auto-registers the nearest existing ancestor of an
absolute-path write as an extra workspace root so ``write_file('/abs/x.py')``
"just works". Historically it called ``add_project_root()`` — the GLOBAL
``_roots`` registry — even for a background ``run_task``. That registry is the
UI-facing "active project" every conversation's project bar reflects via
``get_state()``. So a task writing to a sibling repo (e.g. ``tofu-search``,
``tofu-android``) sprayed that repo onto EVERY conversation's bar, and violated
the committed 2026-06-22 invariant that "a background run_task MUST NOT mutate
the global _state/_roots" (log: ``[run_task-…] Auto-registered workspace root
…/tofu-android``).

Fix: when a ``conv_id`` is present the anchor is registered into that
conversation's OWN scoped registry (``add_conv_root``) only; the global
registry stays reserved for the interactive / no-``conv_id`` path.
``_mod_attribution`` / ``_should_record_modification`` were made
conv-scope-aware so the file-changes bar + undo journal still resolve.

This suite proves:
  1. A conv-scoped write registers in the conv registry, NOT in
     ``get_state().extraRoots``.
  2. Attribution still resolves the conv-scoped root (file-changes bar / undo
     journal keep working) — i.e. the fix is not a bare "drop it on the floor".
  3. NC (invariant): the interactive / no-conv_id path DOES still register
     globally — proving the gate is load-bearing, not a blanket disable.
"""

from __future__ import annotations

import os
import tempfile

import pytest

pytestmark = pytest.mark.unit


def _reset_state():
    from lib.project_mod import config as cfg
    with cfg._lock:
        cfg._roots.clear()
        cfg._conv_roots.clear()
        cfg._conv_primary.clear()
        cfg._state['path'] = None
        cfg._state['modifications'] = []


def _disable_temp_detection():
    """Make ``_is_temp_path`` return False for the tmpdir-based fixtures.

    The real leak (``tofu-search`` / ``tofu-android``) is a NON-temp sibling
    repo, but ``tempfile.mkdtemp`` lands under the OS temp root, where
    ``_resolve_write_path`` has a separate (correct, orthogonal) branch that
    returns early WITHOUT registering ANY root. Empty the ``_temp_roots``
    cache so the fixtures exercise the auto-register path a real sibling repo
    would hit. Caller must ``_reset_temp_detection()`` afterwards."""
    from lib.project_mod import write_tools as wt
    wt._temp_roots._cache = set()


def _reset_temp_detection():
    from lib.project_mod import write_tools as wt
    if hasattr(wt._temp_roots, '_cache'):
        delattr(wt._temp_roots, '_cache')


def test_bg_write_registers_conv_scoped_not_global():
    """A background-task absolute-path write outside all roots registers the
    anchor into the conv registry, and does NOT appear in the global
    ``get_state().extraRoots`` that the project bar renders."""
    from lib.project_mod import config as cfg
    from lib.project_mod.write_tools import _resolve_write_path

    _reset_state()
    conv_id = 'conv-bg-leak-1'
    primary = tempfile.mkdtemp(prefix='tofu_primary_')
    sibling = tempfile.mkdtemp(prefix='tofu_sibling_')  # the "tofu-search" analogue
    _disable_temp_detection()
    try:
        # Wire the conv the way task-start does (scoped registry only).
        cfg.set_conv_roots(conv_id, primary)

        target_abs = os.path.join(sibling, 'README.md')
        # allow_outside=True = the explicit user confirmation the 2026-08-31
        # gate requires; this suite exercises post-confirmation registration.
        resolved = _resolve_write_path(primary, target_abs, conv_id=conv_id,
                                       allow_outside=True)
        assert os.path.abspath(resolved) == os.path.abspath(target_abs)

        # ★ The core assertion: the sibling did NOT leak into the global bar.
        state = cfg.get_state()
        extra_paths = [r['path'] for r in state.get('extraRoots', [])]
        assert os.path.abspath(sibling) not in [os.path.abspath(p) for p in extra_paths], (
            'background write leaked the sibling repo into the GLOBAL project '
            f'bar (get_state().extraRoots={extra_paths})')
        # And the global _roots itself was not mutated by the task.
        global_root_paths = [os.path.abspath(rs['path']) for rs in cfg._roots.values()]
        assert os.path.abspath(sibling) not in global_root_paths, (
            'background write mutated the global _roots registry (2026-06-22 '
            'invariant violation)')

        # ★ But it IS in THIS conv's scoped registry (resolvable in-task).
        conv_root_paths = [os.path.abspath(rs['path'])
                           for rs in cfg.get_conv_roots(conv_id).values()]
        assert os.path.abspath(sibling) in conv_root_paths, (
            'conv-scoped registry did not gain the auto-registered root — a '
            'subsequent namespaced write in the same task would fail')
    finally:
        _reset_temp_detection()
        _reset_state()


def test_bg_write_attribution_resolves_conv_scoped_root():
    """The modifications journal must attribute a conv-scoped absolute-path
    write to the conv-scoped root (not the primary) — proving _mod_attribution
    reads the conv registry, so the file-changes bar / undo journal keep
    working after the global-leak fix."""
    from lib.project_mod import config as cfg
    from lib.project_mod.write_tools import _mod_attribution, _resolve_write_path

    _reset_state()
    conv_id = 'conv-bg-leak-2'
    primary = tempfile.mkdtemp(prefix='tofu_primary_')
    sibling = tempfile.mkdtemp(prefix='tofu_sibling_')
    _disable_temp_detection()
    try:
        cfg.set_conv_roots(conv_id, primary)
        # Do NOT pre-create 'sub/': the auto-register anchors on the DEEPEST
        # EXISTING ancestor, so leaving 'sub' absent makes the sibling root
        # itself the registered root (mirrors a first write into a repo root).
        target_abs = os.path.join(sibling, 'sub', 'file.py')
        # allow_outside=True = the explicit user confirmation the 2026-08-31
        # gate requires; this suite exercises post-confirmation registration.
        resolved = _resolve_write_path(primary, target_abs, conv_id=conv_id,
                                       allow_outside=True)

        mod_base, mod_rel = _mod_attribution(resolved, primary, target_abs, conv_id=conv_id)
        # Attribution must map to the SIBLING root (deepest containing conv
        # root), with a clean root-relative path — not the primary + abs path.
        assert os.path.abspath(mod_base) == os.path.abspath(sibling), (
            f'attribution resolved to {mod_base!r}, expected the conv-scoped '
            f'sibling root {sibling!r}')
        assert mod_rel == os.path.join('sub', 'file.py')
    finally:
        _reset_temp_detection()
        _reset_state()


def test_NC_interactive_write_still_registers_globally():
    """NC / load-bearing gate: with NO conv_id (interactive / human-driven
    path), the auto-register STILL populates the global registry — proving the
    conv-scoping is a targeted gate, not a blanket disable that would break the
    interactive absolute-path-write ergonomics."""
    from lib.project_mod import config as cfg
    from lib.project_mod.scanner import set_project
    from lib.project_mod.write_tools import _resolve_write_path

    _reset_state()
    primary = tempfile.mkdtemp(prefix='tofu_primary_')
    sibling = tempfile.mkdtemp(prefix='tofu_sibling_')
    _disable_temp_detection()
    try:
        set_project(primary)  # interactive "open folder"
        target_abs = os.path.join(sibling, 'x.py')
        _resolve_write_path(primary, target_abs, conv_id=None,
                            allow_outside=True)

        extra_paths = [os.path.abspath(r['path']) for r in cfg.get_state().get('extraRoots', [])]
        assert os.path.abspath(sibling) in extra_paths, (
            'interactive (no conv_id) absolute-path write should still expand '
            'the shared workspace globally — the gate over-fired')
    finally:
        _reset_temp_detection()
        _reset_state()
