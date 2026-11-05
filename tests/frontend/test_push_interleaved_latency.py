"""Inbound data proves life without discarding the newest ping's RTT sample."""
from __future__ import annotations

import json
import subprocess

import pytest

from tests._runtime_sections import runtime_section
from tests._jsdom import require_node_modules

pytestmark = pytest.mark.unit


def test_interleaved_data_keeps_latest_pong_and_rejects_prior_socket_samples():
    require_node_modules('vite')
    source = runtime_section("push.js")
    harness = r"""
const assert = require('node:assert/strict');
global.window = globalThis;
global.location = {protocol: 'http:', host: 'localhost'};
global.apiUrl = value => value;
let now = 1000000;
Date.now = () => now;
let nextTimer = 1;
const timers = new Map(), intervals = new Map(), sockets = [];
global.setTimeout = (fn, ms) => {const id=nextTimer++; timers.set(id,{fn,ms}); return id;};
global.clearTimeout = id => timers.delete(id);
global.setInterval = fn => {const id=nextTimer++; intervals.set(id,fn); return id;};
global.clearInterval = id => intervals.delete(id);
class Socket {
  static OPEN=1; static CONNECTING=0; static CLOSING=2; static CLOSED=3;
  constructor(url) {this.url=url; this.readyState=0; this.sent=[]; sockets.push(this);}
  send(raw) {this.sent.push(JSON.parse(raw));}
  open() {this.readyState=1; this.onopen();}
  frame(value) {this.onmessage({data:JSON.stringify(value)});}
  close() {this.readyState=3; this.onclose({code:1006});}
  lastPing() {return this.sent.filter(value=>value.action==='ping').at(-1).t;}
}
global.WebSocket=Socket;
(0,eval)(SOURCE);
const readings=[];
pushOnLatency(value=>readings.push(value));
pushConnect(); const first=sockets[0]; first.open();
const sendPing=()=>[...intervals.values()][0]();
const pong=(socket,t)=>socket.frame({channel:'system',type:'pong',t});
const data=socket=>socket.frame({channel:'project',type:'notify',data:{}});
const initial=first.lastPing(); now+=1012; pong(first,initial);
assert.equal(pushGetLatency().ms,1012);
for(let index=0;index<20;index++) {
 now+=4000; sendPing(); const t=first.lastPing();
 now+=10; data(first);
 now+=40; pong(first,t);
 assert.equal(pushGetLatency().ms,50,'interleaved data must not freeze the old 1012ms reading');
 assert.equal(pushGetLatency().state,'good');
 assert.equal(pushIsConnected(),true);
 assert.equal(readings.at(-1).lastInboundAt,now,
   'availability subscribers must receive the actual inbound heartbeat timestamp');
}
// Once a new probe starts, a pong from the superseded probe is not evidence.
now+=4000; sendPing(); const previous=first.lastPing(); data(first);
now+=4000; sendPing(); const latest=first.lastPing();
now+=15; const before=readings.length; pong(first,previous);
assert.equal(readings.length,before);
now+=35; pong(first,latest); assert.equal(pushGetLatency().ms,50);
// The data proof-of-life still cancels the original half-open watchdog, but
// total silence after a fresh probe still closes and reconnects.
now+=4000; sendPing(); const dataProbe=first.lastPing(); data(first);
now+=9000; sendPing(); assert.equal(first.readyState,1);
now+=9000; sendPing(); assert.equal(first.readyState,3);
const reconnect=[...timers.values()].at(-1); reconnect.fn();
const second=sockets.at(-1); second.open();
now+=50; const resetReadings=readings.length; pong(second,dataProbe);
assert.equal(readings.length,resetReadings);
pong(second,second.lastPing()); assert.equal(pushGetLatency().ms,50);
console.log(JSON.stringify({cycles:20,ms:pushGetLatency().ms,connected:pushIsConnected()}));
""".replace("SOURCE", json.dumps(source))
    completed = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "cycles": 20, "ms": 50, "connected": True,
    }
