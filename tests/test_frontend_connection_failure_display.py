"""User-visible connection state must recover from stale failure signals.

The tests execute the shipped JavaScript under jsdom. They pin two durable UX
contracts only: the canonical Sidecar warning clears after recovery, and the
connection badge projects explicit Push/SSE health without a second clock.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path


pytestmark = pytest.mark.unit
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

STORAGE_MONITOR = native_module_path(
    '.native/storage-availability-monitor-contract.js',
    Path(ROOT) / 'frontend/src/storage-availability-monitor.ts',
)
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

const messages = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
const translate = (key, values) => {
  let result = messages[key] || key;
  for (const [name, value] of Object.entries(values || {})) {
    result = result.replaceAll('{' + name + '}', String(value));
  }
  return result;
};

let storageReady = false;
let recoveryPoll = null;
const schedule = {
  now: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => {},
  setInterval: (callback) => {
    recoveryPoll = callback;
    return 1;
  },
  clearInterval: () => {
    recoveryPoll = null;
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));
if (typeof createStorageAvailabilityMonitor !== 'function') {
  console.log('FAIL typed_storage_monitor_missing');
  process.exit(0);
}
const monitor = createStorageAvailabilityMonitor({
  document,
  schedule,
  log: { debug: () => {}, info: () => {}, warn: () => {}, error: () => {} },
  warningIconHtml: () => '<svg data-warning-icon></svg>',
  isVisible: () => true,
  probeHealth: async () => ({
    ok: true,
    json: async () => ({ storage: { ready: storageReady } }),
  }),
  copy: {
    unavailableTitle: () => translate('conn.storageUnavailableTitle'),
    unavailableDescription: () => translate('conn.storageUnavailableDesc'),
    dismiss: () => translate('conn.dismiss'),
  },
});

(async () => {
  await monitor.check();
  const banner = document.getElementById('storage-warning-banner');
  const html = banner ? banner.innerHTML : '';
  check('banner_shown_when_storage_unready', !!banner);
  check('banner_zh_title', html.includes('存储服务暂时不可用'));
  check('banner_zh_desc', html.includes('正在自动恢复存储连接'));
  check('banner_zh_dismiss', html.includes('>关闭</button>'));
  check('no_database_specific_recovery_advice', !html.includes('PostgreSQL'));

  storageReady = true;
  await monitor.check();
  check('banner_cleared_on_recovery', !document.getElementById('storage-warning-banner'));
  await monitor.check();
  check('healthy_check_is_idempotent', !document.getElementById('storage-warning-banner'));
  monitor.destroy();
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
let pushUnsubscribed = 0;
global.pushOnLatency = win.pushOnLatency = (listener) => {
  renderLatency = listener;
  return () => { pushUnsubscribed += 1; };
};
let streamHealth = null;
let streamUnsubscribed = 0;
global.streamHealthSubscribe = win.streamHealthSubscribe = (listener) => {
  streamHealth = listener;
  listener({ degraded: false, count: 0, at: Date.now() });
  return () => { streamUnsubscribed += 1; };
};
global.pushGetLatency = win.pushGetLatency = () => ({
  ms: 120, state: 'good', connected: true, at: Date.now(),
});
let intervalCalls = 0;
global.setInterval = win.setInterval = () => {
  intervalCalls += 1;
  return 1;
};
global.clearInterval = win.clearInterval = () => {};
const cleanups = [];
global.retainedCompositionLifecycle = win.retainedCompositionLifecycle = {
  add: cleanup => cleanups.push(cleanup),
};

eval(fs.readFileSync(process.argv[2], 'utf8'));
const init = win.initNetLatency;
if (typeof init !== 'function') {
  console.log('FAIL latency_initializer_missing');
  process.exit(0);
}
init();
check('latency_subscription_registered', typeof renderLatency === 'function');
check('badge_owns_no_second_liveness_clock', intervalCalls === 0);

const badge = document.getElementById('netLatencyBadge');
renderLatency({ ms: 120, state: 'good', connected: true, at: Date.now() });
check('push_sample_paints_good', badge.dataset.state === 'good');
streamHealth({ degraded: true, count: 1, at: Date.now() });
check('sse_degradation_paints_reconnecting',
  badge.dataset.state === 'poor' && badge.querySelector('.net-ms').textContent === 'net.reconnecting');
streamHealth({ degraded: false, count: 0, at: Date.now() });
check('sse_recovery_restores_push_reading', badge.dataset.state === 'good');
renderLatency({ ms: null, state: 'offline', connected: false, at: Date.now() });
check('push_close_event_paints_offline', badge.dataset.state === 'offline');
for (const cleanup of cleanups.reverse()) cleanup();
check('page_teardown_releases_both_subscriptions',
  pushUnsubscribed >= 1 && streamUnsubscribed >= 1);
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
        STORAGE_MONITOR,
        ROOT,
        ZH_I18N,
    )
    assert output.count('PASS') == 7


@pytest.mark.skipif(not _node_deps_available(), reason='node + jsdom required')
def test_net_latency_projects_owned_events_without_a_second_clock(tmp_path):
    output = _run_harness(
        tmp_path,
        'latency-events.js',
        _LATENCY_HARNESS,
        NET_LATENCY,
        ROOT,
    )
    assert output.count('PASS') == 7
