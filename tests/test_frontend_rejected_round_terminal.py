"""tests/test_frontend_rejected_round_terminal.py — 'rejected' is TERMINAL.

The 75→11→76 render anomaly (conv mrltpk1t43e0mw) hinged on the streaming
reorder that shoves any ``status==="searching"`` round AFTER the done-tail.
The backend now settles a pre-hook-blocked round to ``status='rejected'``.
This test pins the FRONTEND half of that contract so the visual bug cannot
recur for blocked rounds:

  1. The card renderer (``_renderUnifiedToolLine``) treats a ``rejected``
     run_command round as TERMINAL — a settled ``⊘ blocked`` / "not run" card,
     NO ``ptool-cmd-running`` spinner. The spinner gate is strictly
     ``isSearching === (status === "searching")`` (tool_rounds.js:2693), so a
     rejected round passes ``isSearching=false`` and can never hit the
     Running… branch.
  2. The active/done partition (streaming_ui.js) classifies ``rejected`` in
     the DONE bucket (active = searching|pending_approval only), so a blocked
     round is never pulled into the trailing "active" reorder.

Loads the REAL shipped tool_rounds.js under jsdom; skips when node+jsdom are
absent.
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
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k, d) => (d || k);
global.renderMarkdown = (s) => s;
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/tool_rounds.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── A blocked run_command settled to 'rejected' (the backend contract) ──
const blocked = {
  status: 'rejected', toolName: 'run_command', roundNum: 11, llmRound: 10,
  query: 'rm -rf /tmp/x && python3 export.py',
  toolContent: 'Tool blocked by pre-execution hook: Blocked catastrophic delete of /\n\nScope the deletion to a subpath.',
  results: [{
    type: 'error', toolName: 'run_command', source: 'Blocked', badge: 'blocked',
    command: 'rm -rf /tmp/x && python3 export.py', notRun: true,
    exitCode: 'not-run',
    reason: 'Tool blocked by pre-execution hook: Blocked catastrophic delete of /\n\nScope the deletion to a subpath.',
  }],
};

// The spinner gate: renderer is called with isSearching = (status==='searching').
const isSearching = (blocked.status === 'searching');
check('rejected_is_not_searching', isSearching === false);

const html = _renderUnifiedToolLine(blocked, isSearching);
// TERMINAL: no running spinner / running class anywhere.
check('no_running_class', !html.includes('ptool-cmd-running'));
check('no_spinner', !html.includes('ptool-spinner'));
check('no_running_label', !html.includes('Running...'));
// Settled "not run / blocked" affordance is shown with its reason inline.
check('shows_blocked_badge', html.includes('blocked'));
check('shows_notrun_card', html.includes('ptool-cmd-notrun'));
check('shows_reason', html.includes('Scope the deletion to a subpath'));
check('shows_command', html.includes('export.py'));

// ── The active/done partition (mirror streaming_ui.js:648-649 logic) ──
// We can't eval streaming_ui.js standalone (heavy deps), so we assert the
// PARTITION PREDICATE itself against the shipped source string, then exercise
// the predicate here to prove 'rejected' lands in `done`.
const rounds = [
  { status: 'done',    roundNum: 1 },
  { status: 'rejected',roundNum: 11 },   // blocked round
  { status: 'searching',roundNum: 76 },  // genuinely active
];
const active = rounds.filter(r => r.status === 'searching' || r.status === 'pending_approval');
const done   = rounds.filter(r => r.status !== 'searching' && r.status !== 'pending_approval');
check('rejected_in_done_bucket', done.some(r => r.roundNum === 11));
check('rejected_not_in_active_bucket', !active.some(r => r.roundNum === 11));
check('searching_still_active', active.some(r => r.roundNum === 76));

console.log(out.join('\n'));
// tool_rounds.js:3730 installs a load-time 1Hz _cmdTimerTicker that keeps the
// node event loop alive — force a clean exit after the synchronous checks.
process.exit(0);
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_rejected_round_renders_terminal():
    harness = os.path.join(HERE, '_rejected_terminal_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'tool_rounds.js'),  # argv[2]
             ROOT],                                          # argv[3]
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
    assert not fails, 'rejected-terminal render failures:\n' + output
    assert output.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{output}'


def test_streaming_partition_treats_rejected_as_done_source_guard():
    """Source-level guard: the streaming reorder partition must classify
    'active' as ONLY searching|pending_approval (so rejected/done fall into the
    done-tail). Pins the exact predicate so a future refactor can't silently
    fold 'rejected' back into the active bucket and reintroduce 75→11→76."""
    src = open(os.path.join(JS_DIR, 'ui', 'streaming_ui.js'), encoding='utf-8').read()
    # The active-bucket filter must gate on searching / pending_approval only.
    m = re.search(r'const active = toolRounds\.filter\(r => (.*?)\);', src)
    assert m, 'could not find the active-bucket filter in streaming_ui.js'
    pred = m.group(1)
    assert 'searching' in pred and 'pending_approval' in pred, pred
    assert 'rejected' not in pred, (
        "'rejected' must NOT be in the active bucket predicate — it is a "
        "terminal state and belongs in the done-tail")


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
