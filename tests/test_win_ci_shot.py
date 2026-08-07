#!/usr/bin/env python3
"""tests/test_win_ci_shot.py — the CI wizard pixel probe's contract.

debug/win_ci_shot.py is the measurement instrument of the blank-wizard
hunt (2026-08-07): it builds the TOFU_DIAG installer variant with a stub
payload on a REAL Windows runner and drives the wizard, replacing the
"hypothesis → owner's real machine → refuted" loop with a CI pixel loop.

What is pinned here (everything checkable WITHOUT Windows):
  * the module imports cleanly on non-Windows (ctypes wiring must be
    win32-gated so the repo's Linux gates can load it);
  * the canonical page set the artifacts are named after;
  * the CLI contract (--help, the platform refusal).

The pixel assertions themselves can only run on the runner — that is
the workflow's job (.github/workflows/installer-ui-probe.yml).

debug/ is opensource-excluded wholesale; this one file is restored into
the public build (export._OPENSOURCE_KEEP_FILES) because the shipped
workflow references it. On a public tree the module is absent — skip
loudly instead of dying at collection (the test_cache_waste_report.py
pattern).
"""

import importlib.util
import os
import subprocess
import sys
import tempfile

import pytest

pytestmark = pytest.mark.unit

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_MOD_PATH = os.path.join(_ROOT, 'debug', 'win_ci_shot.py')

if not os.path.isfile(_MOD_PATH):
    pytest.skip('debug/win_ci_shot.py not shipped in opensource',
                allow_module_level=True)


def _load():
    spec = importlib.util.spec_from_file_location('win_ci_shot', _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_module_imports_on_non_windows():
    """The ctypes wiring must be win32-gated: Linux gates load this module
    and a top-level `ctypes.windll` would die on import."""
    mod = _load()
    assert mod.PAGES == ('welcome', 'directory', 'progress', 'finish')


def test_page_names_match_the_templates_diag_marks():
    """PAGES doubles as the diag-log marker vocabulary used for page
    detection — a template-side rename of a "<page>: reached Show" mark
    must go red here, not silently disable log-driven page turns."""
    import re
    mod = _load()
    with open(os.path.join(_ROOT, 'desktop', 'installer.nsi.tmpl'),
              encoding='utf-8') as f:
        tmpl = f.read()
    marks = set(re.findall(r'TOFU_DIAG_MARK "([\w.]+): reached Show"',
                           tmpl))
    install_marks = {m for m in marks if not m.startswith('un.')}
    assert install_marks == set(mod.PAGES), (
        f'template marks {sorted(install_marks)} vs probe PAGES '
        f'{sorted(mod.PAGES)} — page detection keys drifted')


def test_help_exits_zero():
    mod = _load()
    with pytest.raises(SystemExit) as e:
        mod.main(['--help'])
    assert e.value.code == 0


def test_drive_refuses_non_windows_loudly():
    if sys.platform == 'win32':
        pytest.skip('the refusal only fires off-Windows')
    mod = _load()
    with pytest.raises(RuntimeError, match='Windows-only'):
        mod.drive('nonexistent.exe', tempfile.mkdtemp(), title='Tofu',
                  settle=0, timeout=0)


def test_probe_script_is_tracked_despite_debug_being_ignored():
    """/debug/ is gitignored wholesale; this file rides an explicit
    negation. Untracked = the workflow on the runner clones a repo
    WITHOUT the probe and dies on a missing file in CI, the worst place
    to discover it."""
    tracked = subprocess.run(
        ['git', 'ls-files', 'debug/win_ci_shot.py'], cwd=_ROOT,
        capture_output=True, text=True).stdout.strip()
    assert tracked == 'debug/win_ci_shot.py', (
        'debug/win_ci_shot.py must be tracked (the .gitignore negation '
        'next to /debug/ is load-bearing)')


def test_stub_payload_uses_a_real_harmless_exe():
    """The stub payload's exe is Exec'd by the finish page's
    launch-after-install lane — it must be a real, instantly-exiting
    binary (whoami), never a 0-byte file (CreateProcess failure on a
    lane we are measuring)."""
    src = open(_MOD_PATH, encoding='utf-8').read()
    assert 'whoami.exe' in src
