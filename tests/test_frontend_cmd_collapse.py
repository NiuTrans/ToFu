"""tests/test_frontend_cmd_collapse.py — collapsible run_command/code_exec body.

The render contract (owner directive, 2026-08-01): users read the one-line
DESCRIPTION, not the exact shell string — so when a command card carries a
description AND the command is long enough to be visual noise (multi-line or
> 100 chars), the ``$ command`` <pre> starts COLLAPSED behind the description
itself (click to expand in place — no chevron glyph since 2026-08-02).
Pinned behaviours:

  1. Done block: description + long/multi-line command ⇒ collapsed markup
     (``ptool-cmd-collapsible`` pre + ``ptool-cmd-desc-toggle`` header), and
     the command text is still IN the DOM (hidden via class, not dropped).
  2. Short one-liner (``npm test``) ⇒ stays visible — collapsing saves
     nothing and would add a click to a glanceable row.
  3. No description ⇒ stays visible — the command is the card's only
     identity; collapsing would anonymize it.
  4. Running block: same collapse rule; the live-output pane stays visible
     regardless (it is outside the collapsed pre).
  5. ``_cmdBodyToggle`` flips ``cmd-open`` on the block and PERSISTS the
     choice in ``_cmdBodyExpanded`` keyed by toolCallId, so a mid-run expand
     survives the per-progress re-renders and a done card survives a
     timeline sync.
  6. code_exec gets the identical treatment (it renders through the same
     blocks since pt_0bde0fd8).

Loads the REAL shipped tool_rounds.js under jsdom; skips when node+jsdom are
absent (same convention as test_frontend_cmd_interrupt_button.py).
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
from _runtime_sections import runtime_section_path, shipped_source_text  # noqa: E402

TOOL_ROUNDS = runtime_section_path('ui/tool_rounds.js')

LONG_CMD = "grep -c 'jsonify(' routes/api_v1/browser.py && python3 - <<'EOF'\nimport pathlib\nprint('baseline tightened')\nEOF"


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const LONG_CMD = process.argv[4];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
global.window = dom.window; global.document = dom.window.document;
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
global.t = (k, d) => (d || k);
global.Icon = global.window.Icon = (name) => '<svg></svg>';
global.renderMarkdown = (s) => s;
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/tool_rounds.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── 1. Done: description + long multi-line command ⇒ collapsed ──
const doneLong = {
  status: 'done', toolName: 'run_command', query: LONG_CMD,
  toolCallId: 'call-long-1', roundNum: 4,
  results: [{ command: LONG_CMD, output: '3\nbaseline tightened',
              exitCode: '0', description: 'count sites and run heredoc' }],
};
const html1 = _renderUnifiedToolLine(doneLong, false);
check('done_long_collapsible_pre', html1.includes('ptool-cmd-collapsible'));
check('done_long_desc_toggle', html1.includes('ptool-cmd-desc-toggle'));
check('done_long_toggle_onclick', html1.includes('_cmdBodyToggle(this,event)'));
check('done_long_no_chevron', !html1.includes('ptool-cmd-chev'));
check('done_long_cmd_key', html1.includes('data-cmd-key="call-long-1"'));
check('done_long_not_open', !html1.includes('ptool-cmd-block ptool-cmd-ok cmd-open'));
check('done_long_cmd_still_in_dom', html1.includes('baseline tightened')); // output AND/OR cmd
check('done_long_cmd_pre_kept', html1.includes('$ grep -c'));
check('done_long_output_toggle_kept', html1.includes('ptool-cmd-hasoutput')
  && html1.includes('data-tofu-action="_cmdHeaderToggle(this,event)"')
  && html1.includes('role="button"'));

// ── 2. Done: short one-liner ⇒ stays visible ──
const doneShort = {
  status: 'done', toolName: 'run_command', query: 'npm test',
  toolCallId: 'call-short-1', roundNum: 5,
  results: [{ command: 'npm test', output: 'ok', exitCode: '0',
              description: 'run tests' }],
};
const html2 = _renderUnifiedToolLine(doneShort, false);
check('done_short_not_collapsed', !html2.includes('ptool-cmd-collapsible'));
check('done_short_no_toggle', !html2.includes('ptool-cmd-desc-toggle'));
check('done_short_pre_visible', html2.includes('<pre class="ptool-cmd-code"><code>$ npm test</code></pre>'));

// ── 3. Done: NO description + long command ⇒ stays visible (identity) ──
const doneNoDesc = {
  status: 'done', toolName: 'run_command', query: LONG_CMD,
  toolCallId: 'call-nodesc-1', roundNum: 6,
  results: [{ command: LONG_CMD, output: 'x', exitCode: '0' }],
};
const html3 = _renderUnifiedToolLine(doneNoDesc, false);
check('done_nodesc_not_collapsed', !html3.includes('ptool-cmd-collapsible'));
check('done_nodesc_no_toggle', !html3.includes('ptool-cmd-desc-toggle'));

// ── 4. Running: same collapse rule; live output stays visible ──
const runLong = {
  status: 'searching', toolName: 'run_command', query: LONG_CMD,
  toolCallId: 'call-run-1', roundNum: 7, tStart: Date.now() - 5000,
  toolArgs: JSON.stringify({ description: 'heredoc pipeline' }),
  _partialOutput: 'line1\nline2',
  results: [],
};
const html4 = _renderUnifiedToolLine(runLong, true);
check('running_long_collapsible_pre', html4.includes('ptool-cmd-collapsible'));
check('running_long_desc_toggle', html4.includes('ptool-cmd-desc-toggle'));
check('running_live_output_visible', html4.includes('ptool-cmd-output-live'));

// ── 5. Toggle flips cmd-open + persists via toolCallId across re-renders ──
const host = document.createElement('div');
host.innerHTML = html1;
const descEl = host.querySelector('.ptool-cmd-desc-toggle');
check('toggle_el_present', !!descEl);
_cmdBodyToggle(descEl, { stopPropagation() {} });
const blockEl = host.querySelector('.ptool-cmd-block');
check('toggle_adds_cmd_open', blockEl.classList.contains('cmd-open'));
// Persistence is keyed by toolCallId inside the module — assert it
// BEHAVIOURALLY: a re-render (sync / progress tick) restores the state.
const html1b = _renderUnifiedToolLine(doneLong, false);  // re-render (sync/progress)
check('rerender_restores_open', html1b.includes('ptool-cmd-ok cmd-open'));
_cmdBodyToggle(descEl, { stopPropagation() {} });
check('untoggle_removes_cmd_open', !blockEl.classList.contains('cmd-open'));
const html1c = _renderUnifiedToolLine(doneLong, false);
check('rerender_collapses_again', !html1c.includes('ptool-cmd-ok cmd-open'));

// ── 5b. Output pane toggle: the command HEADER is the ARIA button and its
// named action is `_cmdHeaderToggle` (no `var`, no ternaries in the restricted
// data-tofu-action grammar). The output pane visibility rides `output-open` on
// the block and `aria-expanded` on the header. ──
const outHeader = host.querySelector('.ptool-cmd-header[role="button"]');
check('output_toggle_el_present', !!outHeader);
check('output_toggle_named_action', html1.includes('data-tofu-action="_cmdHeaderToggle(this,event)"'));
check('output_toggle_no_inline_var', !html1.includes('var w=this.parentElement'));
_cmdHeaderToggle(outHeader, { stopPropagation() {}, target: {} });
const outBlock = host.querySelector('.ptool-cmd-block');
check('output_toggle_expands', outBlock.classList.contains('output-open'));
check('output_toggle_aria_expanded', outHeader.getAttribute('aria-expanded') === 'true');
_cmdHeaderToggle(outHeader, { stopPropagation() {}, target: {} });
check('output_toggle_collapses', !outBlock.classList.contains('output-open'));
check('output_toggle_aria_collapsed', outHeader.getAttribute('aria-expanded') === 'false');

// Search/timer disclosure rows contain nested source/copy controls, so they
// use an ARIA button rather than invalid nested <button> markup. The shared
// document listener must supply the native Enter/Space activation contract.
const disclosure = document.createElement('div');
disclosure.className = 'ptool-results-header';
disclosure.setAttribute('role', 'button');
disclosure.tabIndex = 0;
let disclosureClicks = 0;
disclosure.addEventListener('click', () => { disclosureClicks += 1; });
document.body.appendChild(disclosure);
disclosure.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
  key: 'Enter', bubbles: true,
}));
disclosure.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
  key: ' ', bubbles: true,
}));
check('aria_disclosure_keyboard_activation', disclosureClicks === 2);

// ── 6. code_exec: identical treatment ──
const ceLong = {
  status: 'done', toolName: 'code_exec', query: LONG_CMD,
  toolCallId: 'call-ce-1', roundNum: 8,
  results: [{ command: LONG_CMD, output: 'x', exitCode: '0',
              description: 'sandbox pipeline' }],
};
const html6 = _renderUnifiedToolLine(ceLong, false);
check('code_exec_collapsible', html6.includes('ptool-cmd-collapsible'));
check('code_exec_desc_toggle', html6.includes('ptool-cmd-desc-toggle'));

console.log(out.join('\n'));
// Exit explicitly: tool_rounds.js / jsdom may leave a timer handle open.
process.exit(0);
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_cmd_collapse_contract():
    harness = os.path.join(HERE, '_cmd_collapse_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             TOOL_ROUNDS,  # argv[2]
             ROOT,                                          # argv[3]
             LONG_CMD],                                     # argv[4]
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
    assert not fails, 'cmd-collapse render/toggle failures:\n' + output
    assert output.count('PASS') >= 20, f'expected >=20 PASS lines, got:\n{output}'


@pytest.mark.unit
def test_cmd_collapse_css_rules_present():
    """Guard the static half: the collapse classes must have real rules in
    styles.css (a render-side class with no rule silently never hides)."""
    css = shipped_source_text('frontend/src/styles/application/09-tool-terminal-details.css')
    assert '.ptool-cmd-collapsible' in css
    assert '.ptool-cmd-block.cmd-open .ptool-cmd-collapsible' in css
    assert '.ptool-cmd-chev' not in css  # chevron glyph removed (owner call 2026-08-02)
    assert '.ptool-cmd-desc-toggle' in css


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
