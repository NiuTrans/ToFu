"""Runtime guards for user-visible OAuth routing diagnostics."""

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


def test_egress_renderer_explains_every_route_state():
    renderer = _renderer_source()
    assert "el.style.display = '';" in renderer
    assert 'preferred_server_route_mode' in renderer
    for diagnostic in (
        'settings.egressChecking',
        'settings.egressDirect',
        'settings.egressViaProxy',
        'settings.egressViaAgent',
        'settings.egressAgentNoCap',
        'settings.egressUnavailable',
    ):
        assert diagnostic in renderer


def test_unknown_routing_state_repolls_silently():
    renderer = _renderer_source()
    assert "egress.state === 'unknown'" in renderer
    assert '_scheduleEgressRepoll()' in renderer


@pytest.mark.skipif(NODE is None, reason='node is required to execute oauth.js')
def test_runtime_paints_direct_proxy_agent_and_failure_states():
    harness = r"""
const fs = require('fs');
globalThis.window = globalThis;
globalThis.addEventListener = function(){};
delete globalThis.BroadcastChannel;
globalThis.t = (k) => k;
globalThis.showAlert = function(){};
const el = { innerHTML: 'stale', textContent: 'stale',
             style: { display: '' }, className: 'stale' };
globalThis.document = { getElementById: (id) => id === 'oauthClaudeEgress' ? el : null };
eval(fs.readFileSync(process.argv[1], 'utf8'));
globalThis._scheduleEgressRepoll = () => { globalThis.repolled = true; };
const out = {};
const cases = {
  direct: { state: 'direct', preferred_server_route: 'direct',
            preferred_server_route_label: 'direct',
            preferred_server_route_mode: 'direct' },
  proxy: { state: 'direct', preferred_server_route: 'pool:hk',
           preferred_server_route_label: 'proxy Hong Kong',
           preferred_server_route_mode: 'proxy' },
  agent: { state: 'agent', agents: [{ agent_id: 'a1', name: 'Laptop' }] },
  agent_no_capability: { state: 'agent_no_capability', agents: [] },
  unavailable: { state: 'unavailable', agents: [] },
};
for (const [name, egress] of Object.entries(cases)) {
  el.innerHTML = 'stale'; el.textContent = 'stale';
  el.style.display = 'none'; el.className = 'stale';
  _renderEgressLine('claude', egress);
  out[name] = { text: el.textContent, display: el.style.display,
                className: el.className };
}
_renderEgressLine('claude', { state: 'unknown' });
out.repolled = globalThis.repolled === true;
out.unknown = { text: el.textContent, display: el.style.display,
                className: el.className };
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
    assert out['direct']['text'] == 'settings.egressDirect'
    assert out['proxy']['text'] == 'settings.egressViaProxy'
    assert out['agent']['text'] == 'settings.egressViaAgent'
    assert out['agent_no_capability']['text'] == 'settings.egressAgentNoCap'
    assert out['unavailable']['text'] == 'settings.egressUnavailable'
    for state in ('direct', 'proxy', 'agent',
                  'agent_no_capability', 'unavailable'):
        assert out[state]['display'] == ''
        assert out[state]['className'].startswith('oauth-egress-line ')
    assert out['repolled'] is True
    assert out['unknown']['text'] == 'settings.egressChecking'
    assert out['unknown']['display'] == ''
