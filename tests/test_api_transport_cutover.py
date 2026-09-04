"""The main application has one required Vite API transport owner."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import pytest

from tests._runtime_sections import runtime_section, runtime_section_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_retained_registry_imports_one_required_transport_owner():
    registry = runtime_section('api.js')
    main = (ROOT / 'frontend/src/main.ts').read_text(encoding='utf-8')
    runtime = (ROOT / 'frontend/src/runtime/app-runtime.js').read_text(
        encoding='utf-8')
    transport = (ROOT / 'frontend/src/api/transport.ts').read_text(encoding='utf-8')

    assert "apiTransport as requiredApiTransport" in runtime
    assert 'const _transportOwner = requiredApiTransport;' in registry
    assert 'return _transportOwner.request(path, opts || {});' in registry
    assert 'apiTransport' not in main
    assert 'window.TofuModules = Object.freeze({' in main
    assert 'export const apiTransport' in transport
    assert 'resolvePath,' in transport
    assert "const folders = {" in registry
    assert "const artifacts = {" in registry


def test_server_and_vite_tags_have_no_runtime_transport_fallback():
    from lib.vite_assets import VITE_MANIFEST, _dev_tags, _manifest_tags

    route_source = (ROOT / 'routes/common.py').read_text(encoding='utf-8')
    assert '_api_transport_bootstrap' not in route_source
    assert '__TOFU_API_FALLBACK' not in route_source

    dev_tags = _dev_tags('http://127.0.0.1:5173', 'main')
    with open(VITE_MANIFEST, encoding='utf-8') as handle:
        prod_tags = _manifest_tags(json.load(handle))
    for tags in (dev_tags, prod_tags):
        assert 'type="module"' in tags
        assert 'modules-failed' not in tags
        assert '__TOFU_VITE_FAILED__' not in tags
        assert 'onerror=' not in tags


def test_registry_has_no_transport_rollback_or_dom_injection():
    registry = runtime_section('api.js')
    assert 'transport-vite-adapter.js' not in registry
    assert 'document.createElement' not in registry
    assert '@standalone-transport' not in registry
    assert 'function _nativeTransport' not in registry
    assert 'class ApiError' not in registry
    assert 'await fetch(' not in registry
    assert 'TofuModules' not in registry
    assert not (ROOT / 'static/js/api/transport-vite-adapter.js').exists()


def test_read_only_resolvers_recover_only_unattributed_gateway_failures():
    if not shutil.which("node"):
        pytest.skip("node unavailable")
    registry_path = runtime_section_path("api.js", scope_prelude=False)
    harness = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
global.window = globalThis;
global.runtimeScope = globalThis;
global.sessionStorage = {getItem() { return null; }, setItem() {}, removeItem() {}};
global.console = {log() {}, debug() {}, error() {}, info() {}, warn() {}};

let fakeNow = 0;
const delays = [];
Date.now = () => fakeNow;
global.setTimeout = (callback, delay) => {
  delays.push(delay);
  fakeNow += delay;
  queueMicrotask(callback);
  return delays.length;
};
global.clearTimeout = () => {};

class ApiError extends Error {
  constructor(message, detail = {}) { super(message); Object.assign(this, detail); }
}
let mode = 'gateway-recovers';
let calls = 0;
const attemptTimeouts = [];
global.requiredApiTransport = {
  ApiError,
  resolvePath: (path) => path,
  pageRequestId: 'resolver-recovery-test',
  bindTaskAffinity() {},
  newIdempotencyKey: () => 'idem',
  taskStartAffinityOptions: (_body, options) => options,
  request: async (path, options) => {
    if (!path.includes('/conversations/') || !path.endsWith('/resolve')) return {};
    calls += 1;
    attemptTimeouts.push(options.timeout);
    if (mode === 'gateway-recovers' && calls < 3) {
      throw new ApiError('forwarded-port proxy 500', {
        status: 500, code: 'http_500', serverRequestId: null,
      });
    }
    if (mode === 'backend-500') {
      throw new ApiError('backend failure', {
        status: 500, code: 'internal_error', serverRequestId: 'server-rid',
      });
    }
    if (mode === 'network-exhausts') {
      throw new ApiError('offline', {status: 0, code: 'network'});
    }
    return {ok: true, model: 'resolved'};
  },
};

eval(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  const recovered = await runtimeScope.Api.conversations.resolveConfig({model: 'm'});
  assert.deepEqual(recovered, {ok: true, model: 'resolved'});
  assert.equal(calls, 3);
  assert.deepEqual(delays, [250, 500]);

  mode = 'backend-500';
  calls = 0;
  const delayCount = delays.length;
  await assert.rejects(
    runtimeScope.Api.conversations.resolveSettings({conv_settings: {}, overrides: {}}),
    (error) => error.serverRequestId === 'server-rid',
  );
  assert.equal(calls, 1);
  assert.equal(delays.length, delayCount);

  mode = 'network-exhausts';
  calls = 0;
  fakeNow = 0;
  const delayStart = delays.length;
  await assert.rejects(
    runtimeScope.Api.conversations.resolveConfig({model: 'm'}),
    (error) => error.code === 'network',
  );
  const boundedDelays = delays.slice(delayStart);
  assert.ok(calls > 1 && calls <= 20);
  assert.ok(boundedDelays.reduce((total, delay) => total + delay, 0) <= 75000);
  assert.ok(attemptTimeouts.every((timeout) => timeout > 0 && timeout <= 10000));
  process.stdout.write(JSON.stringify({recoveredCalls: 3, boundedCalls: calls}));
})().catch((error) => {
  process.stderr.write(String(error && error.stack || error));
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        ["node", "-e", harness, registry_path],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    evidence = json.loads(result.stdout)
    assert evidence["recoveredCalls"] == 3
    assert 1 < evidence["boundedCalls"] <= 20
