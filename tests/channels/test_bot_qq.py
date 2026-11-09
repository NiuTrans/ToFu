"""QQ 官方机器人 adapter against a fake token endpoint, OpenAPI and gateway (C12)."""
from __future__ import annotations

import threading

import pytest

from tests.channels.bot_fakes import (
    OTHER_ID, OWNER_ID, QQ_APP_ID, QQ_SECRET, QQ_TOKEN, FakeQQ, eventually, qq_c2c, qq_group,
)
from tests.channels.conftest import OWNER, ROUTER
from tests.channels.test_bot_wecom import Sink, run

pytestmark = pytest.mark.unit


@pytest.fixture
def qq():
    fake = FakeQQ()
    yield fake
    fake.close()


def transport_for(fake, clock=None, **extra):
    from lib.channels.qq.transport import QQTransport
    return QQTransport(QQ_APP_ID, lambda: QQ_SECRET, api_base=fake.base, token_url=fake.token_url,
                       clock=clock, **extra)


def test_token_request_and_identify_follow_the_documented_auth(qq):
    sink, transport = Sink(), transport_for(qq)
    run(transport, sink)
    eventually(lambda: sink.info is not None)
    assert qq.token_requests == [{'appId': QQ_APP_ID, 'clientSecret': QQ_SECRET}]
    [gateway] = [r for r in qq.api_requests if r['path'] == '/gateway']
    assert gateway['auth'] == f'QQBot {QQ_TOKEN}'
    [identify] = qq.ops(2)
    assert identify['d']['token'] == f'QQBot {QQ_TOKEN}'
    assert identify['d']['intents'] == 1 << 25 and identify['d']['shard'] == [0, 1]
    assert sink.info == {'appId': 'bot-1', 'name': 'Tofu Bot'}
    sink.stopped.set()
    transport.close()


def test_a_refused_secret_needs_login(qq):
    from lib.channels.bots.platform import SessionEnded
    qq.token_answer = (401, {'code': 100016, 'message': 'invalid appid or secret'})
    sink = Sink()
    outcome = run(transport_for(qq), sink)
    eventually(lambda: outcome.get('done'))
    assert isinstance(outcome['error'], SessionEnded) and outcome['error'].state == 'needs_login'


def test_the_token_is_reused_until_its_documented_refresh_window(qq):
    now = [1_000.0]
    transport = transport_for(qq, clock=lambda: now[0])
    assert transport.token() == QQ_TOKEN and transport.token() == QQ_TOKEN
    assert len(qq.token_requests) == 1
    now[0] += 7200 - 60
    transport.token()
    assert len(qq.token_requests) == 2


def test_private_and_group_messages_are_normalized(qq):
    from lib.channels.bots import ids
    sink, transport = Sink(), transport_for(qq)
    run(transport, sink)
    eventually(lambda: sink.info is not None)
    qq.dispatch('C2C_MESSAGE_CREATE', qq_c2c('ROBOT1.0_a', '你好'))
    qq.dispatch('GROUP_AT_MESSAGE_CREATE', qq_group('ROBOT1.0_b', ' 查一下天气 '))
    first, second = eventually(lambda: sink.batches if len(sink.batches) >= 2 else None)
    assert (first.kind, first.peer, first.sender, first.text) == ('uid', OWNER_ID, OWNER_ID, '你好')
    assert first.sent_at_s == 1_793_930_400 and first.reply == {'msgId': 'ROBOT1.0_a', 'atS': 1_793_930_400}
    assert (second.kind, second.peer, second.sender, second.text) == ('gid', 'GROUPOPENID01', OTHER_ID, '查一下天气')
    assert ids.external_id(first.sent_at_s, first.message_id) != ids.external_id(second.sent_at_s, second.message_id)
    sink.stopped.set()
    transport.close()


def test_heartbeats_carry_the_last_sequence_and_a_reconnect_resumes(qq):
    sink, transport = Sink(), transport_for(qq, heartbeat_s=0.05)
    run(transport, sink)
    eventually(lambda: sink.info is not None)
    qq.dispatch('C2C_MESSAGE_CREATE', qq_c2c('ROBOT1.0_a', 'x'))
    eventually(lambda: any(frame.get('d') == qq.seq for frame in qq.ops(1)))
    last = qq.seq
    qq.connections[-1].send('{"op": 7}')  # the gateway asks for a reconnect
    [resume] = eventually(lambda: qq.ops(6))
    assert resume['d'] == {'token': f'QQBot {QQ_TOKEN}', 'session_id': 'sess-1', 'seq': last}
    sink.stopped.set()
    transport.close()


def test_a_reply_inside_the_passive_window_quotes_the_inbound_message_idempotently(qq):
    now = [1_793_930_400 + 30.0]
    transport = transport_for(qq, clock=lambda: now[0])
    reply = {'msgId': 'ROBOT1.0_a', 'atS': 1_793_930_400}
    result = transport.send('uid', OWNER_ID, '好的', item_id='ob1', reply=reply)
    again = transport.send('uid', OWNER_ID, '好的', item_id='ob1', reply=reply)
    first, second = qq.sends()
    assert first['path'] == f'/v2/users/{OWNER_ID}/messages' and first['auth'] == f'QQBot {QQ_TOKEN}'
    assert first['body']['msg_id'] == 'ROBOT1.0_a' and first['body']['content'] == '好的'
    assert first['body']['msg_type'] == 0 and first['body']['msg_seq'] == second['body']['msg_seq']
    assert result['ok'] and again['ok']
    assert result['reply'] == {'msgId': 'ROBOT1.0_a', 'atS': 1_793_930_400, 'used': 1}


def test_a_duplicate_answer_proves_the_earlier_send(qq):
    transport = transport_for(qq, clock=lambda: 1_793_930_400 + 1.0)
    qq.send_answers.append((400, {'code': 40054005, 'message': '消息被去重'}))
    result = transport.send('uid', OWNER_ID, 'x', item_id='ob1', reply={'msgId': 'm', 'atS': 1_793_930_400})
    assert result['ok'] is True


def test_an_expired_passive_window_falls_back_to_an_active_message(qq):
    transport = transport_for(qq, clock=lambda: 1_793_930_400 + 10.0)
    qq.send_answers.append((400, {'code': 40034128, 'message': '被动回复时间或次数超限'}))
    result = transport.send('gid', 'GROUPOPENID01', 'x', item_id='ob1', reply={'msgId': 'm', 'atS': 1_793_930_400})
    passive, active = qq.sends()
    assert passive['path'] == '/v2/groups/GROUPOPENID01/messages' and passive['body']['msg_id'] == 'm'
    assert 'msg_id' not in active['body'] and result['ok'] is True
    stale = transport_for(qq, clock=lambda: 1_793_930_400 + 3_601.0)
    stale.send('uid', OWNER_ID, 'late', item_id='ob2', reply={'msgId': 'm', 'atS': 1_793_930_400})
    assert 'msg_id' not in qq.sends()[-1]['body']


@pytest.mark.parametrize('status,body,code', [
    (400, {'code': 40054013, 'message': '用户拒收消息'}, 'forbidden'),
    (400, {'code': 40054007, 'message': '消息长度超限'}, 'invalid_input'),
    (401, {'code': 11244, 'message': 'token invalid'}, 'auth_failed'),
    (400, {'code': 40034100, 'message': '主动消息发送超过频控限制'}, 'rate_limited'),
    (502, {'message': 'bad gateway'}, 'unknown'),
])
def test_send_refusals_are_classified(qq, status, body, code):
    from lib.channels.outbox import DEFINITE_SEND_FAILURES
    transport = transport_for(qq)
    qq.send_answers.append((status, body))
    result = transport.send('uid', OWNER_ID, 'x', item_id='ob1', reply={})
    assert result['ok'] is False and result['error']['code'] == code
    assert (code in DEFINITE_SEND_FAILURES) is (code in {'forbidden', 'invalid_input', 'auth_failed'})


def test_end_to_end_command_and_receipt(qq, store, router, dispatch, mailbox, vault):
    from lib.channels.bots import account
    from lib.channels.bots.session import BotSession
    account.update_account(OWNER, 'qq', {'appId': QQ_APP_ID, 'secret': QQ_SECRET, 'enabled': True},
                           store=store, prober=lambda *args: None)
    code = store.account('qq')['pairing']['code']
    session = BotSession(OWNER, 'qq', store=store, transport=transport_for(qq, clock=lambda: 1_793_930_410.0))
    session.start()
    try:
        eventually(lambda: store.account('qq')['status']['state'] == 'connected')
        qq.dispatch('C2C_MESSAGE_CREATE', qq_c2c('ROBOT1.0_p', code, timestamp='2026-11-06T10:00:00+08:00'))
        eventually(lambda: store.account('qq').get('identity'))
        qq.dispatch('C2C_MESSAGE_CREATE', qq_c2c('ROBOT1.0_c', '帮我订会议室', timestamp='2026-11-06T10:00:05+08:00'))
        eventually(lambda: dispatch.to(ROUTER))
        eventually(lambda: len(qq.sends()) >= 2)
        assert dispatch.to(ROUTER)[0]['message']['origin']['initiator'] == 'human'
        assert {request['body'].get('msg_id') for request in qq.sends()} <= {'ROBOT1.0_p', 'ROBOT1.0_c'}
    finally:
        assert session.stop(5.0)
