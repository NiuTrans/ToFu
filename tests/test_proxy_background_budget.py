"""Browser background and first-screen budgets under constrained proxies."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CLIENT_LOG_SCHEDULER = native_module_path(
    '.native/client-log-flush-scheduler-budget.js',
    ROOT / 'frontend/src/core/client-log-flush-scheduler.ts',
)
INFO_RAIL = runtime_section_path('info-rail.js', scope_prelude=False)
AVAILABILITY_PRELUDE = ROOT / 'frontend/src/runtime/sections/_prelude.js'
AVAILABILITY_COORDINATOR = ROOT / 'frontend/src/availability-health-probe.ts'
FEATURE_FLAGS_LOADER = native_module_path(
    '.native/feature-flags-loader.js',
    ROOT / 'frontend/src/core/feature-flags-loader.ts',
)


def test_feature_flags_piggyback_is_idempotent_and_fallback_is_single_flight():
    result = _node(r"""
      (async () => {
        const fs = require('fs');
        eval(fs.readFileSync(SOURCE_PATH, 'utf8'));
        let current = {};
        let apiCalls = 0;
        let bodyReads = 0;
        let resolveRequest;
        let commits = 0;
        const errors = [];
        const loader = createFeatureFlagsLoader({
          current:() => current,
          commit:(next) => { current = next; commits += 1; },
          request:() => {
            apiCalls += 1;
            return new Promise((resolve) => { resolveRequest = resolve; });
          },
          onError:(error) => { errors.push(String(error?.message || error)); },
        }).load;
        const first = {
          pptx_translate_enabled:false, cache_extended_ttl:false,
          debug_mode:true, optimizer_enabled:true, artifacts_enabled:true,
          plugin_enabled:false,
        };
        const oversized = {...first};
        for (let index = 0; index < 257; index += 1) {
          oversized['extra_' + index] = true;
        }
        const validation = {
          metadataIgnored:normalizeFeatureFlags({
            ok:true, request_id:'trace-only', ...first,
          })?.debug_mode === true,
          invalidKeyRejected:normalizeFeatureFlags({...first, 'Bad-Key':true}) === null,
          oversizedRejected:normalizeFeatureFlags(oversized) === null,
        };
        await loader(first);
        const afterFirst = {apiCalls, commits};
        await loader({...first});
        const afterRepeat = {apiCalls, commits};
        await loader({...first, debug_mode:false});
        const afterChange = {apiCalls, commits};
        const fallbackOne = loader({debug_mode:true});
        const fallbackTwo = loader(null);
        await Promise.resolve();
        const callsWhileShared = apiCalls;
        resolveRequest({ok:true, json:async () => {
          bodyReads += 1;
          return {ok:true, request_id:'ignored', ...first, plugin_enabled:true};
        }});
        await Promise.all([fallbackOne, fallbackTwo]);
        process.stdout.write(JSON.stringify({
          afterFirst, afterRepeat, afterChange, callsWhileShared, apiCalls,
          bodyReads, finalFlags:current, commits, errors, validation,
        }) + '\n');
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """, FEATURE_FLAGS_LOADER)
    assert result == {
        'afterFirst': {'apiCalls': 0, 'commits': 1},
        'afterRepeat': {'apiCalls': 0, 'commits': 1},
        'afterChange': {'apiCalls': 0, 'commits': 2},
        'callsWhileShared': 1,
        'apiCalls': 1,
        'bodyReads': 1,
        'finalFlags': {
            'pptx_translate_enabled': False,
            'cache_extended_ttl': False,
            'debug_mode': True,
            'optimizer_enabled': True,
            'artifacts_enabled': True,
            'plugin_enabled': True,
        },
        'commits': 3,
        'errors': [],
        'validation': {
            'metadataIgnored': True,
            'invalidKeyRejected': True,
            'oversizedRejected': True,
        },
    }

    main_source = (ROOT / 'frontend/src/main.ts').read_text(encoding='utf-8')
    toolbar_source = Path(runtime_section_path(
        'main/main_toolbar_ui.js', scope_prelude=False)).read_text(encoding='utf-8')
    epilogue_source = (ROOT / 'frontend/src/runtime/sections/_epilogue.js').read_text(
        encoding='utf-8')
    assert 'void loadFeatureFlags();' not in main_source
    assert 'loadFeatureFlags(data.feature_flags)' in toolbar_source
    assert 'createFeatureFlagsLoader({' in epilogue_source
    assert '_featureFlags = nextFlags;' in epilogue_source
    assert 'renderConversationList()' in epilogue_source
    assert epilogue_source.count("'/api/v1/features'") == 1


def test_mcp_context_summary_piggybacks_without_a_schema_request():
    result = _node(r"""
      (() => {
        const fs = require('fs');
        const listenerNames = [];
        let apiCalls = 0;
        global.runtimeScope = {};
        global.document = {
          addEventListener(name) { listenerNames.push(name); },
        };
        global.Api = {mcp:{toolsList() {
          apiCalls += 1;
          throw new Error('the context rail must own no startup request');
        }}};
        global.config = {model:'test-model', thinkingDepth:''};
        global.serverModel = '';
        global.projectState = {active:false};
        global.searchMode = 'off';
        global.fetchEnabled = false;
        global.browserEnabled = false;
        global.desktopEnabled = false;
        global.codeExecEnabled = false;
        global.memoryEnabled = false;
        global.imageGenEnabled = false;
        global.humanGuidanceEnabled = false;
        global.autoTranslate = false;
        eval(fs.readFileSync(SOURCE_PATH, 'utf8'));
        runtimeScope.applyMcpToolSummary({servers:[
          {name:'zeta', count:1.9}, {name:'alpha', count:2},
          {name:'zero', count:0}, {name:'alpha', count:99},
          {name:'', count:4}, {name:'bad', count:'nan'},
        ], total:999999});
        const labels = runtimeScope.buildTurnCtxSnapshot().tools.map(row => row.label);
        runtimeScope.applyMcpToolSummary({servers:[], total:0});
        const replacedLabels = runtimeScope.buildTurnCtxSnapshot().tools.map(row => row.label);
        process.stdout.write(JSON.stringify({
          apiCalls, listenerNames, labels, replacedLabels,
          hasApply:typeof runtimeScope.applyMcpToolSummary === 'function',
        }) + '\n');
      })();
    """, INFO_RAIL)
    assert result == {
        'apiCalls': 0,
        'listenerNames': ['click'],
        'labels': ['MCP: alpha ×2', 'MCP: zeta'],
        'replacedLabels': [],
        'hasApply': True,
    }

    api_source = Path(runtime_section_path(
        'api.js', scope_prelude=False)).read_text(encoding='utf-8')
    toolbar_source = Path(runtime_section_path(
        'main/main_toolbar_ui.js', scope_prelude=False)).read_text(encoding='utf-8')
    settings_source = Path(runtime_section_path(
        'settings/mcp.js', scope_prelude=False)).read_text(encoding='utf-8')
    info_source = Path(INFO_RAIL).read_text(encoding='utf-8')
    assert 'toolsList:' not in api_source
    assert 'Api.mcp.toolsList()' not in info_source
    assert 'runtimeScope.applyMcpToolSummary(data.mcp_tool_summary)' in toolbar_source
    assert settings_source.count(
        'runtimeScope.applyMcpToolSummary(data.mcp_tool_summary)') == 2


def test_network_badge_reuses_push_liveness_without_a_parallel_clock():
    source = Path(runtime_section_path(
        'net-latency.js', scope_prelude=False,
    )).read_text(encoding='utf-8')
    assert 'setInterval' not in source
    assert '_watchdogTimer' not in source
    assert '_STALE_MS' not in source
    assert 'pushIsConnected' not in source
    assert 'pushOnLatency(_render)' in source
    assert 'streamHealthSubscribe' in source
    assert 'retainedCompositionLifecycle.add(destroyNetLatency)' in source


def test_availability_monitors_share_one_body_safe_health_flight():
    prelude = AVAILABILITY_PRELUDE.read_text(encoding='utf-8')
    coordinator = AVAILABILITY_COORDINATOR.read_text(encoding='utf-8')
    assert prelude.count('Api.health.check({') == 1
    assert prelude.count(
        'probeHealth: availabilityHealthProbe.probe',
    ) == 2
    assert 'let inFlight:' in coordinator
    assert 'bodyFlight ??=' in coordinator
    assert 'void request.then(release, release)' in coordinator


def test_client_log_flush_scheduler_is_demand_scoped_and_resource_complete():
    result = _node(r"""
      (async () => {
        const fs = require('fs');
        eval(fs.readFileSync(SOURCE_PATH, 'utf8'));
        let pending = false;
        let hidden = false;
        let nextTimer = 0;
        const timers = new Map();
        const listeners = new Set();
        const schedule = {
          setTimeout(fn, delay) {
            const id = ++nextTimer;
            timers.set(id, {fn, delay});
            return id;
          },
          clearTimeout(id) { timers.delete(id); },
        };
        const visibility = {
          get hidden() { return hidden; },
          addEventListener(name, fn) {
            if (name === 'visibilitychange') listeners.add(fn);
          },
          removeEventListener(name, fn) {
            if (name === 'visibilitychange') listeners.delete(fn);
          },
        };
        const snapshots = [];
        const flushResolvers = [];
        let flushes = 0;
        const scheduler = createClientLogFlushScheduler({
          baseDelayMs:15000, schedule, visibility,
          hasPending:() => pending, random:() => 0.5,
          flush:() => {
            flushes += 1;
            pending = false;
            return new Promise(resolve => flushResolvers.push(resolve));
          },
        });
        const resources = () => ({
          timers:timers.size, listeners:listeners.size,
          running:scheduler.snapshot().flushRunning,
        });
        const settle = async () => {
          for (let index = 0; index < 5; index += 1) await Promise.resolve();
        };
        snapshots.push(resources());
        pending = true;
        scheduler.demand(); scheduler.demand();
        snapshots.push({...resources(), delay:[...timers.values()][0].delay});
        hidden = true;
        for (const listener of [...listeners]) listener();
        snapshots.push(resources());
        hidden = false;
        for (const listener of [...listeners]) listener();
        const first = timers.entries().next().value;
        timers.delete(first[0]); first[1].fn();
        snapshots.push({...resources(), flushes});
        flushResolvers.shift()();
        await settle();
        snapshots.push(resources());
        pending = true;
        scheduler.demand();
        scheduler.flushNow();
        snapshots.push({...resources(), flushes});
        scheduler.destroy(); scheduler.destroy();
        flushResolvers.shift()();
        await settle();
        snapshots.push({...resources(), destroyed:scheduler.snapshot().destroyed});
        let timerErrors = 0;
        const broken = createClientLogFlushScheduler({
          baseDelayMs:15000, visibility,
          schedule:{
            setTimeout() { throw new Error('fault-injected timer failure'); },
            clearTimeout() {},
          },
          hasPending:() => true, random:() => 0.5, flush:() => undefined,
          onError:() => { timerErrors += 1; },
        });
        broken.demand();
        const faultSoft = timerErrors === 1 &&
          broken.snapshot().timerScheduled === false &&
          broken.snapshot().visibilitySubscribed === false;
        broken.destroy();
        const profiles = [
          clientLogFlushBaseDelayMs({getElementById:() => ({
            textContent:'{"transportProfile":"constrained-proxy"}',
          })}, {pathname:'/'}),
          clientLogFlushBaseDelayMs({getElementById:() => ({
            textContent:'{"transportProfile":"direct"}',
          })}, {pathname:'/proxy/15000/'}),
          clientLogFlushBaseDelayMs({getElementById:() => ({
            textContent:'malformed',
          })}, {pathname:'/absproxy/15000/'}),
          clientLogFlushBaseDelayMs({getElementById:() => null}, {pathname:'/'}),
        ];
        process.stdout.write(JSON.stringify({snapshots, faultSoft, profiles}) + '\n');
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """, CLIENT_LOG_SCHEDULER)
    assert result == {
        'snapshots': [
            {'timers': 0, 'listeners': 0, 'running': False},
            {'timers': 1, 'listeners': 1, 'running': False, 'delay': 15000},
            {'timers': 0, 'listeners': 1, 'running': False},
            {'timers': 0, 'listeners': 0, 'running': True, 'flushes': 1},
            {'timers': 0, 'listeners': 0, 'running': False},
            {'timers': 0, 'listeners': 0, 'running': True, 'flushes': 2},
            {'timers': 0, 'listeners': 0, 'running': False,
             'destroyed': True},
        ],
        'faultSoft': True,
        'profiles': [60000, 15000, 60000, 15000],
    }


def _node(source: str, path: str) -> dict:
    if not shutil.which('node'):
        pytest.skip('node is required')
    completed = subprocess.run(
        ['node', '-e', source.replace('SOURCE_PATH', json.dumps(path))],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_proxy_log_relay_is_jittered_hidden_aware_and_bounded():
    result = _node(r"""
      (async () => {
        const fs = require('fs');
        global.window = globalThis;
        global.location = {href:'https://example/proxy/15000/', pathname:'/proxy/15000/'};
        const listeners = new Set();
        global.document = {
          hidden:true,
          getElementById:() => ({textContent:JSON.stringify({
            transportProfile:'constrained-proxy',
          })}),
          addEventListener:(name, fn) => {
            if (name === 'visibilitychange') listeners.add(fn);
          },
          removeEventListener:(name, fn) => {
            if (name === 'visibilitychange') listeners.delete(fn);
          },
        };
        global.localStorage = {getItem:() => null};
        Object.defineProperty(globalThis, 'navigator', {value:{onLine:true}, configurable:true});
        let nextTimer = 0;
        const timers = new Map();
        global.setTimeout = (fn, delay) => {
          const id = ++nextTimer;
          timers.set(id, {fn, delay});
          return id;
        };
        global.clearTimeout = id => timers.delete(id);
        global.Math.random = () => 0.5;
        global.console = {log(){},info(){},warn(){},error(){}};
        let network = {connected:true, state:'good'};
        global.pushGetLatency = () => network;
        const relays = [];
        global.Api = {logs:{clientRelay:async (payload) => {
          relays.push(JSON.parse(payload)); return {ok:true};
        }}};
        eval(fs.readFileSync(SOURCE_PATH, 'utf8'));
        const emptyTimerCount = timers.size;
        const emptyListenerCount = listeners.size;
        for (let index = 0; index < 500; index += 1) console.info('line-' + index);
        const boundedBeforeFlush = runtimeScope.__clientLogRelay._buf.length;
        const hiddenTimerCount = timers.size;
        const hiddenListenerCount = listeners.size;
        document.hidden = false;
        for (const listener of [...listeners]) listener();
        const firstDelay = [...timers.values()][0].delay;
        network = {connected:false, state:'offline'};
        let first = timers.entries().next().value;
        timers.delete(first[0]); first[1].fn();
        for (let index = 0; index < 5; index += 1) await Promise.resolve();
        const offlinePreserved = relays.length === 0 &&
          runtimeScope.__clientLogRelay._buf.length === boundedBeforeFlush &&
          timers.size === 1 && listeners.size === 1;
        network = {connected:true, state:'good'};
        first = timers.entries().next().value;
        timers.delete(first[0]); first[1].fn();
        for (let index = 0; index < 8; index += 1) await Promise.resolve();

        process.stdout.write(JSON.stringify({
          emptyTimerCount, emptyListenerCount,
          hiddenTimerCount, hiddenListenerCount, firstDelay,
          boundedBeforeFlush,
          offlinePreserved,
          relayCount:relays.length,
          relayedEntries:relays[0] ? relays[0].entries.length : 0,
          remaining:runtimeScope.__clientLogRelay._buf.length,
          followupTimers:timers.size,
          followupListeners:listeners.size,
        }) + '\n');
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """, runtime_section_path('core/client_log_relay.js'))
    assert result == {
        'emptyTimerCount': 0,
        'emptyListenerCount': 0,
        'hiddenTimerCount': 0,
        'hiddenListenerCount': 1,
        'firstDelay': 60000,
        'boundedBeforeFlush': 400,
        'offlinePreserved': True,
        'relayCount': 1,
        'relayedEntries': 200,
        'remaining': 201,
        'followupTimers': 1,
        'followupListeners': 1,
    }


def test_client_log_relay_manual_flush_pagehide_and_kill_switch_cleanup():
    result = _node(r"""
      (async () => {
        const fs = require('fs');
        global.runtimeScope = globalThis;
        global.window = globalThis;
        global.location = {href:'http://localhost/chat', pathname:'/chat'};
        Math.random = () => 0.5;
        let originalCalls = 0;
        global.console = {
          log(){ originalCalls += 1; }, info(){ originalCalls += 1; },
          warn(){ originalCalls += 1; }, error(){ originalCalls += 1; },
          debug(){},
        };
        let nextTimer = 0;
        const timers = new Map();
        global.setTimeout = (fn, delay) => {
          const id = ++nextTimer;
          timers.set(id, {fn, delay});
          return id;
        };
        global.clearTimeout = id => timers.delete(id);
        const pageListeners = new Map();
        global.addEventListener = (name, fn) => {
          if (!pageListeners.has(name)) pageListeners.set(name, new Set());
          pageListeners.get(name).add(fn);
        };
        global.removeEventListener = (name, fn) => pageListeners.get(name)?.delete(fn);
        const visibilityListeners = new Set();
        global.document = {
          hidden:false, getElementById:() => null,
          addEventListener:(name, fn) => {
            if (name === 'visibilitychange') visibilityListeners.add(fn);
          },
          removeEventListener:(name, fn) => {
            if (name === 'visibilitychange') visibilityListeners.delete(fn);
          },
        };
        let enabled = '1';
        global.localStorage = {getItem:() => enabled};
        const beacons = [];
        Object.defineProperty(globalThis, 'navigator', {configurable:true, value:{
          onLine:true,
          sendBeacon:(url, body) => { beacons.push({url, body}); return true; },
        }});
        global.apiUrl = path => '/base' + path;
        global.pushGetLatency = () => ({connected:true, state:'online'});
        const requests = [];
        global.Api = {logs:{clientRelay(payload) {
          let resolve;
          const promise = new Promise(yes => { resolve = yes; });
          requests.push({payload:JSON.parse(payload), resolve});
          return promise;
        }}};
        const settle = async () => {
          for (let index = 0; index < 8; index += 1) await Promise.resolve();
        };
        const fireTimer = () => {
          const first = timers.entries().next().value;
          timers.delete(first[0]); first[1].fn();
        };
        eval(fs.readFileSync(SOURCE_PATH, 'utf8'));
        const empty = timers.size === 0 && visibilityListeners.size === 0;
        console.info('folded'); console.info('folded');
        const demanded = timers.size === 1 && visibilityListeners.size === 1 &&
          runtimeScope.__clientLogRelay._buf[0].n === 2 && originalCalls === 2;
        fireTimer();
        const singleFlight = requests.length === 1 && timers.size === 0 &&
          visibilityListeners.size === 0 && requests[0].payload.entries.length === 1;
        console.warn('do not recurse while relay request is live');
        const originalWins = originalCalls === 3 &&
          runtimeScope.__clientLogRelay._buf.length === 0;
        requests[0].resolve({ok:true}); await settle();
        const settled = timers.size === 0 && visibilityListeners.size === 0;

        console.error('manual');
        runtimeScope.__clientLogRelay.flush();
        const manual = requests.length === 2 && timers.size === 0 &&
          visibilityListeners.size === 0;
        requests[1].resolve({ok:true}); await settle();

        enabled = '0';
        console.error('disabled'); fireTimer(); await settle();
        const killed = requests.length === 2 &&
          runtimeScope.__clientLogRelay._buf.length === 0 && timers.size === 0 &&
          visibilityListeners.size === 0;

        enabled = '1';
        console.log('pagehide');
        for (const listener of [...pageListeners.get('pagehide')]) listener();
        const pagehide = beacons.length === 1 &&
          runtimeScope.__clientLogRelay._buf.length === 0 && timers.size === 0 &&
          visibilityListeners.size === 0;
        process.stdout.write(JSON.stringify({
          empty, demanded, singleFlight, originalWins, settled,
          manual, killed, pagehide,
        }) + '\n');
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """, runtime_section_path('core/client_log_relay.js'))
    assert result == {
        'empty': True,
        'demanded': True,
        'singleFlight': True,
        'originalWins': True,
        'settled': True,
        'manual': True,
        'killed': True,
        'pagehide': True,
    }


def test_client_log_relay_preserves_error_message_and_stack():
    result = _node(r"""
      (() => {
        const fs = require('fs');
        global.window = globalThis;
        global.location = {href:'http://localhost/', pathname:'/'};
        global.document = {
          hidden:false, getElementById:() => null,
          addEventListener() {}, removeEventListener() {},
        };
        global.localStorage = {getItem:() => null};
        global.setTimeout = () => 1;
        global.clearTimeout = () => {};
        global.console = {log(){},info(){},warn(){},error(){}};
        eval(fs.readFileSync(SOURCE_PATH, 'utf8'));
        const failure = new TypeError('owner unavailable');
        failure.stack = 'TypeError: owner unavailable\n    at feature-owner.js:7:3';
        console.error('[feature-bridge] failed:', failure);
        const entry = runtimeScope.__clientLogRelay._buf[0];
        process.stdout.write(JSON.stringify({message:entry.msg}) + '\n');
      })();
    """, runtime_section_path('core/client_log_relay.js'))
    assert 'TypeError: owner unavailable' in result['message']
    assert 'feature-owner.js:7:3' in result['message']
    assert '{} ' not in result['message']


def test_hidden_push_tab_uses_slow_cadence_and_foreground_recovers_immediately():
    result = _node(r"""
      (async () => {
        const fs = require('fs');
        global.window = globalThis;
        global.location = {protocol:'https:', host:'example.test'};
        Date.now = () => 1_000_000;
        global.apiUrl = (path) => '/proxy/15000' + path;
        global.Api = {pageRequestId:() => 'page'};
        const listeners = {};
        global.document = {
          hidden:true,
          addEventListener:(name, fn) => { listeners[name] = fn; },
        };
        const intervals = [];
        const timeouts = [];
        global.setInterval = (fn, delay) => { intervals.push({fn, delay}); return intervals.length; };
        global.clearInterval = () => {};
        global.setTimeout = (fn, delay) => { timeouts.push({fn, delay}); return timeouts.length; };
        global.clearTimeout = () => {};
        const sockets = [];
        class FakeWebSocket {
          static CONNECTING = 0; static OPEN = 1; static CLOSING = 2; static CLOSED = 3;
          constructor(url) { this.url=url; this.readyState=0; this.sent=[]; sockets.push(this); }
          send(raw) { this.sent.push(JSON.parse(raw)); }
          close() { this.readyState=3; }
          open() { this.readyState=1; this.onopen(); }
        }
        global.WebSocket = FakeWebSocket;
        eval(fs.readFileSync(SOURCE_PATH, 'utf8'));
        pushConnect();
        const socket = sockets[0];
        socket.open();
        const hiddenInterval = intervals[intervals.length - 1].delay;
        const hiddenTimeout = timeouts[timeouts.length - 1].delay;
        const firstPing = socket.sent.find((frame) => frame.action === 'ping');
        socket.onmessage({data:JSON.stringify({type:'pong', t:firstPing.t})});

        document.hidden = false;
        listeners.visibilitychange();
        const foregroundInterval = intervals[intervals.length - 1].delay;
        const foregroundTimeout = timeouts[timeouts.length - 1].delay;
        const pingCount = socket.sent.filter((frame) => frame.action === 'ping').length;
        document.hidden = true;
        listeners.visibilitychange();
        const rehiddenTimeout = timeouts[timeouts.length - 1].delay;
        process.stdout.write(JSON.stringify({
          hiddenInterval, hiddenTimeout, foregroundInterval,
          foregroundTimeout, pingCount, rehiddenTimeout,
        }) + '\n');
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """, runtime_section_path('push.js'))
    assert result == {
        'hiddenInterval': 20000,
        'hiddenTimeout': 30000,
        'foregroundInterval': 4000,
        'foregroundTimeout': 8000,
        'pingCount': 2,
        'rehiddenTimeout': 30000,
    }
