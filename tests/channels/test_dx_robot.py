"""大象 notification robot (docs/CHANNELS.md C6): token, send, fallback, echoes, configuration.

The fake below plays the official endpoints the robot uses: the 企平 SSO token
endpoint (应用认证接入指引 §3.2.1/§3.2.4), the 办公开放平台 group send API
(大象消息 §2.2) and its group creation API (大象群管理 §1 机器人创建群), at the
robot's HTTP transport seam. No network, vault or 大象 process; every
credential here is synthetic.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import queue
import uuid
from urllib.parse import parse_qsl

import pytest

from lib.channels.model import ChannelError, ThreadKey
from tests.channels.conftest import OWNER, OWNER_UID, ROUTER, make_event

pytestmark = pytest.mark.unit

CLIENT_ID = 'tofu-robot-fake-client'
SECRET = 'fake-robot-secret-not-real-0001'  # synthetic, never a real credential
COMMAND_CHAT = 'dx:gid:900'
# 大象群管理 §1.2 (https://km.internal.example.com/collabpage/1357068390), production domain.
CREATE_GROUP_URL = 'https://xopen.internal.example.com/open-apis/dx-group/createGroup'


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + '=' * (-len(part) % 4))


class FakeOpenPlatform:
    """Token endpoint plus ``sendGroupMsgByRobot``, checking requests as documented."""

    def __init__(self, robot, *, client_id=CLIENT_ID, secret=SECRET):
        self.robot = robot
        self.client_id, self.secret = client_id, secret
        self.requests: list[dict] = []
        self.tokens: list[str] = []
        self.valid: set[str] = set()
        self.send_codes: list[int] = []
        self.create_codes: list[int] = []
        self.posted_texts: list[str] = []  # texts the API accepted (code 0)
        self.expires_in = 3600
        self.mid = 7500000000000000000
        self.gid = 64014644158
        self.fail_send_with: Exception | None = None
        self.fail_create_with: Exception | None = None
        self.fail_token_with: Exception | None = None
        self.now = 1_793_000_000.0

    def __call__(self, method, url, headers, body):
        self.requests.append({'method': method, 'url': url, 'headers': dict(headers), 'body': body})
        assert method == 'POST'
        if url == self.robot.TOKEN_ENDPOINT:
            return self._token(headers, body)
        if url == CREATE_GROUP_URL:
            return self._create_group(headers, body)
        assert url == self.robot.API_BASE + self.robot.SEND_GROUP_PATH
        return self._send(headers, body)

    def sends(self):
        return [r for r in self.requests if r['url'] == self.robot.API_BASE + self.robot.SEND_GROUP_PATH]

    def creates(self):
        return [r for r in self.requests if r['url'] == CREATE_GROUP_URL]

    def token_requests(self):
        return [r for r in self.requests if r['url'] == self.robot.TOKEN_ENDPOINT]

    def _token(self, headers, body):
        if self.fail_token_with is not None:
            raise self.fail_token_with
        assert headers['Content-Type'] == 'application/x-www-form-urlencoded'
        form = dict(parse_qsl(body.decode('utf-8'), strict_parsing=True))
        assert set(form) == {'grant_type', 'scope', 'client_id', 'client_assertion_type', 'client_assertion'}
        assert form['grant_type'] == 'client_credentials'
        assert form['scope'] == 'client_id:xm-xai'
        assert form['client_id'] == self.client_id
        assert form['client_assertion_type'] == 'urn:ietf:params:oauth:client-assertion-type:jwt-bearer'
        header_b64, claims_b64, signature_b64 = form['client_assertion'].split('.')
        assert json.loads(_b64decode(header_b64)) == {'alg': 'HS256', 'typ': 'JWT'}
        claims = json.loads(_b64decode(claims_b64))
        assert claims['sub'] == claims['iss'] == self.client_id
        assert claims['aud'] == [self.robot.TOKEN_ENDPOINT]
        assert claims['iat'] == int(self.now) and claims['exp'] - claims['iat'] == 86400
        uuid.UUID(claims['jti'])
        expected = hmac.new(self.secret.encode('utf-8'), f'{header_b64}.{claims_b64}'.encode('ascii'),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(_b64decode(signature_b64), expected):
            return 401, json.dumps({'error': 'invalid_client',
                                    'error_description': 'Client secret JWT authentication failed'}).encode()
        token = f'AT-fake-{len(self.tokens) + 1}'
        self.tokens.append(token)
        self.valid.add(token)
        return 200, json.dumps({'access_token': token, 'token_type': 'Bearer',
                                'expires_in': self.expires_in}).encode()

    def _send(self, headers, body):
        if self.fail_send_with is not None:
            raise self.fail_send_with
        assert headers['Content-Type'] == 'application/json; charset=utf-8'
        scheme, _, token = headers['Authorization'].partition(' ')
        if scheme != 'Bearer' or token not in self.valid:
            return 401, json.dumps({'status': {'code': 20005, 'msg': 'token is missing'}}).encode()
        payload = json.loads(body.decode('utf-8'))
        assert set(payload) == {'gid', 'sendMsgInfo'} and isinstance(payload['gid'], int)
        info = payload['sendMsgInfo']
        assert set(info) == {'type', 'body', 'extension', 'isDynamicMsg'}
        assert info['type'] == 'text' and info['isDynamicMsg'] is False
        assert json.loads(info['extension']) == {'fileType': 'markdown'}
        text = json.loads(info['body'])['text']
        assert 1 <= len(text.encode('utf-8')) <= 6000
        code = self.send_codes.pop(0) if self.send_codes else 0
        if code == 0:
            self.mid += 1
            self.posted_texts.append(text)
            return 200, json.dumps({'status': {'code': 0, 'msg': '成功'},
                                    'data': {'messageMap': {str(payload['gid']): str(self.mid)},
                                             'failMap': {}}}).encode()
        if code == 20004:
            self.valid.discard(token)
        http = 500 if code == -1 else 403 if code in (10003, 30002) else 200
        return http, json.dumps({'status': {'code': code, 'msg': f'refused {code}'}}).encode()

    def _create_group(self, headers, body):
        if self.fail_create_with is not None:
            raise self.fail_create_with
        assert headers['Content-Type'] == 'application/json; charset=utf-8'
        scheme, _, token = headers['Authorization'].partition(' ')
        if scheme != 'Bearer' or token not in self.valid:
            return 401, json.dumps({'status': {'code': 20005, 'msg': 'token is missing'}}).encode()
        payload = json.loads(body.decode('utf-8'))
        # CreateGroupReq: optional name (<= 100 chars), owner a real user's uid; the robot is a member.
        assert set(payload) <= {'name', 'info', 'admins', 'users', 'owner', 'botAdmins'}
        assert isinstance(payload.get('owner'), int) and len(payload.get('name', '')) <= 100
        code = self.create_codes.pop(0) if self.create_codes else 0
        if code == 0:
            self.gid += 1
            return 200, json.dumps({'status': {'code': 0, 'msg': '成功'}, 'gid': self.gid}).encode()
        http = 500 if code == -1 else 200
        return http, json.dumps({'status': {'code': code, 'msg': f'refused {code}'}}).encode()

    def sent_texts(self):
        return [json.loads(json.loads(r['body'])['sendMsgInfo']['body'])['text'] for r in self.sends()]


@pytest.fixture
def robot_module():
    from lib.channels.dx import robot
    return robot


@pytest.fixture
def platform(robot_module):
    return FakeOpenPlatform(robot_module)


@pytest.fixture
def clock(platform):
    class Clock:
        def __init__(self):
            self.monotonic = 100.0
            self.sleeps: list[float] = []

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.monotonic += seconds
    return Clock()


def make_robot(robot_module, platform, clock, secret=SECRET):
    return robot_module.DxRobot(CLIENT_ID, lambda: secret, transport=platform,
                                clock=lambda: clock.monotonic, wall_clock=lambda: platform.now,
                                sleep=clock.sleep)


# ── token and send, exactly as documented ─────────────────────────────

def test_token_is_a_client_secret_jwt_grant_and_the_text_posts_as_markdown(robot_module, platform, clock):
    robot = make_robot(robot_module, platform, clock)
    message, = robot_module.text_messages('**done**')
    assert robot.post_group('900', message) == '7500000000000000001'
    token_request, send = platform.requests
    assert 'client_secret' not in dict(parse_qsl(token_request['body'].decode()))
    assert send['headers']['Authorization'] == 'Bearer AT-fake-1'
    assert json.loads(send['body']) == {'gid': 900, 'sendMsgInfo': {
        'type': 'text', 'body': json.dumps({'text': '**done**'}, ensure_ascii=False),
        'extension': json.dumps({'fileType': 'markdown'}), 'isDynamicMsg': False}}


def test_token_is_reused_until_expiry_then_minted_again(robot_module, platform, clock):
    robot = make_robot(robot_module, platform, clock)
    message, = robot_module.text_messages('one')
    robot.post_group('900', message)
    robot.post_group('900', message)
    assert len(platform.token_requests()) == 1
    clock.monotonic += platform.expires_in
    robot.post_group('900', message)
    assert len(platform.token_requests()) == 2


def test_a_token_the_api_rejects_is_minted_again_once(robot_module, platform, clock):
    robot = make_robot(robot_module, platform, clock)
    message, = robot_module.text_messages('hi')
    platform.send_codes = [20004]
    assert robot.post_group('900', message)
    assert len(platform.token_requests()) == 2 and len(platform.sends()) == 2


def test_a_wrong_secret_is_refused_before_anything_is_posted(robot_module, platform, clock):
    robot = make_robot(robot_module, platform, clock, secret='not-the-secret')
    message, = robot_module.text_messages('hi')
    with pytest.raises(robot_module.RobotRefused) as refused:
        robot.post_group('900', message)
    assert refused.value.code == 'token_rejected' and platform.sends() == []
    assert 'not-the-secret' not in str(refused.value)


def test_sends_keep_the_documented_rate(robot_module, platform, clock):
    robot = make_robot(robot_module, platform, clock)
    message, = robot_module.text_messages('x')
    for _ in range(3):
        robot.post_group('900', message)
    assert clock.sleeps == [pytest.approx(0.2), pytest.approx(0.2)]


@pytest.mark.parametrize('code', [70001, 70003, 30002, 20003])
def test_configuration_refusals_mean_nothing_was_posted(robot_module, platform, clock, code):
    robot = make_robot(robot_module, platform, clock)
    platform.send_codes = [code]
    with pytest.raises(robot_module.RobotRefused) as refused:
        robot.post_group('900', robot_module.text_messages('hi')[0])
    assert refused.value.code == f'robot_{code}'


def test_rate_limit_defers_and_backs_off_one_window(robot_module, platform, clock):
    robot = make_robot(robot_module, platform, clock)
    platform.send_codes = [10003]
    with pytest.raises(robot_module.RobotDeferred):
        robot.post_group('900', robot_module.text_messages('hi')[0])
    requests = len(platform.requests)
    with pytest.raises(robot_module.RobotDeferred):
        robot.post_group('900', robot_module.text_messages('again')[0])
    assert len(platform.requests) == requests, 'nothing is requested inside the throttled window'
    clock.monotonic += robot_module.RATE_WINDOW_S
    assert robot.post_group('900', robot_module.text_messages('later')[0])


@pytest.mark.parametrize('failure', ['server_error', 'network'])
def test_server_errors_and_lost_responses_leave_the_outcome_unknown(robot_module, platform, clock, failure):
    robot = make_robot(robot_module, platform, clock)
    if failure == 'server_error':
        platform.send_codes = [-1]
    else:
        platform.fail_send_with = ConnectionError('reset')
    with pytest.raises(robot_module.RobotOutcomeUnknown):
        robot.post_group('900', robot_module.text_messages('hi')[0])


def test_long_text_splits_at_the_documented_size_on_line_ends(robot_module):
    line = '进展更新：一切正常。\n'
    text = line * 900
    parts = robot_module.text_parts(text)
    assert ''.join(parts) == text and len(parts) > 1
    assert all(len(part.encode('utf-8')) <= 6000 for part in parts)
    assert all(part.endswith('\n') for part in parts[:-1])
    unbroken = 'あ' * 2500
    pieces = robot_module.text_parts(unbroken)
    assert ''.join(pieces) == unbroken and all(len(p.encode('utf-8')) <= 6000 for p in pieces)


def test_production_transport_is_the_shared_http_client(robot_module, monkeypatch):
    calls = []

    class Response:
        status_code, content = 200, b'{}'

        def close(self):
            calls.append('closed')

    def http_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return Response()
    monkeypatch.setattr('lib.http_transport.client.http_request', http_request)
    assert robot_module.http_transport('POST', 'https://x.example/p', {'A': 'b'}, b'{}') == (200, b'{}')
    assert calls[0] == ('POST', 'https://x.example/p', {'headers': {'A': 'b'}, 'data': b'{}',
                                                       'allow_redirects': False})
    assert calls[-1] == 'closed'


@pytest.mark.parametrize('account, expected', [
    ({'commandChat': COMMAND_CHAT, 'robot': {'clientId': CLIENT_ID}}, COMMAND_CHAT),
    ({'commandChat': COMMAND_CHAT, 'robot': None}, ''),
    ({'commandChat': 'dx:uid:2002', 'robot': {'clientId': CLIENT_ID}}, ''),
    ({'commandChat': None, 'robot': {'clientId': CLIENT_ID}}, ''),
])
def test_only_a_group_command_chat_is_the_robot_thread(account, expected):
    from lib.channels.policy import robot_thread
    assert robot_thread(account) == expected


# ── the adapter's sender port and the outbox ──────────────────────────

class FakeBridge:
    def __init__(self, history=None):
        self.events = queue.Queue()
        self.sends = []
        self.history = history or []

    def request(self, op, **args):
        if op in ('send_markdown', 'reply_message'):
            self.sends.append((op, args))
            return {'ok': True, 'result': {'serverMsgId': '7400000000000000001', 'msgUuid': 'uu-1'}}
        if op == 'history':
            return {'ok': True, 'result': {'messages': self.history if not args.get('cursor') else [],
                                           'nextCursor': '-1'}}
        raise AssertionError(op)

    def alive(self):
        return True


def bind(store, thread=COMMAND_CHAT):
    """The owner's own message in ``thread``, seen by this server's session, binds it for the robot (C6)."""
    from lib.channels.dx.robot import bind_owner_message
    owner_says = make_event(thread, external_id='7500000000000000900', sender=OWNER_UID, text='hi')
    return bind_owner_message(store, store.account('dx'), owner_says)


@pytest.fixture
def robot_account(store, enabled, router, dispatch, mailbox, vault):
    enabled(command_chat=COMMAND_CHAT)
    entry = f'channel.u{OWNER}.dx.robot'
    vault.set_entry(entry, SECRET)
    store.update_account('dx', lambda account: {
        **account, 'robot': {'clientId': CLIENT_ID, 'credential': {'name': entry, 'managed': True}}})
    bind(store)
    return store


def make_adapter(store, platform, clock, robot_module, bridge=None):
    from lib.channels.dx.adapter import DxAdapter
    bridge = bridge or FakeBridge()
    adapter = DxAdapter(OWNER, store=store, bridge_factory=lambda owner: bridge,
                        robot_factory=lambda client_id, secret: robot_module.DxRobot(
                            client_id, secret, transport=platform, clock=lambda: clock.monotonic,
                            wall_clock=lambda: platform.now, sleep=clock.sleep))
    adapter._bridge = bridge
    adapter._self_uid = OWNER_UID  # the uid the live session logged in as (bridge ``connected``)
    adapter._connected.set()
    return adapter, bridge


def enqueue(store, text, thread=COMMAND_CHAT, key='k1'):
    from lib.channels import outbox
    return outbox.enqueue(OWNER, thread, text, idempotency_key=key,
                          origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': key}, store=store)


def test_command_chat_items_go_out_under_the_robot(robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    item = enqueue(robot_account, 'report ready')
    assert outbox.execute(OWNER, adapter, channel='dx', store=robot_account) == 1
    stored, _ = robot_account.outbox_item(item.id)
    assert stored.state == 'sent' and stored.external_id == '7500000000000000001'
    assert bridge.sends == [] and platform.sent_texts() == ['report ready']
    assert stored.extra['robotTexts'] == ['report ready']


def test_long_items_post_every_part_in_order(robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    adapter, _ = make_adapter(robot_account, platform, clock, robot_module)
    text = '第一段\n' * 1200
    item = enqueue(robot_account, text)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    assert ''.join(platform.sent_texts()) == text and len(platform.sends()) > 1
    stored, _ = robot_account.outbox_item(item.id)
    assert stored.state == 'sent' and stored.external_id == str(platform.mid)


def test_other_threads_and_accounts_without_a_robot_keep_the_owner_identity(
        robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    robot_account.update_thread(ThreadKey.parse('dx:uid:2002'),
                                lambda value: {**value, 'replyPolicy': 'auto'})
    enqueue(robot_account, 'to a colleague', thread='dx:uid:2002', key='k2')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    robot_account.update_account('dx', lambda account: {**account, 'robot': None})
    enqueue(robot_account, 'no robot', key='k3')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    assert platform.requests == [] and [args['text'] for _, args in bridge.sends] == [
        '[Tofu 代发] to a colleague', 'no robot']


def test_a_definite_robot_refusal_falls_back_to_the_owner_identity(robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    platform.send_codes = [70003]
    item = enqueue(robot_account, 'still delivered')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    stored, _ = robot_account.outbox_item(item.id)
    assert stored.state == 'sent' and stored.external_id == '7400000000000000001'
    assert [args['text'] for _, args in bridge.sends] == ['still delivered']
    assert 'robotTexts' not in stored.extra
    error = robot_account.account('dx')['robot']['error']
    assert error['code'] == 'robot_70003' and SECRET not in json.dumps(robot_account.account('dx'))


def test_a_rate_limited_robot_send_goes_as_the_owner_at_once(robot_account, platform, clock, robot_module):
    """The application's quota is shared by every server holding it (C6): no waiting for it."""
    from lib.channels import outbox
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    platform.send_codes = [10003]
    item = enqueue(robot_account, 'now')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    stored, _ = robot_account.outbox_item(item.id)
    assert stored.state == 'sent' and owner_texts(bridge) == ['now'] and platform.posted_texts == []
    assert 'robotTexts' not in stored.extra and robot_account.account('dx')['robot'].get('error') is None
    assert notices(robot_account) == []


def test_a_rate_limit_after_posted_parts_sends_only_the_rest_as_the_owner(
        robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    text = numbered_lines(1100)
    parts = robot_module.text_parts(text)
    platform.send_codes = [0, 10003]
    item = enqueue(robot_account, text)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    assert robot_account.outbox_item(item.id)[0].state == 'sent'
    assert platform.posted_texts == [parts[0]] and owner_texts(bridge) == [parts[1]]


def test_an_unknown_robot_send_reconciles_by_its_posted_text(robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    row = {'id': '7500000000000000077', 'text': 'maybe sent',
           'raw': {'mid': '7500000000000000077', 'from': '137626262046', 'type': 1,
                   'message': {'text': 'maybe sent'}}}
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module, FakeBridge([row]))
    platform.fail_send_with = TimeoutError('read timed out')
    item = enqueue(robot_account, 'maybe sent')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    assert robot_account.outbox_item(item.id)[0].state == 'unknown'
    platform.fail_send_with = None
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    stored, _ = robot_account.outbox_item(item.id)
    assert stored.state == 'sent' and stored.external_id == '7500000000000000077'
    assert len(platform.sends()) == 1 and bridge.sends == []


# ── echoes: the robot's messages reach the owner's connection ─────────

def test_robot_echoes_in_the_command_chat_never_reach_the_router(robot_account, platform, clock,
                                                                 robot_module, dispatch):
    from lib.channels import outbox
    from lib.channels.ingress import ingest
    adapter, _ = make_adapter(robot_account, platform, clock, robot_module)
    enqueue(robot_account, 'done')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    echo = make_event(COMMAND_CHAT, external_id=str(platform.mid), sender='137626262046', text='done')
    assert ingest(OWNER, echo, store=robot_account).outcome == 'echo'
    # Another sender in the robot's chat (a member, or another server's robot post) is context only.
    other = make_event(COMMAND_CHAT, external_id='7500000000000009999', sender='2002', text='done?')
    result = ingest(OWNER, other, store=robot_account)
    assert (result.outcome, result.disposition) == ('recorded', 'context') and dispatch.calls == []


def test_a_robot_echo_ahead_of_the_http_response_is_matched_by_its_text(robot_account, dispatch):
    from lib.channels import outbox
    from lib.channels.ingress import ingest
    item = enqueue(robot_account, 'in flight')
    outbox._transition(robot_account, item.id, lambda current: (
        setattr(current, 'state', 'sending') or current))
    outbox.record_robot_texts(robot_account, item.id, ['in flight'])
    echo = make_event(COMMAND_CHAT, external_id='7500000000000000555', sender='137626262046', text='in flight')
    assert ingest(OWNER, echo, store=robot_account).outcome == 'echo'
    elsewhere = make_event('dx:gid:500', external_id='7500000000000000556', sender='137626262046',
                           text='in flight')
    assert ingest(OWNER, elsewhere, store=robot_account).outcome != 'echo'
    assert dispatch.calls == []


def test_without_a_robot_third_party_text_in_the_command_chat_is_not_an_echo(store, enabled, router,
                                                                             dispatch, mailbox):
    from lib.channels.ingress import ingest
    enabled(command_chat=COMMAND_CHAT)
    event = make_event(COMMAND_CHAT, external_id='7500000000000000600', sender='2002', text='hello')
    assert ingest(OWNER, event, store=store).outcome == 'recorded'


# ── configuration: the secret lives only in the credential vault ──────

def test_robot_configuration_stores_the_secret_in_the_vault_only(store, enabled, router, vault, monkeypatch):
    from lib.channels import service
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', lambda owner: None)
    enabled(command_chat=COMMAND_CHAT)
    probed = []
    view = service.update_account(OWNER, {'robot': {'clientId': CLIENT_ID, 'secret': SECRET}}, store=store,
                                  robot_probe=lambda client_id, secret: probed.append((client_id, secret)))
    entry = f'channel.u{OWNER}.dx.robot'
    assert probed == [(CLIENT_ID, SECRET)] and vault.get_entry(entry) == SECRET
    assert view['robot'] == {'clientId': CLIENT_ID, 'credentialName': entry,
                             'credentialHint': view['robot']['credentialHint'], 'managed': True,
                             'builtIn': False, 'error': None}
    assert SECRET not in json.dumps(store.account('dx')) and SECRET not in json.dumps(view)
    view = service.update_account(OWNER, {'robot': None}, store=store)
    assert view['robot'] is None and vault.get_entry(entry) is None


def test_an_unverified_secret_is_never_stored(store, enabled, router, vault, monkeypatch):
    from lib.channels import service
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', lambda owner: None)
    enabled(command_chat=COMMAND_CHAT)

    def refuse(client_id, secret):
        raise ChannelError('robot_token_rejected', 'The SSO token endpoint refused the robot credentials')
    with pytest.raises(ChannelError):
        service.update_account(OWNER, {'robot': {'clientId': CLIENT_ID, 'secret': SECRET}}, store=store,
                               robot_probe=refuse)
    assert vault.get_entry(f'channel.u{OWNER}.dx.robot') is None and not store.account('dx').get('robot')


def test_an_existing_vault_entry_can_hold_the_secret(store, enabled, router, vault, monkeypatch):
    from lib.channels import service
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', lambda owner: None)
    enabled(command_chat=COMMAND_CHAT)
    vault.set_entry('dx-robot-secret', SECRET)
    view = service.update_account(OWNER, {'robot': {'clientId': CLIENT_ID, 'credentialName': 'dx-robot-secret'}},
                                  store=store, robot_probe=lambda client_id, secret: None)
    assert view['robot']['credentialName'] == 'dx-robot-secret' and view['robot']['managed'] is False
    service.update_account(OWNER, {'robot': None}, store=store)
    assert vault.get_entry('dx-robot-secret') == SECRET


@pytest.mark.parametrize('robot', [
    {'secret': SECRET},
    {'clientId': '', 'secret': SECRET},
    {'clientId': CLIENT_ID, 'secret': 'has space'},
    {'clientId': CLIENT_ID},
    {'clientId': CLIENT_ID, 'credentialName': f'channel.u{OWNER}.github.token'},
    {'clientId': CLIENT_ID, 'secret': SECRET, 'extra': 1},
])
def test_invalid_robot_updates_are_refused(store, enabled, router, vault, monkeypatch, robot):
    from lib.channels import service
    monkeypatch.setattr('lib.channels.dx.supervisor.notify_account_changed', lambda owner: None)
    enabled(command_chat=COMMAND_CHAT)
    with pytest.raises(ChannelError) as refused:
        service.update_account(OWNER, {'robot': robot}, store=store, robot_probe=lambda client_id, secret: None)
    assert refused.value.code == 'invalid_update'


# ── the command chat: the robot creates it, the owner is its 群主 ─────

def robot_factory(robot_module, platform, clock):
    return lambda client_id, secret: robot_module.DxRobot(
        client_id, secret, transport=platform, clock=lambda: clock.monotonic,
        wall_clock=lambda: platform.now, sleep=clock.sleep)


@pytest.fixture
def robot_without_chat(store, enabled, router, vault):
    """An enabled account with a Settings robot and no command chat yet."""
    enabled()
    entry = f'channel.u{OWNER}.dx.robot'
    vault.set_entry(entry, SECRET)
    store.update_account('dx', lambda account: {
        **account, 'robot': {'clientId': CLIENT_ID, 'credential': {'name': entry, 'managed': True}}})
    return store


def create_chat(store, platform, clock, robot_module, created):
    """``create_command_chat`` with the fake open platform and a recording owner-session creator."""
    from lib.channels import service

    def owner_session(owner, *, name, member_uid, mis):
        created.append((name, member_uid, mis))
        return '4242'
    return service.create_command_chat(OWNER, store=store, group_creator=owner_session,
                                       robot_factory=robot_factory(robot_module, platform, clock))


def notices(store, thread=COMMAND_CHAT):
    return [item for item in store.active_outbox(thread) if item.origin.get('kind') == 'robot_notice']


def test_the_robot_creates_the_command_chat_with_the_owner_as_group_owner(
        robot_without_chat, platform, clock, robot_module):
    created = []
    view = create_chat(robot_without_chat, platform, clock, robot_module, created)
    create, = platform.creates()
    assert json.loads(create['body']) == {'name': 'Tofu Router', 'owner': int(OWNER_UID)}
    assert create['headers']['Authorization'] == 'Bearer AT-fake-1'
    assert view['commandChat'] == f'dx:gid:{platform.gid}' and created == []
    assert robot_without_chat.thread(ThreadKey.parse(view['commandChat']))['title'] == 'Tofu Router'
    assert view['robot']['error'] is None and notices(robot_without_chat, view['commandChat']) == []


@pytest.mark.parametrize('code', [70001, 40004, 60001, 10003])
def test_a_robot_that_creates_nothing_leaves_the_group_to_the_owner_session_and_says_why(
        robot_without_chat, platform, clock, robot_module, code):
    platform.create_codes = [code]
    created = []
    view = create_chat(robot_without_chat, platform, clock, robot_module, created)
    assert view['commandChat'] == 'dx:gid:4242' and created == [('Tofu Router', OWNER_UID, 'owner')]
    assert len(platform.creates()) == 1, 'one attempt, no retry'
    error = view['robot']['error']
    assert error['code'] in (f'robot_{code}', 'robot_rate_limited') and f'refused {code}' in error['detail']
    notice, = notices(robot_without_chat, 'dx:gid:4242')
    assert error['code'] in notice.text and SECRET not in notice.text


def test_an_unreachable_token_endpoint_still_gets_the_owner_his_group(
        robot_without_chat, platform, clock, robot_module):
    platform.fail_token_with = ConnectionError('proxy refused')
    created = []
    view = create_chat(robot_without_chat, platform, clock, robot_module, created)
    assert view['commandChat'] == 'dx:gid:4242' and len(created) == 1 and platform.creates() == []
    assert view['robot']['error']['code'] == 'token_unavailable'


@pytest.mark.parametrize('failure', ['server_error', 'network'])
def test_an_unknown_robot_creation_never_makes_a_second_group(
        robot_without_chat, platform, clock, robot_module, failure):
    if failure == 'server_error':
        platform.create_codes = [-1]
    else:
        platform.fail_create_with = TimeoutError('read timed out')
    created = []
    with pytest.raises(ChannelError) as raised:
        create_chat(robot_without_chat, platform, clock, robot_module, created)
    account = robot_without_chat.account('dx')
    assert raised.value.code == 'group_create_unknown' and created == []
    assert account.get('commandChat') is None and account.get('commandChatCreation') is None
    assert account['robot']['error']['code'] == 'robot_unknown'


# ── the Internal package's built-in robot (owner decision 2026-11-08) ──

BUILTIN_CLIENT_ID = 'tofu-builtin-fake-client'
BUILTIN_SECRET = 'fake-builtin-secret-not-real-0002'  # synthetic, never a real credential


@pytest.fixture
def builtin_robot(robot_module, tmp_path, monkeypatch):
    path = tmp_path / 'robot_builtin.json'
    path.write_text(json.dumps({'clientId': BUILTIN_CLIENT_ID, 'secret': BUILTIN_SECRET}), encoding='utf-8')
    monkeypatch.setattr(robot_module, 'BUILTIN_CREDENTIAL_FILE', path)
    return path


@pytest.fixture
def builtin_platform(robot_module):
    return FakeOpenPlatform(robot_module, client_id=BUILTIN_CLIENT_ID, secret=BUILTIN_SECRET)


def test_the_builtin_robot_posts_when_settings_configure_none(
        store, enabled, router, dispatch, mailbox, builtin_robot, builtin_platform, clock, robot_module):
    from lib.channels import outbox, service
    enabled(command_chat=COMMAND_CHAT)
    bind(store)
    adapter, bridge = make_adapter(store, builtin_platform, clock, robot_module)
    item = enqueue(store, 'from the package')
    outbox.execute(OWNER, adapter, channel='dx', store=store)
    assert store.outbox_item(item.id)[0].state == 'sent' and bridge.sends == []
    assert builtin_platform.posted_texts == ['from the package']
    view = service.account_view(OWNER, store=store)
    assert view['robot'] == {'clientId': BUILTIN_CLIENT_ID, 'credentialName': '', 'credentialHint': '',
                             'managed': False, 'builtIn': True, 'error': None}
    assert BUILTIN_SECRET not in json.dumps(view) and BUILTIN_SECRET not in json.dumps(store.account('dx'))


def test_a_settings_robot_overrides_the_builtin_one(robot_account, builtin_robot, platform, clock, robot_module):
    from lib.channels import outbox, service
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    enqueue(robot_account, 'settings win')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    assert platform.posted_texts == ['settings win'] and bridge.sends == []
    assert service.account_view(OWNER, store=robot_account)['robot']['builtIn'] is False


def test_the_builtin_robot_creates_the_command_chat_too(
        store, enabled, router, builtin_robot, builtin_platform, clock, robot_module):
    enabled()
    created = []
    view = create_chat(store, builtin_platform, clock, robot_module, created)
    assert view['commandChat'] == f'dx:gid:{builtin_platform.gid}' and created == []
    assert view['robot']['builtIn'] is True


def test_source_checkouts_have_no_builtin_robot(store, enabled, robot_module):
    from lib.channels.policy import robot_thread
    enabled(command_chat=COMMAND_CHAT)
    assert robot_module.builtin_credential() is None and robot_thread(store.account('dx')) == ''


# ── falling back to the owner's own account, and back (C6) ────────────

def owner_texts(bridge):
    return [args['text'] for _, args in bridge.sends]


def numbered_lines(count):
    """A long text whose robot parts all differ (2 parts for 1100 lines, 3 for 1600)."""
    return ''.join(f'第{index}行\n' for index in range(count))


def test_bot_ok_posts_everything_and_notifies_nothing(robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    for key in ('a', 'b'):
        enqueue(robot_account, f'update {key}', key=key)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    assert platform.posted_texts == ['update a', 'update b'] and bridge.sends == []
    assert notices(robot_account) == [] and robot_account.account('dx')['robot'].get('error') is None


def test_bot_revoked_mid_stream_finishes_as_the_owner_without_repeating_a_part(
        robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    text = numbered_lines(1100)
    parts = robot_module.text_parts(text)
    assert len(parts) == 2
    platform.send_codes = [0] + [70001] * 8  # the application is disabled after the first part
    item = enqueue(robot_account, text)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    stored, _ = robot_account.outbox_item(item.id)
    assert stored.state == 'sent'
    assert platform.posted_texts == [parts[0]] and owner_texts(bridge) == [parts[1]]
    assert robot_account.account('dx')['robot']['error']['code'] == 'robot_70001'
    notice, = notices(robot_account)
    assert 'robot_70001' in notice.text
    enqueue(robot_account, 'next update', key='k2')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    # Each later message still tries the robot first, then goes as the owner; one notice only.
    assert owner_texts(bridge) == [parts[1], notice.text, 'next update']
    assert platform.posted_texts == [parts[0]] and notices(robot_account) == []


def test_bot_recovers_with_one_notice_and_posts_again(robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    platform.send_codes = [70001, 70001]
    enqueue(robot_account, 'while revoked', key='a')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    down = owner_texts(bridge)
    assert down[0] == 'while revoked' and len(down) == 2 and 'robot_70001' in down[1]
    enqueue(robot_account, 'back again', key='b')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    up, = notices(robot_account)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    enqueue(robot_account, 'steady', key='c')
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    assert platform.posted_texts == ['back again', up.text, 'steady'] and owner_texts(bridge) == down
    assert robot_account.account('dx')['robot'].get('error') is None and notices(robot_account) == []


def test_a_part_with_an_unknown_outcome_resumes_without_reposting_earlier_parts(
        robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module)
    text = numbered_lines(1100)
    parts = robot_module.text_parts(text)
    platform.send_codes = [0, -1]
    item = enqueue(robot_account, text)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    assert robot_account.outbox_item(item.id)[0].state == 'unknown'
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    stored, _ = robot_account.outbox_item(item.id)
    assert stored.state == 'sent' and platform.posted_texts == parts and bridge.sends == []
    assert notices(robot_account) == []


def test_a_part_the_history_shows_is_not_posted_again(robot_account, platform, clock, robot_module):
    from lib.channels import outbox
    text = numbered_lines(1600)
    parts = robot_module.text_parts(text)
    assert len(parts) == 3
    rows = [{'id': mid, 'text': part, 'raw': {'mid': mid, 'from': '137626262046', 'type': 1,
                                              'message': {'text': part}}}
            for mid, part in (('7500000000000000101', parts[0]), ('7500000000000000102', parts[1]))]
    adapter, bridge = make_adapter(robot_account, platform, clock, robot_module, FakeBridge(rows))
    platform.send_codes = [0, -1]  # the second part's answer is lost, yet the history shows it
    item = enqueue(robot_account, text)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    outbox.execute(OWNER, adapter, channel='dx', store=robot_account)
    stored, _ = robot_account.outbox_item(item.id)
    assert stored.state == 'sent' and platform.posted_texts == [parts[0], parts[2]]
    assert platform.sent_texts().count(parts[1]) == 1 and bridge.sends == []
