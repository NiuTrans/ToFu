"""Push control frames never target a CONNECTING or superseded socket."""

from __future__ import annotations

import subprocess

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit


def test_replacement_socket_queues_until_open_and_ignores_stale_close():
    harness = r"""
const fs = require('fs');
global.window = { location: { protocol: 'http:', host: 'localhost:15000' } };
global.apiUrl = (path) => path;
global.Api = { pageRequestId: () => 'page' };
global.setInterval = () => 1;
global.clearInterval = () => {};
global.setTimeout = () => 1;
global.clearTimeout = () => {};

const sockets = [];
class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    sockets.push(this);
  }
  send(raw) {
    if (this.readyState !== FakeWebSocket.OPEN) {
      throw new Error('InvalidStateError: send while not OPEN');
    }
    this.sent.push(JSON.parse(raw));
  }
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen();
  }
}
global.WebSocket = FakeWebSocket;
eval(fs.readFileSync(process.argv[1], 'utf8'));

function check(name, condition) {
  if (!condition) throw new Error(name);
}
function actionCount(socket, action) {
  return socket.sent.filter((frame) => frame.action === action).length;
}

pushConnect();
const first = sockets[0];
pushSend({action: 'abort-first'});
check('initial CONNECTING send is queued', actionCount(first, 'abort-first') === 0);
first.open();
check('initial queue flushes on open', actionCount(first, 'abort-first') === 1);

first.readyState = FakeWebSocket.CLOSING;
pushConnect();
const second = sockets[1];
pushSend({action: 'abort-second'});
check('replacement CONNECTING send is queued', actionCount(second, 'abort-second') === 0);

first.readyState = FakeWebSocket.CLOSED;
first.onclose({code: 1006});
second.open();
check('stale close does not clear replacement', pushIsConnected());
check('replacement queue flushes on open', actionCount(second, 'abort-second') === 1);
console.log('ok');
"""
    proc = subprocess.run(
        ['node', '-e', harness, runtime_section_path('push.js')],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 'ok' in proc.stdout
