"""Unrelated diagnostic output survives an in-flight relay HTTP request."""
from pathlib import Path
import subprocess


def test_console_evidence_during_pending_relay_is_retained():
    source = Path(__file__).resolve().parents[2] / 'frontend/src/runtime/sections/core/client_log_relay.js'
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let finish;
let connected = true;
const requests = [];
const scope = {};
const context = {
  pushGetLatency: () => ({ connected }),
  runtimeScope: scope, window: { addEventListener() {} }, document: {}, location: { href: '' },
  localStorage: { getItem() { return null; } }, navigator: { onLine: true },
  console: { log() {}, info() {}, warn() {}, error() {} },
  clientLogFlushBaseDelayMs: () => 15000,
  createClientLogFlushScheduler: ports => ({ demand() {}, destroy() {}, flushNow: () => ports.flush() }),
  Api: { logs: { clientRelay(payload) { requests.push(JSON.parse(payload));
    return new Promise(resolve => { finish = resolve; }); } } },
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
  context.console.info('first');
  scope.__clientLogRelay.flush();
  context.console.warn('[browser-responsiveness] recovered stall');
  context.console.error('/api/v4/logs/client recursion');
  assert.equal(scope.__clientLogRelay._buf.length, 1);
  assert.match(scope.__clientLogRelay._buf[0].msg, /recovered stall/);
  finish();
  await new Promise(resolve => setImmediate(resolve));
  connected = false;
  scope.__clientLogRelay.flush();
  assert.equal(requests.length, 1);
  scope.__clientLogRelay.flush({ urgent: true });
  assert.equal(requests.length, 2);
  assert.match(requests[1].entries[0].msg, /recovered stall/);
  finish();
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run(['node', '-e', script, str(source)], check=True, timeout=15)
