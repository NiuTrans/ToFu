"""jsdom test: the MCP settings card must surface EXPIRED credentials.

Feature: an Overleaf MCP server can be connected (live subprocess) yet its
stored session cookie has expired, so every real tool call fails. The backend
credential probe reports ``cred_health.status === 'expired'``; the settings
card must then (a) render an amber "Credentials expired" badge instead of the
green ON badge, and (b) offer an "Update credentials" action that opens the
reinstall modal — rather than silently showing a healthy green card.

Harness renders the REAL shipped static/js/settings/mcp.js ``_renderMcpCatalog``
over jsdom. Skips cleanly when node + jsdom aren't installed.

NEGATIVE CONTROL (patches a COPY; shipped file stays byte-identical):
  • drop the credExpired branch → the card renders the green ON badge and no
    update-credentials button, proving the expired-detection code is what
    surfaces the warning.
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
  '<!DOCTYPE html><body><div id="mcpCatalogGrid"></div></body>',
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
// Silence the breaker refresh timers the render schedules.
win._mcpScheduleBreakerRefresh = global._mcpScheduleBreakerRefresh = function(){};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // settings/mcp.js (maybe patched)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _renderMcpCatalog !== 'function') {
  console.log('FAIL fn_present _renderMcpCatalog not defined');
  console.log(out.join('\n'));
  process.exit(0);
}
check('fn_present', true);

// The render pulls from the module-scope _mcpCatalog / filters. Assign via the
// eval'd scope by re-declaring on window is not enough (var hoisting), so set
// the state through the functions the file exposes.
_mcpScope = 'all';
_mcpActiveCategory = 'all';
_mcpSearchQuery = '';

// ── Case A: connected + expired credentials ──
_mcpCatalog = [{
  id: 'overleaf', name: 'Overleaf', description: 'LaTeX', category: 'Science & Research',
  installed: true, connected: true, tools_count: 18, env_specs: [{ key: 'OVERLEAF_SESSION', required: true }],
  cred_health: { status: 'expired', checked_at: 0, detail: 'session cookie has expired' },
}];
_renderMcpCatalog();
let grid = document.getElementById('mcpCatalogGrid');
let html = grid.innerHTML;
check('expired_badge_shown', html.indexOf('mcp.credExpired') !== -1);
check('expired_status_class', html.indexOf('mcp-app-status cred-expired') !== -1);
check('update_creds_button', html.indexOf('mcp.updateCreds') !== -1);
// The green ON badge must NOT appear for an expired card.
check('no_green_on_badge', html.indexOf('mcp-app-status on') === -1);
// Card carries the cred-expired modifier for the amber edge.
check('card_cred_expired_class', html.indexOf('mcp-app-card connected cred-expired') !== -1);

// ── Case B: connected + healthy credentials → green ON, no warning ──
_mcpCatalog = [{
  id: 'overleaf', name: 'Overleaf', description: 'LaTeX', category: 'Science & Research',
  installed: true, connected: true, tools_count: 18, env_specs: [{ key: 'OVERLEAF_SESSION', required: true }],
  cred_health: { status: 'ok', checked_at: 0, detail: '' },
}];
_renderMcpCatalog();
html = document.getElementById('mcpCatalogGrid').innerHTML;
check('healthy_green_on', html.indexOf('mcp-app-status on') !== -1);
check('healthy_no_expired_badge', html.indexOf('mcp.credExpired') === -1);
check('healthy_no_update_button', html.indexOf('mcp.updateCreds') === -1);

// ── Case C: no cred_health at all (older server / not probed) → green ON ──
_mcpCatalog = [{
  id: 'overleaf', name: 'Overleaf', description: 'LaTeX', category: 'Science & Research',
  installed: true, connected: true, tools_count: 18, env_specs: [],
}];
_renderMcpCatalog();
html = document.getElementById('mcpCatalogGrid').innerHTML;
check('noprobe_green_on', html.indexOf('mcp-app-status on') !== -1);
check('noprobe_no_expired_badge', html.indexOf('mcp.credExpired') === -1);

console.log(out.join('\n'));
"""


def _run(js_path: str) -> str:
    harness = os.path.join(HERE, '_mcp_cred_expired_harness.js')
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
def test_expired_credentials_surface_in_card():
    output = _run(_MCP_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'MCP cred-expired render failures:\n' + output
    assert output.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{output}'
    print(output)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_without_expired_branch_shows_green():
    """Patch a COPY so credExpired is forced false — the expired card must then
    render the green ON badge and no update button, proving the expired branch
    is load-bearing."""
    with open(_MCP_SRC, encoding='utf-8') as f:
        src = f.read()
    needle = "var credExpired = connected && e.cred_health && e.cred_health.status === 'expired';"
    assert needle in src, 'anchor for neuter not found — did the fix change shape?'
    patched = src.replace(needle, 'var credExpired = false;', 1)
    assert patched != src, 'neuter did not modify the source'
    tmp = os.path.join(HERE, '_mcp_neutered.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(patched)
    try:
        output = _run(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # With credExpired forced false, the expired card falls through to green ON.
    assert 'FAIL expired_badge_shown' in output or 'FAIL no_green_on_badge' in output, (
        'neutered build unexpectedly still surfaced the expired warning — the '
        'assertion is not load-bearing:\n' + output)
    with open(_MCP_SRC, encoding='utf-8') as f:
        assert f.read() == src, 'shipped settings/mcp.js must be byte-identical'


if __name__ == '__main__':
    print(_run(_MCP_SRC))
