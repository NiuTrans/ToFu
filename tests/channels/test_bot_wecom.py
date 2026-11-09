"""企业微信 智能机器人 adapter against a fake long-connection server (C12).

The production ``WeComTransport`` connects to a local WebSocket server that
answers aibot_subscribe, ping and aibot_send_msg as the 长连接 document says.
"""
from __future__ import annotations

import threading

import pytest

from tests.channels.bot_fakes import (
    OTHER_ID, OWNER_ID, WECOM_BOT_ID, WECOM_SECRET, FakeWeCom, eventually, wecom_text,
)
from tests.channels.conftest import OWNER, ROUTER

pytestmark = pytest.mark.unit


@pytest.fixture
def wecom():
    fake = FakeWeCom()
    yield fake
    fake.close()


class Sink:
    def __init__(self):
        self.stopped = threading.Event()
        self.batches: list = []
        self.info = None

    def connected(self, info):
        self.info = dict(info)

    def deliver(self, batch, checkpoint=None):
        self.batches.extend(batch)


def run(transport, sink):
    outcome = {}

    def target():
        try:
            transport.run(sink)
        except BaseException as error:  # the test inspects the session outcome
            outcome['error'] = error
        outcome['done'] = True
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return outcome


def transport_for(fake, **extra):
    from lib.channels.wecom.transport import WeComTransport
    return WeComTransport(WECOM_BOT_ID, lambda: WECOM_SECRET, ws_url=fake.url, **extra)


def test_subscribes_with_the_bot_id_and_secret_then_reports_connected(wecom):
    sink, transport = Sink(), transport_for(wecom)
    outcome = run(transport, sink)
    eventually(lambda: sink.info is not None)
    [frame] = wecom.sent('aibot_subscribe')
    assert frame['body'] == {'bot_id': WECOM_BOT_ID, 'secret': WECOM_SECRET} and frame['headers']['req_id']
    sink.stopped.set()
    transport.close()
    eventually(lambda: outcome.get('done'))
    assert 'error' not in outcome


def test_a_refused_subscription_needs_a_new_secret(wecom):
    from lib.channels.bots.platform import SessionEnded
    wecom.subscribe_errcode = 40001
    sink = Sink()
    outcome = run(transport_for(wecom), sink)
    eventually(lambda: outcome.get('done'))
    assert isinstance(outcome['error'], SessionEnded) and outcome['error'].state == 'needs_login'
    assert sink.info is None


def test_heartbeat_pings_keep_the_connection(wecom):
    sink, transport = Sink(), transport_for(wecom, heartbeat_s=0.05)
    run(transport, sink)
    eventually(lambda: len(wecom.sent('ping')) >= 2)
    sink.stopped.set()
    transport.close()


def test_callbacks_are_normalized_and_non_user_callbacks_skipped(wecom):
    sink, transport = Sink(), transport_for(wecom)
    run(transport, sink)
    wecom.push(wecom_text('msg-1', '你好'))
    wecom.push(wecom_text('msg-2', '@Tofu 看下', sender=OTHER_ID, chat='wrGROUP01'))
    wecom.push(wecom_text('msg-3', '', msgtype='voice', voice={'content': '语音转写的内容'}))
    wecom.push(wecom_text('msg-4', '', msgtype='mixed', mixed={'msg_item': [
        {'msgtype': 'text', 'text': {'content': '看图'}}, {'msgtype': 'image', 'image': {'url': 'https://x'}}]}))
    wecom.push(wecom_text('msg-5', 'from an assistant', compat_info={'source': 'ai_assistant'}))
    wecom.push(wecom_text('msg-6', '', msgtype='file', file={'url': 'https://x', 'aeskey': 'k'}))
    batches = eventually(lambda: sink.batches if len(sink.batches) >= 5 else None)
    by_id = {item.message_id: item for item in batches}
    assert set(by_id) == {'msg-1', 'msg-2', 'msg-3', 'msg-4', 'msg-6'}
    assert (by_id['msg-1'].kind, by_id['msg-1'].peer, by_id['msg-1'].sender) == ('uid', OWNER_ID, OWNER_ID)
    assert (by_id['msg-2'].kind, by_id['msg-2'].peer, by_id['msg-2'].sender) == ('gid', 'wrGROUP01', OTHER_ID)
    assert by_id['msg-3'].text == '语音转写的内容'
    assert by_id['msg-4'].text == '看图 [image]'
    assert by_id['msg-6'].text == '[file]'
    assert all(item.sent_at_s is None for item in batches)  # no provider time: the store mints IDs
    sink.stopped.set()
    transport.close()


def test_send_posts_markdown_to_the_chat_and_returns_its_answer(wecom):
    sink, transport = Sink(), transport_for(wecom)
    run(transport, sink)
    eventually(lambda: sink.info is not None)
    assert transport.send('uid', OWNER_ID, '**done**', item_id='ob1', reply={})['ok'] is True
    assert transport.send('gid', 'wrGROUP01', 'hi all', item_id='ob2', reply={})['ok'] is True
    first, second = wecom.sent('aibot_send_msg')
    assert first['body'] == {'chatid': OWNER_ID, 'chat_type': 1, 'msgtype': 'markdown',
                             'markdown': {'content': '**done**'}}
    assert second['body']['chatid'] == 'wrGROUP01' and second['body']['chat_type'] == 2
    wecom.send_errcode = 40008
    refused = transport.send('uid', OWNER_ID, 'x', item_id='ob3', reply={})
    assert refused['ok'] is False and refused['error']['code'] == 'invalid_input'
    sink.stopped.set()
    transport.close()


def test_a_send_outcome_is_unknown_when_the_connection_drops_before_its_answer(wecom):
    sink, transport = Sink(), transport_for(wecom)
    outcome = run(transport, sink)
    eventually(lambda: sink.info is not None)
    wecom.drop_on_send = True
    result = transport.send('uid', OWNER_ID, 'x', item_id='ob1', reply={})
    assert result['ok'] is False and result['error']['code'] == 'unknown'
    eventually(lambda: outcome.get('done'))


def test_send_posts_exactly_one_message_whatever_its_length(wecom):
    """The transport sends one platform message per intent: chunking a long text is the core's (channel
    protocol P4/P5), so a retried send can never repeat a part already delivered."""
    sink, transport = Sink(), transport_for(wecom)
    run(transport, sink)
    eventually(lambda: sink.info is not None)
    text = 'x' * 30000
    assert transport.send('uid', OWNER_ID, text, item_id='ob1', reply={})['ok'] is True
    [frame] = wecom.sent('aibot_send_msg')
    assert frame['body']['markdown']['content'] == text
    sink.stopped.set()
    transport.close()


def test_a_long_text_resumes_at_the_chunk_whose_answer_was_lost(wecom, store, router, dispatch, mailbox):
    """The core chunks a text beyond 企业微信's markdownBytes, one aibot_send_msg per chunk. The connection
    drops after chunk 1 reached the platform and before its answer: the next session (the supervisor's
    next tick) sends chunk 1 once more (an unknown send on a platform without history goes again, C12)
    and then chunk 2, and never chunk 0 again."""
    from lib.channels import outbox
    from lib.channels.bots import ids
    from lib.channels.bots.session import BotSession
    from lib.channels.model import WECOM
    command = ids.thread_key('wecom', 'uid', OWNER_ID)
    store.update_account('wecom', lambda account: {
        **account, 'enabled': True, 'appId': WECOM_BOT_ID, 'identity': {'uid': OWNER_ID, 'name': 'Owner'},
        'commandChat': str(command)})
    text = ''.join(f'{n:04d} {"长" * 300}\n' for n in range(50))  # 50 lines of 906 bytes: three chunks
    wecom.drop_at_send = 2
    first = BotSession(OWNER, 'wecom', store=store, transport=transport_for(wecom))
    first.start()
    try:
        eventually(lambda: store.account('wecom')['status']['state'] == 'connected')
        item = outbox.enqueue(OWNER, str(command), text, idempotency_key='long report', store=store,
                              origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 'turn-1'})
        eventually(lambda: store.outbox_item(item.id)[0].state == 'unknown' and not first.alive())
    finally:
        first.stop(5.0)
    second = BotSession(OWNER, 'wecom', store=store, transport=transport_for(wecom))
    second.start()
    try:
        eventually(lambda: store.outbox_item(item.id)[0].state == 'sent')
    finally:
        assert second.stop(5.0)
    contents = [frame['body']['markdown']['content'] for frame in wecom.sent('aibot_send_msg')]
    assert all(len(content.encode('utf-8')) <= WECOM['markdownBytes'] for content in contents)
    assert len(contents) == 4 and contents[1] == contents[2]
    assert contents[0] + contents[2] + contents[3] == text


def test_a_newer_connection_kicks_this_one(wecom):
    from lib.channels.bots.platform import SessionEnded
    sink = Sink()
    outcome = run(transport_for(wecom), sink)
    eventually(lambda: sink.info is not None)
    wecom.push({'msgid': 'ev-1', 'create_time': 1700000000, 'aibotid': WECOM_BOT_ID, 'msgtype': 'event',
                'event': {'eventtype': 'disconnected_event'}}, cmd='aibot_event_callback')
    eventually(lambda: outcome.get('done'))
    assert isinstance(outcome['error'], SessionEnded) and outcome['error'].state == 'kicked'


def test_end_to_end_pairing_command_receipt_and_redelivery(wecom, store, router, dispatch, mailbox, vault):
    from lib.channels.bots import account
    from lib.channels.bots.session import BotSession
    account.update_account(OWNER, 'wecom', {'appId': WECOM_BOT_ID, 'secret': WECOM_SECRET, 'enabled': True},
                           store=store)
    code = store.account('wecom')['pairing']['code']
    session = BotSession(OWNER, 'wecom', store=store, transport=transport_for(wecom))
    session.start()
    try:
        eventually(lambda: store.account('wecom')['status']['state'] == 'connected')
        wecom.push(wecom_text('msg-pair', code))
        eventually(lambda: (store.account('wecom').get('identity') or {}).get('uid') == OWNER_ID)
        wecom.push(wecom_text('msg-cmd', '整理一下今天的待办'))
        wecom.push(wecom_text('msg-cmd', '整理一下今天的待办'))  # redelivered: same msgid
        eventually(lambda: dispatch.to(ROUTER))
        eventually(lambda: len(wecom.sent('aibot_send_msg')) >= 2)  # pairing confirmation + receipt
        assert [c['message']['origin']['initiator'] for c in dispatch.to(ROUTER)] == ['human']
        assert all(frame['body']['chatid'] == OWNER_ID for frame in wecom.sent('aibot_send_msg'))
    finally:
        assert session.stop(5.0)
