"""bridge.mjs contract against a stub dx-engine (real Node process, no network).

The stub implements the engine surface the bridge uses: BrowserlessDxRuntime
with ``webSdkRuntime`` (login, addEventHandler, instance constants) and
``handle(request)``, plus ``api/messages.js#decodeDxMessageRows``.
"""
import json
import os
import shutil
import sys
import time

import pytest

from lib.channels.dx.adapter import DxBridge, _BRIDGE_SOURCE

pytestmark = pytest.mark.unit

NODE = shutil.which('node') or os.path.join(os.path.dirname(sys.executable), 'node')

STUB_HANDLER = r"""
const MODE = process.env.STUB_MODE || 'normal';
export class BrowserlessDxRuntime {
  constructor() {
    const handlers = new Map();
    this.handlers = handlers;
    const emit = (event, payload) => { for (const h of handlers.get(event) ?? []) h(payload); };
    this.emit = emit;
    // The real SDK's LOGIN_STATUS values; its ConnStatusChange carries (status, reason),
    // which the engine fans out as one [status, reason] array.
    const LOGIN_STATUS = { ERROR: -1, CONNECTING: 1, CONNECTED: 2, AUTHENTICATING: 5, AUTHENTICATED: 6,
                           DISCONNECTING: 7, DISCONNECTED: 8, TERMINATED: 9, AUTHENTICATE_TIMEOUT: 10 };
    const constants = { MESSAGE_EVENT: { IM_MESSAGE: 'IM_message', PUB_MESSAGE: 'PUB_MESSAGE' } };
    if (MODE !== 'no-login-status') constants.LOGIN_STATUS = LOGIN_STATUS;
    // A kick reports DISCONNECTED('terminated') first, then its kick event in the same call.
    const kick = (event) => { emit('ConnStatusChange', [LOGIN_STATUS.DISCONNECTED, 'terminated']); emit(event, {}); };
    let logins = 0;
    let mints = 0;
    this.authenticated = false;
    this.credentials = () => ({ logins, mints });
    // The engine's credential snapshot of this connection (credential-provider.js).
    this.credentialProvider = { async getCredentials() {
      return { uid: '1001', appId: '1', xsid: 'stub-xsid', alToken: 'stub-al', deviceId: 'stub-device',
               cookieHeader: 'stub-cookie', dxSsoId: 'stub-sso', dxSign: 'stub-sign' };
    } };
    const runtime = this;
    let socketConnected = true;
    this.disconnect = () => {
      socketConnected = false;
      this.authenticated = false;
      emit('ConnStatusChange', [LOGIN_STATUS.DISCONNECTED, 'onClose']);
    };
    this.webSdkRuntime = {
      instance: { CONSTANT: constants, reconnect() { socketConnected = true; } },
      async login(timeout, forceRefresh = false) {
        logins += 1;
        if (logins === 1 || forceRefresh) mints += 1;
        if (logins > 1) {
          if (!socketConnected) throw new Error('SDK login needs a connected socket');
          if (MODE === 'drop-fail') throw new Error('SDK reconnect failed');
          if (MODE === 'drop-pending') return new Promise(() => {});
          if (MODE === 'drop-kick') { kick('KickOff'); return new Promise(() => {}); }
          if (MODE === 'drop-identity') return { credentials: { uid: 'another-owner' } };
          if (MODE === 'drop-expired') {
            // The engine owns AuthFail recovery: it refreshes once and logs in
            // on the same instance. Its intermediate disconnect is not a new loss.
            socketConnected = false;
            emit('AuthFail', {});
            emit('ConnStatusChange', [LOGIN_STATUS.DISCONNECTED, 'authenticate_fail']);
            mints += 1;
          }
          await new Promise((resolve) => setTimeout(resolve, 50));
          if (!socketConnected) throw new Error('SDK auth retry needs a connected socket');
          runtime.authenticated = true;
          setImmediate(() => emit('IM_message', { mid: '7000000000000000004', uuid: 'after-reconnect',
                                                 from: '2002', gid: '500', message: { text: 'recovered' } }));
          return { credentials: { uid: '1001' }, events: ['AuthSuccess'] };
        }
        runtime.authenticated = true;
        // Messages arrive with no command in flight: only persistent handlers see them.
        setTimeout(() => emit('IM_message', { mid: { low: 1, high: 2, toString() { return '7000000000000000001'; } },
                                              uuid: 'm1', from: '2002', gid: '500', type: 1,
                                              message: { text: 'first' } }), 30);
        setTimeout(() => emit('PUB_MESSAGE', { mid: '7000000000000000002', from: '9009', pubUid: '9009',
                                               message: { text: 'notice' } }), 60);
        setTimeout(() => emit('IM_message', { mid: '7000000000000000003', from: '2002', gid: '500', type: 20,
                                              message: { searchText: 'quoted', quoted: JSON.stringify([
                                                { message: JSON.stringify({ mid: '6999', fromUid: '1001' }) }]) } }), 90);
        if (MODE === 'kick') setTimeout(() => kick('KickOff'), 150);
        if (MODE === 'kick-same-device') setTimeout(() => kick('KickOffWithSameDeviceId'), 150);
        if (MODE.startsWith('drop')) setTimeout(() => {
          runtime.authenticated = false;
          socketConnected = false;
          emit('ConnStatusChange', [LOGIN_STATUS.CONNECTING, 'connecting']);
          emit('ConnStatusChange', [LOGIN_STATUS.DISCONNECTED, 'disconnected']);
          emit('ConnStatusChange', [LOGIN_STATUS.DISCONNECTED, 'duplicate']);
        }, 150);
        return { credentials: { uid: '1001' }, events: ['AuthSuccess'] };
      },
      addEventHandler(event, handler) {
        if (!handlers.has(event)) handlers.set(event, new Set());
        handlers.get(event).add(handler);
        return () => handlers.get(event).delete(handler);
      },
    };
    if (MODE === 'no-reconnect') delete this.webSdkRuntime.instance.reconnect;
  }
  async handle(request) {
    const action = request.action;
    if (action === 'get_current_uid') return { action, ok: true, uid: '1001', name: 'Owner', mis: 'owner',
                                             ...this.credentials() };
    if (action === 'get_history_messages') return { action, ok: true, nextCursor: '', target: request.gid,
                                                    limit: request.limit, cursor: request.cursorMsg ?? null,
                                                    messages: [{ id: '7000000000000000001', text: 'first' }] };
    if (action === 'get_session_list') return { action, ok: true, sessions: [{ id: '500', type: 'group', all: request.all }] };
    if (action === 'send_markdown') {
      if (!this.authenticated) return { action, ok: false, error: { code: 'not_connected' } };
      if (MODE === 'send-fails') {
        this.sendAttempts = (this.sendAttempts || 0) + 1;
        this.disconnect();
        return { action, ok: false, error: { code: 'send_outcome_unknown' }, attempts: this.sendAttempts };
      }
      setTimeout(() => this.emit('IM_message', { mid: '7000000000000000009', uuid: 'sent-1', from: '1001',
                                                 gid: request.gid, message: { text: request.text },
                                                 extension: request.extension }), 10);
      return { action, ok: true, status: 'confirmed', msgUuid: 'sent-1', serverMsgId: '7000000000000000009' };
    }
    if (action === 'send_image' || action === 'send_attachment') {
      if (!this.authenticated) return { action, ok: false, error: { code: 'not_connected' } };
      setTimeout(() => this.emit('IM_message', { mid: '7000000000000000011', uuid: 'file-1', from: '1001',
                                                 gid: request.gid, type: action === 'send_image' ? 4 : 8,
                                                 message: { name: 'chart.png' }, extension: request.extension }), 10);
      return { action, ok: true, status: 'confirmed', msgUuid: 'file-1', serverMsgId: '7000000000000000011',
               filePath: request.filePath, extension: request.extension, gid: request.gid };
    }
    if (action === 'create_group') return { action, ok: true, gid: '4242', memberUids: request.memberUids };
    return { action, ok: false, error: { code: 'unsupported_browserless_action', message: action } };
  }
}
"""
STUB_MESSAGES = r"""
export async function decodeDxMessageRows(data) { return (data.msgs || []).map((item) => JSON.parse(item)); }
"""


@pytest.fixture
def engine(tmp_path):
    if not os.path.exists(NODE):
        pytest.skip('node is not available')
    root = tmp_path / 'engine'
    (root / 'api').mkdir(parents=True)
    (root / 'handler.js').write_text(STUB_HANDLER)
    (root / 'api' / 'messages.js').write_text(STUB_MESSAGES)
    return str(root)


def _bridge(engine, mode='normal'):
    env = {**os.environ, 'STUB_MODE': mode}
    return DxBridge([NODE, _BRIDGE_SOURCE.path(), '--engine', engine], env=env, cwd=engine)


def _events(bridge, count):
    events = []
    while len(events) < count:
        item = bridge.events.get(timeout=10)
        if item is None:
            break
        events.append(item)
    return events


def test_persistent_handlers_commands_and_clean_stop(engine):
    bridge = _bridge(engine)
    try:
        connected, first, notice, quote = _events(bridge, 4)
        assert connected == {'type': 'status', 'status': 'connected', 'selfUid': '1001'}
        assert first['source'] == 'im' and first['message']['mid'] == '7000000000000000001'
        assert notice['source'] == 'pub' and notice['message']['pubUid'] == '9009'
        assert quote['quotes'] == [{'mid': '6999', 'from': '1001'}]
        whoami = bridge.request('whoami')
        assert whoami['ok'] and whoami['result']['uid'] == '1001'
        history = bridge.request('history', gid='500', limit=100, cursor='')
        assert history['result']['limit'] == 100 and history['result']['target'] == '500'
        sessions = bridge.request('sessions', startMs=1, endMs=2, limit=500)
        assert sessions['result']['sessions'][0]['all'] is True
        sent = bridge.request('send_markdown', gid='500', text='hi', extension='{"tofuOutbox":"ob1"}')
        assert sent['ok'] and sent['result']['serverMsgId'] == '7000000000000000009'
        [echo] = _events(bridge, 1)
        assert echo['ownConnection'] is True  # the bridge's own send is flagged
        refused = bridge.request('mark_all_read')
        assert refused['ok'] is False and refused['error']['code'] == 'unsupported_command'
    finally:
        assert bridge.close(5)
    assert bridge.returncode == 0  # closing stdin is the stop signal


@pytest.mark.parametrize('mode, status, code', [
    ('kick', 'kicked', 43),
    ('kick-same-device', 'kicked', 43),
    ('drop-fail', 'failed', 4),
    ('drop-kick', 'kicked', 43),
    ('drop-identity', 'failed', 4),
    # Without the SDK's own status vocabulary a lost channel would go unnoticed.
    ('no-login-status', 'failed', 4),
    ('no-reconnect', 'failed', 4),
])
def test_kickoff_and_disconnect_exit_with_their_status(engine, mode, status, code):
    bridge = _bridge(engine, mode)
    events = _events(bridge, 10)
    assert {'type': 'status', 'status': status} == {k: events[-1][k] for k in ('type', 'status')}
    deadline = time.monotonic() + 10
    while bridge.alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert bridge.returncode == code
    with pytest.raises(Exception):
        bridge.request('whoami')
    bridge.close(5)


@pytest.mark.parametrize('mode, mints', [('drop', 1), ('drop-expired', 2)])
def test_disconnect_reuses_credentials_and_keeps_commands_and_pushes(engine, mode, mints):
    bridge = _bridge(engine, mode)
    try:
        events = _events(bridge, 5)
        assert [e.get('status') for e in events if e['type'] == 'status'] == ['connected', 'disconnected']
        # Submitted while the reconnect is pending: it must wait for login and
        # execute once, so it cannot provoke a second login/credential mint.
        sent = bridge.request('send_markdown', gid='500', text='hi')
        assert sent['ok']
        received = _events(bridge, 3)
        assert received[0]['status'] == 'connected'
        assert {e['message']['uuid'] for e in received[1:]} == {'after-reconnect', 'sent-1'}
        assert next(e for e in received[1:] if e['message']['uuid'] == 'sent-1')['ownConnection'] is True
        identity = bridge.request('whoami')['result']
        assert (identity['logins'], identity['mints']) == (2, mints)
        assert bridge.alive()
    finally:
        assert bridge.close(5)
    assert bridge.returncode == 0


def test_parent_close_stops_a_pending_reconnect(engine):
    bridge = _bridge(engine, 'drop-pending')
    try:
        assert _events(bridge, 5)[-1]['status'] == 'disconnected'
    finally:
        assert bridge.close(5)
    assert bridge.returncode == 0


def test_disconnect_never_replays_an_inflight_write(engine):
    bridge = _bridge(engine, 'send-fails')
    try:
        assert _events(bridge, 4)[0]['status'] == 'connected'
        result = bridge.request('send_markdown', gid='500', text='once')
        assert result['ok'] is False
        assert result['error']['code'] == 'send_outcome_unknown'
        recovered = _events(bridge, 3)
        assert [e.get('status') for e in recovered[:2]] == ['disconnected', 'connected']
        assert recovered[2]['message']['uuid'] == 'after-reconnect'
        assert result['result']['attempts'] == 1
        assert bridge.request('whoami')['result']['mints'] == 1
    finally:
        assert bridge.close(5)


class _MediaServer:
    """A fake 大象 file host: the file path redirects to a signed location, like the pan host."""

    def __init__(self):
        import http.server
        import threading
        server = self
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                server.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                if self.path == '/pan/dl/pan/im/4/file/loop':
                    self.send_response(302)
                    self.send_header('Location', self.path)
                    self.end_headers()
                elif self.path == '/pan/dl/pan/im/4/file/deck':
                    self.send_response(302)
                    self.send_header('Location', '/signed/deck?sig=abc')
                    self.end_headers()
                elif self.path.startswith('/signed/deck'):
                    body = b'%PDF-1.7 stub deck'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/pdf')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(403)
                    self.send_header('Content-Length', '0')
                    self.end_headers()

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_download_sends_the_sdk_file_headers_only_to_dx_file_hosts(engine, tmp_path):
    server = _MediaServer()
    env = {**os.environ, 'STUB_MODE': 'normal', 'NO_PROXY': '127.0.0.1,localhost',
           'no_proxy': '127.0.0.1,localhost'}
    bridge = DxBridge([NODE, _BRIDGE_SOURCE.path(), '--engine', engine], env=env, cwd=engine)
    prefixes = [f'{server.base}/pan/dl/']
    try:
        assert _events(bridge, 4)[0]['status'] == 'connected'
        target = tmp_path / 'deck.bin'
        fetched = bridge.request('download', url=f'{server.base}/pan/dl/pan/im/4/file/deck',
                                 token='file-token', path=str(target), credentialPrefixes=prefixes)
        assert fetched['ok'], fetched
        assert fetched['result'] == {'bytes': 18, 'contentType': 'application/pdf'}
        assert target.read_bytes() == b'%PDF-1.7 stub deck'
        (first_path, first), (signed_path, signed) = server.requests
        assert first_path == '/pan/dl/pan/im/4/file/deck'
        # HTTPCommon.getFileBlob's headers: the file token plus the IM session's identity.
        assert {key: first.get(key) for key in ('x-xmftk', 'ai', 'dt', 'u', 'uu', 'ck', 'al')} == {
            'x-xmftk': 'file-token', 'ai': '1', 'dt': '2', 'u': '1001', 'uu': 'stub-device',
            'ck': 'stub-xsid', 'al': 'stub-al'}
        assert not {'cookie', 'dx-sso', 'dx-sign'} & set(first)
        # The signed location outside the file host receives no credentials at all.
        assert signed_path == '/signed/deck?sig=abc'
        assert not {'x-xmftk', 'ck', 'al', 'u', 'uu', 'cookie'} & set(signed)

        server.requests.clear()
        outside = bridge.request('download', url=f'{server.base}/elsewhere/x', token='file-token',
                                 path=str(tmp_path / 'x.bin'), credentialPrefixes=prefixes)
        assert outside['ok'] is False and outside['error']['code'] == 'untrusted_host'
        assert server.requests == [] and not (tmp_path / 'x.bin').exists()
        refused = bridge.request('download', url=f'{server.base}/pan/dl/pan/im/4/file/gone',
                                 path=str(tmp_path / 'gone.bin'), credentialPrefixes=prefixes)
        assert refused['ok'] is False and refused['error']['code'] == 'http_403'
        assert 'x-xmftk' not in server.requests[0][1]  # no token, no token header
        server.requests.clear()
        looping = bridge.request('download', url=f'{server.base}/pan/dl/pan/im/4/file/loop',
                                 path=str(tmp_path / 'loop.bin'), credentialPrefixes=prefixes)
        assert looping['ok'] is False and looping['error']['code'] == 'redirect_limit'
        assert len(server.requests) == 21  # WHATWG Fetch: 20 redirects followed, the 21st refused
    finally:
        assert bridge.close(5)
        server.close()


def test_one_shot_commands_print_one_response(engine):
    import subprocess
    for args, key in ((['--command', 'whoami'], 'uid'),
                      (['--command', 'create_group', '--name', 'Tofu Router', '--member', '1001'], 'gid')):
        done = subprocess.run([NODE, _BRIDGE_SOURCE.path(), '--engine', engine, *args],
                              capture_output=True, timeout=30, check=False)
        lines = [json.loads(line) for line in done.stdout.decode().splitlines() if line.startswith('{')]
        assert done.returncode == 0 and lines[-1]['type'] == 'response' and lines[-1]['ok']
        assert lines[-1]['result'][key]
    group = json.loads(done.stdout.decode().splitlines()[-1])
    assert group['result']['memberUids'] == ['1001']  # single-member group: the owner only


@pytest.mark.parametrize('op', ['send_image', 'send_attachment'])
def test_attachment_sends_hand_the_file_and_its_marker_to_the_engine(engine, op):
    bridge = _bridge(engine)
    try:
        assert _events(bridge, 4)[0]['status'] == 'connected'
        sent = bridge.request(op, gid='500', filePath='/tmp/x/chart.png', extension='{"tofuOutbox":"ob1/0"}')
        assert sent['ok'] and sent['result']['serverMsgId'] == '7000000000000000011'
        assert (sent['result']['filePath'], sent['result']['extension'], sent['result']['gid']) == (
            '/tmp/x/chart.png', '{"tofuOutbox":"ob1/0"}', '500')
        [echo] = _events(bridge, 1)
        assert echo['ownConnection'] is True  # the session's own file message
        assert echo['message']['extension'] == '{"tofuOutbox":"ob1/0"}'
    finally:
        assert bridge.close(5)
