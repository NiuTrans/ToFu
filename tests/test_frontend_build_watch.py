"""Contracts for the timer-free long-lived-tab build handshake.

The served Vite build identity piggybacks on an existing low-rate push pong.
The typed controller keeps idle gating, bounded busy deferral, and the
session reload guard without owning a timer, visibility listener, DOM node,
or ``/api/health`` request.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, runtime_section_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER_SOURCE = ROOT / 'frontend/src/core/build-watch-controller.ts'
OWNER_BUNDLE = native_module_path(
    '.native/build-watch-controller.js',
    OWNER_SOURCE,
)


def test_build_watch_composition_has_no_dedicated_polling_resources():
    owner = OWNER_SOURCE.read_text(encoding='utf-8')
    main = Path(runtime_section_path('main.js', scope_prelude=False)).read_text(
        encoding='utf-8')
    prelude = (ROOT / 'frontend/src/runtime/sections/_prelude.js').read_text(
        encoding='utf-8')
    push = Path(runtime_section_path('push.js', scope_prelude=False)).read_text(
        encoding='utf-8')

    assert 'setInterval' not in owner
    assert 'setTimeout' not in owner
    assert 'document.' not in owner
    assert 'Api.health.info' not in main
    assert '_buildWatchTimer' not in main
    assert '_buildWatchTick' not in main
    assert 'buildWatchController.start();' in main
    assert 'createBuildWatchController({' in prelude
    assert 'buildWatchController.destroy()' in prelude
    assert push.count("addEventListener('visibilitychange'") == 1
    assert 'ping.buildProbe = true' in push
    assert 'function pushOnBuildId(fn)' in push


def test_build_watch_entry_is_top_level_and_boot_wired():
    source = Path(runtime_section_path('main.js', scope_prelude=False)).read_text(
        encoding='utf-8')
    definition = source.index('function _startBuildWatch()')
    boot = source.index('(function init() {')
    assert definition < boot
    assert source[source.rindex('\n', 0, definition) + 1:definition] == ''
    assert "_startBuildWatch === 'function') _startBuildWatch();" in source


_CONTROLLER_HARNESS = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));

const out = [];
function check(name, condition) {
  out.push((condition ? 'PASS ' : 'FAIL ') + name);
}

let listener = null;
let subscribeCalls = 0;
let unsubscribeCalls = 0;
let loaded = 'main-AAA111.js';
let busy = false;
let clock = 1000;
const guard = new Map();
const notices = [];
const reloads = [];
const errors = [];

const controller = createBuildWatchController({
  subscribeBuildId: (fn) => {
    subscribeCalls += 1;
    listener = fn;
    return () => { unsubscribeCalls += 1; listener = null; };
  },
  loadedBuildId: () => loaded,
  isBusy: () => busy,
  now: () => clock,
  readReloadGuard: (key) => guard.get(key) || null,
  writeReloadGuard: (key, value) => guard.set(key, value),
  showPendingNotice: (buildId) => notices.push(buildId),
  reload: () => reloads.push(clock),
  onError: (error) => errors.push(error),
});

controller.start();
controller.start();
check('start_is_idempotent', subscribeCalls === 1 && typeof listener === 'function');

listener('main-AAA111.js');
listener('../main-unsafe.js');
listener(undefined);
check('same_invalid_and_missing_are_quiet', reloads.length === 0 && notices.length === 0);

listener('main-BBB222.js');
listener('main-BBB222.js');
check('idle_mismatch_reloads_once_via_guard',
  reloads.length === 1 && guard.get(BUILD_WATCH_POLICY.reloadGuardKey) === 'main-BBB222.js');

guard.clear();
busy = true;
clock = 2000;
listener('main-CCC333.js');
clock = 3000;
listener('main-CCC333.js');
check('busy_mismatch_defers_and_notices_once',
  reloads.length === 1 && notices.filter(v => v === 'main-CCC333.js').length === 1 &&
  controller.snapshot().pendingBuildId === 'main-CCC333.js');

busy = false;
listener('main-CCC333.js');
check('later_idle_signal_reloads_pending_build', reloads.length === 2 &&
  guard.get(BUILD_WATCH_POLICY.reloadGuardKey) === 'main-CCC333.js');

guard.clear();
busy = true;
clock = 10_000;
listener('main-DDD444.js');
clock += BUILD_WATCH_POLICY.maxBusyDeferMs - 1;
listener('main-DDD444.js');
check('busy_defer_stays_bounded_before_deadline', reloads.length === 2);
clock += 2;
listener('main-DDD444.js');
check('wedged_busy_state_eventually_reloads', reloads.length === 3);

guard.clear();
clock += 1;
listener('main-EEE555.js');
listener('main-AAA111.js');
check('current_build_signal_clears_pending_state',
  controller.snapshot().pendingBuildId === null);

guard.set(BUILD_WATCH_POLICY.reloadGuardKey, 'main-FFF666.js');
listener('main-FFF666.js');
check('session_guard_blocks_stale_index_reload_loop', reloads.length === 3);

controller.destroy();
controller.destroy();
controller.observe('main-GGG777.js');
check('destroy_unsubscribes_and_stops_observation',
  unsubscribeCalls === 1 && listener === null && reloads.length === 3);
check('normal_path_reports_no_errors', errors.length === 0);

let trappedErrors = 0;
const defensive = createBuildWatchController({
  subscribeBuildId: () => { throw new Error('subscription unavailable'); },
  loadedBuildId: () => { throw new Error('entry unavailable'); },
  isBusy: () => { throw new Error('busy unavailable'); },
  now: () => { throw new Error('clock unavailable'); },
  readReloadGuard: () => { throw new Error('storage unavailable'); },
  writeReloadGuard: () => { throw new Error('storage unavailable'); },
  showPendingNotice: () => { throw new Error('notice unavailable'); },
  reload: () => { throw new Error('reload unavailable'); },
  onError: () => { trappedErrors += 1; },
});
defensive.start();
defensive.observe('main-ZZZ999.js');
defensive.destroy();
check('port_failures_never_escape_controller', trappedErrors >= 2);

console.log(out.join('\n'));
"""


def test_typed_build_watch_controller_behaviour():
    run_harness(
        target_js=OWNER_BUNDLE,
        body_js=_CONTROLLER_HARNESS,
        expect_pass=12,
        label='typed build watch controller',
    )


_PUSH_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.location = { protocol: 'http:', host: 'localhost' };
global.apiUrl = path => path;
global.crypto = { randomUUID: () => 'build-watch-test-rid' };

let clock = 1_000_000;
Date.now = () => clock;
const intervals = new Map();
const intervalDelays = [];
let nextTimer = 1;
global.setInterval = (fn, delay) => {
  const id = nextTimer++;
  intervals.set(id, fn);
  intervalDelays.push(delay);
  return id;
};
global.clearInterval = id => intervals.delete(id);
global.setTimeout = () => nextTimer++;
global.clearTimeout = () => {};

let visibilityListener = null;
global.document = {
  hidden: false,
  addEventListener: (type, fn) => {
    if (type === 'visibilitychange') visibilityListener = fn;
  },
};

let socket = null;
function FakeWebSocket(url) {
  this.url = url;
  this.readyState = FakeWebSocket.CONNECTING;
  this.sent = [];
  socket = this;
}
FakeWebSocket.OPEN = 1;
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
FakeWebSocket.prototype.send = function (raw) { this.sent.push(JSON.parse(raw)); };
FakeWebSocket.prototype.close = function () {};
global.WebSocket = FakeWebSocket;

eval(fs.readFileSync(process.argv[2], 'utf8'));
const out = [];
function check(name, condition) { out.push((condition ? 'PASS ' : 'FAIL ') + name); }
function pings() { return socket.sent.filter(frame => frame.action === 'ping'); }
function reply(frame, buildId) {
  socket.onmessage({ data: JSON.stringify({
    channel: 'system', type: 'pong', t: frame.t, ...(buildId ? { buildId } : {}),
  }) });
}
function pingTick() {
  const fn = Array.from(intervals.values())[0];
  fn();
  return pings()[pings().length - 1];
}

pushConnect();
socket.readyState = FakeWebSocket.OPEN;
socket.onopen();
const first = pings()[0];
check('first_liveness_ping_requests_build', first.buildProbe === true);
check('only_existing_ping_interval_is_owned',
  intervals.size === 1 && intervalDelays.length === 1 && intervalDelays[0] === 4000 &&
  !intervalDelays.includes(5 * 60 * 1000));
check('one_existing_visibility_listener_is_reused', typeof visibilityListener === 'function');

reply(first, 'main-AAA111.js');
const builds = [];
const unsubscribe = pushOnBuildId(value => builds.push(value));
check('late_subscriber_replays_first_build', builds.join(',') === 'main-AAA111.js');

clock += 4000;
const ordinary = pingTick();
check('ordinary_liveness_ping_has_no_build_probe', !Object.hasOwn(ordinary, 'buildProbe'));
reply(ordinary, 'main-AAA111.js');
check('same_build_id_is_deduplicated', builds.length === 1);

clock = first.t + 5 * 60 * 1000;
const periodic = pingTick();
check('five_minute_liveness_ping_requests_build', periodic.buildProbe === true);
reply(periodic, 'main-BBB222.js');
check('changed_build_is_emitted', builds.join(',') === 'main-AAA111.js,main-BBB222.js');

clock += 1000;
visibilityListener();
const visible = pings()[pings().length - 1];
check('visibility_resume_reuses_immediate_ping_for_build', visible.buildProbe === true);
reply(visible, 'not/a-valid-build.js');
check('invalid_server_build_id_is_ignored', builds.length === 2);

unsubscribe();
clock += 4000;
visibilityListener();
const afterUnsubscribe = pings()[pings().length - 1];
reply(afterUnsubscribe, 'main-CCC333.js');
check('unsubscribe_releases_build_listener', builds.length === 2);

const boundedCalls = Array.from({ length: 9 }, () => []);
const boundedUnsubscribes = boundedCalls.map((calls, index) =>
  pushOnBuildId(value => calls.push(index + ':' + value)));
check('build_listener_registry_caps_at_eight',
  boundedCalls.slice(0, 8).every(calls => calls.length === 1) &&
  boundedCalls[8].length === 0);
clock += 4000;
visibilityListener();
const boundedFrame = pings()[pings().length - 1];
reply(boundedFrame, 'main-DDD444.js');
check('capped_registry_emits_only_to_admitted_listeners',
  boundedCalls.slice(0, 8).every(calls => calls.length === 2) &&
  boundedCalls[8].length === 0);
boundedUnsubscribes.forEach(remove => remove());

console.log(out.join('\n'));
"""


def test_push_build_probe_reuses_existing_ping_owner():
    run_harness(
        target_js=runtime_section_path('push.js'),
        body_js=_PUSH_HARNESS,
        expect_pass=13,
        label='push build probe',
    )
