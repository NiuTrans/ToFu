"""tests/test_frontend_folder_drop_gate.py — Folder-browser drop gate mirrors
the backend save_uploaded_file guard (static/js/project.js).

Symptom (log-confirmed 2026-07-07 20:30:01): dropping a .zip onto a directory
the folder-browser had navigated to that is OUTSIDE every attached workspace
root produced a generic "1 file(s) failed to save" toast — the backend
``save_uploaded_file`` correctly refused it ("Destination is not inside any
attached workspace folder"), but the UI highlighted the drop as accepted and
gave no actionable reason.

Fix (revised after owner feedback — an upload is a harmless copy, so don't
dead-end it): ``_dirInsideAttachedRoot(dir)`` (over ``_attachedRootPaths()``
reading the live Project state port) still detects the out-of-workspace case, but instead
of refusing, ``_runFolderDrop`` OFFERS to add the folder in one click
(``showConfirm`` → ``_addDropDirAsRoot`` reusing the tested ``setPaths`` path),
then saves. Adding a root has a visible side effect (a scan + a new project-bar
folder), so we ask first rather than expand the workspace silently.

We brace-EXTRACT the two REAL shipped pure functions from project.js and eval
them under node against a stubbed live Project state port — an empty dir (→ active
root, backend-resolved) is allowed; a path inside a root (incl. a nested
subdir) is allowed; a sibling/outside path is blocked. A NEUTER that forces the
predicate to always-true proves the gate is load-bearing.
"""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_SRC = runtime_section_path('project.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_fn(src: str, name: str) -> str:
    """Brace-match-extract a top-level `function name(...) { ... }` block."""
    start = src.index('function ' + name + '(')
    depth = 0
    i = src.index('{', start)
    body_start = i
    while i < len(src):
        c = src[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError('unbalanced braces extracting ' + name)


def _build_harness(neuter: bool) -> str:
    with open(_SRC, encoding='utf-8') as f:
        src = f.read()
    fn_roots = _extract_fn(src, '_attachedRootPaths')
    fn_inside = _extract_fn(src, '_dirInsideAttachedRoot')
    assert 'ProjectPresentationShellState.projectState' in fn_roots, (
        'gate must read the live Project state port')

    if neuter:
        # Force the predicate to always accept — reproduces the pre-fix bug
        # where any drop target was treated as valid.
        fn_inside = fn_inside.replace(
            'function _dirInsideAttachedRoot(dir) {',
            'function _dirInsideAttachedRoot(dir) { return true;', 1)

    return (
        "var projectState = {\n"
        "  path: '/home/u/proj',\n"
        "  extraRoots: [{ path: '/home/u/extra', readOnly: false }, '/home/u/strroot'],\n"
        "};\n"
        "var ProjectPresentationShellState = { get projectState() { return projectState; } };\n"
        + fn_roots + "\n" + fn_inside + "\n"
        "var out = [];\n"
        "function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }\n"
        # empty dir → active project root (backend resolves it) → allowed
        "check('empty_allowed', _dirInsideAttachedRoot('') === true);\n"
        # primary root itself + nested subdir → allowed
        "check('primary_allowed', _dirInsideAttachedRoot('/home/u/proj') === true);\n"
        "check('primary_nested_allowed', _dirInsideAttachedRoot('/home/u/proj/sub/deep') === true);\n"
        "check('primary_trailing_slash_allowed', _dirInsideAttachedRoot('/home/u/proj/') === true);\n"
        # extra roots (object form + string form) → allowed
        "check('extra_obj_allowed', _dirInsideAttachedRoot('/home/u/extra/x') === true);\n"
        "check('extra_str_allowed', _dirInsideAttachedRoot('/home/u/strroot/y') === true);\n"
        # sibling / outside → blocked (the reported bug)
        "check('sibling_blocked', _dirInsideAttachedRoot('/home/u/arxiv') === false);\n"
        "check('outside_blocked', _dirInsideAttachedRoot('/tmp/whatever') === false);\n"
        # a path that merely shares a name PREFIX with a root is NOT inside it
        "check('prefix_not_inside', _dirInsideAttachedRoot('/home/u/proj-evil') === false);\n"
        "console.log(out.join('\\n'));\n"
    )


def _run(harness: str) -> str:
    path = os.path.join(HERE, '_folder_drop_gate_harness.js')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(harness)
    try:
        proc = subprocess.run(['node', path], capture_output=True, text=True, timeout=30)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


_EXPECTED = (
    'empty_allowed', 'primary_allowed', 'primary_nested_allowed',
    'primary_trailing_slash_allowed', 'extra_obj_allowed', 'extra_str_allowed',
    'sibling_blocked', 'outside_blocked', 'prefix_not_inside',
)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_folder_drop_gate():
    output = _run(_build_harness(neuter=False))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'folder-drop-gate failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_gate_is_load_bearing():
    """Neuter the predicate to always-true → the blocked cases FAIL (the
    pre-fix bug returns) while the allowed cases still PASS."""
    output = _run(_build_harness(neuter=True))
    for m in ('sibling_blocked', 'outside_blocked', 'prefix_not_inside'):
        assert ('FAIL ' + m) in output, f'NC: expected {m} to FAIL:\n{output}'
    for m in ('primary_allowed', 'extra_obj_allowed', 'empty_allowed'):
        assert ('PASS ' + m) in output, f'NC must be surgical — {m} should still PASS:\n{output}'


def test_run_folder_drop_confirms_then_adds():
    """Source-contract: for an out-of-workspace drop _runFolderDrop asks
    (showConfirm) and, on OK, adds the dir as a root (_addDropDirAsRoot) BEFORE
    the upload loop — a non-destructive copy should succeed via one click, not
    dead-end."""
    with open(_SRC, encoding='utf-8') as f:
        src = f.read()
    body = _extract_fn(src, '_runFolderDrop')
    assert '_dirInsideAttachedRoot(dir)' in body, \
        '_runFolderDrop must still detect the out-of-workspace case'
    assert 'showConfirm' in body, 'must confirm (not dead-end) before adding a root'
    assert '_addDropDirAsRoot(dir)' in body, 'on confirm it must add the dir as a root'
    # confirm + add must precede the upload loop (Promise.allSettled(...upload)).
    assert body.index('_dirInsideAttachedRoot(dir)') < body.index('Promise.allSettled')
    assert body.index('_addDropDirAsRoot(dir)') < body.index('Promise.allSettled')
    # The add-root helper reuses the tested setPaths apply path (no backend change).
    add_body = _extract_fn(src, '_addDropDirAsRoot')
    assert 'Api.project.setPaths' in add_body, '_addDropDirAsRoot must reuse setPaths'


def test_i18n_keys_present():
    """The confirm keys exist in zh + en so the dialog never renders a raw key."""
    locale_dir = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales')
    with open(os.path.join(locale_dir, 'zh.json'), encoding='utf-8') as f:
        zh = json.load(f)
    with open(os.path.join(locale_dir, 'en.json'), encoding='utf-8') as f:
        en = json.load(f)
    for key in ('folderDrop.notInWorkspace', 'folderDrop.addRootConfirm',
                'folderDrop.addAndSave'):
        assert key in zh, f'missing zh i18n key {key}'
        assert key in en, f'missing en i18n key {key}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
