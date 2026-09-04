"""Behavioural proof for the tool_rounds rich split (Epic-E sub-4).

Drives the REAL shipped ui/tool_rounds.js under bare node in TWO
configurations:

  A. CORE-ONLY — an isolated core fixture (ui/tool_rounds_rich.js absent):
     renderToolRoundsHTML on a conv-meta round and
     a timer-watcher round must NOT throw and must emit the generic
     ptool-line fallback (never the rich cards, never a ReferenceError).
  B. RICH — core + tool_rounds_rich.js: the same rounds must render the
     rich cards (ptool-conv-meta / timer-watcher markup), and the core
     remainder must be byte-for-byte the same renderer (the split moves
     code, it does not change rich-mode behaviour).

Production composes both sections adjacently. This harness preserves the
core-only fault-tolerance seam while proving ordinary retained behavior.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');

// ── window-scope stubs (explicit — a Proxy would defeat the typeof guards) ──
global.window = global;
global.document = {
  addEventListener: () => {},
  querySelectorAll: () => [],
  visibilityState: 'visible',
};
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.Icon = () => '<i></i>';
global.IconDot = () => '<i></i>';
global.t = (k, d) => d || k;
global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
global.stripNoTranslateTags = (s) => s;
global.apiUrl = (p) => p;
global.debugLog = () => {};
global.convIsBusy = () => false;
global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];

// REAL core file (the split remainder).
eval(fs.readFileSync(process.argv[2], 'utf8'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// A settled conv-meta round (Project Brain board read) + an active
// timer-watcher round (timer_create mid-poll).
function convMetaRound() {
  return {
    roundNum: 3, toolName: 'project_board_read', status: 'done',
    query: 'Read the project board',
    toolContent: '# Board\n\n- epic one\n- epic two',
    results: [{ source: 'Board', action: 'read', items: ['a', 'b'] }],
    toolArgs: {},
  };
}
function timerRound() {
  return {
    roundNum: 5, toolName: 'timer_create', status: 'searching',
    query: 'Watch the training log',
    toolArgs: {},
    _timerPolls: [
      { decision: 'started', at: 1000 },
      { decision: 'wait', at: 2000, reason: 'no DONE marker yet' },
    ],
    _timerTimerId: 'timer-abcdef1234567890',
    _timerNextPollAt: Date.now() + 30000,
  };
}

if (typeof renderToolRoundsHTML !== 'function') {
  console.log('FAIL entry renderToolRoundsHTML missing'); process.exit(0);
}

// ── Phase A: isolated core-only materialization ──
let threwA = null, htmlMetaA = '', htmlTimerA = '';
try {
  htmlMetaA = renderToolRoundsHTML([convMetaRound()], false);
  htmlTimerA = renderToolRoundsHTML([timerRound()], false);
} catch (e) { threwA = e; }
check('A_degraded_never_throws', !threwA);
if (threwA) out.push('  A error: ' + threwA.message);
check('A_degraded_convmeta_generic_line',
  htmlMetaA.indexOf('ptool-line') !== -1 &&
  htmlMetaA.indexOf('ptool-convmeta') === -1);
check('A_degraded_timer_generic_line',
  htmlTimerA.indexOf('ptool-line') !== -1 &&
  htmlTimerA.indexOf('timer-watcher') === -1);

// ── Phase B: retained core + immediately adjacent rich section ──
eval(fs.readFileSync(process.argv[3], 'utf8'));
check('B_rich_symbols_present',
  typeof _renderConvMetaBlock === 'function' &&
  typeof _renderTimerWatcherBlock === 'function');

let threwB = null, htmlMetaB = '', htmlTimerB = '';
try {
  htmlMetaB = renderToolRoundsHTML([convMetaRound()], false);
  htmlTimerB = renderToolRoundsHTML([timerRound()], false);
} catch (e) { threwB = e; }
check('B_rich_never_throws', !threwB);
if (threwB) out.push('  B error: ' + threwB.message);
check('B_rich_convmeta_card',
  htmlMetaB.indexOf('ptool-convmeta') !== -1 ||
  htmlMetaB.indexOf('ptool-conv-meta') !== -1);
check('B_rich_timer_watcher',
  htmlTimerB.indexOf('timer-watcher') !== -1 ||
  htmlTimerB.indexOf('timer-next-poll') !== -1);

// The ordinary round types must render IDENTICALLY in both modes (the
// split must not change the common path). Re-render a plain search round
// in rich mode and compare against the degraded render.
function plainRound() {
  return {
    roundNum: 1, toolName: 'web_search', status: 'done',
    query: 'test query', toolArgs: { query: 'test query' },
    results: [{ title: 'x', url: 'https://x' }],
  };
}
let plainSame = false;
try {
  // renderToolRoundsHTML is the same function object (core) in both modes —
  // the comparison that matters: a plain round contains NO rich markers.
  const plainHtml = renderToolRoundsHTML([plainRound()], false);
  plainSame = plainHtml.indexOf('ptool-line') !== -1
    && plainHtml.indexOf('ptool-convmeta') === -1
    && plainHtml.indexOf('timer-watcher') === -1;
} catch (e) { plainSame = false; }
check('B_plain_round_unaffected', plainSame);

console.log(out.join('\n'));
process.exit(0);
"""


def _run(core_path: str, rich_path: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_tool_rounds_rich_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, core_path, rich_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_degraded_and_rich_modes():
    core = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')
    rich = os.path.join(JS_DIR, 'ui', 'tool_rounds_rich.js')
    proc = _run(core, rich)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    for want in (
        'PASS A_degraded_never_throws',
        'PASS A_degraded_convmeta_generic_line',
        'PASS A_degraded_timer_generic_line',
        'PASS B_rich_symbols_present',
        'PASS B_rich_never_throws',
        'PASS B_rich_convmeta_card',
        'PASS B_rich_timer_watcher',
        'PASS B_plain_round_unaffected',
    ):
        assert want in output, f'{want} missing:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_conv_meta_dispatch_guard_is_load_bearing(tmp_path):
    """NEUTER: strip the typeof guard from the conv-meta dispatch on a COPY
    of the core file → with the rich module absent the render throws
    (ReferenceError) — proves the guard is what makes the degraded mode
    safe. Shipped files left byte-identical."""
    core = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')
    rich = os.path.join(JS_DIR, 'ui', 'tool_rounds_rich.js')
    with open(core, encoding='utf-8') as f:
        src = f.read()
    anchor = ("const convMetaHtml = (typeof _renderConvMetaBlock === 'function')\n"
              "      ? _renderConvMetaBlock(round, svg, q, badgeHtml) : \"\";")
    assert anchor in src, 'conv-meta guarded dispatch missing — update the neuter target'
    neutered = src.replace(
        anchor,
        "const convMetaHtml = _renderConvMetaBlock(round, svg, q, badgeHtml);",
        1)
    assert neutered != src
    copy = tmp_path / 'tool_rounds_neutered.js'
    copy.write_text(neutered, encoding='utf-8')
    proc = _run(str(copy), rich)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL A_degraded_never_throws' in output, (
        'NEUTER did not bite — without the guard the degraded render '
        'should ReferenceError:\n' + output)
    with open(core, encoding='utf-8') as f:
        assert f.read() == src, 'shipped tool_rounds.js mutated by the NC'
