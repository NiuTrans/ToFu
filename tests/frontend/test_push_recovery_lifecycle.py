"""Push transport must recover physical failures without stranding browser work."""

from __future__ import annotations

import json
import subprocess

import pytest

from tests._jsdom import require_node_modules
from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit

_HARNESS = r"""
const assert = require('node:assert/strict');
global.window = globalThis;
global.location = {protocol:'http:', host:'example.test'};
global.apiUrl = value => value;
let now = 1000000, nextTimer = 1;
Date.now = () => now;
Math.random = () => 0.5;
const timers = new Map(), intervals = new Map(), sockets = [];
global.setTimeout = (fn,ms) => {const id=nextTimer++; timers.set(id,{fn,ms}); return id;};
global.clearTimeout = id => timers.delete(id);
global.setInterval = fn => {const id=nextTimer++; intervals.set(id,fn); return id;};
global.clearInterval = id => intervals.delete(id);
class Socket {
  static OPEN=1; static CONNECTING=0; static CLOSING=2; static CLOSED=3;
  constructor(url) {this.url=url; this.readyState=0; this.sent=[]; sockets.push(this);}
  send(raw) {this.sent.push(JSON.parse(raw));}
  open() {this.readyState=1; this.onopen();}
  frame(value) {this.onmessage({data:JSON.stringify(value)});}
  close() {
    this.readyState=2;
    if (closeMode==='normal') this.remoteClose(1000);
    if (closeMode==='throws') throw new Error('closing transport failed');
  }
  remoteClose(code) {this.readyState=3; this.onclose({code});}
  lastPing() {return this.sent.filter(value=>value.action==='ping').at(-1).t;}
}
global.WebSocket = Socket;
(0,eval)(SOURCE);
function fireTimer(entry) {
  const [id,timer]=entry; timers.delete(id); timer.fn();
}
function open() {pushConnect(); const socket=sockets.at(-1); socket.open(); return socket;}
function watchdog() {
  const timer=[...timers].find(([,timer])=>timer.ms===8000);
  assert(timer,'a first ping must arm the existing watchdog');
  now+=8000; fireTimer(timer);
}
function runReconnect() {
  const timer=[...timers].find(([,timer])=>timer.ms===500);
  assert(timer,'a failed socket must schedule the existing reconnect');
  fireTimer(timer); return sockets.at(-1);
}
async function run() {
  if (scenario==='transient-close') {
    const socket=open(); socket.remoteClose(1011);
    assert.equal(pushIsConnected(),false);
    const next=runReconnect(); assert.notEqual(next,socket); next.open();
    next.remoteClose(1008);
    assert.equal(timers.size,0,'policy refusal must remain terminal');
    return {reconnects:sockets.length-1};
  }
  if (scenario==='normal-close') {
    const socket=open(); socket.remoteClose(1000);
    assert.equal(timers.size,0,'an intentional normal close stays terminal');
    return {reconnects:0};
  }
  if (scenario==='superseded-retry') {
    const socket=open(); socket.remoteClose(1011);
    const next=open(); assert.notEqual(next,socket);
    next.remoteClose(1008);
    assert.equal(timers.size,0,'an explicit connection must consume the older retry before a policy refusal');
    return {reconnects:sockets.length-1};
  }
  if (scenario==='observer-reentry') {
    const socket=open();
    let replacement;
    const unsubscribe=pushOnLatency(reading=>{
      if (!reading.connected && reading.state==='offline' && !replacement) {
        pushConnect(); replacement=sockets.at(-1);
      }
    });
    socket.remoteClose(1011);
    assert(replacement && replacement!==socket,'the latency observer owns a new connection');
    unsubscribe(); replacement.open(); replacement.remoteClose(1008);
    assert.equal(timers.size,0,'the retired close must not leave a retry behind its observer replacement');
    return {reconnects:sockets.length-1};
  }
  if (scenario==='watchdog') {
    const socket=open();
    let settled='';
    const pending=pushRpcRequest('project.browse',{path:'/old'},{timeout:20000})
      .catch(error=>{settled=error.code;});
    watchdog(); await Promise.resolve();
    assert.equal(settled,'rpc_disconnected','waiters must not wait for TCP close completion');
    assert.equal(pushIsConnected(),false);
    const next=runReconnect(); assert.notEqual(next,socket); next.open();
    const result=pushRpcRequest('project.browse',{path:'/new'},{timeout:20000});
    const request=next.sent.find(frame=>frame.method==='project.browse');
    socket.frame({jsonrpc:'2.0',id:request.id,result:{path:'/wrong'}});
    socket.remoteClose(1000);
    assert.equal(pushIsConnected(),true,'late retired socket close cannot retire the replacement');
    next.frame({jsonrpc:'2.0',id:request.id,result:{path:'/new'}});
    assert.deepEqual(await result,{path:'/new'});
    await pending;
    return {reconnects:sockets.length-1,settled};
  }
  if (scenario==='proof-of-life') {
    const socket=open();
    now+=4000;
    if (closeMode==='malformed') socket.onmessage({data:'not-json'});
    else if (closeMode==='ping') socket.frame({channel:'system',type:'ping'});
    else socket.frame({channel:'system',type:'pong',t:socket.lastPing()-1});
    assert.equal(timers.size,0,'every inbound frame must disarm the transport-silence watchdog');
    assert.equal(pushGetLatency().ms,null,'unmatched data must not become an RTT sample');
    now+=9000; [...intervals.values()][0]();
    assert.equal(socket.readyState,1,'recent inbound bytes prevent the old probe from closing the socket');
    watchdog(); assert.equal(pushIsConnected(),false,'new total silence must still retire the socket');
    return {lastInboundAt:pushGetLatency().lastInboundAt};
  }
  if (scenario==='rpc-demand') {
    const socket=open();
    const controller=new AbortController();
    const requests=Array.from({length:128},(_,index)=>pushRpcRequest(
      'project.browse',{path:`/${index}`},{timeout:10000,signal:index===0?controller.signal:undefined}));
    const outcomes=Promise.allSettled(requests);
    const sent=socket.sent.filter(frame=>frame.method==='project.browse');
    assert.equal(sent.length,requests.length,'every accepted read starts before any earlier response');
    controller.abort();
    for(const request of [...sent].reverse()) {
      socket.frame({jsonrpc:'2.0',id:request.id,result:{path:request.params.path}});
    }
    const results=await outcomes;
    assert.equal(results[0].status,'rejected');
    assert.equal(results[0].reason.name,'AbortError');
    results.slice(1).forEach((value,index)=>assert.deepEqual(value,{status:'fulfilled',value:{path:`/${index+1}`}}));
    return {started:sent.length,fulfilled:results.filter(value=>value.status==='fulfilled').length};
  }
  throw new Error(`Unknown scenario ${scenario}`);
}
run().then(result=>console.log(JSON.stringify(result))).catch(error=>{console.error(error);process.exitCode=1;});
"""


def _run(scenario: str, close_mode: str = "delayed") -> dict:
    require_node_modules()
    script = (
        "const scenario=" + json.dumps(scenario)
        + ", closeMode=" + json.dumps(close_mode) + ";\n"
        + _HARNESS.replace("SOURCE", json.dumps(runtime_section("push.js")))
    )
    completed = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_internal_error_reconnects_but_policy_refusal_stays_terminal():
    assert _run("transient-close") == {"reconnects": 1}


def test_remote_normal_close_stays_terminal():
    assert _run("normal-close") == {"reconnects": 0}


def test_explicit_connection_consumes_retry_before_a_terminal_policy_close():
    assert _run("superseded-retry") == {"reconnects": 1}


def test_close_observer_replacement_owns_its_terminal_policy_close():
    assert _run("observer-reentry") == {"reconnects": 1}


@pytest.mark.parametrize("close_mode", ["delayed", "normal", "throws"])
def test_silence_recovery_does_not_wait_for_physical_close(close_mode):
    assert _run("watchdog", close_mode) == {
        "reconnects": 1, "settled": "rpc_disconnected",
    }


@pytest.mark.parametrize("frame", ["malformed", "ping", "stale-pong"])
def test_inbound_bytes_disarm_silence_without_fabricating_rtt(frame):
    assert _run("proof-of-life", frame) == {"lastInboundAt": 0}


def test_rpc_reads_start_on_demand_and_keep_independent_cancellation():
    assert _run("rpc-demand") == {"started": 128, "fulfilled": 127}
