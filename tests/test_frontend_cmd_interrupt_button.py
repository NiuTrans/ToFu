"""tests/test_frontend_cmd_interrupt_button.py — per-command interrupt button.

The render contract for pt_232244fb (2026-08-01): a running ``run_command``
row carries an INTERRUPT button that kills ONLY the command (the task
continues with the partial output) — the whole-task Stop is a different,
unchanged control. Three pinned behaviours:

  1. ``_renderSearchingRow`` shows the button only when the round is a
     run_command AND a taskId is resolvable (an interrupt that cannot name
     its task is worse than no button).
  2. ``_cmdInterruptClick`` is optimistic: disable + "Interrupting…" on
     click; the success path stays disabled (the tool_result SSE re-render
     removes the row); a refusal / network failure restores the button and
     toasts.
  3. ``_renderCmdDoneBlock`` renders an interrupted command as an amber
     neutral stop (``ptool-cmd-interrupted``), never the red ``✗ exit -1``
     error frame — the turn CONTINUED.

Loads the REAL shipped tool_rounds.js under jsdom; skips when node+jsdom are
absent (same convention as test_frontend_approval_card_render.py).
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

// ── 1. Running run_command row carries the button (taskId resolvable) ──
const runRound = {
  status: 'searching', toolName: 'run_command', query: 'find . -name bundle.js',
  roundNum: 3, tStart: Date.now() - 60000, _taskId: 'task-abc123',
};
const html = _renderUnifiedToolLine(runRound, true);
check('running_has_button', html.includes('ptool-cmd-interrupt'));
check('button_data_task', html.includes('data-cmd-task="task-abc123"'));
check('button_onclick', html.includes('_cmdInterruptClick(this,event)'));
check('button_label', html.includes('>Interrupt</button>'));
check('button_has_tip', html.includes('task continues with the partial output'));

// ── 2. code_exec row: button too (pt_0bde0fd8 — the standalone path now
//    forwards task= into tool_run_command, so the endpoint interrupts it
//    identically) ──
const ceRound = {
  status: 'searching', toolName: 'code_exec', query: 'sleep 30',
  roundNum: 4, tStart: Date.now() - 1000, _taskId: 'task-abc123',
};
const ceHtml = _renderUnifiedToolLine(ceRound, true);
check('code_exec_has_button', ceHtml.includes('ptool-cmd-interrupt'));
check('code_exec_button_data_task', ceHtml.includes('data-cmd-task="task-abc123"'));

// ── 3. Unresolvable taskId: NO button ──
const orphanRound = {
  status: 'searching', toolName: 'run_command', query: 'sleep 30',
  roundNum: 5, tStart: Date.now() - 1000,
};
const orHtml = _renderUnifiedToolLine(orphanRound, true);
check('no_taskid_no_button', !orHtml.includes('ptool-cmd-interrupt'));

// ── 4. Done + interrupted meta: amber neutral stop, never red exit -1 ──
const doneRound = {
  status: 'done', toolName: 'run_command', query: 'find . -name bundle.js',
  roundNum: 6,
  results: [{ command: 'find . -name bundle.js', exitCode: '-1',
              interrupted: true, output: './static/js/bundle.js' }],
};
const doneHtml = _renderUnifiedToolLine(doneRound, false);
check('done_interrupted_cls', doneHtml.includes('ptool-cmd-interrupted'));
check('done_interrupted_label', doneHtml.includes('⏸ interrupted'));
check('done_never_exit_minus1', !doneHtml.includes('✗ exit -1'));

// ── 5. Click semantics (async) ──
(async () => {
  // Success: stays disabled with "Interrupting…" — the SSE re-render settles.
  const btnOk = { disabled: false, textContent: '', getAttribute: () => 'task-abc123' };
  let postedTo = '';
  global.Api = { chat: { interruptCommand: async (id) => { postedTo = id; return { interrupted: true, pid: 7 }; } } };
  await _cmdInterruptClick(btnOk, { stopPropagation() {} });
  check('click_posts_taskid', postedTo === 'task-abc123');
  check('click_ok_disabled', btnOk.disabled === true);
  check('click_ok_label', btnOk.textContent === 'Interrupting…');

  // Refusal (no active command): button restored + toast.
  const btnNo = { disabled: false, textContent: '', getAttribute: () => 'task-abc123' };
  global.Api = { chat: { interruptCommand: async () => ({ interrupted: false, reason: 'no_active_command' }) } };
  let toasted = '';
  global.showToast = (m) => { toasted = m; };
  await _cmdInterruptClick(btnNo, { stopPropagation() {} });
  check('click_refusal_restored', btnNo.disabled === false);
  check('click_refusal_label', btnNo.textContent === 'Interrupt');
  check('click_refusal_toast', toasted.length > 0);

  // Network failure (null from onError:'null'): restored, no crash.
  const btnErr = { disabled: false, textContent: '', getAttribute: () => 'task-abc123' };
  global.Api = { chat: { interruptCommand: async () => null } };
  await _cmdInterruptClick(btnErr, { stopPropagation() {} });
  check('click_network_fail_restored', btnErr.disabled === false);

  console.log(out.join('\n'));
  // Exit explicitly: tool_rounds.js / jsdom may leave a timer handle open.
  process.exit(0);
})();
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_cmd_interrupt_button_contract():
    harness = os.path.join(HERE, '_cmd_interrupt_button_harness.js')
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
    assert not fails, 'interrupt-button render/click failures:\n' + output
    assert output.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{output}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
