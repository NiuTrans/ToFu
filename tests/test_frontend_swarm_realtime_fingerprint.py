"""Regression: a COMMITTED (non-streaming) swarm panel repaints in real-time
when a cross-turn ``/api/push`` frame mutates a sub-agent's status.

WHY
---
An async swarm spawned on turn T keeps running after T ends. Its later
per-agent events are mirrored onto the conv-scoped ``/api/push`` channel and
replayed by ``static/js/ui/swarm_push.js``, which mutates the agent objects
inside ``round._swarmAgents`` and then calls ``renderChat(conv, false)``.

That render reaches the **surgical** per-message diff path, which only touches
a message when its ``_msgFingerprint`` changed. The bug: ``_msgFingerprint``
(``static/js/ui/chat_render.js``) keyed only on ``toolRounds.length`` — it
never looked INSIDE ``_swarmAgents``. So when a ``swarm_agent_complete`` frame
flipped an agent ``running → done`` on a committed panel, the fingerprint was
unchanged, the message was diffed as "unchanged", and the panel never
repainted — i.e. the swarm panel was NOT real-time for the exact detached
case the push channel exists to serve.

The fix folds each swarm round's agent status/phase/tokens/preview into
``_msgFingerprint`` (and the swarm round flags). This test loads the REAL
shipped ``_msgFingerprint`` under jsdom and asserts the fingerprint MOVES on
each kind of agent-state transition, while staying STABLE when nothing
swarm-related changed.

Runs the REAL shipped JS under jsdom; skips cleanly when node + jsdom aren't
installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
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

// chat_render.js references a few render helpers at call time only; we only
// invoke _msgFingerprint (a pure function), so a guarded load is enough.
try { eval(fs.readFileSync(process.argv[2], 'utf8')); } catch (e) {
  console.log('FAIL chat_render_load ' + (e && e.message)); process.exit(0);
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _msgFingerprint !== 'function') {
  console.log('FAIL fn_exposed _msgFingerprint missing'); process.exit(0);
}
check('fn_exposed', true);

/* A committed assistant message owning ONE async swarm panel with two agents
   that are still running (the live SSE flags are gone post-commit). */
function mkMsg() {
  return {
    role: 'assistant', content: 'intermediate reply', thinking: '',
    toolRounds: [{
      roundNum: 1, toolName: 'spawn_agents', _swarm: true, _asyncRunning: true,
      status: 'done',
      _swarmAgents: [
        { id: 'a1', role: 'researcher', status: 'running', phase: 'tool_use', tokens: 0, preview: '' },
        { id: 'a2', role: 'coder', status: 'running', phase: 'thinking', tokens: 0, preview: '' },
      ],
    }],
  };
}

// ── 1. agent status running → done moves the fingerprint ──
const m1 = mkMsg();
const fp0 = _msgFingerprint(m1);
m1.toolRounds[0]._swarmAgents[0].status = 'done';
m1.toolRounds[0]._swarmAgents[0].phase = 'done';
const fp1 = _msgFingerprint(m1);
check('status_flip_moves_fp', fp1 !== fp0);

// ── 2. preview / tokens streaming in moves the fingerprint ──
const m2 = mkMsg();
const fp2a = _msgFingerprint(m2);
m2.toolRounds[0]._swarmAgents[1].preview = 'partial finding text';
m2.toolRounds[0]._swarmAgents[1].tokens = 1234;
const fp2b = _msgFingerprint(m2);
check('preview_tokens_move_fp', fp2b !== fp2a);

// ── 3. the terminal complete sweep (asyncRunning cleared + endTime set) moves it ──
const m3 = mkMsg();
const fp3a = _msgFingerprint(m3);
m3.toolRounds[0]._asyncRunning = false;
m3.toolRounds[0]._swarmEndTime = Date.now();
for (const a of m3.toolRounds[0]._swarmAgents) { a.status = 'done'; a.phase = 'done'; }
const fp3b = _msgFingerprint(m3);
check('terminal_sweep_moves_fp', fp3b !== fp3a);

// ── 4. NEGATIVE: an unrelated re-fingerprint with NO swarm change is STABLE
//      (the token must not be noisy / time-dependent → no needless repaints). ──
const m4 = mkMsg();
const fp4a = _msgFingerprint(m4);
const fp4b = _msgFingerprint(m4);   // identical object, no mutation
check('stable_when_unchanged', fp4a === fp4b);

// ── 5. a message with NO swarm round must be byte-identical to the legacy
//      shape (no trailing :sw token) — the fold is additive, never disturbs
//      non-swarm messages. ──
const plain = { role: 'assistant', content: 'hi', thinking: '', toolRounds: [
  { roundNum: 1, toolName: 'web_search', status: 'done', results: [{ title: 'r' }] },
] };
check('non_swarm_has_no_sw_token', _msgFingerprint(plain).indexOf(':sw') === -1);

// ── 6. the swarm token is actually PRESENT for a swarm message (proves the
//      fold ran, so checks 1-3 aren't passing for an unrelated reason). ──
check('swarm_msg_has_sw_token', _msgFingerprint(mkMsg()).indexOf(':sw') !== -1);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_msg_fingerprint_tracks_swarm_agent_state():
    harness = os.path.join(HERE, '_swarm_realtime_fp_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'chat_render.js'),   # argv[2]
             ROOT,                                            # argv[3]
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
    assert not fails, 'swarm realtime-fingerprint failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'
