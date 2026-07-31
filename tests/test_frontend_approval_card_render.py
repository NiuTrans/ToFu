"""tests/test_frontend_approval_card_render.py — Approval-card RENDER contract.

Complements tests/test_frontend_sse_dispatch.py (which proves the SSE
*dispatcher* stores ``approvalMeta`` on the round) by proving the *renderer*
in ``static/js/ui/tool_rounds.js`` turns that meta into the correct card.

The untested path this pins (2026-06): a DESTRUCTIVE ``run_command`` gated in
Manual mode emits a ``write_approval_request`` whose ``meta`` carries
``command`` / ``description`` (not the file-write ``search`` / ``replace`` /
``contentPreview`` keys). ``_renderUnifiedToolLine`` must therefore select the
``ameta.command != null`` branch and render a shell-command card with working
approve/reject buttons — never mis-route into the apply_diff diff branch.

Loads the REAL shipped tool_rounds.js under jsdom; skips when node+jsdom are
absent.
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

// Destructive run_command, gated → meta has command/description only.
const cmdRound = {
  status: 'pending_approval', approvalId: 'ap2', toolName: 'run_command',
  query: 'run_command', toolRounds: [],
  approvalMeta: { toolName: 'run_command', command: 'rm foo.py', description: 'delete foo' },
};
const html = _renderUnifiedToolLine(cmdRound, false);
check('cmd_card_class', html.includes('ptool-cmd-code'));
check('cmd_shows_command', html.includes('$ rm foo.py'));
check('cmd_shows_description', html.includes('delete foo'));
check('cmd_approve_btn', html.includes("resolveWriteApproval('ap2',true)"));
check('cmd_reject_btn', html.includes("resolveWriteApproval('ap2',false)"));
check('cmd_awaiting_badge', html.includes('awaiting approval'));
// Must NOT have mis-routed into the apply_diff search/replace branch.
check('cmd_no_diff_lines', !html.includes('ptool-diff-del'));

// Sanity: a write_file approval still renders its content-preview card and
// does NOT render a command card (branch isolation in the other direction).
const wfRound = {
  status: 'pending_approval', approvalId: 'ap3', toolName: 'write_file',
  query: 'write_file', toolRounds: [],
  approvalMeta: { toolName: 'write_file', path: 'x.py',
    contentPreview: 'print(1)\nprint(2)', contentLines: 2, contentChars: 17 },
};
const wfHtml = _renderUnifiedToolLine(wfRound, false);
check('wf_no_cmd_card', !wfHtml.includes('ptool-cmd-code'));
check('wf_approve_btn', wfHtml.includes("resolveWriteApproval('ap3',true)"));

console.log(out.join('\n'));
// Exit explicitly: tool_rounds.js / jsdom may leave a timer or listener handle
// open, which would keep node's event loop alive past the 60s harness timeout
// even though every assertion already ran.
process.exit(0);
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_run_command_approval_card_renders():
    harness = os.path.join(HERE, '_approval_card_render_harness.js')
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
    assert not fails, 'approval-card render failures:\n' + output
    assert output.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{output}'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
