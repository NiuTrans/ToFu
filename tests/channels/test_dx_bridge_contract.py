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
    this.webSdkRuntime = {
      instance: { CONSTANT: constants },
      async login() {
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
        if (MODE === 'drop') setTimeout(() => {
          // Transitional statuses keep the session; only DISCONNECTED ends the channel.
          emit('ConnStatusChange', [LOGIN_STATUS.CONNECTING, 'connecting']);
          emit('ConnStatusChange', [LOGIN_STATUS.DISCONNECTED, 'disconnected']);
        }, 150);
        return { credentials: { uid: '1001' }, events: ['AuthSuccess'] };
      },
      addEventHandler(event, handler) {
        if (!handlers.has(event)) handlers.set(event, new Set());
        handlers.get(event).add(handler);
        return () => handlers.get(event).delete(handler);
      },
    };
  }
  async handle(request) {
    const action = request.action;
    if (action === 'get_current_uid') return { action, ok: true, uid: '1001', name: 'Owner', mis: 'owner' };
    if (action === 'get_history_messages') return { action, ok: true, nextCursor: '', target: request.gid,
                                                    limit: request.limit, cursor: request.cursorMsg ?? null,
                                                    messages: [{ id: '7000000000000000001', text: 'first' }] };
    if (action === 'get_session_list') return { action, ok: true, sessions: [{ id: '500', type: 'group', all: request.all }] };
    if (action === 'send_markdown') {
      setTimeout(() => this.emit('IM_message', { mid: '7000000000000000009', uuid: 'sent-1', from: '1001',
                                                 gid: request.gid, message: { text: request.text },
                                                 extension: request.extension }), 10);
      return { action, ok: true, status: 'confirmed', msgUuid: 'sent-1', serverMsgId: '7000000000000000009' };
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
    ('drop', 'disconnected', 3),
    # Without the SDK's own status vocabulary a lost channel would go unnoticed.
    ('no-login-status', 'failed', 4),
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
