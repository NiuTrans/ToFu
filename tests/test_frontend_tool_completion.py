"""Regression: a tool round flips to DONE on its OWN tool_result — never
"waits for the next tool to arrive".

WHY
---
`_syncToolRoundsDOM` (static/js/ui/streaming_ui.js) used to decide "settle
this slot to done" by SNIFFING the existing slot DOM for one of four spinner
marker classes (`.ptool-active` / `.ptool-cmd-running` / `.ptool-pending` /
`.code-exec-running`). That made completion fragile: any active renderer
whose markup did NOT happen to emit one of those exact classes would keep
its spinner until an UNRELATED later event forced a full rebuild — exactly
the user-reported "tool only marks complete when the next tool arrives".

The fix makes completion DATA-DRIVEN: each slot stamps `data-rendered-status`
with the status it was last rendered at, and re-renders whenever the round's
own `status` differs — independent of which CSS classes the active markup
used.

This harness loads the REAL shipped `_syncToolRoundsDOM` under jsdom and
drives a `searching → done` transition for a tool whose active markup carries
NO legacy marker class (the worst case the old code mishandled). It asserts
the slot shows the DONE markup (no spinner) after the second sync, with NO
intervening event for any other round — i.e. completion fires on the round's
OWN tool_result, not when the next tool arrives.

Runs the REAL JS under jsdom; skips cleanly when node + jsdom aren't
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
global.CSS = win.CSS = undefined;

// ── Stubs for globals _syncToolRoundsDOM touches ──
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k, o) => k + (o && o.n != null ? (':' + o.n) : '');
win._isRoundSwarm = global._isRoundSwarm = (r) => false;  // none of these are swarm panels
win._buildSwarmPanelHTML = global._buildSwarmPanelHTML = () => '<div class="sw-panel"></div>';
win._renderTurnHead = global._renderTurnHead = () => '';
win._renderSoloRoundTag = global._renderSoloRoundTag = () => '';
win._turnLabelText = global._turnLabelText = () => '';
win.renderMarkdown = global.renderMarkdown = (s) => '<p>' + global.escapeHtml(s) + '</p>';

// ── KEY STUB: an active renderer whose markup carries NONE of the four
//    legacy marker classes (.ptool-active / .ptool-cmd-running /
//    .ptool-pending / .code-exec-running). Under the OLD class-sniff logic
//    this slot's spinner would never clear on its own tool_result. ──
win._renderUnifiedToolLine = global._renderUnifiedToolLine = (round, active) => {
  if (active) {
    // Deliberately NO legacy marker class; just a bespoke spinner.
    return '<div class="xtool-line"><span class="xtool-spinner"></span>' +
           '<span class="xtool-text">' + global.escapeHtml(round.query || '') + '</span></div>';
  }
  return '<div class="xtool-line xtool-done"><span class="xtool-check">done</span>' +
         '<span class="xtool-text">' + global.escapeHtml(round.query || '') + '</span></div>';
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_ui.js (real)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _syncToolRoundsDOM !== 'function') {
  console.log('FAIL fn_exposed _syncToolRoundsDOM missing'); process.exit(0);
}
check('fn_exposed', true);

// ── Scenario 1: NO-marker-class active tool flips to done on its OWN result,
//    with NO other round/event in between. ──
{
  const container = document.createElement('div');
  document.body.appendChild(container);

  // Round 1 arrives active (tool_start).
  const rounds = [{ roundNum: 1, status: 'searching', toolName: 'await_agents', query: 'await all', results: null }];
  _syncToolRoundsDOM(container, rounds);
  let slot = container.querySelector('[data-prn="1"]');
  check('active_spinner_shown', !!slot && !!slot.querySelector('.xtool-spinner'));
  check('active_stamp', slot.getAttribute('data-rendered-status') === 'searching');

  // tool_result for THE SAME round: status flips to done. NO new round added.
  rounds[0].status = 'done';
  rounds[0].results = [{ title: 'r', snippet: 's' }];
  _syncToolRoundsDOM(container, rounds);

  slot = container.querySelector('[data-prn="1"]');
  check('done_no_spinner', !!slot && !slot.querySelector('.xtool-spinner'));
  check('done_markup_shown', !!slot && !!slot.querySelector('.xtool-done'));
  check('done_stamp', slot.getAttribute('data-rendered-status') === 'done');
  // Exactly one slot — no phantom "next tool" was needed.
  check('single_slot', container.querySelectorAll('[data-prn]').length === 1);
}

// ── Scenario 2: the FINGERPRINT gate must let the status flip through even
//    when nothing else changed (searching→done changes the fp). ──
{
  const container = document.createElement('div');
  document.body.appendChild(container);
  const rounds = [{ roundNum: 7, status: 'searching', toolName: 'spawn_agents', query: 'x', results: null }];
  _syncToolRoundsDOM(container, rounds);
  const fpAfterActive = container._roundsFingerprint;
  rounds[0].status = 'done';
  rounds[0].results = [{ title: 'ok' }];
  _syncToolRoundsDOM(container, rounds);
  check('fingerprint_moved_on_status_flip', container._roundsFingerprint !== fpAfterActive);
  const slot = container.querySelector('[data-prn="7"]');
  check('s2_done_no_spinner', !!slot && !slot.querySelector('.xtool-spinner') && !!slot.querySelector('.xtool-done'));
}

// ── Scenario 3: a SECOND idempotent sync (no data change) must NOT thrash —
//    the slot stays done, no re-render churn (fingerprint gate holds). ──
{
  const container = document.createElement('div');
  document.body.appendChild(container);
  const rounds = [{ roundNum: 3, status: 'done', toolName: 'web_search', query: 'q', results: [{ title: 'r' }] }];
  _syncToolRoundsDOM(container, rounds);
  const slot = container.querySelector('[data-prn="3"]');
  const html1 = slot.innerHTML;
  _syncToolRoundsDOM(container, rounds);  // identical → fp gate returns early
  check('idempotent_no_change', container.querySelector('[data-prn="3"]').innerHTML === html1);
  check('s3_done_no_spinner', !slot.querySelector('.xtool-spinner'));
}

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_tool_completion_is_data_driven():
    harness = os.path.join(HERE, '_tool_completion_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'streaming_ui.js'),  # argv[2]
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
    assert not fails, 'tool-completion failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'
