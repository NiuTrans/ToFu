"""Wiring guard: the Project-Brain surfaces refresh at the project-state FUNNEL.

Reported bug family (2026-07-28): the collab bar (#presenceStrip) lags the
displayed project by up to one presence tick (~15s) — a stale, clickable lie.

  • newChat: cleared the conv/projectState but never re-resolved the bar
    (fixed first, by sprinkling presenceRefresh/projectBrainRefresh at the
    newChat call site).
  • Owner review: the SAME window exists on clearProject() (clear via the
    project panel, or dial Studio→Chat) and mpApplyFolders (attach) — the bar
    keeps the old project's counts ≤15s after a clear, and appears ≤15s late
    after an attach. Sprinkling the refresh at individual callers is the wrong
    layer: the defect class is "project-state mutation points never notify".

Root seam: EVERY projectState mutation repaints the project bar through
``_updateProjectUI()`` (static/js/project.js — attach / clear / rollback /
restore / remote-state / RO-toggle, seven call sites). So the Brain-surface
refresh lives THERE, once:

  A. (behavior) driving the REAL sliced ``_updateProjectUI`` on the clear
     path AND the attach path calls presenceRefresh + projectBrainRefresh
     exactly once per repaint;
  B. (static) ``_clearProjectStateLocal`` (the newChat !hasInput path) still
     funnels through ``_updateProjectUI`` — so newChat needs NO sprinkled
     call of its own, and indeed carries none;
  NC: strip the two calls from the funnel body → the behavior harness goes
     red on BOTH paths.

Slicing idiom follows tests/test_frontend_studio_requires_project.py: the
function under test is brace-matched out of the SHIPPED project.js at run
time, never hand-copied, so the guard can never drift from production.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
# Epic-E sub-7 (2026-08-01) split project.js: the STATE subset
# (_updateProjectUI / _clearProjectStateLocal / …) lives in
# project_state.js (core); the PANEL stayed in project.js (deferred).
# Extract state-first with a panel fallback — loud when in neither.
PROJECT_STATE_JS = os.path.join(ROOT, 'static', 'js', 'project_state.js')
PROJECT_JS = os.path.join(ROOT, 'static', 'js', 'project.js')
LIFECYCLE_JS = os.path.join(ROOT, 'static', 'js', 'main', 'main_conv_lifecycle.js')

REFRESH_CALLS = ("if (typeof presenceRefresh === 'function') presenceRefresh();\n"
                 "  if (typeof projectBrainRefresh === 'function') projectBrainRefresh();")


def _node() -> str:
    exe = shutil.which('node')
    if not exe:
        pytest.skip('node not available')
    return exe


def _slice_fn(src: str, signature: str) -> str:
    start = src.index(signature)
    i = src.index('{', start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == '{':
            depth += 1
        elif src[j] == '}':
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f'could not slice {signature}')


def _project_fn(name: str) -> str:
    header = f'function {name}() {{'
    for path in (PROJECT_STATE_JS, PROJECT_JS):
        with open(path, encoding='utf-8') as f:
            src = f.read()
        if header in src:
            return _slice_fn(src, header)
    raise AssertionError(
        f'{name} not found in project_state.js or project.js — the sub-7 '
        'split moved it somewhere new; update this harness')


def _newchat_fn() -> str:
    with open(LIFECYCLE_JS, encoding='utf-8') as f:
        return _slice_fn(f.read(), 'function newChat() {')


# ── Harness A (behavior): drive the REAL sliced _updateProjectUI ─────────
_HARNESS = r"""
const calls = [];
const elStub = () => ({
  style: {},
  classList: { add() {}, remove() {} },
  set innerHTML(v) {}, get innerHTML() { return ''; },
});
const els = {};
global.document = { getElementById: (id) => els[id] || (els[id] = elStub()) };
global.escapeHtml = (s) => String(s == null ? '' : s);
global._isRemotePath = (p) => String(p || '').indexOf('remote:') === 0;
global.presenceRefresh = () => calls.push('presence');
global.projectBrainRefresh = () => calls.push('brain');
global.projectState = { active: false, path: '', fileCount: 0, dirCount: 0,
                        totalSize: 0, languages: {}, scanning: false,
                        scanProgress: '', scanDetail: '', scannedAt: 0,
                        extraRoots: [], readOnly: false, crossDC: null };

__FN__

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── CLEAR path (project just detached / newChat !hasInput / dial→Chat) ──
_updateProjectUI();
check('clear_path_calls_presence', calls.filter(c => c === 'presence').length === 1);
check('clear_path_calls_brain', calls.filter(c => c === 'brain').length === 1);

// ── ATTACH path (mpApplyFolders / restore landed a project) ──
calls.length = 0;
projectState = { active: true, path: '/proj/A', fileCount: 0, dirCount: 0,
                 totalSize: 0, languages: {}, scanning: false, scanProgress: '',
                 scanDetail: '', scannedAt: 0, extraRoots: [], readOnly: false,
                 crossDC: null };
_updateProjectUI();
check('attach_path_calls_presence', calls.filter(c => c === 'presence').length === 1);
check('attach_path_calls_brain', calls.filter(c => c === 'brain').length === 1);

console.log(out.join('\n'));
"""


def _run_funnel(fn_src: str) -> str:
    script = _HARNESS.replace('__FN__', fn_src)
    proc = subprocess.run([_node(), '-e', script], cwd=ROOT,
                          capture_output=True, text=True)
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return proc.stdout.strip()


# ═══════════════════════════════════════════════════════════════════════
# A. Behavior — the funnel notifies both Brain surfaces on both paths
# ═══════════════════════════════════════════════════════════════════════

def test_funnel_notifies_brain_surfaces_on_clear_and_attach():
    output = _run_funnel(_project_fn('_updateProjectUI'))
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'funnel refresh failures:\n' + output
    assert output.count('PASS') == 4, f'expected 4 PASS, got:\n{output}'


def test_NC_funnel_seam_is_load_bearing():
    """NEUTER: strip the two refresh calls from the REAL sliced funnel → both
    recorders stay silent on both paths → all four assertions go red. Proves
    the seam (not some incidental caller) is what notifies the surfaces."""
    fn = _project_fn('_updateProjectUI')
    assert REFRESH_CALLS in fn, 'harness stale: refresh calls not found in funnel'
    neutered = fn.replace(REFRESH_CALLS, '', 1)
    assert neutered != fn
    output = _run_funnel(neutered)
    for must in ('FAIL clear_path_calls_presence', 'FAIL clear_path_calls_brain',
                 'FAIL attach_path_calls_presence', 'FAIL attach_path_calls_brain'):
        assert must in output, f'NEUTER did not bite on {must}:\n{output}'


# ═══════════════════════════════════════════════════════════════════════
# B. Static chain — newChat reaches the funnel, and no caller re-sprinkles
# ═══════════════════════════════════════════════════════════════════════

def test_clear_state_local_funnels_through_update_ui():
    """The newChat !hasInput path reaches the seam: _clearProjectStateLocal
    must repaint via _updateProjectUI() (else newChat loses coverage)."""
    body = _project_fn('_clearProjectStateLocal')
    assert '_updateProjectUI()' in body, (
        '_clearProjectStateLocal lost its _updateProjectUI() repaint — the '
        'newChat !hasInput path would no longer reach the refresh funnel')


def test_newchat_carries_no_sprinkled_refresh():
    """Anti-regression: the seam lives in the FUNNEL, not at callers. newChat
    must NOT carry its own presenceRefresh/projectBrainRefresh calls (the
    first-fix shape the owner sent back) — it reaches the funnel via
    _clearProjectStateLocal."""
    body = _newchat_fn()
    assert '_clearProjectStateLocal()' in body, (
        'newChat lost the _clearProjectStateLocal() path to the funnel')
    assert 'presenceRefresh()' not in body, (
        'sprinkled caller is back: newChat must reach the seam via the '
        '_updateProjectUI funnel, not a private presenceRefresh call')
    assert 'projectBrainRefresh()' not in body, (
        'sprinkled caller is back: newChat must reach the seam via the '
        '_updateProjectUI funnel, not a private projectBrainRefresh call')
