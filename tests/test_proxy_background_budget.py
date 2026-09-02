"""Hidden-tab telemetry and push budgets for constrained proxy deployments."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit


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
        const listeners = {};
        global.document = {
          hidden:true,
          getElementById:() => ({textContent:JSON.stringify({
            transportProfile:'constrained-proxy',
          })}),
          addEventListener:(name, fn) => { listeners[name] = fn; },
        };
        global.localStorage = {getItem:() => null};
        Object.defineProperty(globalThis, 'navigator', {value:{onLine:true}, configurable:true});
        const timers = [];
        global.setTimeout = (fn, delay) => { timers.push({fn, delay}); return timers.length; };
        global.Math.random = () => 0.5;
        global.console = {log(){},info(){},warn(){},error(){}};
        let network = {connected:true, state:'good'};
        global.pushGetLatency = () => network;
        const relays = [];
        global.Api = {logs:{clientRelay:async (payload) => {
          relays.push(JSON.parse(payload)); return {ok:true};
        }}};
        eval(fs.readFileSync(SOURCE_PATH, 'utf8'));
        for (let index = 0; index < 500; index += 1) console.info('line-' + index);
        const boundedBeforeFlush = runtimeScope.__clientLogRelay._buf.length;
        const firstDelay = timers[0].delay;

        timers.shift().fn();
        const hiddenPreserved = relays.length === 0 &&
          runtimeScope.__clientLogRelay._buf.length === boundedBeforeFlush;
        document.hidden = false;
        network = {connected:false, state:'offline'};
        timers.shift().fn();
        const offlinePreserved = relays.length === 0 &&
          runtimeScope.__clientLogRelay._buf.length === boundedBeforeFlush;
        network = {connected:true, state:'good'};
        timers.shift().fn();
        await Promise.resolve(); await Promise.resolve();

        process.stdout.write(JSON.stringify({
          firstDelay,
          boundedBeforeFlush,
          hiddenPreserved,
          offlinePreserved,
          relayCount:relays.length,
          relayedEntries:relays[0] ? relays[0].entries.length : 0,
          remaining:runtimeScope.__clientLogRelay._buf.length,
        }) + '\n');
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """, runtime_section_path('core/client_log_relay.js'))
    assert result == {
        'firstDelay': 60000,
        'boundedBeforeFlush': 400,
        'hiddenPreserved': True,
        'offlinePreserved': True,
        'relayCount': 1,
        'relayedEntries': 200,
        'remaining': 201,
    }


def test_client_log_relay_preserves_error_message_and_stack():
    result = _node(r"""
      (() => {
        const fs = require('fs');
        global.window = globalThis;
        global.location = {href:'http://localhost/', pathname:'/'};
        global.document = {getElementById:() => null, addEventListener() {}};
        global.localStorage = {getItem:() => null};
        global.setTimeout = () => 1;
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
