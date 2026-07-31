"""Regression guard for the "project mode auto-enables Swarm + Autopilot" change.

WHY
---
Swarm + Autopilot used to default ON globally (core.js) and were force-applied
ON for a project-LESS chat (_resetToolsToDefaults). The change flips both to
OFF by default and instead auto-enables them the moment the user turns Project
mode ON — via `_autoEnableProjectModes()`, called from `mpApplyFolders()` (the
single gate every "Set Project" gesture funnels through: the modal button,
`setProject(path)` quick-set, and the recent-project one-click all end there).

Reopening an existing project-backed conversation deliberately does NOT
auto-enable — `loadConversation → _restoreConvProject` re-activates the project,
then `_restoreConvToolState(conv)` restores the conv's PERSISTED per-conv
swarm/autopilot choice. Auto-enable is a fresh-attach gesture, not a restore
gesture, so keeping it solely in `mpApplyFolders` is correct, not incomplete.

This suite locks in three things:
  1. Source defaults: core.js declares both OFF; _resetToolsToDefaults applies
     both OFF; mpApplyFolders does NOT call _autoEnableProjectModes() —
     owner-directed 2026-07-19: the tier is DECOUPLED from execution strategy,
     attaching a project only promotes the capability dial (onProjectAttached).
  2. Behavior (jsdom, real shipped `_autoEnableProjectModes`): OFF → ON on
     apply; the endpoint/flow mutual-exclusion guardrail; already-ON is left
     untouched.
  3. A neuter proving the enable calls are load-bearing.

Skips cleanly when node isn't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
PROJECT_SRC = os.path.join(ROOT, 'static', 'js', 'project.js')
CORE_SRC = os.path.join(ROOT, 'static', 'js', 'core.js')
MAIN_SRC = os.path.join(ROOT, 'static', 'js', 'main.js')


# ── (1) Source-level default + wiring assertions (no node needed) ──────────

def test_core_defaults_swarm_and_autopilot_off():
    src = open(CORE_SRC, encoding='utf-8').read()
    assert re.search(r'\bswarmEnabled\s*=\s*false\b', src), \
        'core.js must declare swarmEnabled = false by default'
    assert re.search(r'\bautopilotEnabled\s*=\s*false\b', src), \
        'core.js must declare autopilotEnabled = false by default'


def test_reset_tools_applies_swarm_autopilot_off():
    src = open(MAIN_SRC, encoding='utf-8').read()
    # Isolate _resetToolsToDefaults so we don't match _restoreConvToolState.
    start = src.index('function _resetToolsToDefaults(')
    body = src[start:src.index('\n}', start)]
    assert '_applySwarmUI(false)' in body, \
        '_resetToolsToDefaults must apply swarm OFF for a project-less chat'
    assert '_applyAutopilotUI(false)' in body, \
        '_resetToolsToDefaults must apply autopilot OFF for a project-less chat'


def test_mpapplyfolders_retires_auto_enable():
    """Owner-directed 2026-07-19: attaching a project is DECOUPLED from
    execution strategy — mpApplyFolders must NOT auto-enable Swarm/Autopilot
    (_autoEnableProjectModes is retired from this path; the function survives
    for other callers). The apply path promotes the dial via onProjectAttached
    instead. Re-adding the auto-enable call would silently re-couple the two
    axes the owner split apart."""
    src = open(PROJECT_SRC, encoding='utf-8').read()
    start = src.index('async function mpApplyFolders(')
    body = src[start:src.index('\n}\n', start)]
    assert '_autoEnableProjectModes()' not in body, \
        'mpApplyFolders must NOT call _autoEnableProjectModes() — the tier is ' \
        'decoupled from execution strategy (owner-directed 2026-07-19); ' \
        'Swarm/Autopilot are explicit opt-ins'
    assert 'onProjectAttached' in body, \
        'mpApplyFolders must promote the dial via onProjectAttached'


def test_auto_enable_refreshes_submenu_counts():
    src = open(PROJECT_SRC, encoding='utf-8').read()
    fn = _extract_auto_enable(src)
    assert 'updateSubmenuCounts()' in fn, \
        '_autoEnableProjectModes must call updateSubmenuCounts() so the ' \
        'toolbar mode-count badge reflects the newly-enabled modes'


# ── (2)+(3) Behavioral jsdom harness over the real shipped function ────────

def _node_available() -> bool:
    return bool(shutil.which('node'))


def _extract_auto_enable(src_text: str) -> str:
    """Slice the `_autoEnableProjectModes` function body from project.js."""
    marker = 'function _autoEnableProjectModes()'
    start = src_text.index(marker)
    end = src_text.index('\n}\n', start) + 2
    return src_text[start:end]


_HARNESS = r"""
const fn = process.argv[2];   // path to the (possibly neutered) function source

// Mutable state the function reads/writes as bare identifiers.
var swarmEnabled = false, autopilotEnabled = false;
var endpointEnabled = false, activeFlow = '';
var saved = 0;

var counted = 0;
function _applySwarmUI(v) { swarmEnabled = !!v; }
function _applyAutopilotUI(v) { autopilotEnabled = !!v; }
function _saveConvToolState() { saved++; }
function updateSubmenuCounts() { counted++; }
function debugLog() {}

eval(require('fs').readFileSync(fn, 'utf8'));

const out = [];
const chk = (n, c) => out.push((c ? 'PASS ' : 'FAIL ') + n);

// Scenario A: fresh attach, both OFF → both flip ON, state persisted.
swarmEnabled = false; autopilotEnabled = false; endpointEnabled = false; activeFlow = ''; saved = 0; counted = 0;
_autoEnableProjectModes();
chk('A_swarm_on', swarmEnabled === true);
chk('A_autopilot_on', autopilotEnabled === true);
chk('A_persisted', saved === 1);
chk('A_counts_refreshed', counted === 1);

// Scenario B: Endpoint already active → swarm ON but autopilot NOT forced.
swarmEnabled = false; autopilotEnabled = false; endpointEnabled = true; activeFlow = ''; saved = 0;
_autoEnableProjectModes();
chk('B_swarm_on', swarmEnabled === true);
chk('B_autopilot_left_off', autopilotEnabled === false);

// Scenario C: a custom Flow active → autopilot NOT forced on top.
swarmEnabled = false; autopilotEnabled = false; endpointEnabled = false; activeFlow = 'builtin:endpoint'; saved = 0;
_autoEnableProjectModes();
chk('C_autopilot_left_off', autopilotEnabled === false);

// Scenario D: user already turned them ON manually → no-op, nothing persisted.
swarmEnabled = true; autopilotEnabled = true; endpointEnabled = false; activeFlow = ''; saved = 0; counted = 0;
_autoEnableProjectModes();
chk('D_swarm_still_on', swarmEnabled === true);
chk('D_autopilot_still_on', autopilotEnabled === true);
chk('D_no_persist', saved === 0);
chk('D_no_count_refresh', counted === 0);

console.log(out.join('\n'));
"""


def _run(fn_src: str) -> str:
    fn_path = os.path.join(HERE, '_auto_enable_fn.js')
    harness = os.path.join(HERE, '_auto_enable_harness.js')
    with open(fn_path, 'w', encoding='utf-8') as f:
        f.write(fn_src)
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, fn_path],
                              capture_output=True, text=True, timeout=60)
    finally:
        for p in (fn_path, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


def _status(output: str, name: str) -> str | None:
    for ln in output.splitlines():
        if ln.endswith(' ' + name):
            return ln.split(' ', 1)[0]
    return None


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_auto_enable_flips_modes_on_with_guardrails():
    fn_src = _extract_auto_enable(open(PROJECT_SRC, encoding='utf-8').read())
    output = _run(fn_src)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'auto-enable behavior failures:\n' + output
    assert output.count('PASS') == 11, f'expected 11 PASS, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_disabling_the_enable_calls_breaks_it():
    """Byte-neuter: flip the enabling calls to `(false)` in an in-memory copy.
    The OFF→ON scenario must then FAIL, proving the calls are load-bearing.
    The shipped file is never touched (extract → mutate string → eval copy)."""
    fn_src = _extract_auto_enable(open(PROJECT_SRC, encoding='utf-8').read())
    neutered = (fn_src
                .replace('_applySwarmUI(true);', '_applySwarmUI(false);')
                .replace('_applyAutopilotUI(true);', '_applyAutopilotUI(false);'))
    assert neutered != fn_src, 'neuter did not change the source'
    output = _run(neutered)
    assert _status(output, 'A_swarm_on') == 'FAIL', \
        'neuter should leave swarm OFF:\n' + output
    assert _status(output, 'A_autopilot_on') == 'FAIL', \
        'neuter should leave autopilot OFF:\n' + output
    # The manual-ON no-op path is unaffected by the neuter (D leaves them as-set).
    assert _status(output, 'D_swarm_still_on') == 'PASS', \
        'manual-ON path must be untouched by the neuter:\n' + output
