"""Browser-side correlation, cancellation, and disconnect contracts."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_push_rpc_correlates_cancels_and_rejects_disconnects():
    if not shutil.which('node'):
        pytest.skip('node is required')
    source = runtime_section_path('push.js')
    script = f"""
const assert = require('assert');
global.window = globalThis;
global.location = {{protocol:'http:', host:'example.test', pathname:'/proxy/9000/'}};
global.document = {{hidden:false, addEventListener:() => {{}}}};
global.apiUrl = (path) => '/proxy/9000' + path;
global.Api = {{pageRequestId:() => 'page1'}};

class FakeWebSocket {{
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static instances = [];
  constructor(url) {{
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    FakeWebSocket.instances.push(this);
  }}
  send(value) {{ this.sent.push(JSON.parse(value)); }}
  close() {{
    this.readyState = FakeWebSocket.CLOSED;
    if (this.onclose) this.onclose({{code:1000}});
  }}
}}
global.WebSocket = FakeWebSocket;
require({json.dumps(source)});

(async () => {{
  let firstCode = '';
  try {{ await global.pushRpcRequest('project.browse', {{path:'~'}}, {{timeout:100}}); }}
  catch (error) {{ firstCode = error.code; }}
  assert.strictEqual(firstCode, 'rpc_unavailable');
  const socket = FakeWebSocket.instances[0];
  socket.readyState = FakeWebSocket.OPEN;
  socket.onopen();

  const pending = global.pushRpcRequest(
    'project.browse', {{path:'/workspace'}}, {{timeout:1000}});
  const request = socket.sent.find((frame) => frame.method === 'project.browse');
  assert(request);
  socket.onmessage({{data:JSON.stringify({{
    jsonrpc:'2.0', id:request.id,
    result:{{ok:true,path:'/workspace',dirs:[],parent:null,filesCount:0,truncated:false}},
  }})}});
  const result = await pending;
  assert.strictEqual(result.path, '/workspace');

  const controller = new AbortController();
  const cancelled = global.pushRpcRequest(
    'project.browse', {{path:'/slow'}},
    {{timeout:1000, signal:controller.signal}});
  const cancelRequest = socket.sent.filter(
    (frame) => frame.method === 'project.browse').at(-1);
  controller.abort();
  let abortName = '';
  try {{ await cancelled; }} catch (error) {{ abortName = error.name; }}
  assert.strictEqual(abortName, 'AbortError');
  assert(socket.sent.some((frame) =>
    frame.method === '$/cancelRequest' && frame.params.id === cancelRequest.id));

  const disconnected = global.pushRpcRequest(
    'project.browse', {{path:'/disconnect'}}, {{timeout:1000}});
  socket.readyState = FakeWebSocket.CLOSED;
  socket.onclose({{code:1000}});
  let disconnectCode = '';
  try {{ await disconnected; }} catch (error) {{ disconnectCode = error.code; }}
  assert.strictEqual(disconnectCode, 'rpc_disconnected');

  console.log(JSON.stringify({{
    firstCode, resultPath:result.path, abortName, disconnectCode,
  }}));
}})().catch((error) => {{ console.error(error); process.exitCode = 1; }});
"""
    completed = subprocess.run(
        ['node', '-e', script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        'firstCode': 'rpc_unavailable',
        'resultPath': '/workspace',
        'abortName': 'AbortError',
        'disconnectCode': 'rpc_disconnected',
    }
