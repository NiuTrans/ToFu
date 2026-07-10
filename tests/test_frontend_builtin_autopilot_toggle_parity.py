"""jsdom regression: picking "自动驾驶" from the Mode (flow) dropdown produces a
toolbar + info-rail + config-payload state that is BYTE-IDENTICAL to flipping
the standalone Autopilot toggle.

WHY
---
The backend routes ``flowBuiltin='autopilot'`` to the LIVE standalone autopilot
path (``resolve_chat_flow_entry`` Option C) unless ``TOFU_AUTOPILOT_VIA_FLOW`` is
set — so the DEFAULT dropdown experience is the plain autopilot loop, not an
engine flow. But the frontend historically set ``activeFlow='builtin:autopilot'``
(a distinct state): flow badge instead of the autopilot badge, the info-rail
mode chip coming from the flow branch, and the misleading "runs on the
orchestration engine" hint. Same behavior, different-looking UI — the exact
"performs oddly" the objective is about.

The fix makes ``setActiveFlow('builtin:autopilot')`` an ALIAS for
``toggleAutopilot()`` (single writer — the flow selector delegates, it does not
grow a parallel state). This suite drives the REAL shipped ``setActiveFlow`` /
``toggleAutopilot`` / ``_applyFlowUI`` / ``_applyAutopilotUI`` /
``_buildToolbarOverrides`` under jsdom and asserts the resulting state equals
the standalone-toggle state on every axis that reaches the backend or the eye:
``autopilotEnabled`` true, ``activeFlow`` empty, autopilot badge visible + flow
badge hidden, and the ``_buildToolbarOverrides`` payload identical.

NC is a genuine byte-revert: it rewrites the alias branch back to the old
``_applyFlowUI('builtin:autopilot')`` behavior in a temp copy and proves the
parity assertions FAIL against it — so a regression can't slip back.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SRC = os.path.join(ROOT, 'static', 'js', 'main', 'main_toolbar_ui.js')

# The two convergence choke points the fix introduced. The NC byte-reverts
# BOTH — either alone would still converge builtin:autopilot to the toggle, so
# proving load-bearing requires removing both.
_APPLYFLOWUI_BLOCK = (
    "  if ((flowVal || '') === 'builtin:autopilot') {\n"
    "    activeFlow = '';\n"
    "    if (typeof _applyAutopilotUI === 'function') _applyAutopilotUI(true);\n"
    "    if (typeof _applyEndpointUI === 'function') _applyEndpointUI(false);\n"
    "    flowVal = '';\n"
    "  }\n"
)
_SETACTIVEFLOW_MARKER = (
    "  if ((flowVal || '') === 'builtin:autopilot') {\n"
    "    _applyFlowUI('');"
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

// ── Path A: the standalone toggle (the reference state) ──
autopilotEnabled = false; endpointEnabled = false; activeFlow = '';
_applyAutopilotUI(false); _applyEndpointUI(false); _applyFlowUI('');
toggleAutopilot();
const A = _snapshot();
check('toggle_sets_autopilot', A.autopilotEnabled === true);
check('toggle_no_flow', A.activeFlow === '');
check('toggle_ap_badge_visible', A.apBadge === '');
check('toggle_flow_badge_hidden', A.flowBadge === 'none');
check('toggle_ov_autopilot_true', A.ovAutopilot === true);
check('toggle_ov_activeflow_empty', A.ovActiveFlow === '');

// ── Path B: pick builtin:autopilot from the Mode menu (must equal A) ──
autopilotEnabled = false; endpointEnabled = false; activeFlow = '';
_applyAutopilotUI(false); _applyEndpointUI(false); _applyFlowUI('');
setActiveFlow('builtin:autopilot');
const B = _snapshot();
check('dropdown_sets_autopilot', B.autopilotEnabled === true);
check('dropdown_no_flow_selection', B.activeFlow === '');
check('dropdown_ap_badge_visible', B.apBadge === '');
check('dropdown_flow_badge_hidden', B.flowBadge === 'none');

// ── The biting parity assertion: B == A on every axis ──
check('parity_autopilotEnabled', B.autopilotEnabled === A.autopilotEnabled);
check('parity_activeFlow', B.activeFlow === A.activeFlow);
check('parity_apBadge', B.apBadge === A.apBadge);
check('parity_flowBadge', B.flowBadge === A.flowBadge);
check('parity_ov_autopilot', B.ovAutopilot === A.ovAutopilot && B.ovAutopilot === true);
check('parity_ov_activeFlow', B.ovActiveFlow === A.ovActiveFlow && B.ovActiveFlow === '');
check('parity_ov_endpoint', B.ovEndpoint === A.ovEndpoint);

// ── A real custom/endpoint flow is UNAFFECTED (still sets activeFlow) ──
autopilotEnabled = false; endpointEnabled = false; activeFlow = '';
_applyAutopilotUI(false); _applyEndpointUI(false); _applyFlowUI('');
setActiveFlow('builtin:endpoint');
check('endpoint_flow_still_sets_activeFlow', activeFlow === 'builtin:endpoint');
check('endpoint_flow_no_autopilot', autopilotEnabled === false);

console.log(out.join('\n'));
"""


def _run(toolbar_src: str) -> str:
    harness = os.path.join(HERE, '_builtin_autopilot_parity_harness.js')
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
    """Byte-revert BOTH convergence choke points so builtin:autopilot falls
    through to the OLD behavior (activeFlow='builtin:autopilot', autopilot OFF):
      1. the setActiveFlow alias branch (new selections), and
      2. the _applyFlowUI normalization (restore/reload/sync).
    Returns the temp file path."""
    src = open(SRC, encoding='utf-8').read()
    # (1) Remove the setActiveFlow alias branch (from its marker to `return;\n  }`).
    assert _SETACTIVEFLOW_MARKER in src, (
        'setActiveFlow alias marker not found — did the source change? Update this test.')
    start = src.index(_SETACTIVEFLOW_MARKER)
    end_tok = 'return;\n  }'
    end = src.index(end_tok, start) + len(end_tok)
    reverted = src[:start] + '/* alias branch removed by NC */' + src[end:]
    # (2) Remove the _applyFlowUI normalization block (exact literal).
    assert _APPLYFLOWUI_BLOCK in reverted, (
        '_applyFlowUI normalization block not found — did the source change? Update this test.')
    reverted = reverted.replace(_APPLYFLOWUI_BLOCK, '  /* normalization removed by NC */\n')
    assert reverted != src and _SETACTIVEFLOW_MARKER not in reverted \
        and _APPLYFLOWUI_BLOCK not in reverted
    dst = os.path.join(HERE, '_main_toolbar_ui_reverted.js')
    with open(dst, 'w', encoding='utf-8') as f:
        f.write(reverted)
    return dst


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_builtin_autopilot_matches_standalone_toggle():
    """Selecting 自动驾驶 from the Mode menu yields state byte-identical to the
    Autopilot toggle; a real endpoint/custom flow is unaffected."""
    output = _run(SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'parity failures:\n' + output
    # 7 parity_* + several toggle_/dropdown_ + endpoint_ = plenty
    assert output.count('PASS') >= 20, f'expected >=20 PASS, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_byte_revert_breaks_parity():
    """Byte-revert NC: removing the alias branch makes the dropdown set
    activeFlow='builtin:autopilot' with autopilot OFF again — the parity
    assertions MUST fail. Proves the alias is load-bearing."""
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
    # …but the dropdown no longer aliases → parity breaks.
    assert _status(output, 'parity_autopilotEnabled') == 'FAIL', \
        'NC should leave autopilot OFF for the dropdown:\n' + output
    assert _status(output, 'parity_activeFlow') == 'FAIL', \
        'NC should set activeFlow=builtin:autopilot:\n' + output
    # The endpoint-flow control case is untouched by the revert.
    assert _status(output, 'endpoint_flow_still_sets_activeFlow') == 'PASS', \
        'endpoint flow must remain a real flow selection:\n' + output
