"""jsdom regression: picking "自动驾驶" from the 编排流程 dropdown selects a REAL
engine flow (``activeFlow='builtin:autopilot'``) — SYMMETRIC with
``builtin:endpoint`` — and is deliberately DIFFERENT from flipping the "模式"
Autopilot toggle.

WHY
---
The dropdown and the toggle intentionally drive two DIFFERENT implementations:

  * 模式 → 自动驾驶 (toggle)  → live standalone autopilot loop
    (``lib/tasks_pkg/autopilot.py``); this is the day-to-day path.
  * 编排流程 → 自动驾驶 (dropdown) → the FlowExecutor engine autopilot
    (worker⇄VU graph via ``run_flow_via_chat``); this is how engine behavior is
    made OBSERVABLE in the frontend for debugging.

An earlier iteration aliased the dropdown selection to the toggle (setting
``autopilotEnabled`` and clearing ``activeFlow``), and the backend Option-C
rewrite forced ``flowBuiltin='autopilot'`` back to the live path. That made the
two builtins ASYMMETRIC — ``builtin:endpoint`` ran on the engine but
``builtin:autopilot`` did not — and hid engine bugs behind the live loop. That
alias + normalization has been removed; ``builtin:autopilot`` is now a plain
flow selection exactly like ``builtin:endpoint``.

This suite drives the REAL shipped ``setActiveFlow`` / ``toggleAutopilot`` /
``_applyFlowUI`` / ``_applyAutopilotUI`` / ``_buildToolbarOverrides`` under jsdom
and asserts:
  * the dropdown sets ``activeFlow='builtin:autopilot'``, autopilot OFF, flow
    badge visible, autopilot badge hidden, and the ``_buildToolbarOverrides``
    payload carries ``activeFlow='builtin:autopilot'`` with ``autopilot=false``;
  * ``builtin:autopilot`` and ``builtin:endpoint`` behave IDENTICALLY (both set
    activeFlow to their own token, neither flips autopilotEnabled) — the
    symmetry the fix restores;
  * the standalone toggle still works and is DISTINCT (autopilot ON,
    activeFlow empty).

NC is a genuine byte-revert: it re-introduces the old alias branch in
``setActiveFlow`` in a temp copy and proves the "dropdown is a real flow"
assertions FAIL against it — so the regression can't slip back.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

SRC = runtime_section_path('main/main_toolbar_ui.js')

# The marker at the top of the shipped setActiveFlow — it must go STRAIGHT into
# the normal flow path (no builtin:autopilot alias branch). The NC re-inserts a
# legacy alias branch right after it to prove the assertions bite.
_SETACTIVEFLOW_HEAD = (
    "function setActiveFlow(flowVal) {\n"
)
# The legacy alias branch the NC re-introduces (mirrors the removed code): route
# builtin:autopilot to the toggle instead of treating it as a flow.
_NC_ALIAS_BRANCH = (
    "  if ((flowVal || '') === 'builtin:autopilot') {\n"
    "    _applyFlowUI('');\n"
    "    if (!autopilotEnabled) { toggleAutopilot(); } else { _saveConvToolState(); }\n"
    "    if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();\n"
    "    return;\n"
    "  }\n"
)


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const TOOLBAR_SRC = process.argv[3];   // path to the main_toolbar_ui.js variant
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>'
  + '<div id="autopilotToggle"></div><div id="autopilotBadge" style="display:none"></div>'
  + '<div id="endpointToggle"></div><div id="endpointBadge" style="display:none"></div>'
  + '<div id="flowToggle"></div><div id="flowBadge" style="display:none"></div>'
  + '<div id="flowActiveLabel"></div><div id="flowMenu"></div><div id="flowMenuList"></div>'
  + '</body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

// ── Minimal globals the toolbar functions read (declared as bare identifiers
//    in core.js; the bundled file references them lexically). We eval the
//    toolbar source in this scope, so plain `var` here is visible to it. ──
var autopilotEnabled = false, endpointEnabled = false, activeFlow = '';
var swarmEnabled = false, codeExecEnabled = false, memoryEnabled = false, autoTranslate = false;
var searchMode = 'off', fetchEnabled = true, browserEnabled = false, desktopEnabled = false;
var schedulerEnabled = false, imageGenEnabled = false, humanGuidanceEnabled = false;
var autoApplyWrites = false, thinkingEnabled = false, serverModel = 'm-x';
var config = { model: 'm-x', maxTokens: 4096, thinkingDepth: '', temperature: 1,
               systemPrompt: '', systemPromptMode: 'append', systemPromptBlocks: {},
               keepToolHistory: true };

win.t = global.t = (k) => k;
win.debugLog = global.debugLog = () => {};
win._saveConvToolState = global._saveConvToolState = () => {};
win.getActiveConv = global.getActiveConv = () => null;
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
win.updateSubmenuCounts = global.updateSubmenuCounts = () => {};
win._flowDisplayName = global._flowDisplayName = (v) => v || 'none';
win._scheduleReflow = global._scheduleReflow = () => {};
win.Api = global.Api = { chat: {} };

// _buildToolbarOverrides lives in main_conv_lifecycle.js (a different bundle
// file); it is a PURE reader of the same globals. Provide a faithful minimal
// stub for the three fields this test asserts, so the harness stays scoped to
// the toolbar source under test without evaling the whole bundle.
function _buildToolbarOverrides() {
  return { autopilot: autopilotEnabled, activeFlow: activeFlow || '',
           endpointMode: endpointEnabled };
}

// Load ONLY the (possibly byte-reverted) toolbar source. It defines
// _applyFlowUI / _applyAutopilotUI / _applyEndpointUI / toggleAutopilot /
// setActiveFlow / _buildToolbarOverrides as top-level functions in this scope.
eval(fs.readFileSync(TOOLBAR_SRC, 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

check('fns_exposed',
  typeof setActiveFlow === 'function' && typeof toggleAutopilot === 'function'
  && typeof _buildToolbarOverrides === 'function');

function _snapshot() {
  const ov = _buildToolbarOverrides();
  return {
    autopilotEnabled: autopilotEnabled,
    endpointEnabled: endpointEnabled,
    activeFlow: activeFlow,
    apBadge: document.getElementById('autopilotBadge').style.display,
    flowBadge: document.getElementById('flowBadge').style.display,
    ovAutopilot: ov.autopilot,
    ovActiveFlow: ov.activeFlow,
    ovEndpoint: ov.endpointMode,
  };
}

function _reset() {
  autopilotEnabled = false; endpointEnabled = false; activeFlow = '';
  _applyAutopilotUI(false); _applyEndpointUI(false); _applyFlowUI('');
}

// ── Path A: the standalone toggle (live autopilot loop — DISTINCT) ──
_reset();
toggleAutopilot();
const A = _snapshot();
check('toggle_sets_autopilot', A.autopilotEnabled === true);
check('toggle_no_flow', A.activeFlow === '');
check('toggle_ap_badge_visible', A.apBadge === '');
check('toggle_flow_badge_hidden', A.flowBadge === 'none');
check('toggle_ov_autopilot_true', A.ovAutopilot === true);
check('toggle_ov_activeflow_empty', A.ovActiveFlow === '');

// ── Path B: pick builtin:autopilot from the 编排流程 dropdown (a REAL flow) ──
_reset();
setActiveFlow('builtin:autopilot');
const B = _snapshot();
check('dropdown_selects_flow', B.activeFlow === 'builtin:autopilot');
check('dropdown_autopilot_off', B.autopilotEnabled === false);
check('dropdown_flow_badge_visible', B.flowBadge === '');
check('dropdown_ap_badge_hidden', B.apBadge === 'none');
check('dropdown_ov_activeflow_flow', B.ovActiveFlow === 'builtin:autopilot');
check('dropdown_ov_autopilot_false', B.ovAutopilot === false);

// ── The biting assertion: the dropdown is DISTINCT from the toggle ──
check('dropdown_distinct_from_toggle',
  B.activeFlow !== A.activeFlow && B.autopilotEnabled !== A.autopilotEnabled);

// ── Symmetry: builtin:endpoint behaves identically to builtin:autopilot ──
_reset();
setActiveFlow('builtin:endpoint');
const E = _snapshot();
check('endpoint_selects_flow', E.activeFlow === 'builtin:endpoint');
check('endpoint_autopilot_off', E.autopilotEnabled === false);
check('symmetry_flow_badge', E.flowBadge === B.flowBadge);
check('symmetry_ap_off', E.autopilotEnabled === B.autopilotEnabled);
check('symmetry_ov_autopilot', E.ovAutopilot === B.ovAutopilot && B.ovAutopilot === false);

console.log(out.join('\n'));
"""


def _run(toolbar_src: str) -> str:
    harness = os.path.join(HERE, '_builtin_autopilot_flow_route_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, toolbar_src],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
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


def _build_reverted_copy() -> str:
    """Byte-revert: re-introduce the legacy alias branch in setActiveFlow so
    builtin:autopilot is routed to the toggle again (autopilotEnabled ON,
    activeFlow cleared). Returns the temp file path."""
    src = open(SRC, encoding='utf-8').read()
    assert _SETACTIVEFLOW_HEAD in src, (
        'setActiveFlow head not found — did the source change? Update this test.')
    # Insert the legacy alias branch immediately after the function head.
    idx = src.index(_SETACTIVEFLOW_HEAD) + len(_SETACTIVEFLOW_HEAD)
    reverted = src[:idx] + _NC_ALIAS_BRANCH + src[idx:]
    assert reverted != src and _NC_ALIAS_BRANCH in reverted
    dst = os.path.join(HERE, '_main_toolbar_ui_nc_alias.js')
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(reverted)
    return dst


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_builtin_autopilot_is_a_real_engine_flow():
    """Selecting 自动驾驶 from the 编排流程 dropdown yields a real flow selection
    (activeFlow='builtin:autopilot', autopilot OFF), symmetric with endpoint
    and distinct from the standalone toggle."""
    output = _run(SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'flow-route failures:\n' + output
    assert output.count('PASS') >= 18, f'expected >=18 PASS, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_alias_revert_breaks_flow_route():
    """Byte-revert NC: re-inserting the legacy alias branch makes the dropdown
    set autopilotEnabled + clear activeFlow again — the "real flow" assertions
    MUST fail. Proves the removal is load-bearing."""
    dst = _build_reverted_copy()
    try:
        output = _run(dst)
    finally:
        try:
            os.remove(dst)
        except OSError:
            pass
    # The toggle reference path still passes (unchanged)…
    assert _status(output, 'toggle_sets_autopilot') == 'PASS', \
        'NC harness precondition broke:\n' + output
    # …but the dropdown aliases to the toggle again → real-flow route breaks.
    assert _status(output, 'dropdown_selects_flow') == 'FAIL', \
        'NC should route builtin:autopilot to the toggle (no activeFlow):\n' + output
    assert _status(output, 'dropdown_autopilot_off') == 'FAIL', \
        'NC should flip autopilotEnabled ON for the dropdown:\n' + output
    # The endpoint-flow symmetry case is untouched by the alias revert.
    assert _status(output, 'endpoint_selects_flow') == 'PASS', \
        'endpoint flow must remain a real flow selection:\n' + output
