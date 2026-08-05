"""jsdom regression: a branch panel whose persisted pin (`branch.activeTaskId`)
is STALE (task finished while the tab was closed) must render the SETTLED tail
message statically — never slice it off and paint a fake "Generating…" zone.

WHY (same absence-of-settled-marker class as the mse9r2ir7ql0v4 live-view
incident, one lane over — 2026-08-05 audit)
---------------------------------------------------------------------------
`_renderBranchPanel` used to compute `(isStreaming || hasPersistentTask)
? msgs.slice(0,-1) : msgs`. `hasPersistentTask = !!branch.activeTaskId` reads a
PERSISTED field; when the task ended but the pin was never cleared (crash /
closed tab mid-stream), the panel unconditionally hid the tail branch message
— whatever its role — and showed an eternal streaming zone. The in-memory
`_branchStreams` map is precise, so the fix gates the stale-pin arm on the
tail's own persisted record: `finishReason`/`error` are persisted, so a
settled tail renders statically even with a stale pin.

HARNESS — drives the REAL shipped branch.js `_renderBranchPanel` under jsdom:
  • branch with stale activeTaskId + settled assistant tail (finishReason set)
    → tail message HTML present, NO streaming zone.
  • same branch but tail unfinished (no finishReason) → tail sliced, streaming
    zone present (the legit mid-crash reconnect shape is preserved).
NC: revert the predicate to the pin-only slice → settled tail hidden → red.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')

sys.path.insert(0, HERE)
from _jsdom import frontend_module_guard  # noqa: E402

frontend_module_guard(need_jsdom=True)

_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const NEUTER = process.argv[3] || 'none';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.navigator = win.navigator;

const esc = (s) => String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
global.escapeHtml = esc;
global.renderMarkdown = (s) => '<p>' + esc(s) + '</p>';
global.t = (k, o) => (o && o.text != null ? k + ':' + o.text : (o && o.n != null ? k + ':' + o.n : k));
global.getToolRoundsFromMsg = () => [];
global.renderToolRoundsHTML = () => '';
global.searchMode = 'on';
global.fetchEnabled = false;
global.codeExecEnabled = false;
global.browserEnabled = false;
global.memoryEnabled = false;

const CONV = {
  id: 'c1',
  messages: [{
    role: 'assistant', content: 'parent answer',
    branches: [{
      title: 'side quest', icon: 'B', activeTaskId: 'STALE-task-pin',
      messages: [
        { role: 'user', content: 'branch question' },
        { role: 'assistant', content: 'branch SETTLED answer', finishReason: 'stop' },
      ],
    }],
  }],
};
global.conversations = [CONV];
global.getActiveConv = () => CONV;

let src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'branch.js'), 'utf8');
if (NEUTER === 'pin_only') {
  const needle = 'const _tailIsLive = isStreaming || (hasPersistentTask && _tailUnfinished);';
  if (src.indexOf(needle) < 0) { console.log('FAIL neuter_target_drifted'); process.exit(0); }
  src = src.replace(needle,
    'const _tailIsLive = isStreaming || hasPersistentTask;  // NEUTERED-pin-only');
}
(0, eval)(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _renderBranchPanel !== 'function') {
  console.log('FAIL fn_exposed _renderBranchPanel missing'); process.exit(0);
}
check('fn_exposed', true);

/* ── A. stale pin + SETTLED tail → tail rendered, no fake streaming zone ── */
{
  const html = _renderBranchPanel(CONV.messages[0], 0, 0);
  /* Static-only shape: the sliced tail's content ALSO echoes inside the
   * streaming zone, so a bare text match can't discriminate. The static
   * branch-msg render appends the finish-info div (finishReason) right after
   * the content — the zone never does. */
  check('A_settled_tail_rendered', html.includes('<p>branch SETTLED answer</p><div style='));
  check('A_no_fake_streaming_zone', !html.includes('branch-streaming-msg'));
}

/* ── B. stale pin + UNFINISHED tail (crash mid-stream) → tail sliced, zone shown ── */
{
  CONV.messages[0].branches[0].messages[1] =
    { role: 'assistant', content: 'partial draft…' };   // no finishReason
  const html = _renderBranchPanel(CONV.messages[0], 0, 0);
  /* The sliced tail's partial content still renders INSIDE the streaming zone
   * (by design — the zone shows the in-flight draft), so discriminate by
   * occurrence count: 1 = zone only (tail sliced from statics);
   * 2 = tail ALSO rendered statically (the bug). */
  const occurrences = html.split('partial draft…').length - 1;
  check('B_unfinished_tail_sliced', occurrences === 1);
  check('B_streaming_zone_present', html.includes('branch-streaming-msg'));
}

console.log(out.join('\n'));
console.log('__JSDOM_RESULT__ ' + JSON.stringify({
  pass: out.filter(l => l.startsWith('PASS')).length,
  fail: out.filter(l => l.startsWith('FAIL')).length,
}));
"""


def _run(neuter='none'):
    import subprocess
    harness = os.path.join(HERE, '_branch_panel_settled_tail_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT, neuter],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


def test_branch_panel_settled_tail_visible_with_stale_pin():
    output = _run('none')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'branch panel settled-tail failures:\n' + output
    for want in ('PASS fn_exposed',
                 'PASS A_settled_tail_rendered',
                 'PASS A_no_fake_streaming_zone',
                 'PASS B_unfinished_tail_sliced',
                 'PASS B_streaming_zone_present'):
        assert want in output, output


def test_NC_pin_only_predicate_hides_settled_tail():
    """NEUTER: restore the pin-only slice → the settled tail is hidden again."""
    output = _run('pin_only')
    assert 'FAIL A_settled_tail_rendered' in output, (
        'NEUTER did not bite: settled tail stayed statically rendered with the '
        'pin-only slice.\n' + output)
    assert 'FAIL A_no_fake_streaming_zone' in output, (
        'NEUTER did not bite: no fake streaming zone appeared with the pin-only '
        'slice.\n' + output)
