"""Runtime guards for silent, system-owned OAuth routing diagnostics."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
OAUTH_JS = Path(runtime_section_path('settings/oauth.js'))
NODE = shutil.which('node')


def _renderer_source() -> str:
    src = OAUTH_JS.read_text(encoding='utf-8')
    start = src.index('function _renderEgressLine(provider, egress)')
    end = src.index('function _updateOAuthCard(provider, status)', start)
    return src[start:end]


def test_egress_renderer_keeps_diagnostics_hidden_from_normal_users():
    renderer = _renderer_source()
    assert "el.style.display = 'none';" in renderer
    assert "el.innerHTML = '';" in renderer
    for diagnostic in (
        'settings.egressDirect',
        'settings.egressViaAgent',
        'settings.egressAgentNoCap',
        'settings.egressUnavailable',
        'oauth-egress-pin',
    ):
        assert diagnostic not in renderer


def test_unknown_routing_state_repolls_silently():
    renderer = _renderer_source()
    assert "egress.state === 'unknown'" in renderer
    assert '_scheduleEgressRepoll()' in renderer


@pytest.mark.skipif(NODE is None, reason='node is required to execute oauth.js')
def test_runtime_never_paints_routing_diagnostics():
    harness = r"""
const fs = require('fs');
globalThis.window = globalThis;
globalThis.addEventListener = function(){};
delete globalThis.BroadcastChannel;
globalThis.t = (k) => k;
globalThis.showAlert = function(){};
const el = { innerHTML: 'stale', style: { display: '' }, className: 'stale' };
globalThis.document = { getElementById: (id) => id === 'oauthClaudeEgress' ? el : null };
eval(fs.readFileSync(process.argv[1], 'utf8'));
globalThis._scheduleEgressRepoll = () => { globalThis.repolled = true; };
const out = {};
for (const state of ['direct', 'agent', 'agent_no_capability', 'unavailable']) {
  el.innerHTML = 'stale'; el.style.display = ''; el.className = 'stale';
  _renderEgressLine('claude', { state });
  out[state] = { html: el.innerHTML, display: el.style.display };
}
_renderEgressLine('claude', { state: 'unknown' });
out.repolled = globalThis.repolled === true;
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        [NODE, '-e', harness, str(OAUTH_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[:800]
    out = json.loads(proc.stdout)
    for state in ('direct', 'agent', 'agent_no_capability', 'unavailable'):
        assert out[state] == {'html': '', 'display': 'none'}
    assert out['repolled'] is True
