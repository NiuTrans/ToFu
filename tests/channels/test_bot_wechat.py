"""微信 ClawBot (iLink) adapter against a fake iLink server (C12)."""
from __future__ import annotations

import pytest

from tests.channels.bot_fakes import WECHAT_TOKEN, FakeILink, eventually, ilink_message
from tests.channels.conftest import OWNER, ROUTER
from tests.channels.test_bot_wecom import Sink, run

pytestmark = pytest.mark.unit


@pytest.fixture
def ilink():
    fake = FakeILink()
    yield fake
    fake.close()


def transport_for(fake, *, cursor='', token=WECHAT_TOKEN):
    from lib.channels.wechat.transport import WeChatTransport
    return WeChatTransport(lambda: token, api_base=fake.base, cursor=cursor)


def test_polls_with_the_documented_headers_and_reports_connected(ilink):
    sink, transport = Sink(), transport_for(ilink, cursor='cur-0')
    run(transport, sink)
    eventually(lambda: sink.info is not None)
    request = ilink.paths('/ilink/bot/getupdates')[0]
    headers = request['headers']
    assert headers['authorizationtype'] == 'ilink_bot_token'
    assert headers['authorization'] == f'Bearer {WECHAT_TOKEN}'
    assert headers['ilink-app-id'] == 'bot' and headers['x-wechat-uin']
    assert request['body']['get_updates_buf'] == 'cur-0' and request['body']['base_info']['channel_version']
    sink.stopped.set()
    transport.close()


@pytest.mark.parametrize('phase', ['login', 'messages'])
def test_connect_timeout_reaches_session_recovery_instead_of_empty_poll(store, router, phase):
    import requests
    from lib.channels.bots import account
    from lib.channels.model import ChannelError
    from lib.channels.wechat import login
    from lib.channels.wechat.transport import WeChatTransport

    sink = Sink()
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args)
        sink.stopped.set()
        raise requests.exceptions.ConnectTimeout('proxy tunnel did not open')

    with pytest.raises(ChannelError if phase == 'login' else requests.exceptions.ConnectTimeout):
        if phase == 'login':
            account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                                   login_starter=lambda owner: {'qrcode': 'pending', 'content': 'qr'})
            login.complete(OWNER, store=store, http=unavailable, stop=sink.stopped)
        else:
            WeChatTransport(lambda: WECHAT_TOKEN, http=unavailable).run(sink)
    assert len(calls) == 1


@pytest.mark.parametrize('phase', ['login', 'messages'])
def test_response_read_timeout_still_continues_the_long_poll(store, router, phase):
    import requests
    from lib.channels.bots import account
    from lib.channels.wechat import login
    from lib.channels.wechat.transport import WeChatTransport

    sink = Sink()
    calls = []

    def response(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout('long poll finished without an event')
        sink.stopped.set()
        return 200, {'status': 'wait'}

    if phase == 'login':
        account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                               login_starter=lambda owner: {'qrcode': 'pending', 'content': 'qr'})
        assert login.complete(OWNER, store=store, http=response, stop=sink.stopped) is False
    else:
        WeChatTransport(lambda: WECHAT_TOKEN, http=response).run(sink)
    assert len(calls) == 2


def test_user_messages_are_delivered_with_their_cursor_and_bot_messages_skipped(ilink):
    sink, transport = Sink(), transport_for(ilink)
    checkpoints = []
    sink.deliver = lambda batch, checkpoint=None: (sink.batches.extend(batch), checkpoints.append(checkpoint))
    ilink.updates.append({'ret': 0, 'get_updates_buf': 'cur-1', 'msgs': [
        ilink_message(11, '早上好'),
        ilink_message(12, 'echo of the bot', message_type=2),
        ilink_message(13, '', item_list=[{'type': 3, 'voice_item': {'text': '这是语音'}}]),
        ilink_message(14, '', item_list=[{'type': 4, 'file_item': {'file_name': 'plan.pdf'}}]),
    ]})
    run(transport, sink)
    eventually(lambda: len(sink.batches) >= 3)
    texts = [item.text for item in sink.batches]
    assert texts == ['早上好', '这是语音', '[file: plan.pdf]']
    first = sink.batches[0]
    assert (first.kind, first.peer, first.sender) == ('uid', 'owner@im.wechat', 'owner@im.wechat')
    assert first.reply == {'contextToken': 'ctx-1'} and first.sent_at_s == 1_793_934_000
    assert checkpoints[0] == {'cursor': 'cur-1'}
    sink.stopped.set()
    transport.close()


def test_a_poll_answer_with_messages_is_logged_by_count_only(ilink, caplog):
    """Recorded 2026-11-08: once 'connected', nothing showed whether iLink delivered
    anything, so 'no inbound receipt' could not be told from 'nothing was sent'."""
    import logging
    caplog.set_level(logging.INFO, logger='lib.channels.wechat.transport')
    sink, transport = Sink(), transport_for(ilink)
    ilink.updates.append({'ret': 0, 'get_updates_buf': 'cur-1', 'msgs': [
        ilink_message(31, '只给 Tofu 看'), ilink_message(32, 'echo of the bot', message_type=2)]})
    run(transport, sink)
    eventually(lambda: sink.batches)
    sink.stopped.set()
    transport.close()
    lines = [record.getMessage() for record in caplog.records if record.name == 'lib.channels.wechat.transport']
    assert '[Channels] wechat getupdates returned 2 message(s), 1 from users' in lines
    for private in ('只给 Tofu 看', 'owner@im.wechat', 'ctx-1', 'cur-1'):
        assert private not in caplog.text


def test_an_expired_session_needs_a_new_login(ilink):
    from lib.channels.bots.platform import SessionEnded
    ilink.updates.append({'ret': -14, 'errmsg': 'session timeout'})
    sink = Sink()
    outcome = run(transport_for(ilink), sink)
    eventually(lambda: outcome.get('done'))
    assert isinstance(outcome['error'], SessionEnded) and outcome['error'].state == 'needs_login'


def test_replies_carry_the_context_token_and_the_outbox_item_as_client_id(ilink):
    transport = transport_for(ilink)
    result = transport.send('uid', 'owner@im.wechat', '收到', item_id='ob1', reply={'contextToken': 'ctx-7'})
    [sent] = ilink.sent
    message = sent['body']['msg']
    assert message['to_user_id'] == 'owner@im.wechat' and message['client_id'] == 'ob1'
    assert message['context_token'] == 'ctx-7' and message['message_type'] == 2
    assert message['item_list'] == [{'type': 1, 'text_item': {'text': '收到'}}]
    assert sent['headers']['authorization'] == f'Bearer {WECHAT_TOKEN}' and result['ok'] is True
    missing = transport.send('uid', 'stranger@im.wechat', 'x', item_id='ob2', reply={})
    assert missing['ok'] is False and missing['error']['code'] == 'unsupported_target'


@pytest.mark.parametrize('cut', ['no_route', 'after_send'])
def test_a_send_no_network_route_carried_is_deferred_not_an_unknown_fault(ilink, store, router, monkeypatch, cut):
    """Recorded 2026-11-09: four 微信 sends hit the proxy's 30 s connect timeout. Route
    exhaustion proves nothing reached iLink, yet each item became unknown and a Watchdog
    outbox fault, which the Watchdog read as silent delivery failure and told the owner to
    log in again. Only a failure after the request may have been delivered stays unknown."""
    import requests
    from lib.channels import outbox
    from lib.channels.bots import ids
    from lib.channels.bots.http import request_json
    from lib.channels.bots.session import BotSession
    from lib.channels.wechat.transport import WeChatTransport
    from lib.http_transport.connection_setup import NetworkRoutesExhausted

    faults = []
    monkeypatch.setattr('lib.router.watchdog.signals.report_outbox_fault',
                        lambda owner, item: faults.append(item.id))
    chat = ids.thread_key('wechat', 'uid', 'owner@im.wechat')
    store.update_account('wechat', lambda account: {
        **account, 'enabled': True, 'appId': 'bot@im.bot', 'commandChat': str(chat),
        'credential': {'name': f'channel.u{OWNER}.wechat.token', 'managed': True},
        'identity': {'uid': 'owner@im.wechat', 'name': ''}})
    store.update_thread(chat, lambda record: {**record, 'botReply': {'contextToken': 'ctx-1'}})
    failures = [NetworkRoutesExhausted('No configured network route connected') if cut == 'no_route'
                else requests.exceptions.ConnectionError('connection reset after the request was sent')]

    def http(method, url, **kwargs):
        if url.endswith('/ilink/bot/sendmessage') and failures:
            raise failures.pop()
        return request_json(method, url, **kwargs)

    session = BotSession(OWNER, 'wechat', store=store,
                         transport=WeChatTransport(lambda: WECHAT_TOKEN, api_base=ilink.base, http=http))
    item = outbox.enqueue(OWNER, str(chat), '收到', idempotency_key='k',
                          origin={'kind': 'tool', 'conversationId': ROUTER, 'turnId': 't'}, store=store)
    session.execute_outbox()
    held = store.outbox_item(item.id)[0]
    if cut == 'no_route':
        assert (held.state, held.error) == ('queued', 'not_connected') and faults == []
    else:
        assert held.state == 'unknown' and faults == [item.id]
    session.execute_outbox()
    sent = store.outbox_item(item.id)[0]
    assert (sent.state, sent.error) == ('sent', None)
    assert [request['body']['msg']['client_id'] for request in ilink.sent] == [item.id]
    assert faults == ([] if cut == 'no_route' else [item.id])


def test_qr_login_binds_the_owner_and_keeps_the_token_in_the_vault(ilink, store, router, vault):
    from lib.channels.bots import account, ids
    from lib.channels.wechat import login
    view = account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                                  login_starter=lambda owner: login.request_qr(api_base=ilink.base))
    assert view['enabled'] and view['status']['state'] == 'authorizing'
    assert view['login']['state'] == 'waiting' and view['login']['qrDataUri'].startswith('data:image/png;base64,')
    qr_headers = ilink.paths('/ilink/bot/get_bot_qrcode')[0]['headers']
    assert qr_headers['authorizationtype'] == 'ilink_bot_token' and qr_headers['x-wechat-uin']
    assert 'authorization' not in qr_headers
    ilink.qr_statuses.extend([{'status': 'scaned'}, ilink.confirmed()])
    login.complete(OWNER, store=store, api_base=ilink.base)
    stored = store.account('wechat')
    entry = f'channel.u{OWNER}.wechat.token'
    assert vault.get_entry(entry) == WECHAT_TOKEN and WECHAT_TOKEN not in str(stored)
    assert stored['identity']['uid'] == 'owner@im.wechat'
    assert stored['commandChat'] == str(ids.thread_key('wechat', 'uid', 'owner@im.wechat'))
    assert stored['appId'] == 'bot@im.bot' and not stored.get('login')
    assert ilink.paths('/ilink/bot/get_qrcode_status')[0]['query']['qrcode'] == ['qr-key-1']


def test_a_login_naming_an_untrusted_host_is_refused(ilink, store, router, vault):
    from lib.channels.bots import account
    from lib.channels.bots.platform import SessionEnded
    from lib.channels.wechat import login
    account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                           login_starter=lambda owner: login.request_qr(api_base=ilink.base))
    ilink.qr_statuses.append(ilink.confirmed(baseurl='https://evil.example.com'))
    with pytest.raises(SessionEnded) as refused:
        login.complete(OWNER, store=store, api_base=ilink.base)
    assert refused.value.state == 'needs_login'
    assert vault.get_entry(f'channel.u{OWNER}.wechat.token') is None


@pytest.mark.parametrize('error_name', ['ReadTimeout', 'ConnectTimeout', 'ConnectionError'])
def test_qr_network_failures_are_actionable_and_keep_the_previous_login(store, router, error_name):
    import requests
    from lib.channels.bots import account
    from lib.channels.model import ChannelError
    from lib.channels.wechat import login
    account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                           login_starter=lambda owner: {'qrcode': 'previous', 'content': 'old code'})
    previous = store.account('wechat')

    def unavailable(*args, **kwargs):
        raise getattr(requests.exceptions, error_name)('private proxy address and token')

    with pytest.raises(ChannelError) as failed:
        account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                               login_starter=lambda owner: login.request_qr(http=unavailable))
    assert failed.value.code == 'wechat_unavailable'
    assert 'private proxy' not in str(failed.value)
    assert store.account('wechat') == previous


def test_a_failed_login_status_poll_never_logs_the_qr_key(store, router, caplog):
    """Recorded 2026-11-08: the session's failure log carried the status URL and with it
    the QR key, which is what claims the bot token once the owner confirms."""
    import logging
    import requests
    from lib.channels.bots import account
    from lib.channels.bots.session import BotSession
    from lib.channels.model import ChannelError
    from lib.channels.wechat import login
    caplog.set_level(logging.INFO)
    account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                           login_starter=lambda owner: {'qrcode': 'qr-key-secret', 'content': 'qr'})

    def unavailable(method, url, **kwargs):
        raise requests.exceptions.ConnectTimeout(
            f"Max retries exceeded with url: {url} (Caused by ConnectTimeoutError('HTTP connection setup timed out'))")

    with pytest.raises(ChannelError) as failed:
        login.complete(OWNER, store=store, http=unavailable)
    assert failed.value.code == 'wechat_unavailable' and 'qr-key-secret' not in str(failed.value)

    class StatusPoll:
        def run(self, sink):
            login.complete(OWNER, store=store, http=unavailable, stop=sink.stopped)

        def close(self):
            pass

    session = BotSession(OWNER, 'wechat', store=store, transport=StatusPoll())
    session.start()
    eventually(lambda: not session.alive())
    session.stop()
    assert store.account('wechat')['status']['detail'] == 'session failed: wechat_unavailable'
    assert 'session failed' in caplog.text and 'qr-key-secret' not in caplog.text


def test_qr_expiry_is_a_refreshable_state_without_a_stale_image(ilink, store, router):
    from lib.channels.bots import account
    from lib.channels.bots.platform import SessionEnded
    from lib.channels.wechat import login
    account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                           login_starter=lambda owner: login.request_qr(api_base=ilink.base))
    ilink.qr_statuses.append({'status': 'expired'})
    with pytest.raises(SessionEnded):
        login.complete(OWNER, store=store, api_base=ilink.base)
    view = account.account_view(OWNER, 'wechat', store=store)
    assert view['login']['state'] == 'expired'
    assert view['login']['qrDataUri'] is None


@pytest.mark.parametrize('replacement', [None, 'new-code'])
@pytest.mark.parametrize('answer', ['confirmed', 'expired', 'scaned'])
def test_late_login_answers_cannot_replace_a_cancelled_or_refreshed_login(
        ilink, store, router, vault, replacement, answer):
    from lib.channels.bots import account
    from lib.channels.wechat import login
    account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                           login_starter=lambda owner: {'qrcode': 'old-code', 'content': 'old'})
    expected = None

    def late_answer(*args, **kwargs):
        nonlocal expected
        assert expected is None, 'a retired login must stop polling'
        if replacement:
            account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                                   login_starter=lambda owner: {'qrcode': replacement, 'content': 'new'})
        else:
            account.update_account(OWNER, 'wechat', {'login': False}, store=store)
        expected = store.account('wechat')
        return 200, ilink.confirmed() if answer == 'confirmed' else {'status': answer}

    assert login.complete(OWNER, store=store, api_base=ilink.base, http=late_answer) is False
    assert store.account('wechat') == expected
    assert vault.get_entry(f'channel.u{OWNER}.wechat.token') is None


def test_a_delayed_login_transport_does_not_adopt_a_newer_qr(ilink, store, router):
    from lib.channels.bots import account
    from lib.channels.wechat.transport import open_transport
    account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                           login_starter=lambda owner: {'qrcode': 'old', 'content': 'old'})
    store.update_account('wechat', lambda value: {**value, 'apiBaseUrl': ilink.base})
    transport = open_transport(OWNER, store.account('wechat'), store=store)
    account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                           login_starter=lambda owner: {'qrcode': 'new', 'content': 'new'})
    # A stale transport must stop before it asks the provider about the replacement.
    ilink.qr_statuses.append({'status': 'expired'})
    transport.run(Sink())
    assert not ilink.paths('/ilink/bot/get_qrcode_status')
    assert store.account('wechat')['login']['state'] == 'waiting'


def test_the_session_that_completes_the_qr_login_keeps_polling(ilink, store, router, vault):
    """Recorded 2026-11-08: the login was confirmed at 14:59:42 and that session already
    polled with the issued token, yet the next tick replaced it (15:00:13): a second
    poller ran beside it and the first one's answer was dropped."""
    from lib.channels.bots import account, supervisor
    from lib.channels.bots.session import BotSession
    made = []

    def session_factory(owner, channel, *, store=None):
        made.append(BotSession(owner, channel, store=store))
        return made[-1]

    account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                           login_starter=lambda owner: {'qrcode': 'qr-key-1', 'content': 'qr'})
    store.update_account('wechat', lambda value: {**value, 'apiBaseUrl': ilink.base})
    ilink.qr_statuses.append(ilink.confirmed())
    try:
        assert supervisor.reconcile_owner(OWNER, 'wechat', store=store,
                                           session_factory=session_factory) == 'started'
        eventually(lambda: store.account('wechat')['status']['state'] == 'connected')
        assert supervisor.reconcile_owner(OWNER, 'wechat', store=store,
                                           session_factory=session_factory) == 'running'
        assert len(made) == 1 and supervisor.running_session(OWNER, 'wechat') is made[0]
    finally:
        supervisor.stop_bot_sessions()


def test_end_to_end_owner_command_and_cursor_after_ingest(ilink, store, router, dispatch, mailbox, vault):
    from lib.channels.bots import account
    from lib.channels.bots.session import BotSession
    from lib.channels.wechat import login
    account.update_account(OWNER, 'wechat', {'login': True}, store=store,
                           login_starter=lambda owner: login.request_qr(api_base=ilink.base))
    ilink.qr_statuses.append(ilink.confirmed())
    login.complete(OWNER, store=store, api_base=ilink.base)
    ilink.updates.append({'ret': 0, 'get_updates_buf': 'cur-9', 'msgs': [ilink_message(21, '明天提醒我交周报')]})
    session = BotSession(OWNER, 'wechat', store=store, transport=transport_for(ilink))
    session.start()
    try:
        eventually(lambda: dispatch.to(ROUTER))
        assert dispatch.to(ROUTER)[0]['message']['origin']['initiator'] == 'human'
        eventually(lambda: store.account('wechat').get('cursor') == 'cur-9')
        eventually(lambda: ilink.sent)  # the receipt goes back with the context token
        assert ilink.sent[0]['body']['msg']['context_token'] == 'ctx-1'
    finally:
        session.stop(5.0)
