"""The Tofu page carries desktop polls through an authenticated SSO tab."""

import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path(runtime_section_path('local-control.js'))
API_SOURCE = Path(runtime_section_path('api.js'))
MAIN_ENTRY = ROOT / 'frontend' / 'src' / 'main.ts'
FEATURE_ENTRY = ROOT / 'frontend' / 'src' / 'features' / 'local-control.ts'

pytestmark = pytest.mark.unit


def test_relay_contract_is_wired_to_modal_download_and_hash_entry():
    src = SOURCE.read_text(encoding='utf-8')
    api_src = API_SOURCE.read_text(encoding='utf-8')
    main_src = MAIN_ENTRY.read_text(encoding='utf-8')
    feature_src = FEATURE_ENTRY.read_text(encoding='utf-8')
    assert "credentials: 'include'" in api_src
    assert 'Api.desktop.relayPoll' in src
    assert "targetAddressSpace: 'local'" in src
    assert "#tofu-agent-relay" in main_src
    assert "prepareFeature('_lcEnsureAgentRelay')" in main_src
    assert 'AGENT_RELAY_DEEP_LINK_DURATION_MS' in feature_src
    assert "name !== '_lcEnsureAgentRelay'" in feature_src
    assert '立即通过浏览器连接' in src
    assert 'data-tofu-action="_lcEnsureAgentRelay(1800000)"' in src
    assert 'onclick="_lcEnsureAgentRelay(1800000)"' not in src
    assert "target !== expected" in src
    assert "job.headers['X-Bridge-Secret']" in src


def test_page_forwards_one_poll_and_returns_the_exact_response(tmp_path):
    harness = tmp_path / 'relay.js'
    harness.write_text(r"""
const fs = require('fs');
global.window = global;
window.location = {
  href: 'https://lab.example/proxy/15000/',
  origin: 'https://lab.example', hash: ''
};
global.document = { getElementById: () => null };
global.apiUrl = (p) => '/proxy/15000' + p;
global._i18nLang = 'zh';
global.t = (_k) => '';

let localResult = null;
let takeCount = 0;
let localAddressSpace = true;
function response(status, body) {
  return {
    status, ok: status >= 200 && status < 300,
    json: async () => body,
    text: async () => typeof body === 'string' ? body : JSON.stringify(body)
  };
}
global.fetch = async (url, opts = {}) => {
  if (url.endsWith('/v1/take')) {
    localAddressSpace = localAddressSpace && opts.targetAddressSpace === 'local';
    takeCount++;
    if (takeCount === 1) return response(200, {
      id: 'job-1',
      url: 'https://lab.example/proxy/15000/api/desktop/poll',
      payload: {results: [], agent: {agent_id: 'a'}},
      headers: {'X-Bridge-Secret': 'tofu_live_x'}
    });
    return new Promise(() => {}); // keep the worker alive after one cycle
  }
  if (url.endsWith('/v1/result')) {
    localAddressSpace = localAddressSpace && opts.targetAddressSpace === 'local';
    localResult = JSON.parse(opts.body);
    return response(200, {accepted: true});
  }
  if (url === 'https://lab.example/proxy/15000/api/desktop/poll') {
    if (opts.credentials !== 'include') throw new Error('cookies not included');
    if (opts.headers['X-Bridge-Secret'] !== 'tofu_live_x') {
      throw new Error('bridge secret missing');
    }
    return response(200, '{"commands":[{"id":"c1"}]}');
  }
  throw new Error('unexpected URL ' + url);
};
global.Api = {desktop: {relayPoll: (payload, bridgeSecret) => fetch(
  'https://lab.example/proxy/15000/api/desktop/poll', {
    method: 'POST', credentials: 'include',
    headers: {'X-Bridge-Secret': bridgeSecret},
    body: JSON.stringify(payload || {})
  })}};

eval(fs.readFileSync(process.argv[2], 'utf8'));
_lcAgentRelayLoop('http://127.0.0.1:15180');
setTimeout(() => {
  const ok = localResult && localResult.id === 'job-1' &&
    localResult.status === 200 &&
    localResult.body === '{"commands":[{"id":"c1"}]}' &&
    _lcAgentRelay.state === 'connected' && localAddressSpace;
  console.log(ok ? 'PASS' : 'FAIL ' + JSON.stringify({localResult,
    state:_lcAgentRelay.state, localAddressSpace}));
  process.exit(ok ? 0 : 1);
}, 40);
""", encoding='utf-8')
    proc = subprocess.run(['node', str(harness), str(SOURCE)],
                          capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'PASS' in proc.stdout
