"""jsdom test: the MCP settings card's per-tool toggle panel.

Epic pt_53065dbe86bb4286: a connected server card's tools badge is now a
BUTTON that expands a checkbox list (checked = offered to the model);
toggling a row PUTs the FULL disabled list to the backend, and the badge
shows enabled/total once anything is disabled.

Harness renders the REAL shipped static/js/settings/mcp.js over jsdom with
a fake Api (recording the PUT payload). Skips cleanly without node+jsdom.

NEGATIVE CONTROL (patches a COPY; shipped file stays byte-identical):
  • drop the panel-render call → an expanded card shows no checkbox rows,
    proving the expansion code is what surfaces the list.
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
_MCP_SRC = os.path.join(JS_DIR, 'settings', 'mcp.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><span id="mcpToolCount"></span><div id="mcpCatalogGrid"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
win.Icon = global.Icon = (name, size) => `<svg data-icon="${name}" width="${size||14}"></svg>`;
win.t = global.t = (k) => k;
win.debugLog = global.debugLog = function(){};
win.showAlert = global.showAlert = function(){};
win._mcpScheduleBreakerRefresh = global._mcpScheduleBreakerRefresh = function(){};

// Fake Api: records the PUT payload so the test can assert the exact
// disabled list the toggle computed.
const putCalls = [];
global.Api = win.Api = {
  mcp: {
    toolsListForServer: async () => ({ ok: true, json: async () => ({ tools: [] }) }),
    serverToolsSet: async (server, disabled) => {
      putCalls.push({ server, disabled });
      return { ok: true };
    },
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // settings/mcp.js (maybe patched)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
if (typeof _renderMcpCatalog !== 'function') {
  console.log('FAIL fn_present _renderMcpCatalog not defined');
  console.log(out.join('\n'));
  process.exit(0);
}
check('fn_present', true);

_mcpScope = 'all';
_mcpActiveCategory = 'all';
_mcpSearchQuery = '';

// ── Case A: connected card with one disabled tool ──
_mcpCatalog = [{
  id: 'hope', name: 'Hope', description: 'jobs', category: 'DevOps',
  installed: true, connected: true, tools_count: 10,
  disabled_tools: ['stop_job'],
}];
_renderMcpCatalog();
let html = document.getElementById('mcpCatalogGrid').innerHTML;
check('badge_is_button', html.indexOf('<button class="mcp-app-tools-count mcp-tools-toggle"') !== -1);
check('badge_shows_enabled_of_total', html.indexOf('mcp.toolsCountOf') !== -1);

// ── Case B: expand the panel → checkbox rows from the cache ──
_mcpToolsCache['hope'] = [
  { name: 'submit_job', description: 'submit', enabled: true },
  { name: 'stop_job', description: 'stop', enabled: false },
];
_mcpToolsOpen['hope'] = true;
_renderMcpCatalog();
html = document.getElementById('mcpCatalogGrid').innerHTML;
check('panel_rendered', html.indexOf('mcp-tool-panel') !== -1);
check('row_submit_present', html.indexOf('submit_job') !== -1);
check('row_stop_unchecked', /<input type="checkbox" onchange[^>]*stop_job/.test(html));
check('enabled_header', html.indexOf('mcp.toolsEnabledOf') !== -1);

// ── Case C: toggling a row PUTs the FULL disabled list ──
await _mcpToggleTool('hope', 'submit_job', false);
check('put_called_once', putCalls.length === 1);
// Set comparison: the disabled list's semantics are a set (the backend
// persists sorted(set(...))), so row order in the payload must not matter.
const sameSet = (a, b) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());
check('put_full_list', putCalls.length === 1 &&
  sameSet(putCalls[0].disabled, ['stop_job', 'submit_job']));
check('catalog_entry_updated', sameSet(_mcpCatalog[0].disabled_tools,
  ['stop_job', 'submit_job']));

// ── Case D: re-enabling removes the entry from the disabled list ──
await _mcpToggleTool('hope', 'stop_job', true);
check('reenable_put', putCalls.length === 2 &&
  sameSet(putCalls[1].disabled, ['submit_job']));

console.log(out.join('\n'));
})();
"""


def _run(js_path: str) -> str:
    harness = os.path.join(HERE, '_mcp_tool_toggle_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_path, ROOT],
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_tool_toggle_panel_and_put_payload():
    output = _run(_MCP_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'MCP per-tool toggle failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_without_panel_render_expanded_card_shows_nothing():
    """Patch a COPY so the panel render call is dropped — the expanded card
    must then show NO checkbox rows, proving the expansion branch is
    load-bearing."""
    with open(_MCP_SRC, encoding='utf-8') as f:
        src = f.read()
    needle = "      html += _renderMcpToolPanel(e);"
    assert needle in src, 'anchor for neuter not found — did the panel wiring change shape?'
    patched = src.replace(needle, "      html += '';", 1)
    assert patched != src, 'neuter did not modify the source'
    tmp = os.path.join(HERE, '_mcp_tool_toggle_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(patched)
    try:
        output = _run(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    assert 'FAIL panel_rendered' in output, (
        'neutered build still rendered the tool panel — the assertion is not '
        'load-bearing:\n' + output)
    with open(_MCP_SRC, encoding='utf-8') as f:
        assert f.read() == src, 'shipped settings/mcp.js must be byte-identical'


if __name__ == '__main__':
    print(_run(_MCP_SRC))
