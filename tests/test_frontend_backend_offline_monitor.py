"""Behavior contract for the typed backend/storage availability owners.

The backend verdict requires two consecutive failed liveness probes. Push and
browser events are suspicion signals only; proxy authentication denial is not
an outage. The storage readiness warning is deliberately independent and owns
one visibility-aware, self-stopping recovery poll.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path, runtime_section_names


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
BACKEND_SOURCE = ROOT / "frontend/src/backend-availability-monitor.ts"
STORAGE_SOURCE = ROOT / "frontend/src/storage-availability-monitor.ts"
COORDINATOR_SOURCE = ROOT / "frontend/src/availability-health-probe.ts"
BACKEND_BUNDLE = native_module_path(
    "backend-availability-monitor.js", BACKEND_SOURCE,
)
STORAGE_BUNDLE = native_module_path(
    "storage-availability-monitor.js", STORAGE_SOURCE,
)
COORDINATOR_BUNDLE = native_module_path(
    "availability-health-probe.js", COORDINATOR_SOURCE,
)
HAS_BROWSER_DEPS = bool(
    shutil.which("node")
    and (ROOT / "node_modules/jsdom/package.json").is_file()
)


_BACKEND_HARNESS = r"""
const fs = require('fs');
const {JSDOM} = require('jsdom');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const scenario = process.argv[2];

function createScheduler() {
  let now = 2_000_000;
  let nextHandle = 1;
  const timers = new Map();
  const create = (kind, callback, delayMs) => {
    const handle = nextHandle++;
    timers.set(handle, {kind, callback, delayMs, active: true});
    return handle;
  };
  const clear = (handle) => {
    const timer = timers.get(handle);
    if (timer) timer.active = false;
  };
  const fire = (kind, delayMs) => {
    const match = [...timers.values()].reverse().find(
      (timer) => timer.active && timer.kind === kind && timer.delayMs === delayMs,
    );
    if (!match) return false;
    if (kind === 'timeout') match.active = false;
    match.callback();
    return true;
  };
  return {
    port: {
      now: () => now,
      setTimeout: (callback, delayMs) => create('timeout', callback, delayMs),
      clearTimeout: clear,
      setInterval: (callback, delayMs) => create('interval', callback, delayMs),
      clearInterval: clear,
    },
    advance: (milliseconds) => { now += milliseconds; },
    fireTimeout: (delayMs) => fire('timeout', delayMs),
    fireInterval: (delayMs) => fire('interval', delayMs),
    active: (kind, delayMs) => [...timers.values()].some(
      (timer) => timer.active && timer.kind === kind && timer.delayMs === delayMs,
    ),
  };
}

function createEnvironment() {
  const dom = new JSDOM('<!doctype html><title>Tofu</title><body></body>', {
    url: 'http://tofu.test/',
  });
  Object.defineProperty(dom.window.document, 'visibilityState', {
    configurable: true,
    value: 'visible',
  });
  const scheduler = createScheduler();
  let healthOk = true;
  let healthStatus = 200;
  let probeCount = 0;
  let networkOnline = true;
  const pushReadings = new Set();
  const pushReconnects = new Set();
  const recovery = {push: 0, streams: 0, conversations: 0, catalog: 0, notices: 0};
  const logs = [];
  const logger = Object.fromEntries(
    ['debug', 'info', 'warn', 'error'].map((level) => [
      level, (...parts) => logs.push([level, ...parts.map(String)]),
    ]),
  );
  const monitor = createBackendAvailabilityMonitor({
    document: dom.window.document,
    browserEvents: dom.window,
    schedule: scheduler.port,
    log: logger,
    offlineIconHtml: () => '<svg aria-hidden="true"></svg>',
    isVisible: () => dom.window.document.visibilityState === 'visible',
    isNetworkOnline: () => networkOnline,
    probeHealth: async () => {
      probeCount += 1;
      return {ok: healthOk, status: healthStatus};
    },
    subscribePushReading: (listener) => {
      pushReadings.add(listener);
      listener({connected: true});
      return () => pushReadings.delete(listener);
    },
    subscribePushReconnect: (listener) => {
      pushReconnects.add(listener);
      return () => pushReconnects.delete(listener);
    },
    nudgePushConnection: () => { recovery.push += 1; },
    probeStuckStreams: () => { recovery.streams += 1; },
    recoverOfflineConversations: async () => { recovery.conversations += 1; },
    revalidateOnResume: () => { recovery.catalog += 1; },
    notifyRecovery: () => { recovery.notices += 1; },
    copy: {
      backendOfflineTitle: () => '后端服务器已离线',
      backendOfflineDescription: (seconds) => `每 ${seconds} 秒自动重试`,
      networkOfflineTitle: () => '本机网络已断开',
      networkOfflineDescription: () => '网络断开desc',
      offlineElapsed: (duration) => `已离线 ${duration}`,
      retryNow: () => '立即重试',
      snooze: () => '暂时隐藏',
      restoredTitle: () => '后端已恢复',
      restoredDescription: () => '重新同步中',
      backendTitlePrefix: () => '【后端离线】',
      networkTitlePrefix: () => '【网络断开】',
    },
  });
  return {
    dom, scheduler, monitor, recovery, logs, pushReadings, pushReconnects,
    setHealth(ok, status = ok ? 200 : 503) { healthOk = ok; healthStatus = status; },
    setNetworkOnline(value) { networkOnline = value; },
    emitPush(connected) {
      for (const listener of pushReadings) listener({connected});
    },
    probeCount: () => probeCount,
    banner: () => dom.window.document.getElementById('backend-offline-banner'),
  };
}

const flush = async () => {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
};

(async () => {
  const env = createEnvironment();
  const {dom, scheduler, monitor, recovery} = env;
  monitor.start();
  const observed = {};

  if (scenario === 'offline-recovery') {
    env.setHealth(false);
    env.emitPush(false);
    await flush();
    observed.firstFailure = monitor.snapshot().phase === 'suspect'
      && monitor.snapshot().consecutiveFailures === 1
      && env.banner() === null
      && scheduler.fireTimeout(4000);
    await flush();
    observed.offline = monitor.snapshot().phase === 'offline'
      && !!env.banner()
      && dom.window.document.title.startsWith('【后端离线】')
      && scheduler.active('interval', 5000)
      && scheduler.active('interval', 1000);
    env.setHealth(true);
    observed.pollFired = scheduler.fireInterval(5000);
    await flush();
    observed.recovered = monitor.snapshot().phase === 'online'
      && env.banner() === null
      && dom.window.document.title === 'Tofu';
    observed.recovery = recovery;
  } else if (scenario === 'proxy-hiccup') {
    env.setHealth(false);
    env.emitPush(false);
    await flush();
    env.setHealth(true);
    observed.confirmationArmed = scheduler.fireTimeout(4000);
    await flush();
    observed.quiet = monitor.snapshot().phase === 'online'
      && env.banner() === null
      && dom.window.document.title === 'Tofu'
      && Object.values(recovery).every((count) => count === 0);
  } else if (scenario === 'browser-network') {
    env.setNetworkOnline(false);
    env.setHealth(false);
    dom.window.dispatchEvent(new dom.window.Event('offline'));
    await flush();
    scheduler.fireTimeout(4000);
    await flush();
    observed.networkBanner = !!env.banner()
      && env.banner().textContent.includes('网络断开desc')
      && dom.window.document.title.startsWith('【网络断开】');
    env.setNetworkOnline(true);
    env.setHealth(true);
    dom.window.dispatchEvent(new dom.window.Event('online'));
    await flush();
    observed.recovered = env.banner() === null
      && dom.window.document.title === 'Tofu';
  } else if (scenario === 'snooze') {
    env.setHealth(false);
    env.emitPush(false);
    await flush();
    scheduler.fireTimeout(4000);
    await flush();
    const buttons = env.banner().querySelectorAll('button');
    buttons[1].click();
    observed.hidden = env.banner() === null
      && monitor.snapshot().phase === 'offline';
    scheduler.advance(61_000);
    observed.tickFired = scheduler.fireInterval(1000);
    observed.reshown = !!env.banner();
  } else if (scenario === 'lifecycle') {
    observed.initialSubscriptions = env.pushReadings.size === 1
      && env.pushReconnects.size === 1;
    monitor.destroy();
    const probesAfterDestroy = env.probeCount();
    env.setHealth(false);
    dom.window.dispatchEvent(new dom.window.Event('offline'));
    await flush();
    observed.released = env.pushReadings.size === 0
      && env.pushReconnects.size === 0
      && env.probeCount() === probesAfterDestroy
      && monitor.snapshot().started === false;
    monitor.start();
    observed.restartedOnce = env.pushReadings.size === 1
      && env.pushReconnects.size === 1
      && monitor.snapshot().started === true;
  } else if (scenario === 'proxy-auth') {
    env.setHealth(false, 401);
    env.emitPush(false);
    await flush();
    observed.online = env.probeCount() === 1
      && monitor.snapshot().phase === 'online'
      && env.banner() === null
      && !scheduler.active('timeout', 4000);
  } else if (scenario === 'planned-interruption') {
    monitor.beginPlannedInterruption();
    env.setHealth(false);
    env.emitPush(false);
    dom.window.dispatchEvent(new dom.window.Event('offline'));
    await flush();
    observed.suppressed = monitor.snapshot().plannedInterruption === true
      && monitor.snapshot().phase === 'online'
      && env.probeCount() === 0
      && env.banner() === null
      && dom.window.document.documentElement.dataset.tofuPlannedInterruption === 'true'
      && !scheduler.active('timeout', 4000);

    env.setHealth(true);
    monitor.endPlannedInterruption(true);
    await flush();
    observed.knownOnline = monitor.snapshot().plannedInterruption === false
      && monitor.snapshot().phase === 'online'
      && env.banner() === null
      && !('tofuPlannedInterruption' in dom.window.document.documentElement.dataset)
      && env.probeCount() === 0;

    monitor.beginPlannedInterruption();
    env.setHealth(false);
    env.emitPush(false);
    await flush();
    monitor.endPlannedInterruption(false);
    await flush();
    observed.unknownRechecks = monitor.snapshot().phase === 'suspect'
      && monitor.snapshot().consecutiveFailures === 1
      && env.probeCount() === 1
      && scheduler.active('timeout', 4000);
  }
  console.log(JSON.stringify(observed));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


_STORAGE_HARNESS = r"""
const fs = require('fs');
const {JSDOM} = require('jsdom');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const scenario = process.argv[2];
const dom = new JSDOM('<!doctype html><body></body>', {url: 'http://tofu.test/'});
let visible = true;
let mode = 'unhealthy';
let probes = 0;
let nextHandle = 1;
const intervals = new Map();
const logs = [];
const scheduler = {
  now: () => 0,
  setTimeout: () => 0,
  clearTimeout: () => undefined,
  setInterval: (callback, delayMs) => {
    const handle = nextHandle++;
    intervals.set(handle, {callback, delayMs, active: true});
    return handle;
  },
  clearInterval: (handle) => {
    const interval = intervals.get(handle);
    if (interval) interval.active = false;
  },
};
const activePoll = () => [...intervals.values()].some(
  (interval) => interval.active && interval.delayMs === 15000,
);
const firePoll = () => {
  const interval = [...intervals.values()].find(
    (item) => item.active && item.delayMs === 15000,
  );
  if (!interval) return false;
  interval.callback();
  return true;
};
let resolveDeferred = null;
const monitor = createStorageAvailabilityMonitor({
  document: dom.window.document,
  schedule: scheduler,
  isVisible: () => visible,
  warningIconHtml: () => '<svg aria-hidden="true"></svg>',
  probeHealth: async () => {
    probes += 1;
    if (mode === 'unreachable') throw new Error('network down');
    if (mode === 'deferred') {
      return await new Promise((resolve) => { resolveDeferred = resolve; });
    }
    if (mode === 'malformed') {
      return {ok: true, json: async () => { throw new Error('bad json'); }};
    }
    return {
      ok: true,
      json: async () => ({storage: {ready: mode === 'healthy'}}),
    };
  },
  copy: {
    unavailableTitle: () => '存储服务暂时不可用',
    unavailableDescription: () => '持久化操作已安全暂停',
    dismiss: () => '关闭',
  },
  log: Object.fromEntries(
    ['debug', 'info', 'warn', 'error'].map((level) => [
      level, (...parts) => logs.push([level, ...parts.map(String)]),
    ]),
  ),
});
const banner = () => dom.window.document.getElementById('storage-warning-banner');
const flush = async () => {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
};

(async () => {
  const observed = {};
  if (scenario === 'recovery') {
    await monitor.check();
    observed.warned = !!banner()
      && banner().textContent.includes('持久化操作已安全暂停')
      && activePoll();
    visible = false;
    mode = 'healthy';
    const beforeHiddenPoll = probes;
    observed.hiddenPollFired = firePoll();
    await flush();
    observed.hiddenSkipped = probes === beforeHiddenPoll && !!banner();
    visible = true;
    observed.visiblePollFired = firePoll();
    await flush();
    observed.recovered = !banner() && !activePoll();
  } else if (scenario === 'dismiss') {
    await monitor.check();
    banner().querySelector('button').click();
    observed.dismissed = !banner() && !activePoll();
  } else if (scenario === 'malformed') {
    mode = 'malformed';
    await monitor.check();
    observed.failSoft = !banner() && !activePoll()
      && logs.some((row) => row[0] === 'debug');
  } else if (scenario === 'destroy-race') {
    mode = 'deferred';
    const pending = monitor.check();
    await flush();
    monitor.destroy();
    resolveDeferred({ok: true, json: async () => ({storage: {ready: false}})});
    await pending;
    observed.closed = !banner() && !activePoll();
  } else if (scenario === 'single-flight') {
    mode = 'deferred';
    const first = monitor.check();
    const second = monitor.check();
    await flush();
    observed.oneProbe = probes === 1;
    resolveDeferred({ok: true, json: async () => ({storage: {ready: true}})});
    await Promise.all([first, second]);
    observed.settled = !banner() && !activePoll();
  }
  console.log(JSON.stringify(observed));
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


_COORDINATOR_HARNESS = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
let requests = 0;
let bodyReads = 0;
let release = null;
let fail = false;
const coordinator = createAvailabilityHealthProbeCoordinator({
  request: (timeoutMs) => {
    requests += 1;
    if (fail) return Promise.reject(new Error('offline'));
    return new Promise((resolve) => {
      release = () => resolve({
        ok: true,
        status: 200,
        json: async () => {
          bodyReads += 1;
          return {storage: {ready: true}, timeoutMs};
        },
      });
    });
  },
});
const flush = async () => {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
};

(async () => {
  const first = coordinator.probe(4000);
  const second = coordinator.probe(3000);
  await flush();
  const sharedPromise = first === second;
  const oneRequest = requests === 1;
  release();
  const [left, right] = await Promise.all([first, second]);
  const [leftBody, rightBody] = await Promise.all([left.json(), right.json()]);
  const repeatableBody = bodyReads === 1
    && leftBody.storage.ready === true
    && rightBody.timeoutMs === 4000;

  fail = true;
  let failed = false;
  try { await coordinator.probe(2000); } catch (_) { failed = true; }
  fail = false;
  const recovered = coordinator.probe(1000);
  await flush();
  const releasedAfterFailure = failed && requests === 3;
  release();
  await recovered;
  process.stdout.write(JSON.stringify({
    sharedPromise, oneRequest, repeatableBody, releasedAfterFailure,
  }) + '\n');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
"""


def _run_harness(bundle: str, harness: str, scenario: str) -> dict[str, object]:
    result = subprocess.run(
        ["node", "-e", harness, bundle, scenario],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not HAS_BROWSER_DEPS, reason="node/jsdom unavailable")
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("offline-recovery", {
            "firstFailure": True,
            "offline": True,
            "pollFired": True,
            "recovered": True,
            "recovery": {
                "push": 1,
                "streams": 1,
                "conversations": 1,
                "catalog": 1,
                "notices": 1,
            },
        }),
        ("proxy-hiccup", {"confirmationArmed": True, "quiet": True}),
        ("browser-network", {"networkBanner": True, "recovered": True}),
        ("snooze", {"hidden": True, "tickFired": True, "reshown": True}),
        ("lifecycle", {
            "initialSubscriptions": True,
            "released": True,
            "restartedOnce": True,
        }),
        ("proxy-auth", {"online": True}),
        ("planned-interruption", {
            "suppressed": True,
            "knownOnline": True,
            "unknownRechecks": True,
        }),
    ],
)
def test_backend_availability_outcomes(
        scenario: str, expected: dict[str, object]) -> None:
    assert _run_harness(BACKEND_BUNDLE, _BACKEND_HARNESS, scenario) == expected


@pytest.mark.skipif(not HAS_BROWSER_DEPS, reason="node/jsdom unavailable")
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("recovery", {
            "warned": True,
            "hiddenPollFired": True,
            "hiddenSkipped": True,
            "visiblePollFired": True,
            "recovered": True,
        }),
        ("dismiss", {"dismissed": True}),
        ("malformed", {"failSoft": True}),
        ("destroy-race", {"closed": True}),
        ("single-flight", {"oneProbe": True, "settled": True}),
    ],
)
def test_storage_availability_outcomes(
        scenario: str, expected: dict[str, object]) -> None:
    assert _run_harness(STORAGE_BUNDLE, _STORAGE_HARNESS, scenario) == expected


@pytest.mark.skipif(not HAS_BROWSER_DEPS, reason="node/jsdom unavailable")
def test_overlapping_health_probes_share_one_repeatable_wire_response() -> None:
    assert _run_harness(
        COORDINATOR_BUNDLE, _COORDINATOR_HARNESS, "coordinator",
    ) == {
        "sharedPromise": True,
        "oneRequest": True,
        "repeatableBody": True,
        "releasedAfterFailure": True,
    }


def test_retained_monitor_section_is_retired() -> None:
    old_name = "core/backend_offline_monitor.js"
    assert old_name not in runtime_section_names()
    assert not (ROOT / "frontend/src/runtime/sections" / old_name).exists()
