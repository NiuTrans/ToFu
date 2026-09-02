"""User-visible connection state must recover from stale failure signals.

The tests execute the shipped JavaScript under jsdom. They pin two durable UX
contracts only: the canonical Sidecar warning clears after recovery, and a
stale latency sample cannot leave the connection badge looking healthy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path

BACKEND_MONITOR = runtime_section_path('core/backend_offline_monitor.js')
NET_LATENCY = runtime_section_path('net-latency.js')
ZH_I18N = os.path.join(ROOT, 'frontend', 'src', 'i18n', 'locales', 'zh.json')


def _node_deps_available() -> bool:
    return bool(
        shutil.which('node')
        and os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))
    )


_STORAGE_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.AbortSignal = win.AbortSignal || { timeout: () => undefined };

const out = [];
function check(name, condition) {
  out.push((condition ? 'PASS ' : 'FAIL ') + name);
}

// The production prelude owns timers/listeners. This minimal deterministic
// implementation prevents standalone evaluation from registering real work.
global.createLifecycleScope = win.createLifecycleScope = () => ({
  add: () => {},
  listen: () => {},
  timeout: () => 0,
  interval: () => 0,
  dispose: () => {},
});

const messages = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
global.t = win.t = (key, values) => {
  let result = messages[key] || key;
  for (const [name, value] of Object.entries(values || {})) {
    result = result.replaceAll('{' + name + '}', String(value));
  }
  return result;
};

let storageReady = false;
global.Api = win.Api = {
  health: {
    check: async () => ({
      ok: true,
      json: async () => ({ storage: { ready: storageReady } }),
    }),
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));
if (typeof _checkStorageHealth !== 'function') {
  console.log('FAIL canonical_storage_health_function_missing');
  process.exit(0);
}

(async () => {
  await _checkStorageHealth();
  const banner = document.getElementById('storage-warning-banner');
  const html = banner ? banner.innerHTML : '';
  check('banner_shown_when_storage_unready', !!banner);
  check('banner_zh_title', html.includes('存储服务暂时不可用'));
  check('banner_zh_desc', html.includes('正在自动恢复存储连接'));
  check('banner_zh_dismiss', html.includes('>关闭</button>'));
  check('no_database_specific_recovery_advice', !html.includes('PostgreSQL'));

  storageReady = true;
  await _checkStorageHealth();
  check('banner_cleared_on_recovery', !document.getElementById('storage-warning-banner'));
  await _checkStorageHealth();
  check('healthy_check_is_idempotent', !document.getElementById('storage-warning-banner'));
  console.log(out.join('\n'));
})();
"""


_LATENCY_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<span id="netLatencyBadge"><span class="net-bars"></span><span class="net-ms"></span></span>' +
  '</body>',
  { url: 'http://localhost/' },
);
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
global.t = win.t = (key) => key;
global.pushConnect = win.pushConnect = () => {};

const out = [];
function check(name, condition) {
  out.push((condition ? 'PASS ' : 'FAIL ') + name);
}

let renderLatency = null;
global.pushOnLatency = win.pushOnLatency = (listener) => {
  renderLatency = listener;
  return () => {};
};
let watchdog = null;
global.setInterval = win.setInterval = (callback) => {
  watchdog = callback;
  return 1;
};
global.clearInterval = win.clearInterval = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));
const init = win.initNetLatency;
if (typeof init !== 'function') {
  console.log('FAIL latency_initializer_missing');
  process.exit(0);
}
init();
check('latency_subscription_registered', typeof renderLatency === 'function');
check('staleness_watchdog_registered', typeof watchdog === 'function');

const badge = document.getElementById('netLatencyBadge');
renderLatency({ ms: 120, state: 'good', connected: true, at: Date.now() });
check('fresh_sample_paints_good', badge.dataset.state === 'good');
watchdog();
check('fresh_sample_remains_good', badge.dataset.state === 'good');

const realNow = Date.now;
Date.now = () => realNow() + 60000;
try {
  watchdog();
} finally {
  Date.now = realNow;
}
check('stale_sample_forces_offline', badge.dataset.state === 'offline');
console.log(out.join('\n'));
"""


def _run_harness(tmp_path, name: str, source: str, *args: str) -> str:
    harness = tmp_path / name
    harness.write_text(source, encoding='utf-8')
    result = subprocess.run(
        ['node', str(harness), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = result.stdout.strip()
    assert result.returncode == 0, f'node failed: {result.stderr}\n{output}'
    failures = [line for line in output.splitlines() if line.startswith('FAIL')]
    assert not failures, output
    return output


@pytest.mark.skipif(not _node_deps_available(), reason='node + jsdom required')
def test_storage_banner_clears_on_recovery(tmp_path):
    output = _run_harness(
        tmp_path,
        'storage-health.js',
        _STORAGE_HARNESS,
        BACKEND_MONITOR,
        ROOT,
        ZH_I18N,
    )
    assert output.count('PASS') == 7


@pytest.mark.skipif(not _node_deps_available(), reason='node + jsdom required')
def test_net_latency_staleness_watchdog(tmp_path):
    output = _run_harness(
        tmp_path,
        'latency-watchdog.js',
        _LATENCY_HARNESS,
        NET_LATENCY,
        ROOT,
    )
    assert output.count('PASS') == 5
