"""The swarm panel must RENDER complete sub-agent results — jsdom proof.

Companion to ``test_swarm_panel_full_results.py`` (which pins the backend
seams). This one runs the REAL shipped ``streaming_swarm_panel.js`` under jsdom
and asserts the produced HTML actually contains the whole text.

Owner acceptance criterion: a 5,000-char sub-agent answer and a 2,000-char
tool-result preview both render in full, with no ellipsis.

The screenshot that triggered this showed a fetch_url result cut at 300 chars,
mid-path through ``/mnt/your-fs`` — a debugging
surface that silently drops the tail is worse than none.
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setInterval = win.setInterval = () => 0;
global.setTimeout = win.setTimeout = (fn) => 0;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k) => String(k || '').split('.').pop();
win._TOOL_DISPLAY = global._TOOL_DISPLAY = {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_swarm_panel.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Criterion 1: a 5,000-char final answer renders IN FULL ──
// Distinct head/tail sentinels so a truncation anywhere is detectable.
const HEAD = 'ANSWERHEAD';
const TAIL = 'ANSWERTAIL';
const bigAnswer = HEAD + 'z'.repeat(5000 - HEAD.length - TAIL.length) + TAIL;
check('fixture_answer_is_5000', bigAnswer.length === 5000);

const doneRound = {
  roundNum: 1, _swarm: true, _swarmActive: false, status: 'done',
  _swarmStartTime: Date.now() - 60000, _swarmEndTime: Date.now(),
  _swarmAgents: [{
    id: 'a1', role: 'researcher', objective: 'read the docs',
    status: 'done', phase: 'done', preview: bigAnswer,
  }],
};
const doneHtml = _buildSwarmPanelHTML(doneRound, [doneRound]);
check('answer_head_rendered', doneHtml.includes(HEAD));
check('answer_tail_rendered', doneHtml.includes(TAIL));
// The whole body must be present, not just its ends.
check('answer_full_body_rendered', doneHtml.includes(bigAnswer));

// ── Criterion 2: a 2,000-char tool-result preview renders IN FULL ──
const THEAD = 'TOOLHEAD';
const TTAIL = 'TOOLTAIL';
const bigPreview = THEAD + 'q'.repeat(2000 - THEAD.length - TTAIL.length) + TTAIL;
check('fixture_preview_is_2000', bigPreview.length === 2000);

const toolRound = {
  roundNum: 2, _swarm: true, _swarmActive: false, status: 'done',
  _swarmStartTime: Date.now() - 60000, _swarmEndTime: Date.now(),
  _swarmAgents: [{
    id: 'a2', role: 'researcher', objective: 'fetch pages',
    status: 'done', phase: 'done', preview: 'short',
    _toolCalls: [{
      callId: 'c1', toolName: 'fetch_url', argsBrief: '3 URLs',
      status: 'done', elapsed: 23.4, preview: bigPreview,
    }],
  }],
};
const toolHtml = _buildSwarmPanelHTML(toolRound, [toolRound]);
check('tool_preview_head_rendered', toolHtml.includes(THEAD));
check('tool_preview_tail_rendered', toolHtml.includes(TTAIL));
check('tool_preview_full_body_rendered', toolHtml.includes(bigPreview));

// ── Criterion 2b: bounded durable detail is explicit, including after F5 ──
const boundedAgent = {
  id: 'a2b', role: 'researcher', objective: 'long audit',
  status: 'done', phase: 'done', preview: 'complete answer',
  toolCallsOmitted: 7,
  toolCalls: [{
    toolName: 'fetch_url', argsBrief: 'many URLs', status: 'done',
    preview: 'p'.repeat(2000), previewTruncated: true,
    previewFullChars: 5000,
  }, {
    toolName: 'run_command', argsBrief: 'failing command', status: 'failed',
    error: '', errorTruncated: true, errorFullChars: 9000,
  }],
};
const boundedRound = {
  roundNum: 22, _swarm: true, _swarmActive: false, status: 'done',
  _swarmSnapshot: {agents: [boundedAgent], settled: true, agentCount: 1,
                   doneCount: 1, totalTokens: 1, version: 100001},
};
const recovered = _recoverSwarmAgents(boundedRound, []);
check('durable_omitted_count_recovered',
  recovered.length === 1 && recovered[0]._toolCallsOmitted === 7);
const boundedHtml = _buildSwarmPanelHTML(boundedRound, [boundedRound]);
check('omitted_calls_marker_rendered', boundedHtml.includes('sw-tl-omitted'));
check('truncated_preview_marker_rendered', boundedHtml.includes('sw-tl-truncated'));
check('fully_elided_error_stays_expandable',
  boundedHtml.includes('sw-tl-detail sw-tl-detail-error'));

// ── Criterion 3: a FAILED agent's error renders in full (was cut at 200) ──
const EHEAD = 'ERRHEAD';
const ETAIL = 'ERRTAIL';
const bigErr = EHEAD + 'e'.repeat(1200 - EHEAD.length - ETAIL.length) + ETAIL;
const failRound = {
  roundNum: 3, _swarm: true, _swarmActive: false, status: 'done',
  _swarmStartTime: Date.now() - 60000, _swarmEndTime: Date.now(),
  _swarmAgents: [{
    id: 'a3', role: 'coder', objective: 'build it',
    status: 'failed', phase: 'error', preview: bigErr,
  }],
};
const failHtml = _buildSwarmPanelHTML(failRound, [failRound]);
check('error_head_rendered', failHtml.includes(EHEAD));
check('error_tail_rendered', failHtml.includes(ETAIL));
check('error_full_body_rendered', failHtml.includes(bigErr));

// ── Criterion 4: canonical agent IDs are never shortened ──
const idRound = {
  roundNum: 4, _swarm: true, _swarmActive: false, status: 'done',
  _swarmStartTime: Date.now() - 1000, _swarmEndTime: Date.now(),
  _swarmAgents: [{
    id: 'test-audit', role: 'coder', objective: 'audit tests',
    status: 'done', phase: 'done', preview: 'ok',
  }],
};
const idHtml = _buildSwarmPanelHTML(idRound, [idRound]);
check('canonical_agent_id_visible', idHtml.includes('>test-audit</span>'));
check('canonical_log_token_copyable',
  idHtml.includes('data-grep="agent-coder-test-audit"'));
check('truncated_agent_id_absent', !idHtml.includes('>test-aud</span>'));

// ── No ellipsis artifacts injected by the renderer itself ──
// (The agent-card body must not add a "…" of its own; a genuine backend-side
//  truncation marker would arrive inside the text and is not the renderer's.)
const renderedBodies = doneHtml + toolHtml + failHtml + idHtml;
check('no_renderer_ellipsis', !renderedBodies.includes('…]'));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_panel_renders_full_results():
    harness = os.path.join(HERE, '_swarm_full_results_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js'),  # argv[2]
             ROOT,                                                    # argv[3]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Swarm panel truncated a result:\n' + output
    assert output.count('PASS') >= 19, f'expected >=19 PASS, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_slice_removal_is_load_bearing():
    """NEUTER: restore the ``.slice(0, 1200)`` cap and prove the 5,000-char
    answer is then clipped — confirming the removal is the load-bearing edit
    and not incidental. A future refactor that reintroduces any slice here
    silently re-breaks the debugging surface."""
    src_path = os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js')
    with open(src_path, encoding='utf-8') as f:
        src = f.read()
    needle = 'const preview = (a.preview || "");'
    assert needle in src, 'preview assignment shape changed — update this neuter'
    neutered = src.replace(
        needle, 'const preview = (a.preview || "").slice(0, 1200);', 1)
    assert neutered != src, 'neuter did not modify the source'

    neutered_path = os.path.join(HERE, '_swarm_full_results_neutered.js')
    harness = os.path.join(HERE, '_swarm_full_results_neuter_harness.js')
    with open(neutered_path, 'w', encoding='utf-8') as f:
        f.write(neutered)
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, neutered_path, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        for p in (neutered_path, harness):
            try:
                os.remove(p)
            except OSError:
                pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'FAIL answer_tail_rendered' in output, (
        'NC (slice restored) must clip the 5,000-char answer tail:\n' + output)
