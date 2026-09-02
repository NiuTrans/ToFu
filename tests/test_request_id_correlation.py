"""Request correlation joins the Vite transport to backend log context."""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
TRANSPORT = ROOT / 'frontend/src/api/transport.ts'
PUSH_RUNTIME = ROOT / 'frontend/src/runtime/sections/push.js'
SERVER = ROOT / 'server.py'
ESBUILD = ROOT / 'scripts' / 'vite_test_bundle.mjs'


@pytest.fixture(scope='module')
def transport_capture(tmp_path_factory):
    if not shutil.which('node') or not ESBUILD.is_file():
        pytest.skip('node + esbuild unavailable')
    output = tmp_path_factory.mktemp('request-id') / 'transport.cjs'
    compiled = subprocess.run(
        [str(ESBUILD), str(TRANSPORT), '--bundle', '--format=cjs',
         '--platform=node', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    harness = r"""
global.window = global;
global.location = { pathname: '/' };
global.document = { getElementById: () => ({
  textContent: JSON.stringify({ entry: 'main' }),
}) };
global.sessionStorage = { getItem: () => null, setItem: () => {} };
const captured = [];
global.fetch = async (url, init) => {
  captured.push({ url, headers: Object.assign({}, init.headers) });
  return {
    ok: true, status: 200,
    headers: { get: (key) => String(key).toLowerCase() === 'content-type'
      ? 'application/json' : null },
    text: async () => '{"ok":true}',
  };
};
const transport = require(BUILT);
(async () => {
  await transport.request('/api/one');
  await transport.request('/api/two', { method: 'POST', json: { x: 1 } });
  await transport.request('/api/three');
  await transport.request('/api/four', {
    headers: { 'X-Request-ID': 'caller-supplied' },
  });
  console.log(JSON.stringify({
    pageId: transport.pageRequestId(),
    rids: captured.map((item) => item.headers['X-Request-ID'] || null),
  }));
})().catch((error) => { console.error(error); process.exit(3); });
""".replace('BUILT', json.dumps(str(output)))
    executed = subprocess.run(
        ['node', '-e', harness], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    return json.loads(executed.stdout.strip().splitlines()[-1])


def test_request_chokepoint_sets_request_id_header(transport_capture):
    assert len(transport_capture['rids']) == 4
    assert all(transport_capture['rids'][:3])


def test_caller_supplied_request_id_is_not_clobbered(transport_capture):
    assert transport_capture['rids'][3] == 'caller-supplied'


def test_request_ids_share_page_prefix_and_are_unique(transport_capture):
    page_id = transport_capture['pageId']
    automatic = transport_capture['rids'][:3]
    assert page_id
    assert len(set(automatic)) == len(automatic)
    for request_id in automatic:
        assert request_id.startswith(page_id + '-')
        assert re.fullmatch(r'[a-z0-9]+-\d+', request_id)


def test_push_socket_carries_stable_page_observer_id():
    if not shutil.which('node'):
        pytest.skip('node unavailable')
    harness = r"""
global.window = { location: { protocol: 'https:', host: 'tofu.test' } };
var runtimeScope = global;
global.Api = { pageRequestId: () => 'page-observer' };
global.apiUrl = (path) => path;
global.WebSocket = class WebSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  constructor(url) { global.capturedUrl = url; }
  send() {}
  close() {}
};
""" + PUSH_RUNTIME.read_text(encoding='utf-8') + r"""
pushConnect();
console.log(JSON.stringify({
  requestId: pushSocketRequestId(),
  url: global.capturedUrl,
}));
"""
    executed = subprocess.run(
        ['node', '-e', harness], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    captured = json.loads(executed.stdout.strip().splitlines()[-1])
    assert captured == {
        'requestId': 'page-observer-ws1',
        'url': 'wss://tofu.test/api/push?_rid=page-observer-ws1',
    }


def test_errors_carry_request_ids_to_the_user_surface():
    source = TRANSPORT.read_text(encoding='utf-8')
    assert 'clientRequestId: requestId' in source
    assert "response.headers.get('X-Request-ID')" in source
    assert 'serverRequestId || requestId' in source
    assert "'[Api] %s %s failed: %s [rid=%s]'" in source
    assert "'[Api] %s [rid=%s]'" in source


def test_backend_prefers_inbound_request_id():
    from lib.log import resolve_inbound_rid
    assert resolve_inbound_rid('client-mint-42') == 'client-mint-42'
    minted = resolve_inbound_rid(None)
    assert minted and minted != 'client-mint-42'


def test_backend_wires_the_resolver_into_the_request_lifecycle():
    middleware = ROOT / 'lib/http_request_lifecycle.py'
    source = middleware.read_text(encoding='utf-8')
    tree = ast.parse(source)
    handler = next(
        (node for node in ast.walk(tree)
         if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
         and node.name == 'assign_request_id_and_log'),
        None,
    )
    assert handler is not None
    called = set()
    for node in ast.walk(handler):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    assert {'set_req_id'} <= called
    assert {'_resolve_inbound_rid', 'resolve_inbound_rid'} & called
    assert re.search(r"response\.headers\[['\"]X-Request-ID['\"]\]\s*=\s*rid", source)
    assert 'configure_application(' in SERVER.read_text(encoding='utf-8')
    assembly_tree = ast.parse(
        (ROOT / 'lib/app_assembly.py').read_text(encoding='utf-8'))
    registration = next(
        (node for node in ast.walk(assembly_tree)
         if isinstance(node, ast.Call)
         and isinstance(node.func, ast.Name)
         and node.func.id == 'register_request_lifecycle'),
        None,
    )
    assert registration is not None
    assert registration.args and isinstance(registration.args[0], ast.Name)
    assert registration.args[0].id == 'app'
